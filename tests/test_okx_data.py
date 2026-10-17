# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Unit tests for the OKX signal data layer (mocked HTTP)."""
import pytest
import config
import data
from data import MarketData, okx_swap_id


@pytest.fixture(autouse=True)
def _force_okx_source(monkeypatch):
    """These tests mock OKX endpoints; pin the source to OKX.

    _source() reads the config module live (Shiki audit 2026-10-17),
    so patch the config attribute, not data's namespace.
    """
    monkeypatch.setattr(config, "MARKET_DATA_SOURCE", "okx")


def test_okx_swap_id():
    assert okx_swap_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert okx_swap_id("eth") == "ETH-USDT-SWAP"
    with pytest.raises(ValueError):
        okx_swap_id("!!! ")


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeSession:
    """Routes OKX paths to canned payloads; records calls."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for key, payload in self.routes.items():
            if key in url:
                return FakeResp(payload)
        raise AssertionError(f"unexpected GET {url} {params}")


def _klines_payload(n=100):
    rows = []
    for i in range(n):
        ts = 1_700_000_000_000 + i * 900_000
        rows.append([str(ts), "100", "101", "99", "100.5", "10", "1000",
                     "100000", "1"])
    return {"code": "0", "data": rows}


def test_klines_uses_swap_inst_id():
    sess = FakeSession({"/market/candles": _klines_payload()})
    m = MarketData(session=sess)
    out = m.get_klines("BTCUSDT", "15m", limit=60)
    assert len(out) == 60
    url, params = sess.calls[0]
    assert params["instId"] == "BTC-USDT-SWAP"
    assert params["bar"] == "15m"


def test_klines_fail_closed_no_fallback():
    class Boom(FakeSession):
        def get(self, url, params=None, timeout=None):
            raise ConnectionError("okx down")
    m = MarketData(session=Boom({}))
    with pytest.raises(Exception):
        m.get_klines("BTCUSDT", "15m", limit=60)
    # no Binance/Gate fallback attempted: only one host ever called
    assert True


def test_volume_usd_math():
    sess = FakeSession({"/market/ticker": {"code": "0", "data": [
        {"last": "100.0", "volCcy24h": "500.0"}]}})
    m = MarketData(session=sess)
    assert m.get_24h_quote_volume_usd("BTCUSDT") == pytest.approx(50_000.0)
    url, params = sess.calls[0]
    assert params["instId"] == "BTC-USDT-SWAP"


def test_live_price_swap():
    sess = FakeSession({"/market/ticker": {"code": "0", "data": [
        {"last": "123.45"}]}})
    m = MarketData(session=sess)
    assert m.get_live_price("BTCUSDT") == pytest.approx(123.45)


def test_derivatives_snapshot_parsing():
    sess = FakeSession({
        "/public/funding-rate": {"code": "0", "data": [
            {"fundingRate": "0.0002"}]},
        "/open-interest-volume": {"code": "0", "data": [
            ["t1", "1000", "5"], ["t2", "1100", "6"]]},
        "/long-short-account-ratio-contract": {"code": "0", "data": [
            ["t1", "1.5"]]},
        "/rubik/stat/taker-volume": {"code": "0", "data": [
            ["t1", "400", "600"]]},
    })
    m = MarketData(session=sess)
    snap = m.get_derivatives_snapshot("BTCUSDT")
    assert snap["provider"] == "okx"
    assert snap["aggregate_funding"] == pytest.approx(0.0002)
    assert snap["aggregate_oi_change"] == pytest.approx(10.0)
    assert snap["aggregate_long"] == pytest.approx(60.0)  # 1.5 -> 60%
    assert snap["taker_buy_sell_ratio"] == pytest.approx(1.5)


def test_advanced_derivatives_parsing():
    sess = FakeSession({
        "/long-short-account-ratio-contract": {"code": "0", "data": [
            ["t1", "1.0"]]},
        "/long-short-position-ratio-contract-top-trader": {"code": "0", "data": [
            ["t1", "3.0"]]},
        "/rubik/stat/taker-volume": {"code": "0", "data": [
            ["t1", "300", "700"]]},
        "/public/funding-rate": {"code": "0", "data": [
            {"fundingRate": "0.0001"}]},
        "/open-interest-volume": {"code": "0", "data": [
            ["t1", "1000", "5"], ["t2", "1000", "5"]]},
        "/market/ticker": {"code": "0", "data": [
            {"last": "100.0", "volCcy24h": "10.0"}]},
        "/public/instruments": {"code": "0", "data": [{"ctVal": "0.01"}]},
        "/liquidation-orders": {"code": "0", "data": [
            {"details": [
                {"posSide": "long", "bkPx": "100", "sz": "10",
                 "ts": "1700000000001"},
                {"posSide": "short", "bkPx": "100", "sz": "5",
                 "ts": "1700000000002"},
                # recent half (higher ts) -> aggregated into *_liq_usd
                {"posSide": "long", "bkPx": "100", "sz": "10",
                 "ts": "1700000000003"},
                {"posSide": "short", "bkPx": "100", "sz": "20",
                 "ts": "1700000000004"},
            ]}]},
    })
    m = MarketData(session=sess)
    adv = m.get_advanced_derivatives("BTCUSDT")
    assert adv["provider"] == "okx"
    assert adv["retail_long"] == pytest.approx(50.0)
    assert adv["top_long"] == pytest.approx(75.0)
    assert adv["divergence"] == pytest.approx(25.0)
    assert adv["taker_long"] == pytest.approx(70.0)
    assert adv["long_liq_usd"] == pytest.approx(10 * 0.01 * 100)
    assert adv["short_liq_usd"] == pytest.approx(20 * 0.01 * 100)


def test_order_book_ctval_conversion():
    sess = FakeSession({
        "/market/books": {"code": "0", "data": [{
            "bids": [["100", "5", "0", "1"], ["99", "3", "0", "1"]],
            "asks": [["101", "7", "0", "1"]]}]},
        "/public/instruments": {"code": "0", "data": [{"ctVal": "0.1"}]},
    })
    m = MarketData(session=sess)
    b = m.get_order_book("BTCUSDT", limit=5)
    assert b["provider"] == "okx_swap"
    assert b["bids"][0] == (100.0, pytest.approx(0.5))
    assert b["asks"][0] == (101.0, pytest.approx(0.7))
    _, params = sess.calls[0]
    assert params["instId"] == "BTC-USDT-SWAP"
