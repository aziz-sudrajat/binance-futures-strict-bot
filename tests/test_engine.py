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
def _inject_pattern(candles: list, bullish: bool) -> None:
    """Rewrite the last candle as a hammer (bullish) or shooting star
    (bearish) so the ketat pattern review passes on synthetic data."""
    c = candles[-1]
    o, cl = float(c["open"]), float(c["close"])
    mid = (o + cl) / 2
    body = abs(cl - o) * 0.2 + mid * 0.0005  # small body
    if bullish:
        # hammer: small body at top, long lower wick
        c["close"] = mid + body / 2
        c["open"] = mid - body / 2
        c["high"] = mid + body
        c["low"] = mid - body * 6
    else:
        # shooting star: small body at bottom, long upper wick
        c["close"] = mid - body / 2
        c["open"] = mid + body / 2
        c["high"] = mid + body * 6
        c["low"] = mid - body
    c["volume"] = float(c.get("volume", 1000.0))


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
        self.klines = {tf: gen() for tf in ("15m", "30m", "4h", "1d", "1h")}
        # Inject a supporting candlestick pattern into the last 15m candle
        # so the ketat pattern review passes on synthetic data.
        _inject_pattern(self.klines["15m"], bullish)
        # BTC 1h override for the circuit breaker (None = use bullish 1h).
        # Tests set this explicitly to simulate a BTC dump.
        self.btc_1h = None
        self.adv = dict(self.BENIGN_ADV)
        self.raise_adv = False
        # News feed: benign (no headlines) by default.
        self.news_ok = True
        self.news_items = []
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
        if symbol == "BTCUSDT" and interval == "1h" and self.btc_1h is not None:
            return self.btc_1h
        return self.klines[interval]

    def news_feed_ok(self):
        return self.news_ok

    def get_coin_news(self, symbol, hours=24):
        return list(self.news_items)

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
    assert "KETAT PASS" in note


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
    # T13: a 1h fetch blowing up rejects the symbol — never skip-and-pass.
    m = FakeMarket(bullish=True)

    def klines(symbol, interval, limit=220):
        if interval == "1h":
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


def test_bearish_produces_short_when_bidirectional():
    """Bidirectional mode (biasa): a BEARISH WATCH must produce a proper
    SHORT signal — direction SHORT with bearish geometry (stop above entry
    above TPs), never an inverted LONG."""
    import config
    old = config.BULLISH_ONLY
    config.BULLISH_ONLY = False
    try:
        signal, note = evaluate_symbol("BTCUSDT", FakeMarket(bullish=False))
    finally:
        config.BULLISH_ONLY = old
    assert signal is not None, note
    assert signal["direction"] == "SHORT"
    assert signal["stop_loss"] > signal["entry"] > signal["tp1"]
    assert signal["entry_low"] <= signal["entry"] <= signal["entry_high"]
    assert signal["quality"] >= config.MIN_QUALITY_SCORE


def test_bearish_cascade_mirrored():
    k4h_bear = [{"close": c} for c in [100.0 * 0.998 ** i for i in range(100)]]
    k1d_bear = [{"close": c} for c in [100.0 * 0.999 ** i for i in range(100)]]
    a30_bear = {"signal": "BEARISH WATCH", "trade_plan": {"entry": 1}}
    r = mtf_cascade_check(k4h_bear, k1d_bear, a30_bear, "BEARISH")
    assert r["pass"] is True and r["boost"] == config.MTF_CASCADE_BOOST
    # bullish higher TFs fail a SHORT cascade
    k4h_bull = [{"close": c} for c in uptrend(100)]
    r = mtf_cascade_check(k4h_bull, k1d_bear, a30_bear, "BEARISH")
    assert r["pass"] is False and r["stage"] == "4h"
    # default direction stays BULLISH (GAS parity)
    a30_bull = {"signal": "BULLISH WATCH", "trade_plan": {"entry": 1}}
    assert mtf_cascade_check(k4h_bull, k1d_bear, a30_bull)["pass"] is False


def test_mtf_confirm_strict_bearish():
    ok = {"signal": "BEARISH WATCH", "trade_plan": {"entry": 1}}
    r = mtf_confirm_strict(ok, "BEARISH")
    assert r["pass"] is True and r["boost"] == config.MTF_CONFIRM_BOOST
    assert mtf_confirm_strict(
        {"signal": "BULLISH WATCH", "trade_plan": {"entry": 1}},
        "BEARISH")["pass"] is False
    # default direction unchanged
    assert mtf_confirm_strict(ok)["pass"] is False


def test_delta_mirrors_for_shorts():
    from quality_deltas import (mtf_4h_delta, h1_trend_delta,
                                btc_breadth_warn_delta)
    a4h_bear = {"signal": "BEARISH WATCH", "trade_plan": {"entry": 1}}
    d, _ = mtf_4h_delta(a4h_bear, "BEARISH")
    assert d == config.MTF_CONFIRM_4H_BOOST
    d, _ = mtf_4h_delta({"signal": "BULLISH WATCH", "trade_plan": {"entry": 1}},
                        "BEARISH")
    assert d == -config.MTF_CONFIRM_4H_PENALTY
    d, _ = h1_trend_delta([100.0 * 0.999 ** i for i in range(60)], "BEARISH")
    assert d == config.H1_TREND_BONUS
    d, _ = h1_trend_delta([100.0 * 1.001 ** i for i in range(60)], "BEARISH")
    assert d == -config.H1_TREND_PENALTY
    d, _ = btc_breadth_warn_delta(2.5, "BEARISH")
    assert d == -config.BTC_DUMP_PENALTY
    d, _ = btc_breadth_warn_delta(-2.5, "BEARISH")
    assert d == 0  # BTC dumping is fine for shorts


def test_mtf_cascade_relaxed_1d_leg():
    """MTF_REQUIRE_1D=False (biasa): 1d neutral no longer hard-rejects; the
    1d trend is threaded through for soft scoring."""
    import config
    old = config.MTF_REQUIRE_1D
    config.MTF_REQUIRE_1D = False
    try:
        k4h_bear = [{"close": c} for c in [100.0 * 0.998 ** i for i in range(100)]]
        k1d_neutral = [{"close": 100.0 + (i % 2)} for i in range(100)]
        a30_bear = {"signal": "BEARISH WATCH", "trade_plan": {"entry": 1}}
        r = mtf_cascade_check(k4h_bear, k1d_neutral, a30_bear, "BEARISH")
        assert r["pass"] is True
        assert r["t1d"] in ("BULLISH", "BEARISH", "NEUTRAL")  # threaded for soft delta
        # 4h leg is still a hard gate
        k4h_bull = [{"close": c} for c in uptrend(100)]
        r = mtf_cascade_check(k4h_bull, k1d_neutral, a30_bear, "BEARISH")
        assert r["pass"] is False and r["stage"] == "4h"
    finally:
        config.MTF_REQUIRE_1D = old


def test_mtf_1d_delta_soft_scoring():
    from quality_deltas import mtf_1d_delta
    d, _ = mtf_1d_delta("BEARISH", "BEARISH")
    assert d == config.MTF_CONFIRM_1D_BOOST
    d, _ = mtf_1d_delta("BULLISH", "BEARISH")
    assert d == -config.MTF_CONFIRM_1D_PENALTY
    d, note = mtf_1d_delta("NEUTRAL", "BEARISH")
    assert d == 0
    d, _ = mtf_1d_delta(None, "BULLISH")
    assert d == 0
    # BULLISH direction mirrors
    d, _ = mtf_1d_delta("BULLISH", "BULLISH")
    assert d == config.MTF_CONFIRM_1D_BOOST


def test_biasa_preset_relaxes_1d_leg():
    # User spec 2026-10-15: MTF is 4h/1h/30m/15m — no 1d leg in any mode.
    assert config.SIGNAL_MODE_PRESETS["ketat"]["mtf_require_1d"] is True
    assert config.SIGNAL_MODE_PRESETS["sedang"]["mtf_require_1d"] is False
    assert config.SIGNAL_MODE_PRESETS["biasa"]["mtf_require_1d"] is False
    assert config.SIGNAL_MODE_PRESETS["sedang"]["mtf_require_1h"] is True
    assert config.SIGNAL_MODE_PRESETS["ketat"]["mtf_require_1h"] is True
    assert config.SIGNAL_MODE_PRESETS["biasa"]["mtf_require_1h"] is False


# ------------------------------------------------- mode layers (2026-10-15)
@pytest.fixture
def _mode(monkeypatch):
    def _set(mode):
        monkeypatch.setattr(config, "SIGNAL_MODE", mode)
        # apply the preset knobs so the engine sees a coherent bundle
        bundle = dict(config.SIGNAL_MODE_PRESETS[mode])
        for k, v in bundle.items():
            attr = {"bullish_only": "BULLISH_ONLY",
                    "mtf_require_1d": "MTF_REQUIRE_1D",
                    "mtf_require_1h": "MTF_REQUIRE_1H",
                    "btc_align_required": "BTC_ALIGN_REQUIRED",
                    "pattern_required": "PATTERN_REQUIRED",
                    "news_check": "NEWS_CHECK",
                    "liq_review": "LIQ_REVIEW",
                    "whale_review": "WHALE_REVIEW"}.get(k)
            if attr:
                monkeypatch.setattr(config, attr, v)
        monkeypatch.setattr(config, "MIN_QUALITY_SCORE",
                            bundle["min_quality_score"])
    return _set


def test_biasa_mode_15m_only(_mode):
    # biasa: 15m setup alone passes — no MTF/BTC/pattern/news needed.
    _mode("biasa")
    m = FakeMarket(bullish=True)
    # break everything above 15m: flat 1h/4h, no pattern, no news feed
    m.klines["1h"] = candles_from_closes([100.0] * 220)
    m.klines["4h"] = candles_from_closes([100.0] * 220)
    m.klines["15m"] = bull_candles()  # no injected pattern
    signal, note = evaluate_symbol("ETHUSDT", m)
    assert signal is not None, note
    assert "BIASA PASS" in note
    assert signal["direction"] == "LONG"


def test_biasa_mode_bearish_short(_mode):
    _mode("biasa")
    m = FakeMarket(bullish=False)
    signal, note = evaluate_symbol("ETHUSDT", m)
    assert signal is not None, note
    assert signal["direction"] == "SHORT"


def test_sedang_rejects_mtf_misalignment(_mode):
    _mode("sedang")
    m = FakeMarket(bullish=True)
    m.klines["1h"] = candles_from_closes([100.0] * 220)  # flat -> neutral
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "MTF tak selaras" in reason


def test_sedang_rejects_btc_misalignment(_mode):
    _mode("sedang")
    m = FakeMarket(bullish=True)
    # BTC bearish while coin is bullish
    m.klines["15m"] = bull_candles()
    orig = m.get_klines

    def klines(symbol, interval, limit=220):
        if symbol == "BTCUSDT" and interval == "15m":
            return bear_candles()
        return orig(symbol, interval, limit)
    m.get_klines = klines
    _inject_pattern(m.klines["15m"], True)
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "arah BTC tak selaras" in reason


def test_sedang_passes_when_aligned(_mode):
    _mode("sedang")
    m = FakeMarket(bullish=True)
    signal, note = evaluate_symbol("ETHUSDT", m)
    assert signal is not None, note
    assert "SEDANG PASS" in note
    assert "MTF selaras" in note and "BTC selaras" in note


def test_ketat_rejects_no_pattern(_mode):
    _mode("ketat")
    m = FakeMarket(bullish=True)
    m.klines["15m"] = bull_candles()  # no injected pattern
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "pattern" in reason


def test_ketat_rejects_bearish_news(_mode):
    _mode("ketat")
    m = FakeMarket(bullish=True)
    m.news_items = [
        {"title": "Ethereum hacked in major exploit, funds stolen",
         "ts": 9999999999, "sentiment": -6},
        {"title": "ETH crash as lawsuit filed against foundation",
         "ts": 9999999999, "sentiment": -3},
    ]
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "news bearish" in reason


def test_ketat_rejects_news_feed_down(_mode):
    _mode("ketat")
    m = FakeMarket(bullish=True)
    m.news_ok = False
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "news feed tak tersedia" in reason


def test_ketat_rejects_liq_spike_against(_mode):
    _mode("ketat")
    m = FakeMarket(bullish=True)
    m.adv["spike"] = 5.0
    m.adv["long_liq_usd"] = 9_000_000.0
    m.adv["short_liq_usd"] = 1_000_000.0
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "likuidasi" in reason


def test_ketat_rejects_whale_against(_mode):
    _mode("ketat")
    m = FakeMarket(bullish=True)
    m.adv["top_long"] = 5.0    # whales extremely short
    m.adv["divergence"] = -30.0
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "whale melawan" in reason


def test_detect_pattern_unit():
    from signal_engine import detect_pattern
    # hammer
    candles = candles_from_closes([100.0] * 10)
    c = candles[-1]
    c["open"] = 100.0; c["close"] = 100.1
    c["high"] = 100.15; c["low"] = 99.0
    assert detect_pattern(candles, "BULLISH") == "hammer"
    assert detect_pattern(candles, "BEARISH") is None
    # shooting star
    c["high"] = 101.0; c["low"] = 99.95
    c["open"] = 100.0; c["close"] = 99.9
    assert detect_pattern(candles, "BEARISH") == "shooting-star"
    # bullish engulfing
    cs = candles_from_closes([100.0, 99.0, 101.0])
    cs[-2]["open"] = 100.0; cs[-2]["close"] = 99.0
    cs[-1]["open"] = 98.9; cs[-1]["close"] = 100.5
    assert detect_pattern(cs, "BULLISH") == "bullish-engulfing"


def test_news_sentiment_unit():
    from data import _news_sentiment
    assert _news_sentiment("Bitcoin hacked, funds stolen in exploit") < -4
    assert _news_sentiment("Ethereum ETF approval drives rally") > 0
    assert _news_sentiment("Bitcoin price dips below $82K on CPI data") == 0


def test_mtf_alignment_check_unit():
    from signal_engine import mtf_alignment_check
    m = FakeMarket(bullish=True)
    ok, note = mtf_alignment_check("ETHUSDT", m, "BULLISH")
    assert ok, note
    m.klines["1h"] = candles_from_closes([100.0] * 220)
    ok, note = mtf_alignment_check("ETHUSDT", m, "BULLISH")
    assert not ok and "1h=neutral" in note


def test_btc_direction_check_unit():
    from signal_engine import btc_direction_check
    m = FakeMarket(bullish=True)
    ok, note = btc_direction_check(m, "BULLISH")
    assert ok, note
    ok, note = btc_direction_check(m, "BEARISH")
    assert not ok and "tak selaras" in note
