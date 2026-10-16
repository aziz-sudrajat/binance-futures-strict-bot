# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Thread-safe token-bucket rate limiter for market data and exchange requests.

Provides per-domain rate limiting to prevent exchange 429 errors when scanning
many coins across OKX and Binance.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("rate_limiter")


class TokenBucket:
    """Token bucket implementation for a single domain/endpoint."""

    def __init__(self, rate: float, capacity: float):
        """
        rate: tokens added per second
        capacity: maximum burst capacity
        """
        # L7: fail fast on nonsense config — rate<=0 makes acquire() divide
        # by zero, capacity<=0 makes every acquire unsatisfiable.
        if float(rate) <= 0:
            raise ValueError(f"rate must be > 0 (got {rate})")
        if float(capacity) <= 0:
            raise ValueError(f"capacity must be > 0 (got {capacity})")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_update
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

    def acquire(self, tokens: float = 1.0, blocking: bool = True, timeout: float | None = None) -> bool:
        """Acquire `tokens` from the bucket.

        If blocking=True, sleeps until tokens are available or timeout expires.
        Returns True if tokens were acquired, False otherwise.
        """
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        while True:
            with self.lock:
                now = time.monotonic()
                self._refill(now)

                if tokens > self.capacity:
                    # L7: never satisfiable — the bucket caps at capacity,
                    # so blocking here would spin forever.
                    log.warning("rate_limiter: requested %.3g tokens > "
                                "capacity %.3g — refusing", tokens,
                                self.capacity)
                    return False

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

                if not blocking:
                    return False

                # Calculate sleep duration needed
                needed = tokens - self.tokens
                sleep_sec = needed / self.rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                sleep_sec = min(sleep_sec, remaining)

            time.sleep(max(0.001, sleep_sec))


class RateLimiter:
    """Registry of domain token buckets."""

    # Default limits (requests per second, burst capacity)
    DEFAULT_LIMITS = {
        "okx": (20.0, 30.0),       # OKX public endpoints (~20 req/s)
        "binance": (10.0, 20.0),   # Binance Futures REST endpoints
        "default": (15.0, 25.0),
    }

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._enabled = True

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def is_enabled(self) -> bool:
        return self._enabled

    def get_bucket(self, domain: str) -> TokenBucket:
        key = domain.lower()
        with self._lock:
            if key not in self._buckets:
                rate, cap = self.DEFAULT_LIMITS.get(key, self.DEFAULT_LIMITS["default"])
                self._buckets[key] = TokenBucket(rate, cap)
            return self._buckets[key]

    def set_limit(self, domain: str, rate: float, capacity: float) -> None:
        key = domain.lower()
        with self._lock:
            self._buckets[key] = TokenBucket(rate, capacity)

    def wait(self, domain: str = "default", tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Wait for rate limit token before proceeding."""
        if not self._enabled:
            return True
        bucket = self.get_bucket(domain)
        return bucket.acquire(tokens=tokens, blocking=True, timeout=timeout)


# Global singleton instance
_GLOBAL_LIMITER = RateLimiter()


def wait(domain: str = "default", tokens: float = 1.0, timeout: float | None = None) -> bool:
    """Module-level convenience function."""
    return _GLOBAL_LIMITER.wait(domain=domain, tokens=tokens, timeout=timeout)


def get_limiter() -> RateLimiter:
    return _GLOBAL_LIMITER
