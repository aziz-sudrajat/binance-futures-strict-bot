# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for rate_limiter.py — L7 hardening (2026-10-17).

The token bucket wraps every Binance REST call; a hang or a crash here
stalls the whole executor. These tests lock the two edge cases:
  - tokens > capacity with no timeout must refuse, never spin forever
  - rate/capacity <= 0 must fail fast, never ZeroDivisionError later
"""

import pytest

from rate_limiter import RateLimiter, TokenBucket


def test_zero_rate_rejected():
    with pytest.raises(ValueError):
        TokenBucket(0, 10)


def test_negative_rate_rejected():
    with pytest.raises(ValueError):
        TokenBucket(-1, 10)


def test_zero_capacity_rejected():
    with pytest.raises(ValueError):
        TokenBucket(10, 0)


def test_acquire_more_than_capacity_returns_false():
    # Never satisfiable — the bucket caps at capacity, so blocking here
    # would spin forever. Must refuse immediately instead.
    b = TokenBucket(1000.0, 2.0)
    assert b.acquire(tokens=5.0, blocking=True, timeout=5.0) is False


def test_normal_acquire_and_exhaust():
    b = TokenBucket(1000.0, 2.0)
    assert b.acquire(blocking=False) is True
    assert b.acquire(blocking=False) is True
    assert b.acquire(blocking=False) is False


def test_set_limit_validates():
    rl = RateLimiter()
    with pytest.raises(ValueError):
        rl.set_limit("x", 0, 10)
    with pytest.raises(ValueError):
        rl.set_limit("x", 10, 0)
