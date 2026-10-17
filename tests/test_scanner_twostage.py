# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for the two-stage scan (prefilter_symbol + scanner orchestration).

The core invariant: prefilter_symbol() implements EXACTLY engine stages
0-2 of evaluate_symbol(), so the candidate set is always a superset of
the approvable set — the two-stage scan yields identical approvals with
far fewer HTTP calls.
"""

import pytest

import config
from signal_engine import evaluate_symbol, prefilter_symbol
from scanner import _prime_tickers, _select_candidates
from tests.test_indicators import candles_from_closes, uptrend


def _downtrend(n=220):
    return [100.0 * (0.997) ** i for i in range(n)]


class StageMarket:
    """Duck-typed market with per-test knobs for prefilter stages 0-2."""

    def __init__(self, closes_15m, closes_30m=None, closes_5m=None,
                 closes_4h=None, closes_1h=None, volume=50_000_000.0,
                 raise_30m=False, btc_bearish=False):
        base = closes_15m
        self.klines = {
            "15m": candles_from_closes(closes_15m, last_vol_mult=2.0),
            "30m": candles_from_closes(closes_30m or base, last_vol_mult=2.0),
            "5m": candles_from_closes(closes_5m or base, last_vol_mult=2.0),
            "4h": candles_from_closes(closes_4h or base, last_vol_mult=2.0),
            "1h": candles_from_closes(closes_1h or base, last_vol_mult=2.0),
        }
        self.volume = volume
        self.raise_30m = raise_30m
        self.btc_bearish = btc_bearish
        self.volume_calls = 0
        self.tickers_calls = 0

    def get_klines(self, symbol, interval, limit=220):
        if symbol == "BTCUSDT" and self.btc_bearish:
            return candles_from_closes(_downtrend(), last_vol_mult=2.0)
        if interval == "30m" and self.raise_30m:
            raise RuntimeError("30m exploded")
        return self.klines[interval]

    def get_live_price(self, symbol):
        return self.klines["15m"][-1]["close"]

    def get_24h_quote_volume_usd(self, symbol):
        self.volume_calls += 1
        return self.volume

    def get_swap_tickers(self):
        self.tickers_calls += 1
        return [{"symbol": "TESTUSDT", "vol_usd_24h": self.volume,
                 "last": 100.0}]


@pytest.fixture(autouse=True)
def _clean_config(monkeypatch):
    # Pin the knobs the prefilter reads; restore after each test.
    monkeypatch.setattr(config, "BULLISH_ONLY", False)
    monkeypatch.setattr(config, "MIN_STRENGTH", 3)
    monkeypatch.setattr(config, "MIN_24H_VOLUME_USD", 2_000_000)
    yield


def _bull_market(**kw):
    c = uptrend(220)
    return StageMarket(c, **kw)


# ------------------------------------------------------------ stage 0 ------
def test_prefilter_rejects_low_volume():
    m = _bull_market(volume=100.0)
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "volume" in reason
    assert m.volume_calls == 1  # per-symbol fallback used


def test_prefilter_uses_bulk_tickers_for_volume():
    m = _bull_market()
    tickers = {"TESTUSDT": {"vol_usd_24h": 50_000_000.0, "last": 100.0}}
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers=tickers)
    assert m.volume_calls == 0  # no per-symbol volume call
    assert ok is True and direction == "BULLISH"


def test_prefilter_bulk_miss_falls_back_per_symbol():
    m = _bull_market()
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers={"OTHER": {}})
    assert m.volume_calls == 1  # fallback used, then volume stage passed
    assert ok is True and direction == "BULLISH"


def test_prefilter_volume_unavailable_fail_closed():
    m = _bull_market(volume=None)
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "fail-closed" in reason


# ------------------------------------------------------------ stage 1 ------
def test_prefilter_rejects_no_directional_setup():
    m = StageMarket([100.0] * 220)  # flat -> NEUTRAL
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "no directional setup" in reason


def test_prefilter_respects_bullish_only(monkeypatch):
    monkeypatch.setattr(config, "BULLISH_ONLY", True)
    m = StageMarket(_downtrend())  # bearish setup
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "bullish-only" in reason


def test_prefilter_rejects_weak_strength(monkeypatch):
    monkeypatch.setattr(config, "MIN_STRENGTH", 99)
    m = _bull_market()
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "strength" in reason


# ------------------------------------------------------------ stage 2 ------
def test_prefilter_rejects_mtf_misaligned():
    c = uptrend(220)
    m = StageMarket(c, closes_1h=_downtrend())  # 1h disagrees
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "MTF tak selaras" in reason


def test_prefilter_30m_error_fail_closed():
    m = _bull_market(raise_30m=True)
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "fail-closed" in reason


def test_prefilter_approves_clean_bullish_setup():
    m = _bull_market()
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is True
    assert direction == "BULLISH"


def test_prefilter_approves_clean_bearish_setup():
    c = _downtrend()
    m = StageMarket(c)
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is True
    assert direction == "BEARISH"


# ------------------------------------------------- superset invariant ------
@pytest.mark.parametrize("scenario", [
    "lowvol", "nosetup", "mtf_bad", "bullish_only_bear",
])
def test_prefilter_false_implies_engine_rejects(scenario, monkeypatch):
    """Superset invariant, reject direction: whenever the prefilter says
    no, the full engine must also reject (stages 0-2 are necessary)."""
    if scenario == "lowvol":
        m = _bull_market(volume=10.0)
    elif scenario == "nosetup":
        m = StageMarket([100.0] * 220)
    elif scenario == "mtf_bad":
        m = StageMarket(uptrend(220), closes_1h=_downtrend())
    elif scenario == "bullish_only_bear":
        monkeypatch.setattr(config, "BULLISH_ONLY", True)
        m = StageMarket(_downtrend())
    ok, _reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    sig, _note = evaluate_symbol("TESTUSDT", m)
    assert sig is None


def test_prefilter_true_does_not_preempt_later_stages():
    """Accept direction, meaningful: a prefilter pass must not decide for
    the engine — here the symbol is bullish and MTF-aligned (prefilter
    passes) but BTC 15m points bearish, so the full engine must still
    reject at its stage 3."""
    m = _bull_market(btc_bearish=True)
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is True and direction == "BULLISH"
    sig, note = evaluate_symbol("TESTUSDT", m)
    assert sig is None
    assert "BTC" in note


# ------------------------------------------------------ orchestration ------
def test_prime_tickers_builds_symbol_map():
    m = _bull_market()
    out = _prime_tickers(m)
    assert out == {"TESTUSDT": {"symbol": "TESTUSDT", "vol_usd_24h": 50_000_000.0,
                                "last": 100.0}}
    assert m.tickers_calls == 1


def test_prime_tickers_failure_returns_none():
    class Broken:
        def get_swap_tickers(self):
            raise RuntimeError("boom")
    assert _prime_tickers(Broken()) is None


def test_select_candidates_never_raises_one_symbol():
    good = _bull_market()
    bad = _bull_market(volume=1.0)

    class Multi:
        def __init__(self):
            self.calls = 0

        def get_klines(self, symbol, interval, limit=220):
            if symbol == "BOOMUSDT":
                raise RuntimeError("kaboom")
            return good.get_klines(symbol, interval, limit)

        def get_24h_quote_volume_usd(self, symbol):
            return bad.volume if symbol == "LOWUSDT" else good.volume

    got = _select_candidates(["OKUSDT", "BOOMUSDT", "LOWUSDT"], Multi(), None)
    assert got == ["OKUSDT"]


def test_select_candidates_empty_universe():
    assert _select_candidates([], _bull_market(), None) == []


def test_prefilter_adx_floor(monkeypatch):
    m = _bull_market()
    # When ADX minimum is set higher than market ADX (pure uptrend has ADX 100.0), prefilter should reject
    monkeypatch.setattr(config, "PREFILTER_ADX_MIN", 101.0)
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "ADX" in reason

    # When ADX minimum is low, prefilter passes
    monkeypatch.setattr(config, "PREFILTER_ADX_MIN", 25.0)
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is True
    assert direction == "BULLISH"


def test_prefilter_pattern_min(monkeypatch):
    m = _bull_market()
    # High pattern requirement rejects if not enough candlestick pattern points
    monkeypatch.setattr(config, "PREFILTER_PATTERN_MIN", 5)
    ok, reason = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is False
    assert "pattern score" in reason

    # Pattern requirement 0 passes
    monkeypatch.setattr(config, "PREFILTER_PATTERN_MIN", 0)
    ok, direction = prefilter_symbol("TESTUSDT", m, tickers=None)
    assert ok is True
    assert direction == "BULLISH"
