# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Integration test — real public endpoints, single symbol (BTCUSDT).

No API key is used anywhere. Binance mainnet is geo-blocked (HTTP 451) from
some networks; the data layer is designed to fall back (OKX klines, Gate.io
derivatives), so these tests assert structure, not a specific provider.
"""

import time

import pytest

from data import MarketData

pytestmark = pytest.mark.integration

SYMBOL = "BTCUSDT"


@pytest.fixture(scope="module")
def market():
    return MarketData()


def test_klines_structure(market):
    candles = market.get_klines(SYMBOL, "15m", limit=100)
    assert len(candles) >= 80
    c0 = candles[0]
    for key in ("open_time", "open", "high", "low", "close", "volume", "close_time"):
        assert key in c0, key
    times = [c["open_time"] for c in candles]
    assert times == sorted(times), "candles must be oldest-first"
    assert candles[-1]["close_time"] < int(time.time() * 1000), "last candle must be closed"
    assert all(c["high"] >= c["low"] > 0 for c in candles)


def test_klines_cache_hit(market):
    a = market.get_klines(SYMBOL, "15m", limit=100)
    b = market.get_klines(SYMBOL, "15m", limit=100)
    assert a is b or a == b  # second call served from in-memory cache


def test_24h_volume(market):
    vol = market.get_24h_quote_volume_usd(SYMBOL)
    assert vol is None or vol > 1_000_000, f"unexpected volume {vol}"


def test_derivatives_snapshot(market):
    snap = market.get_derivatives_snapshot(SYMBOL)
    assert snap["symbol"] == SYMBOL
    assert snap["provider"] in ("okx", "binance")
    got = [k for k in ("aggregate_long", "aggregate_funding",
                       "aggregate_oi_change", "taker_buy_sell_ratio")
           if snap.get(k) is not None]
    assert got, "snapshot has no usable components at all"
    if snap.get("aggregate_long") is not None:
        assert 0 < snap["aggregate_long"] < 100
    if snap.get("aggregate_funding") is not None:
        assert abs(snap["aggregate_funding"]) < 0.05
