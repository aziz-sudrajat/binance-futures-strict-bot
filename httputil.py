# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Shared HTTP plumbing: rate limiting + retry with exponential backoff.

Used by exchange.py (Binance signed REST) and data.py (OKX public market
data). The scanner's 'all' universe (~485 symbols) fans out hundreds of
requests per pass; without pacing a burst can hit the venue's throttle
(HTTP 429) and waste the whole scan.

Everything here is fail-safe: if the limiter or the backoff machinery
ever raises internally, ThrottledSession degrades to a single plain
request so pre-existing behavior is never broken.

SECURITY: nothing in this module logs URLs, query params, headers, or
bodies — signed Binance URLs carry the API signature in the query string.
"""
from __future__ import annotations

import logging
import random
import threading
import time

import requests

import config

log = logging.getLogger("bot.httputil")

# Response statuses worth one more attempt: 429 = throttled, 5xx = server
# side transient. Any other 4xx is a client error — retrying it is
# pointless and, on signed endpoints, burns request weight for nothing.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Methods safe to retry on ambiguous failures (timeout / 5xx): the server
# either processed them idempotently or not at all. POST is excluded on
# purpose: retrying POST /fapi/v1/order after a timeout could DOUBLE-SUBMIT
# an order whose first attempt was filled but whose response was lost.
# 429 is still retried for every method: a 429 means the gateway rejected
# the request before the matching engine ever saw it.
IDEMPOTENT_METHODS = frozenset({"GET", "DELETE", "HEAD", "OPTIONS"})

# Transport failures worth retrying (subclasses of RequestException, so the
# specific branch must come first in the except chain below).
RETRYABLE_EXC = (requests.Timeout, requests.ConnectionError)

# Upper bound when honoring a server-sent Retry-After header (seconds).
RETRY_AFTER_MAX_SEC = 30.0


def _parse_retry_after(resp) -> float | None:
    """Seconds from a Retry-After header, capped; None if absent/invalid."""
    raw = resp.headers.get("Retry-After") if resp.headers else None
    if raw is None:
        return None
    try:
        secs = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if secs != secs or secs < 0:  # NaN / negative
        return None
    return min(secs, RETRY_AFTER_MAX_SEC)


class RateLimiter:
    """Token-bucket throttle.

    Tokens refill at 1 per `min_interval_sec`, up to `capacity`. With the
    default capacity=1 this degenerates to a strict minimum gap between
    requests — the simplest safe shape for exchange REST calls — while
    still allowing a short burst at startup (the bucket starts full).

    `clock` / `sleeper` are injectable so tests can run without real time.
    Thread-safe via an internal lock.
    """

    def __init__(self, min_interval_sec: float = config.HTTP_MIN_INTERVAL_SEC,
                 capacity: int = 1,
                 clock=time.monotonic, sleeper=time.sleep):
        self.min_interval = max(0.0, float(min_interval_sec))
        self.capacity = max(1, int(capacity))
        self._clock = clock
        self._sleeper = sleeper
        self._tokens = float(self.capacity)
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until the caller may issue one request."""
        with self._lock:
            if self.min_interval <= 0:
                return
            now = self._clock()
            elapsed = max(0.0, now - self._last)
            self._tokens = min(float(self.capacity),
                               self._tokens + elapsed / self.min_interval)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._last = now
                return
            wait = (1.0 - self._tokens) * self.min_interval
            self._sleeper(wait)
            # After the sleep exactly one token has accrued; consume it.
            self._tokens = 0.0
            self._last = self._clock()


class ThrottledSession(requests.Session):
    """requests.Session with a per-instance RateLimiter and bounded retries.

    Retry policy: 429 responses are retried for every method (the gateway
    rejected the request before the matching engine saw it). 5xx responses
    and transient transport failures (timeout, connection error) are
    retried only for idempotent methods (GET/DELETE/HEAD/OPTIONS) — never
    for POST, where a retry after a lost response could double-submit an
    order. Retries are bounded by `max_retries` (total attempts =
    max_retries + 1) with exponential backoff + full jitter: sleep ~
    U(0, backoff_base * 2**(attempt-1)). A server-sent Retry-After header
    on 429 is honored instead (capped at 30s).

    Not retried: other 4xx responses (returned as-is; callers raise from
    them) and non-transient request errors (bad URL, redirect loops, ...).
    """

    def __init__(self, min_interval_sec: float = config.HTTP_MIN_INTERVAL_SEC,
                 max_retries: int = config.HTTP_MAX_RETRIES,
                 backoff_base_sec: float = config.HTTP_BACKOFF_BASE_SEC,
                 limiter: RateLimiter | None = None,
                 sleeper=time.sleep):
        super().__init__()
        self._limiter = limiter if limiter is not None else RateLimiter(
            min_interval_sec=min_interval_sec)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base_sec))
        self._sleeper = sleeper

    # -- internals (overridable in tests) --------------------------------
    def _do_request(self, method, url, **kwargs):
        """The actual transport call. Tests replace this with a fake."""
        return super().request(method, url, **kwargs)

    def _retry_delay(self, attempt: int, resp) -> float:
        """Seconds to wait before retry `attempt` (1-based)."""
        if resp is not None:
            retry_after = _parse_retry_after(resp)
            if retry_after is not None:
                return retry_after
        cap = self._backoff_base * (2 ** (attempt - 1))
        return random.uniform(0, cap) if cap > 0 else 0.0

    # -- public API -------------------------------------------------------
    def request(self, method, url, **kwargs):
        # Fail-safe: a broken limiter must never break the request path.
        try:
            self._limiter.acquire()
        except Exception:  # noqa: BLE001 - limiter is advisory only
            log.warning("httputil: rate limiter failed; sending unthrottled")
            return self._do_request(method, url, **kwargs)
        return self._request_with_retry(method, url, **kwargs)

    def _request_with_retry(self, method, url, **kwargs):
        idempotent = str(method).upper() in IDEMPOTENT_METHODS
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._do_request(method, url, **kwargs)
            except RETRYABLE_EXC as e:
                if not idempotent or attempt > self._max_retries:
                    raise
                log.info("httputil: %s transient transport error (%s); "
                         "retry %d/%d", method, type(e).__name__,
                         attempt, self._max_retries)
                self._sleep_before_retry(attempt, None)
                continue
            except requests.RequestException:
                raise  # non-transient (bad URL, redirect loop, ...) — no retry
            if resp.status_code in RETRYABLE_STATUS \
                    and attempt <= self._max_retries:
                # 429: gateway rejected before processing — safe for any
                # method. 5xx on non-idempotent methods: the server may
                # have processed it — do NOT retry (avoid double-submit).
                if resp.status_code != 429 and not idempotent:
                    return resp
                resp.close()  # release the connection before retrying
                log.info("httputil: %s -> HTTP %s; retry %d/%d",
                         method, resp.status_code, attempt, self._max_retries)
                self._sleep_before_retry(attempt, resp)
                continue
            return resp

    def _sleep_before_retry(self, attempt: int, resp) -> None:
        # Fail-safe: if the sleeper itself breaks, retry immediately
        # rather than letting backoff internals fail the request.
        try:
            self._sleeper(self._retry_delay(attempt, resp))
        except Exception:  # noqa: BLE001
            log.warning("httputil: backoff sleep failed; retrying immediately")
