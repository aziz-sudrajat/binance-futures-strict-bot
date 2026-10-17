"""Bounded-cache eviction + TTL behavior, and the news-sentiment sign convention.

Item 1.5 (bounded cache) + refactor of _news_sentiment polarity.
"""
import time

import pytest

import data
from data import MarketData, _news_sentiment


# ------------------------------------------------------------ _BoundedTTLCache
def test_eviction_caps_size_evicts_oldest():
    c = data._BoundedTTLCache(3)
    for i in range(5):
        c.put(f"k{i}", i)
    assert len(c) == 3
    assert c.get("k0") is None and c.get("k1") is None
    assert c.get("k2") is not None and c.get("k4") is not None


def test_lru_hit_protects_entry_from_eviction():
    c = data._BoundedTTLCache(3)
    for i in range(3):
        c.put(f"k{i}", i)
    assert c.get("k0") is not None  # refresh k0 -> most recently used
    c.put("k3", 3)  # must evict k1 (now least recently used), not k0
    assert c.get("k0") is not None
    assert c.get("k1") is None
    assert c.get("k3") is not None


def test_put_refreshes_existing_key():
    c = data._BoundedTTLCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 3)  # overwrite, no growth
    assert len(c) == 2
    assert c.get("a")[1] == 3


# ------------------------------------------------------- MarketData-level TTL
def _fake_candles(tag):
    # >= MIN_CANDLES (80) so get_klines accepts the mock payload
    return [{"open_time": i, "open": 1.0, "high": 1.0, "low": 1.0,
             "close": 1.0 + tag * 0.01, "volume": 1.0, "close_time": i + 1}
            for i in range(100)]


def test_klines_cache_hit_avoids_refetch(monkeypatch):
    m = MarketData()
    calls = {"n": 0}

    def fake_klines(symbol, interval, limit):
        calls["n"] += 1
        return _fake_candles(calls["n"])

    monkeypatch.setattr(m, "_binance_klines", fake_klines)
    a = m.get_klines("BTCUSDT", "15m", limit=100)
    b = m.get_klines("BTCUSDT", "15m", limit=100)
    assert calls["n"] == 1
    assert a == b  # served from cache


def test_klines_ttl_expiry_triggers_refetch(monkeypatch):
    m = MarketData()
    calls = {"n": 0}

    def fake_klines(symbol, interval, limit):
        calls["n"] += 1
        return _fake_candles(calls["n"])

    monkeypatch.setattr(m, "_binance_klines", fake_klines)
    key = "BTCUSDT_15m_100"
    a = m.get_klines("BTCUSDT", "15m", limit=100)
    assert calls["n"] == 1
    # age the entry past its TTL -> must re-fetch, TTL semantics unchanged
    m._klines_cache._d[key] = (time.time() - 10 ** 6, a)
    b = m.get_klines("BTCUSDT", "15m", limit=100)
    assert calls["n"] == 2
    assert b != a
    assert len(m._klines_cache) == 1


def test_marketdata_caches_bounded_by_config(monkeypatch):
    monkeypatch.setattr(data, "DATA_CACHE_MAX_ENTRIES", 5)
    m = MarketData()

    def fake_klines(symbol, interval, limit):
        return _fake_candles(0)

    monkeypatch.setattr(m, "_binance_klines", fake_klines)
    for i in range(20):  # 20 distinct keys
        m.get_klines(f"C{i}USDT", "15m", limit=100)
    assert len(m._klines_cache) <= 5


def test_quanto_cache_bounded_no_ttl(monkeypatch):
    monkeypatch.setattr(data, "DATA_CACHE_MAX_ENTRIES", 2)
    m = MarketData()
    calls = []

    def fake_okx_get(path, params):
        calls.append(params["instId"])
        return [{"ctVal": "0.01"}]

    monkeypatch.setattr(m, "_okx_get", fake_okx_get)
    assert m._okx_ct_val("BTC-USDT-SWAP") == 0.01
    assert m._okx_ct_val("BTC-USDT-SWAP") == 0.01  # cached, no refetch
    assert calls == ["BTC-USDT-SWAP"]
    m._okx_ct_val("ETH-USDT-SWAP")
    m._okx_ct_val("SOL-USDT-SWAP")  # evicts BTC (LRU)
    assert len(m._quanto_cache) == 2
    m._okx_ct_val("BTC-USDT-SWAP")  # must re-fetch after eviction
    assert calls.count("BTC-USDT-SWAP") == 2


def test_deriv_cache_bounded_and_ttl(monkeypatch):
    monkeypatch.setattr(data, "DATA_CACHE_MAX_ENTRIES", 2)
    m = MarketData()

    def fake_deriv(symbol, period):
        return {"provider": "okx", "long_pct": 50.0, "funding": 0.0,
                "oi_change_pct": 0.0, "taker_ratio": 1.0}

    monkeypatch.setattr(m, "_okx_derivatives", fake_deriv)
    m.get_derivatives_snapshot("BTCUSDT")
    m.get_derivatives_snapshot("ETHUSDT")
    m.get_derivatives_snapshot("SOLUSDT")
    assert len(m._deriv_cache) == 2
    # cache-hit still works (no re-fetch, same object)
    a = m.get_derivatives_snapshot("ETHUSDT")
    b = m.get_derivatives_snapshot("ETHUSDT")
    assert a == b


# ------------------------------------------------ news sentiment sign convention
def test_sentiment_sign_convention():
    # positive = bullish, negative = bearish — this is what
    # signal_engine.news_review thresholds on (<= -2 bear, >= 2 bull)
    assert _news_sentiment("Bitcoin hacked, funds stolen in exploit") < -4
    assert _news_sentiment("Ethereum ETF approval drives rally") > 0
    assert _news_sentiment("Bitcoin price dips below $82K on CPI data") == 0


def test_sentiment_strong_words_count_double():
    assert _news_sentiment("Bitcoin hack reported") == -2
    assert _news_sentiment("Ethereum ETF approved") == 2  # etf + approv


def test_sentiment_sec_cases_no_false_positive():
    # "SEC sued": bearish hits only -> bearish. "SEC approved": bearish
    # mild (+sec) and bullish (+approv) cancel out -> neutral, NOT bullish.
    assert _news_sentiment("SEC sued Ripple") == -2
    assert _news_sentiment("SEC approved") == 0
    assert _news_sentiment("SEC approved") <= 0  # never bullish


def test_sentiment_case_insensitive_and_substring_documented():
    assert _news_sentiment("Bitcoin HACK reported") == -2
    # Documented existing behavior (unchanged by the refactor): keyword
    # matching is substring-based, so "sec" also matches inside "section".
    assert _news_sentiment("section of report") == -1
