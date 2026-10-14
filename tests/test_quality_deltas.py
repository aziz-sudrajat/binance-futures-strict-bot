"""ROADMAP ITEM 3 — unit tests for every warn-tier delta component.

Each test uses hand-computed values against the GAS source
(workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs).
T7: quality boundary behavior — synthetic signals scoring 66/69/70/76/86.
"""

import pytest

from quality_deltas import (
    btc_breadth_warn_delta,
    clamp_score_delta,
    final_quality,
    funding_delta,
    h1_trend_delta,
    leverage_warn_delta,
    liq_event_delta,
    liq_wall_warn_delta,
    market_regime,
    mtf_30m_delta,
    mtf_4h_delta,
    smart_money_delta,
    smart_money_score,
    squeeze_warn_delta,
    whale_warn_delta,
)


# ------------------------------------------------------------- MTF components
def test_mtf_30m_delta_strict_boost():
    # GAS: deepCheckMtf_ L3292-3298 — STRICT branch returns boost 8.
    d, note = mtf_30m_delta({"pass": True, "boost": 8})
    assert d == 8 and "+8" in note


def test_mtf_30m_delta_lean_boost():
    d, _ = mtf_30m_delta({"pass": True, "boost": 3, "lean": True})
    assert d == 3


def test_mtf_30m_delta_weak_penalty():
    # GAS L3296: penalty takes precedence over boost when set.
    d, note = mtf_30m_delta({"pass": True, "boost": 0, "penalty": -5})
    assert d == -5 and "weak" in note


def test_mtf_4h_delta_tiers():
    # GAS: mtfConfirm4hPass_ L2074-2085.
    assert mtf_4h_delta({"signal": "BULLISH WATCH",
                         "trade_plan": {"entry": 1}})[0] == 6
    assert mtf_4h_delta({"signal": "BEARISH WATCH"})[0] == -10
    assert mtf_4h_delta({"signal": "NEUTRAL", "bull_score": 6,
                         "bear_score": 4})[0] == 3
    assert mtf_4h_delta({"signal": "NEUTRAL", "bull_score": 3,
                         "bear_score": 6})[0] == -5
    assert mtf_4h_delta(None)[0] == 0  # soft: missing -> 0, never a veto


# ------------------------------------------------------------------ 1h trend
def _rising(n=60, start=100.0, step=1.0):
    return [start + step * i for i in range(n)]


def test_h1_trend_delta_bullish():
    # 60 rising closes: last > EMA50 and EMA50 rising -> +8 (GAS L3619).
    d, note = h1_trend_delta(_rising())
    assert d == 8 and "+8" in note


def test_h1_trend_delta_bearish():
    # 60 falling closes: last < EMA50 and EMA50 falling -> -10 (GAS L3623).
    d, note = h1_trend_delta(_rising(start=160.0, step=-1.0))
    assert d == -10 and "-10" in note


def test_h1_trend_delta_mixed_and_short():
    d, _ = h1_trend_delta([100.0] * 60)  # flat -> mixed
    assert d == 0
    d, note = h1_trend_delta([100.0] * 40)  # < 55 closes -> 0, no bonus
    assert d == 0 and "data kurang" in note


# ------------------------------------------------------- BTC breadth (warn)
def test_btc_breadth_warn_delta():
    # GAS: deepCheckBtcBreadth_ L3657-3660. -3% veto stays in vetoes.py V15.
    assert btc_breadth_warn_delta(-2.5)[0] == -15
    assert btc_breadth_warn_delta(-2.0)[0] == -15
    assert btc_breadth_warn_delta(-1.9)[0] == 0
    assert btc_breadth_warn_delta(0.5)[0] == 0
    assert btc_breadth_warn_delta(None)[0] == 0  # missing -> 0, never positive
    # at the veto tier the delta function defers to the veto
    d, note = btc_breadth_warn_delta(-3.5)
    assert d == 0 and "V15" in note


# ------------------------------------------------- derivatives warn tiers
def test_leverage_warn_delta():
    # GAS: deepCheckDerivatives_ L3336-3342. >= 90 is the V9 veto.
    assert leverage_warn_delta(75)[0] == -10
    assert leverage_warn_delta(70)[0] == -10
    assert leverage_warn_delta(69)[0] == 0
    assert leverage_warn_delta(None)[0] == 0


def test_funding_delta():
    # GAS L3344-3354. Extreme + aligned -> +5; against -> veto tier (vetoes).
    assert funding_delta(-0.0012, "BULLISH")[0] == 5   # negative funding
    assert funding_delta(0.0012, "BEARISH")[0] == 5    # supports shorts
    d, note = funding_delta(0.0012, "BULLISH")          # crowded long vs LONG
    assert d == 0 and "veto" in note
    assert funding_delta(0.0002, "BULLISH")[0] == 0     # below extreme
    assert funding_delta(None, "BULLISH")[0] == 0


def test_squeeze_warn_delta():
    # GAS L3356-3363. >= 85 is the V11 veto.
    assert squeeze_warn_delta(70)[0] == -8
    assert squeeze_warn_delta(65)[0] == -8
    assert squeeze_warn_delta(64)[0] == 0
    assert squeeze_warn_delta(None)[0] == 0


def test_whale_warn_delta():
    # GAS L3365-3380. BULLISH penalty tier: topLong <= 35 and div <= -10.
    assert whale_warn_delta(30.0, -12.0, "BULLISH")[0] == -12
    assert whale_warn_delta(35.0, -10.0, "BULLISH")[0] == -12
    assert whale_warn_delta(36.0, -12.0, "BULLISH")[0] == 0
    assert whale_warn_delta(30.0, -9.0, "BULLISH")[0] == 0
    assert whale_warn_delta(70.0, 12.0, "BEARISH")[0] == -12  # mirror
    assert whale_warn_delta(None, -12.0, "BULLISH")[0] == 0


def test_smart_money_score_hand_computed():
    # GAS: derivGateAdvanced_ L1052-1058.
    # 50 + (70-50)*.35 + (70-50)*.35 + (60-50)*.2 + 12*.25 + 4
    #   = 50 + 7 + 7 + 2 + 3 + 4 = 73
    adv = {"top_long": 70.0, "top_position_long": 70.0, "taker_long": 60.0,
           "divergence": 12.0, "oi_change": 5.0, "price_change_24h": 1.0}
    assert smart_money_score(adv) == 73
    # missing positioning -> None (never fabricated)
    assert smart_money_score({"top_long": 70.0}) is None
    # divergence clamped to [-25, 25]: 50 + 20*.35 + 20*.35 + 0 + 25*.25 = 70.25 -> 70
    adv2 = {"top_long": 70.0, "top_position_long": 70.0, "taker_long": 50.0,
            "divergence": 100.0, "oi_change": 0.0}
    assert smart_money_score(adv2) == 70


def test_smart_money_delta():
    # GAS: deepCheckDerivatives_ L3381-3386.
    assert smart_money_delta(73, "BULLISH")[0] == 6
    assert smart_money_delta(30, "BULLISH")[0] == -6
    assert smart_money_delta(50, "BULLISH")[0] == 0
    assert smart_money_delta(None, "BULLISH")[0] == 0  # missing -> 0
    assert smart_money_delta(30, "BEARISH")[0] == 6    # mirror
    assert smart_money_delta(73, "BEARISH")[0] == -6


def test_market_regime_and_liq_event():
    # GAS: derivGateAdvanced_ L1060-1068; LIQUIDATION EVENT -> -15 (L3388).
    base = {"price_change_24h": 0.5, "oi_change": 1.0, "spike": 5.0,
            "long_liq_usd": 10.0, "short_liq_usd": 10.0}
    assert market_regime(base) == "LIQUIDATION EVENT"
    assert liq_event_delta("LIQUIDATION EVENT")[0] == -15
    bull = dict(base, price_change_24h=2.0, oi_change=5.0, spike=1.0)
    assert market_regime(bull) == "LEVERAGE BUILDUP • BULLISH"
    calm = dict(base, spike=1.0)
    assert market_regime(calm) == "BALANCED"
    assert liq_event_delta("BALANCED")[0] == 0
    assert market_regime({}) == "UNKNOWN"  # missing -> UNKNOWN, not BALANCED


# ------------------------------------------------- liquidity wall warn tier
def _book(mid, walls):
    # walls: list of (dist_pct, notional) on the ask side
    asks = [(mid * (1 + d / 100), n / (mid * (1 + d / 100))) for d, n in walls]
    return {"mid": mid, "asks": asks, "bids": [(mid * 0.999, 1.0)]}


def test_liq_wall_warn_delta():
    # GAS deepCheckLiquidity_ L3428-3436 — the GAS version was dead (NaN bug,
    # audit M1); here it is a real -8 for walls in the 0.5%-1.0% band.
    d, note = liq_wall_warn_delta(_book(100.0, [(0.7, 300_000)]), "BULLISH")
    assert d == -8 and "-8" in note
    # inside the veto band (<= 0.5%) -> veto territory, not the warn tier
    assert liq_wall_warn_delta(_book(100.0, [(0.3, 300_000)]),
                               "BULLISH")[0] == 0
    # below the $250k notional floor -> no penalty
    assert liq_wall_warn_delta(_book(100.0, [(0.7, 100_000)]),
                               "BULLISH")[0] == 0
    # beyond the warn band -> no penalty
    assert liq_wall_warn_delta(_book(100.0, [(1.5, 300_000)]),
                               "BULLISH")[0] == 0
    # bearish mirrors to the bid side
    book = {"mid": 100.0, "bids": [(99.3, 300_000 / 99.3)],
            "asks": [(100.1, 1.0)]}
    assert liq_wall_warn_delta(book, "BEARISH")[0] == -8
    # missing book -> 0, never assumed clear
    assert liq_wall_warn_delta(None, "BULLISH")[0] == 0


# ------------------------------------------------- aggregation / T7 boundary
def test_clamp_score_delta():
    assert clamp_score_delta(45) == 40
    assert clamp_score_delta(-50) == -40
    assert clamp_score_delta(16) == 16


def test_final_quality_t7_boundaries():
    """T7: synthetic signals at 66/69/70/76/86 — reject/accept correctly."""
    # 66: strength 3, delta -4 -> REJECT
    q = final_quality(3, {"mtf_30m": 8, "mtf_4h": 6, "h1": -10, "lev_warn": -8})
    assert q["quality"] == 66 and q["pass"] is False
    assert q["delta"] == -4
    # 69: strength 3, delta -1 -> REJECT
    q = final_quality(3, {"mtf_30m": 8, "mtf_4h": 6, "h1": -10, "squeeze_warn": -5})
    assert q["quality"] == 69 and q["pass"] is False
    # 70: strength 3, delta 0 -> ACCEPT (boundary inclusive, GAS: qNow < minQ)
    q = final_quality(3, {"mtf_30m": 8, "mtf_4h": 6, "h1": -14})
    assert q["quality"] == 70 and q["pass"] is True
    # 76: strength 3, delta +6 -> ACCEPT
    q = final_quality(3, {"mtf_30m": 8, "mtf_4h": 6, "squeeze_warn": -8})
    assert q["quality"] == 76 and q["pass"] is True
    # 86: strength 4, delta +6 -> ACCEPT
    q = final_quality(4, {"mtf_30m": 8, "mtf_4h": 6, "squeeze_warn": -8})
    assert q["quality"] == 86 and q["pass"] is True
    # breakdown is traceable on the result
    assert q["components"]["squeeze_warn"] == -8
    assert q["raw_delta"] == 6


def test_final_quality_warn_tiers_can_reject():
    """The point of item 3: real negative deltas can pull a strength-4
    signal below 70 (impossible under the old constant-+16 formula)."""
    q = final_quality(4, {"mtf_30m": 8, "mtf_4h": 6, "h1": -10,
                          "lev_warn": -10, "whale_warn": -12,
                          "liq_event": -15})
    # 80 + 14 - 47 -> raw -33 -> quality 47
    assert q["quality"] == 47 and q["pass"] is False
    assert q["raw_delta"] == -33 and q["delta"] == -33
