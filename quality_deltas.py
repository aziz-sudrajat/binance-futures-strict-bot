"""Warn-tier score deltas — ROADMAP ITEM 3 (quality-gate recalibration).

Ports every scoreDelta contributor from the GAS deep check
(prePublishDeepCheck_, L3172-3262) as an explicit, named, tested component —
positive and negative. Veto tiers stay vetoes (vetoes.py): they REJECT the
signal, they never merely subtract.

GAS reference: workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs

Component map (GAS line -> function):
  deepCheckMtf_ 30m leg (L3292-3298)        -> mtf_30m_delta
  mtfConfirm4hPass_ (L2074-2085)            -> mtf_4h_delta
  deepCheckH1Trend_ (L3605-3632)            -> h1_trend_delta
  deepCheckBtcBreadth_ warn tier (L3634)    -> btc_breadth_warn_delta
  deepCheckDerivatives_ leverage (L3341)    -> leverage_warn_delta
  deepCheckDerivatives_ funding (L3344)     -> funding_delta
  deepCheckDerivatives_ squeeze (L3358)     -> squeeze_warn_delta
  deepCheckDerivatives_ whale (L3371)       -> whale_warn_delta
  derivGateAdvanced_ smart money (L1052)    -> smart_money_score / smart_money_delta
  derivGateAdvanced_ regime (L1060)         -> market_regime
  deepCheckDerivatives_ liq event (L3388)   -> liq_event_delta
  deepCheckLiquidity_ warn tier (L3428)     -> liq_wall_warn_delta
  scoreDelta clamp (L3250)                  -> clamp_score_delta
  deepCheckQualityScore_ (L3532-3535)       -> final_quality

Deliberately NOT ported:
  - deepCheckPatterns_ deltas (L3268): chart-pattern detection is not ported
    in the Python engine (documented simplification).
  - deepCheckHourBoost_ (L3244): self-referential hour-of-day boost; the audit
    explicitly excludes it.
  - deepCheckNews_ (L3437): AI sentiment veto only, no delta; AI stays out
    until calibrated.

Missing data is never positive evidence: every component returns (0, note)
when its inputs are unavailable, and records the gap in the note.
"""

from __future__ import annotations

import math

import config
from indicators import ema_series


def _finite(v) -> bool:
    return v is not None and math.isfinite(float(v))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ------------------------------------------------------------- MTF components
def mtf_30m_delta(chk: dict | None,
                direction: str = "BULLISH") -> tuple[int, str]:
    """GAS: deepCheckMtf_ (L3292-3298): delta = penalty when set, else boost."""
    chk = chk or {}
    pen = int(chk.get("penalty") or 0)
    if pen:
        return pen, f"mtf 30m {pen}: weak 30m lean"
    boost = int(chk.get("boost") or 0)
    lean = " lean" if chk.get("lean") else ""
    side = "bearish" if direction == "BEARISH" else "bullish"
    return boost, f"mtf +{boost}: 15m & 30m {side}{lean}"


def mtf_4h_delta(a4h: dict | None,
                 direction: str = "BULLISH") -> tuple[int, str]:
    """GAS: mtfConfirm4hPass_ (L2074-2085), wired via deepCheckMtf_ (L3302-3316).

    BULLISH WATCH + plan -> +6; BEARISH -> -10; neutral bull-lean -> +3 lean;
    neutral bear-lean -> -5 weak. Soft scoring: missing data -> 0, never a veto.
    Mirrored for BEARISH direction (shorts): BEARISH WATCH + plan -> +6,
    BULLISH -> -10.
    """
    if not a4h:
        return 0, "mtf 4h: data tidak tersedia"
    sig = str(a4h.get("signal") or "")
    is_bull = sig.startswith("BULL")
    is_bear = sig.startswith("BEAR")
    want_bear = direction == "BEARISH"
    aligned = (is_bear if want_bear else is_bull) and a4h.get("trade_plan")
    opposed = is_bull if want_bear else is_bear
    if aligned:
        b = config.MTF_CONFIRM_4H_BOOST
        return b, f"mtf 4h +{b}: 15m/30m/4h searah"
    if opposed:
        p = config.MTF_CONFIRM_4H_PENALTY
        return -p, f"mtf 4h -{p}: 4h {sig.lower()}"
    bull = float(a4h.get("bull_score") or 0)
    bear = float(a4h.get("bear_score") or 0)
    if (bear >= bull) if want_bear else (bull >= bear):
        return 3, "mtf 4h +3: lean"
    return -5, "mtf 4h -5: weak"


def mtf_1d_delta(trend: str | None,
                 direction: str = "BULLISH") -> tuple[int, str]:
    """Soft 1d-trend scoring. Used when the 1d cascade leg is NOT a hard gate
    (relaxed modes, e.g. biasa): the major-trend information still moves the
    quality score — aligned -> +MTF_CONFIRM_1D_BOOST, opposed ->
    -MTF_CONFIRM_1D_PENALTY, neutral/missing -> 0. Never a veto."""
    want = "BEARISH" if direction == "BEARISH" else "BULLISH"
    if not trend:
        return 0, "mtf 1d: data tidak tersedia"
    if trend == want:
        b = config.MTF_CONFIRM_1D_BOOST
        return b, f"mtf 1d +{b}: trend besar searah"
    if trend == "NEUTRAL":
        return 0, "mtf 1d netral (tidak searah, tidak berlawanan)"
    p = config.MTF_CONFIRM_1D_PENALTY
    return -p, f"mtf 1d -{p}: trend besar berlawanan"


# ------------------------------------------------------------------ 1h trend
def h1_trend_delta(closes: list | None,
                   direction: str = "BULLISH") -> tuple[int, str]:
    """GAS: deepCheckH1Trend_ (L3605-3632).

    >= 55 closes; EMA50; aligned -> +H1_TREND_BONUS (8),
    opposed -> -H1_TREND_PENALTY (10), mixed -> 0.
    Insufficient data -> 0 with a note (GAS behavior), never a bonus.
    Mirrored for BEARISH direction (shorts).
    """
    bonus, penalty = config.H1_TREND_BONUS, config.H1_TREND_PENALTY
    if not closes or len(closes) < 55:
        return 0, "1h trend: data kurang"
    xs = [float(c) for c in closes]
    e = ema_series(xs, 50)
    last, ema_now, ema_prev = xs[-1], e[-1], e[-11]
    bull_aligned = last > ema_now and ema_now >= ema_prev
    bear_aligned = last < ema_now and ema_now <= ema_prev
    if direction == "BEARISH":
        if bear_aligned:
            return bonus, f"1h trend bearish selaras (+{bonus})"
        if bull_aligned:
            return -penalty, f"1h trend bullish berlawanan (-{penalty})"
        return 0, "1h trend mixed (netral)"
    if bull_aligned:
        return bonus, f"1h trend bullish selaras (+{bonus})"
    if bear_aligned:
        return -penalty, f"1h trend bearish berlawanan (-{penalty})"
    return 0, "1h trend mixed (netral)"


# ------------------------------------------------------- BTC breadth (warn)
def btc_breadth_warn_delta(chg: float | None,
                           direction: str = "BULLISH") -> tuple[int, str]:
    """GAS: deepCheckBtcBreadth_ warn tier (L3634-3662).

    BULLISH: BTC <= -BTC_DUMP_VETO_PCT (3%) is the V15 circuit-breaker veto
    and stays in vetoes.py. The warn tier (<= -BTC_DUMP_WARN_PCT, 2%)
    subtracts -BTC_DUMP_PENALTY (15) from quality.
    BEARISH (shorts): mirrored — a vertical BTC pump (+2%/+3% per 1h) is the
    hostile regime; the veto tier lives in vetoes.py V15.
    """
    if not _finite(chg):
        return 0, "btc breadth: data tidak valid"
    chg = float(chg)
    if direction == "BEARISH":
        if chg >= config.BTC_DUMP_VETO_PCT:
            return 0, (f"BTC +{chg:.2f}%/1h at veto tier — veto handled by "
                       f"vetoes.py V15")
        if chg >= config.BTC_DUMP_WARN_PCT:
            p = config.BTC_DUMP_PENALTY
            return -p, f"BTC menguat +{chg:.2f}%/1h (-{p})"
        return 0, f"BTC breadth OK ({chg:.2f}%/1h)"
    if chg <= -config.BTC_DUMP_VETO_PCT:
        return 0, (f"BTC {chg:.2f}%/1h at veto tier — veto handled by "
                   f"vetoes.py V15")
    if chg <= -config.BTC_DUMP_WARN_PCT:
        p = config.BTC_DUMP_PENALTY
        return -p, f"BTC lemah {chg:.2f}%/1h (-{p})"
    return 0, f"BTC breadth OK ({chg:.2f}%/1h)"


# ------------------------------------------------- derivatives warn tiers
def leverage_warn_delta(lev: int | float | None) -> tuple[int, str]:
    """GAS: deepCheckDerivatives_ (L3336-3342). >=90 is the V9 veto (vetoes.py);
    70-89 subtracts 10."""
    if not _finite(lev):
        return 0, "deriv: leverage risk unavailable"
    lev = int(lev)
    if lev >= 70:
        return -10, f"deriv -10: leverage risk {lev}"
    return 0, f"deriv: leverage risk {lev} OK"


def funding_delta(funding: float | None, direction: str) -> tuple[int, str]:
    """GAS: deepCheckDerivatives_ (L3344-3354).

    |funding| >= DERIV_FUNDING_EXTREME (0.1%) *against* the direction is the
    V10 veto (vetoes.py). Aligned extreme funding adds +5 ("supports
    direction"). Below the extreme threshold there is no delta.
    """
    if not _finite(funding):
        return 0, "deriv: funding unavailable"
    f = float(funding)
    if abs(f) >= config.DERIV_FUNDING_EXTREME:
        crowded_long = f > 0  # GAS L3347: positive funding = longs pay shorts
        d = direction.upper()
        against = ((d == "BULLISH" and crowded_long)
                   or (d == "BEARISH" and not crowded_long))
        if against:
            return 0, "deriv: funding extreme against direction — veto tier (vetoes.py)"
        return 5, "deriv +5: Funding supports direction"
    return 0, "deriv: funding not extreme"


def squeeze_warn_delta(sq: int | float | None) -> tuple[int, str]:
    """GAS: deepCheckDerivatives_ (L3356-3363). >=85 is the V11 veto (vetoes.py);
    65-84 subtracts 8."""
    if not _finite(sq):
        return 0, "deriv: squeeze risk unavailable"
    sq = int(sq)
    if sq >= 65:
        return -8, f"deriv -8: squeeze risk {sq}"
    return 0, f"deriv: squeeze risk {sq} OK"


def whale_warn_delta(top_long: float | None, divergence: float | None,
                     direction: str) -> tuple[int, str]:
    """GAS: deepCheckDerivatives_ (L3365-3380).

    Extreme divergence (topLong <= 25 & div <= -15 for BULLISH) is the V12
    veto (vetoes.py). The penalty tier subtracts 12:
      BULLISH: topLong <= 35 and divergence <= -10
      BEARISH: topLong >= 65 and divergence >= +10
    """
    if not _finite(top_long) or not _finite(divergence):
        return 0, "deriv: top-trader positioning unavailable"
    tl, dv = float(top_long), float(divergence)
    pen = config.DERIV_TOP_TRADER_VETO_LONG
    d = direction.upper()
    if ((d == "BULLISH" and tl <= pen and dv <= -10)
            or (d == "BEARISH" and tl >= 100 - pen and dv >= 10)):
        return -12, f"deriv -12: top traders against ({tl:.1f}% long)"
    return 0, "deriv: top traders not against"


def smart_money_score(adv: dict) -> int | None:
    """GAS: derivGateAdvanced_ (L1052-1058).

    smart = 50 + (topAccountLong-50)*.35 + (topPositionLong-50)*.35
            + (takerLong-50)*.2 + clamp(divergence,-25,25)*.25
            + (oiChange>3 ? (priceChange>=0 ? +4 : -4) : 0), clamped 0-100.
    Returns None when the required positioning inputs are missing.
    """
    adv = adv or {}
    vals = [adv.get("top_long"), adv.get("top_position_long"),
            adv.get("taker_long"), adv.get("divergence")]
    if not all(_finite(v) for v in vals):
        return None
    top, top_pos, taker, div = (float(v) for v in vals)
    smart = (50.0 + (top - 50) * 0.35 + (top_pos - 50) * 0.35
             + (taker - 50) * 0.2 + _clamp(div, -25, 25) * 0.25)
    oi_change = adv.get("oi_change")
    if _finite(oi_change) and float(oi_change) > 3:
        pc = adv.get("price_change_24h")
        smart += 4 if (_finite(pc) and float(pc) >= 0) else -4
    return round(_clamp(smart, 0, 100))


def smart_money_delta(smart: int | None, direction: str) -> tuple[int, str]:
    """GAS: deepCheckDerivatives_ (L3381-3386). Aligned -> +6, against -> -6,
    neutral band -> 0. Unavailable -> 0 (missing data is never positive)."""
    if smart is None or not _finite(smart):
        return 0, "deriv: smart money unavailable"
    s = float(smart)
    d = direction.upper()
    if (d == "BULLISH" and s >= 60) or (d == "BEARISH" and s <= 40):
        return 6, "deriv +6: Smart money aligned"
    if (d == "BULLISH" and s <= 40) or (d == "BEARISH" and s >= 60):
        return -6, "deriv -6: Smart money against signal"
    return 0, f"deriv: smart money neutral ({s:g})"


def market_regime(adv: dict) -> str:
    """GAS: derivGateAdvanced_ regime ladder (L1060-1068).

    Uses 24h price change, OI change, liquidation imbalance and the spike
    ratio. 'UNKNOWN' when the required inputs are missing.
    """
    adv = adv or {}
    pc, oic = adv.get("price_change_24h"), adv.get("oi_change")
    if not (_finite(pc) and _finite(oic)):
        return "UNKNOWN"
    pc, oic = float(pc), float(oic)
    spike = float(adv.get("spike") or 0)
    long_liq = float(adv.get("long_liq_usd") or 0)
    short_liq = float(adv.get("short_liq_usd") or 0)
    if pc > 0.8 and oic > 2:
        return "LEVERAGE BUILDUP • BULLISH"
    if pc < -0.8 and oic > 2:
        return "LEVERAGE BUILDUP • BEARISH"
    if pc > 0.8 and oic < -2 and short_liq > long_liq:
        return "SHORT SQUEEZE"
    if pc < -0.8 and oic < -2 and long_liq > short_liq:
        return "LONG FLUSH"
    if oic < -3:
        return "DELEVERAGING"
    if spike >= config.DERIV_LIQ_SPIKE_THRESHOLD:
        return "LIQUIDATION EVENT"
    return "BALANCED"


def liq_event_delta(regime: str) -> tuple[int, str]:
    """GAS: deepCheckDerivatives_ (L3388-3391). A liquidation-event regime
    subtracts 15 (stand aside — high volatility)."""
    if "LIQUIDATION EVENT" in str(regime or ""):
        return -15, "deriv -15: liquidation event (high volatility)"
    return 0, f"deriv: regime {regime}"


# ------------------------------------------------- liquidity wall warn tier
def liq_wall_warn_delta(book: dict | None, direction: str) -> tuple[int, str]:
    """Warn tier of deepCheckLiquidity_ (GAS L3428-3436), implemented correctly.

    The GAS version was dead twice over (audit M1): `r.delta` was never
    initialized (NaN on `r.delta -= 8`) and never summed into scoreDelta.
    Here the warn tier is a real component: the biggest wall >=
    LIQ_WALL_MIN_NOTIONAL_USD ($250k) within LIQ_WALL_WARN_DISTANCE_PCT (1.0%)
    but OUTSIDE the veto band (0.5%, V13 in vetoes.py) subtracts 8.
    """
    wd = config.LIQ_WALL_WARN_DISTANCE_PCT
    if (not book or not _finite(book.get("mid")) or float(book["mid"]) <= 0):
        return 0, "liq: order book tidak tersedia"
    mid = float(book["mid"])
    d = direction.upper()
    side = book.get("asks") if d == "BULLISH" else book.get("bids")
    side_name = "ask" if d == "BULLISH" else "bid"
    warners = []
    for price, size in side or []:
        if not (_finite(price) and _finite(size)):
            continue
        dist = abs(float(price) - mid) / mid * 100
        notional = float(price) * float(size)
        if (dist <= wd and notional >= config.LIQ_WALL_MIN_NOTIONAL_USD
                and dist > config.LIQ_WALL_VETO_DISTANCE_PCT):
            warners.append((notional, dist))
    if warners:
        notional, dist = max(warners)
        return -8, (f"liq -8: {side_name} wall ${notional:,.0f} at {dist:.2f}% "
                    f"(within {wd}%)")
    return 0, f"liq: tidak ada wall besar <{wd}% menghadang"


# ------------------------------------------------- aggregation (GAS L3250+)
def clamp_score_delta(delta: float) -> int:
    """GAS L3250: scoreDelta is clamped to [-40, +40] before the quality bar."""
    return int(_clamp(round(delta), -config.SCORE_DELTA_CLAMP,
                      config.SCORE_DELTA_CLAMP))


def final_quality(strength: int, components: dict) -> dict:
    """GAS: deepCheckQualityScore_ (L3532-3535).

    quality = clamp(40 + strength*10 + clamped_delta, 5, 98).
    `components` is an ordered dict of name -> delta (int); the full
    breakdown is returned for the audit trail. STRICT passes at quality >= 70
    (getMinQuality_, L3137-3141).
    """
    raw = sum(int(v) for v in (components or {}).values())
    delta = clamp_score_delta(raw)
    q = 40 + int(strength) * 10 + delta
    q = int(_clamp(q, config.QUALITY_MIN, config.QUALITY_MAX))
    return {
        "quality": q,
        "delta": delta,
        "raw_delta": int(round(raw)),
        "components": dict(components or {}),
        "pass": q >= config.MIN_QUALITY_SCORE,
    }
