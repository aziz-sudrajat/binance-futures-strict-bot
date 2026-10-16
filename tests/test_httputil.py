"""Unit tests for httputil (token-bucket rate limiter + retry/backoff).

No network access: the transport is a scripted fake and the limiter uses
a fake clock/sleeper.
"""
import pytest
import requests

from httputil import (
    RETRY_AFTER_MAX_SEC,
    RateLimiter,
    ThrottledSession,
)


# ------------------------------------------------------------------ fakes
class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeResp:
    """Minimal stand-in for requests.Response (status + headers only)."""

    def __init__(self, status, retry_after=None):
        self.status_code = status
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)
        self.closed = False

    def close(self):
        self.closed = True


def make_session(script, **kw):
    """ThrottledSession whose transport replays `script` (FakeResp or
    exception instances). Returns (session, calls, sleeps)."""
    sleeps = []
    calls = []
    kw.setdefault("limiter", RateLimiter(min_interval_sec=0))
    kw.setdefault("sleeper", sleeps.append)
    sess = ThrottledSession(**kw)

    def fake_do(method, url, **kwargs):
        calls.append((method, url))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    sess._do_request = fake_do
    return sess, calls, sleeps


# ------------------------------------------------------------- rate limit
def test_min_interval_enforced():
    clock = FakeClock()
    lim = RateLimiter(min_interval_sec=0.5, clock=clock, sleeper=clock.sleep)
    lim.acquire()          # bucket starts full: no wait
    assert clock.sleeps == []
    lim.acquire()          # next one must wait the full interval
    assert clock.sleeps == [pytest.approx(0.5)]


def test_no_throttle_when_interval_zero():
    clock = FakeClock()
    lim = RateLimiter(min_interval_sec=0, clock=clock, sleeper=clock.sleep)
    for _ in range(5):
        lim.acquire()
    assert clock.sleeps == []


def test_burst_capacity_allows_short_burst():
    clock = FakeClock()
    lim = RateLimiter(min_interval_sec=1.0, capacity=3,
                      clock=clock, sleeper=clock.sleep)
    for _ in range(3):
        lim.acquire()
    assert clock.sleeps == []
    lim.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]


def test_tokens_refill_over_time():
    clock = FakeClock()
    lim = RateLimiter(min_interval_sec=0.5, clock=clock, sleeper=clock.sleep)
    lim.acquire()
    lim.acquire()
    assert clock.sleeps == [pytest.approx(0.5)]
    clock.t += 10.0  # long idle: bucket refills
    lim.acquire()
    assert len(clock.sleeps) == 1  # no additional wait


# ------------------------------------------------------------------ retry
def test_retry_then_success():
    sess, calls, sleeps = make_session(
        [FakeResp(500), FakeResp(503), FakeResp(200)])
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1.0   # U(0, base*2^0), base=1.0
    assert 0 <= sleeps[1] <= 2.0   # U(0, base*2^1)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_no_retry_on_non_429_client_error(status):
    sess, calls, sleeps = make_session([FakeResp(status)])
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == status
    assert len(calls) == 1
    assert sleeps == []


def test_retry_on_429_then_success():
    sess, calls, sleeps = make_session([FakeResp(429), FakeResp(200)])
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_retry_on_timeout_and_connection_error():
    sess, calls, sleeps = make_session(
        [requests.Timeout("t"), requests.ConnectionError("c"), FakeResp(200)])
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_non_transient_error_not_retried():
    sess, calls, sleeps = make_session(
        [requests.TooManyRedirects("loop")])
    with pytest.raises(requests.TooManyRedirects):
        sess.request("GET", "https://example.invalid/x")
    assert len(calls) == 1
    assert sleeps == []


def test_exhaustion_returns_last_response():
    sess, calls, sleeps = make_session(
        [FakeResp(500), FakeResp(500), FakeResp(500), FakeResp(500)],
        max_retries=2)
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 500
    assert len(calls) == 3          # 1 + max_retries
    assert len(sleeps) == 2


def test_exhaustion_reraises_last_transport_error():
    sess, calls, sleeps = make_session(
        [requests.Timeout("t")] * 3, max_retries=2)
    with pytest.raises(requests.Timeout):
        sess.request("GET", "https://example.invalid/x")
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_retry_after_header_honored():
    sess, calls, sleeps = make_session(
        [FakeResp(429, retry_after=5), FakeResp(200)])
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert sleeps == [5.0]


def test_retry_after_header_capped():
    sess, calls, sleeps = make_session(
        [FakeResp(429, retry_after=9999), FakeResp(200)])
    sess.request("GET", "https://example.invalid/x")
    assert sleeps == [RETRY_AFTER_MAX_SEC]


def test_retry_after_invalid_ignored():
    sess, calls, sleeps = make_session(
        [FakeResp(429, retry_after="not-a-number"), FakeResp(200)])
    sess.request("GET", "https://example.invalid/x")
    assert len(sleeps) == 1
    assert 0 <= sleeps[0] <= 1.0  # fell back to backoff+jitter


def test_retried_response_closed_before_retry():
    r500 = FakeResp(500)
    sess, calls, sleeps = make_session([r500, FakeResp(200)])
    sess.request("GET", "https://example.invalid/x")
    assert r500.closed is True


def test_backoff_windows_double():
    sess, calls, sleeps = make_session(
        [FakeResp(500)] * 4 + [FakeResp(200)],
        max_retries=4, backoff_base_sec=2.0)
    sess.request("GET", "https://example.invalid/x")
    assert len(sleeps) == 4
    for i, s in enumerate(sleeps):
        assert 0 <= s <= 2.0 * (2 ** i)


# --------------------------------------------------------------- fail-safe
class BoomLimiter:
    def acquire(self):
        raise RuntimeError("limiter exploded")


def test_limiter_failure_falls_through_to_plain_request():
    sess, calls, sleeps = make_session(
        [FakeResp(200)], limiter=BoomLimiter())
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(calls) == 1  # single attempt, no retry machinery
    assert sleeps == []


def test_broken_sleeper_still_completes_request():
    def bad_sleep(s):
        raise RuntimeError("sleeper exploded")

    sess, calls, sleeps = make_session(
        [FakeResp(500), FakeResp(200)], sleeper=bad_sleep)
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(calls) == 2


# ------------------------------------------------- integration with .get()
def test_session_get_routes_through_limiter():
    clock = FakeClock()
    lim = RateLimiter(min_interval_sec=0.5, clock=clock, sleeper=clock.sleep)
    sess, calls, _ = make_session(
        [FakeResp(200), FakeResp(200)], limiter=lim, sleeper=clock.sleep)
    sess.get("https://example.invalid/a")
    sess.get("https://example.invalid/b")
    assert [c[0] for c in calls] == ["GET", "GET"]
    assert clock.sleeps == [pytest.approx(0.5)]  # second call was paced


# --------------------------------- idempotent-method retry policy (review #1)
def test_post_not_retried_on_transport_timeout():
    """POST order must never be retried on timeout (double-submit risk)."""
    sess, calls, _ = make_session(
        [requests.Timeout("boom"), FakeResp(200)])
    with pytest.raises(requests.Timeout):
        sess.request("POST", "https://example.invalid/fapi/v1/order")
    assert len(calls) == 1  # single attempt, no retry


def test_post_not_retried_on_500():
    """POST must not be retried on 5xx (server may have processed it)."""
    sess, calls, _ = make_session([FakeResp(500)])
    resp = sess.request("POST", "https://example.invalid/fapi/v1/order")
    assert resp.status_code == 500
    assert len(calls) == 1


def test_post_retried_on_429():
    """429 = gateway rejected before processing -> safe to retry any method."""
    sess, calls, _ = make_session([FakeResp(429), FakeResp(200)])
    resp = sess.request("POST", "https://example.invalid/fapi/v1/order")
    assert resp.status_code == 200
    assert len(calls) == 2


def test_get_still_retried_on_timeout_and_500():
    """Idempotent methods keep the full retry policy."""
    sess, calls, _ = make_session(
        [requests.Timeout("x"), FakeResp(500), FakeResp(200)])
    resp = sess.request("GET", "https://example.invalid/x")
    assert resp.status_code == 200
    assert len(calls) == 3


def test_delete_retried_on_connection_error():
    sess, calls, _ = make_session(
        [requests.ConnectionError("x"), FakeResp(200)])
    resp = sess.request("DELETE", "https://example.invalid/fapi/v1/order")
    assert resp.status_code == 200
    assert len(calls) == 2
