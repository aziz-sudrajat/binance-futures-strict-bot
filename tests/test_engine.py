"""Unit tests for signal_engine.py with synthetic candles.

Verifies the STRICT decision pipeline against the GAS logic:
analyzeCandles_ (L1904), buildTradePlan_ (L1984), smartCryptoScore_ (L2026),
calculateDerivativesScore_ (L879), mtfTrendDirection_ (L3144),
mtfConfirmPass_ STRICT (L2091), mtfCascadeCheck_ (L3156),
deepCheckQualityScore_ base (L3532), getMinQuality_ STRICT (L3137).
"""

import pytest

import config
import vetoes
from signal_engine import (
    analyze_candles,
    build_trade_plan,
    derivatives_score,
    evaluate_symbol,
    mtf_cascade_check,
    mtf_confirm,
    mtf_confirm_lean,
    mtf_confirm_strict,
    mtf_trend_direction,
    quality_score,
    signal_quality_rank,
    smart_crypto_score,
)
from tests.test_indicators import candles_from_closes, uptrend


@pytest.fixture(autouse=True)
def _reset_veto_caches():
    # The BTC-breaker cache is module-global; reset between tests so one
    # test's BTC fixture can never leak into another.
    vetoes.reset_caches()
    yield
    vetoes.reset_caches()


def bull_candles(n=220):
    return candles_from_closes(uptrend(n), last_vol_mult=2.0)


def bear_candles(n=220):
    closes = [100.0 * (0.997) ** i for i in range(n)]
    return candles_from_closes(closes, last_vol_mult=2.0)


# ------------------------------------------------------------------ analysis
def test_bullish_watch_detected():
    a = analyze_candles("BTCUSDT", "15m", bull_candles())
    assert a["signal"] == "BULLISH WATCH"
    assert a["strength"] >= 3
    assert a["trade_plan"] is not None
    assert "EMA9 > EMA21" in a["reasons"]
    assert a["warning"] == "RSI overbought"  # pure rise -> RSI 100 (GAS quirk)


def test_bearish_watch_detected():
    a = analyze_candles("BTCUSDT", "15m", bear_candles())
    assert a["signal"] == "BEARISH WATCH"
    assert a["trade_plan"] is not None


def test_neutral_on_flat_market():
    a = analyze_candles("BTCUSDT", "15m", candles_from_closes([100.0] * 220))
    assert a["signal"] == "NEUTRAL"
    assert a["strength"] == 0
    assert a["trade_plan"] is None


def test_too_few_candles_raises():
    with pytest.raises(ValueError):
        analyze_candles("BTCUSDT", "15m", bull_candles(50))


# --------------------------------------------------------------- trade plan
def test_build_trade_plan_math():
    # 12 candles, swing low 97 / high 102, atr forced to 1.0
    cs = []
    for i in range(12):
        cs.append({"open": 99, "high": 102, "low": 97, "close": 100, "volume": 1})
    p = build_trade_plan("BULLISH WATCH", 100.0, 99.5, 99.0, 1.0, cs)
    # pullback = min(100, max(99.5, 99)) = 99.5
    # entry   = max(100 - 0.5*1, 99.5) = 99.5
    assert p["entry"] == pytest.approx(99.5)
    # stop = min(99.5 - 1.25, 97 - 0.1) = 96.9 -> max(96.9, 99.5 - 2.0) = 97.5
    assert p["stop_loss"] == pytest.approx(97.5)
    # risk = max(2.0, 0.5) = 2.0; tp1 = 99.5 + 2*1.5
    assert p["tp1"] == pytest.approx(102.5)
    assert p["tp2"] == pytest.approx(104.0)
    assert p["tp3"] == pytest.approx(105.5)
    assert p["rr1"] == pytest.approx(1.5)
    assert p["entry_low"] == pytest.approx(99.4)   # zone = 0.1 * atr
    assert p["entry_high"] == pytest.approx(99.6)
    assert p["stop_loss"] < p["entry"] < p["tp1"] < p["tp2"] < p["tp3"]


def test_build_trade_plan_atr_guard():
    cs = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}] * 12
    p = build_trade_plan("BULLISH WATCH", 100.0, 100.0, 100.0, 0.0, cs)
    assert p["atr"] == pytest.approx(0.5)  # max(price*0.005, 1e-8)
    assert p["stop_loss"] < p["entry"]


# ------------------------------------------------------------------ scoring
def test_smart_crypto_score_hand_computed():
    a = {"strength": 4, "volume_ratio": 2.0, "regime": "TRENDING BULLISH",
         "signal": "BULLISH WATCH", "warning": "",
         "trade_plan": {"rr1": 1.5}}
    # 4*20 + min(15, 12) + 12 + min(10, 6) - 0 = 110
    assert smart_crypto_score(a) == pytest.approx(110.0)
    assert signal_quality_rank(a) == round(110 / 1.37)  # GAS /1.37 normalization


def test_smart_crypto_score_warning_penalty():
    a = {"strength": 4, "volume_ratio": 2.0, "regime": "TRENDING BULLISH",
         "signal": "BULLISH WATCH", "warning": "RSI overbought",
         "trade_plan": {"rr1": 1.5}}
    assert smart_crypto_score(a) == pytest.approx(105.0)


def test_derivatives_score_components():
    d = derivatives_score({"aggregate_long": 70, "aggregate_funding": -0.001,
                           "aggregate_oi_change": 10, "taker_buy_sell_ratio": 1.5})
    # 50 -10 (crowded longs) +10 (negative funding) +8 (OI fast) +6 (buyers)
    assert d["value"] == 64
    assert "crowded longs" in d["notes"]


def test_derivatives_score_neutral_and_empty():
    d = derivatives_score({"aggregate_long": 50, "aggregate_funding": 0.0001,
                           "aggregate_oi_change": 1.0, "taker_buy_sell_ratio": 1.0})
    assert d["value"] == 48  # 50 -2 (positive funding)
    assert d["bias"] == "NEUTRAL / BALANCED"
    d2 = derivatives_score({})
    assert d2["value"] == 50
    assert d2["notes"] == []


def test_quality_score_gate():
    # ROADMAP ITEM 3: explicit named components replace the old synthetic
    # deriv blend. Base 40 + strength*10; delta clamped to [-40, +40].
    q = quality_score(5, {"mtf_30m": 8, "mtf_4h": 6, "h1": 8})
    # 40 + 50 + 22 = 112 -> clamp 98
    assert q["quality"] == 98 and q["pass"] is True
    assert q["delta"] == 22 and q["raw_delta"] == 22
    q = quality_score(3, {"mtf_30m": 8, "mtf_4h": 6})
    # 70 + 14 = 84
    assert q["quality"] == 84 and q["pass"] is True
    q = quality_score(3, {"mtf_30m": 8, "mtf_4h": 6, "lev_warn": -10,
                          "whale_warn": -12})
    # 70 + 14 - 22 = 62 -> below STRICT 70: warn tiers make the gate binding
    assert q["quality"] == 62 and q["pass"] is False
    assert q["components"]["lev_warn"] == -10
    # delta clamp: raw +60 -> +40
    q = quality_score(5, {"a": 30, "b": 30})
    assert q["delta"] == 40 and q["quality"] == 98
    # quality clamp floor (40 + 1*10 - 40 = 10; floor 5 rarely binds)
    q = quality_score(1, {"a": -40})
    assert q["quality"] == 10 and q["pass"] is False


# ---------------------------------------------------------------------- MTF
def test_mtf_trend_direction():
    assert mtf_trend_direction(uptrend(60)) == "BULLISH"
    assert mtf_trend_direction([100.0 * 0.998 ** i for i in range(60)]) == "BEARISH"
    assert mtf_trend_direction([100.0] * 60) == "NEUTRAL"
    assert mtf_trend_direction(uptrend(50)) == "NEUTRAL"  # < 55 closes


def test_mtf_confirm_strict():
    ok = {"signal": "BULLISH WATCH", "trade_plan": {"entry": 1}}
    r = mtf_confirm_strict(ok)
    assert r["pass"] is True and r["boost"] == config.MTF_CONFIRM_BOOST
    assert mtf_confirm_strict({"signal": "BEARISH WATCH",
                               "trade_plan": {}})["pass"] is False
    assert mtf_confirm_strict({"signal": "NEUTRAL"})["pass"] is False
    assert mtf_confirm_strict(None)["pass"] is False


def test_mtf_cascade_check():
    k4h = [{"close": c} for c in uptrend(100)]
    k1d = [{"close": c} for c in uptrend(100)]
    a30 = {"signal": "BULLISH WATCH", "trade_plan": {"entry": 1}}
    r = mtf_cascade_check(k4h, k1d, a30)
    assert r["pass"] is True and r["boost"] == config.MTF_CASCADE_BOOST
    k4h_bear = [{"close": c} for c in [100.0 * 0.998 ** i for i in range(100)]]
    r = mtf_cascade_check(k4h_bear, k1d, a30)
    assert r["pass"] is False and r["stage"] == "4h"


# ---------------------------------------------------------- STRICT pipeline
class FakeMarket:
    """Duck-typed stand-in for data.MarketData."""

    BENIGN_ADV = {
        "symbol": "BTCUSDT", "provider": "fake",
        "retail_long": 52.0, "top_long": 50.0, "top_position_long": 51.0,
        "taker_long": 51.0, "divergence": -2.0,
        "funding": 0.0001, "oi_usd": 50_000_000.0, "oi_change": 2.0,
        "volume_24h": 200_000_000.0,
        "long_liq_usd": 100_000.0, "short_liq_usd": 100_000.0, "spike": 1.0,
    }

    def __init__(self, bullish=True):
        gen = bull_candles if bullish else bear_candles
        self.klines = {tf: gen() for tf in ("15m", "30m", "4h", "1d")}
        # BTC 1h for the circuit breaker: last two closes flat (0%/1h).
        self.btc_1h = candles_from_closes([100.0] * 10)
        self.adv = dict(self.BENIGN_ADV)
        self.raise_adv = False
        # Benign book: small sizes, nothing near the $250k wall threshold.
        px = self.klines["15m"][-1]["close"]
        self.book = {
            "bids": [(px * 0.999, 10.0), (px * 0.998, 20.0)],
            "asks": [(px * 1.001, 10.0), (px * 1.002, 20.0)],
            "mid": px, "provider": "fake",
        }
        self.raise_book = False
        self.live_price = None       # override to pin the freshness price
        self.raise_live_price = False  # simulate ticker failure

    def get_klines(self, symbol, interval, limit=220):
        if symbol == "BTCUSDT" and interval == "1h":
            return self.btc_1h
        return self.klines[interval]

    def get_advanced_derivatives(self, symbol, period="4h"):
        if self.raise_adv:
            raise RuntimeError("Gate stats unavailable")
        return dict(self.adv)

    def get_order_book(self, symbol, limit=100):
        if self.raise_book:
            raise RuntimeError("book unavailable")
        return {"bids": list(self.book["bids"]), "asks": list(self.book["asks"]),
                "mid": self.book["mid"], "provider": self.book["provider"]}

    def get_24h_quote_volume_usd(self, symbol):
        return 50_000_000.0

    def get_derivatives_snapshot(self, symbol, period="4h"):
        return {"symbol": symbol, "provider": "fake",
                "aggregate_long": 52.0, "aggregate_funding": 0.0001,
                "aggregate_oi_change": 5.0, "taker_buy_sell_ratio": 1.1}

    def get_live_price(self, symbol):
        if self.raise_live_price:
            raise RuntimeError("ticker unavailable")
        if self.live_price is not None:
            return self.live_price
        return self.klines["15m"][-1]["close"]


def test_evaluate_symbol_strict_pass():
    signal, note = evaluate_symbol("BTCUSDT", FakeMarket(bullish=True))
    assert signal is not None, note
    assert signal["direction"] == "LONG"
    assert signal["quality"] >= config.MIN_QUALITY_SCORE
    for key in ("entry", "entry_low", "entry_high", "stop_loss",
                "tp1", "tp2", "tp3", "strength", "rsi", "regime"):
        assert key in signal, key
    assert signal["stop_loss"] < signal["entry"] < signal["tp1"]
    assert "STRICT PASS" in note


def test_evaluate_symbol_bearish_rejected():
    signal, reason = evaluate_symbol("BTCUSDT", FakeMarket(bullish=False))
    assert signal is None
    assert "bullish-only" in reason


def test_evaluate_symbol_low_volume_rejected():
    m = FakeMarket(bullish=True)
    m.get_24h_quote_volume_usd = lambda s: 100.0
    signal, reason = evaluate_symbol("BTCUSDT", m)
    assert signal is None
    assert "volume" in reason


def test_evaluate_symbol_stale_entry_rejected():
    # T1 (reject path): GAS freshnessCheck_ — LIVE price >0.4% above entryHigh
    # -> stale entry. The Python port previously used the last closed 15m
    # candle (up to ~23 min stale); it now uses a live ticker price (audit D1).
    m = FakeMarket(bullish=True)
    plan = analyze_candles("BTCUSDT", "15m", m.klines["15m"])["trade_plan"]
    m.live_price = plan["entry_high"] * 1.01  # +1% above the zone
    signal, reason = evaluate_symbol("BTCUSDT", m)
    assert signal is None
    assert "freshness" in reason


def test_freshness_ticker_failure_rejected():
    # T1 (fail-closed): live price unavailable -> HOLD, never ok:true.
    # (GAS returned {ok:true, skipped:'price_unavailable'} — audit C2.)
    m = FakeMarket(bullish=True)
    m.raise_live_price = True
    signal, reason = evaluate_symbol("BTCUSDT", m)
    assert signal is None
    assert "freshness" in reason and "fail-closed" in reason


def test_freshness_live_price_below_zone_accepts():
    # One-sided check (GAS L5075): live price below/at the zone is a valid
    # pullback entry for longs — only drift ABOVE the zone vetoes.
    m = FakeMarket(bullish=True)
    plan = analyze_candles("BTCUSDT", "15m", m.klines["15m"])["trade_plan"]
    m.live_price = plan["entry_low"] * 0.999
    signal, note = evaluate_symbol("BTCUSDT", m)
    assert signal is not None, note
    assert signal["live_price"] == pytest.approx(m.live_price)


def test_derivatives_total_failure_held():
    # T2 (audit D4/H6): snapshot with no usable components -> HOLD,
    # never auto-neutralized to score 50 and passed.
    m = FakeMarket(bullish=True)
    m.get_derivatives_snapshot = lambda s, period="4h": {
        "symbol": s, "provider": "none",
        "aggregate_long": None, "aggregate_funding": None,
        "aggregate_oi_change": None, "taker_buy_sell_ratio": None}
    signal, reason = evaluate_symbol("BTCUSDT", m)
    assert signal is None
    assert "derivatives" in reason and "holding" in reason


def test_derivatives_optional_gap_flagged_on_signal():
    # Required components (funding + OI) present, optional taker missing ->
    # signal passes with the gap recorded on the signal dict.
    m = FakeMarket(bullish=True)
    m.get_derivatives_snapshot = lambda s, period="4h": {
        "symbol": s, "provider": "fake",
        "aggregate_long": 52.0, "aggregate_funding": 0.0001,
        "aggregate_oi_change": 5.0, "taker_buy_sell_ratio": None}
    signal, note = evaluate_symbol("BTCUSDT", m)
    assert signal is not None, note
    assert signal["deriv_missing"] == ["taker_volume"]


def test_volume_unavailable_rejected():
    # T12 (audit D6/M5): volume fetch failure -> fail-closed.
    m = FakeMarket(bullish=True)
    m.get_24h_quote_volume_usd = lambda s: None
    signal, reason = evaluate_symbol("BTCUSDT", m)
    assert signal is None
    assert "volume" in reason and "fail-closed" in reason


def test_mtf_exception_rejected():
    # T13: a 1d fetch blowing up rejects the symbol — never skip-and-pass.
    m = FakeMarket(bullish=True)

    def klines(symbol, interval, limit=220):
        if interval == "1d":
            raise RuntimeError("provider timeout")
        return m.klines[interval]

    m.get_klines = klines
    signal, reason = evaluate_symbol("BTCUSDT", m)
    assert signal is None
    assert "MTF data error" in reason


def test_cascade_rejects_neutral_30m():
    # T5 (audit D5): STRICT 30m leg — a NEUTRAL 30m with bullish lean must NOT
    # pass the cascade. (GAS default LEAN would pass this exact setup.)
    assert config.MTF_30M_MODE == "STRICT"
    k4h = [{"close": c} for c in uptrend(100)]
    k1d = [{"close": c} for c in uptrend(100)]
    neutral_lean = {"signal": "NEUTRAL", "bull_score": 6, "bear_score": 4,
                    "trade_plan": None}
    r = mtf_cascade_check(k4h, k1d, neutral_lean)
    assert r["pass"] is False and r["stage"] == "30m"


def test_mtf_confirm_lean_matches_gas_default():
    # Documents the deliberate divergence: LEAN passes a neutral-lean 30m
    # (GAS default, CFG L86); STRICT rejects it. Bearish 30m fails in both.
    neutral_lean = {"signal": "NEUTRAL", "bull_score": 6, "bear_score": 4,
                    "trade_plan": None}
    lean = mtf_confirm(neutral_lean, "LEAN")
    assert lean["pass"] is True and lean.get("lean") is True
    assert lean["boost"] == config.MTF_LEAN_BOOST
    assert mtf_confirm(neutral_lean, "STRICT")["pass"] is False
    assert mtf_confirm(neutral_lean)["pass"] is False  # default is STRICT
    bearish = {"signal": "BEARISH WATCH", "bull_score": 1, "bear_score": 7,
               "trade_plan": {"entry": 1}}
    assert mtf_confirm(bearish, "LEAN")["pass"] is False
    assert mtf_confirm_lean(None)["pass"] is False


def test_evaluate_symbol_weak_strength_rejected():
    m = FakeMarket(bullish=True)
    weak = analyze_candles("BTCUSDT", "15m", m.klines["15m"])
    weak["strength"] = 2  # simulate a marginal setup
    import signal_engine
    orig = signal_engine.analyze_candles
    signal_engine.analyze_candles = lambda s, i, c: weak if i == "15m" else orig(s, i, c)
    try:
        signal, reason = evaluate_symbol("BTCUSDT", m)
    finally:
        signal_engine.analyze_candles = orig
    assert signal is None
    assert "strength" in reason
