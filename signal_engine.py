"""STRICT signal engine — port of the GAS bot's decision pipeline.

GAS source: workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs
Only the decision pipeline is ported; autopost/publishing machinery
(Threads captions, charts, Telegram, Square, X) is out of scope.

Ported functions (GAS line numbers in each docstring):
  analyze_candles        <- analyzeCandles_          (L1904)
  build_trade_plan       <- buildTradePlan_          (L1984)
  smart_crypto_score     <- smartCryptoScore_        (L2026)
  regime_bonus           <- regimeBonus_             (L1878)
  classify_market_regime <- classifyMarketRegime_    (L1889)
  signal_quality_rank    <- signalNowCryptoQuality_  (L2031)
  derivatives_score      <- calculateDerivativesScore_ (L879)
  mtf_trend_direction    <- mtfTrendDirection_       (L3144)
  mtf_confirm_strict     <- mtfConfirmPass_ (STRICT) (L2097-2101)
  mtf_confirm_lean       <- mtfConfirmPass_ (LEAN, the GAS default) (L2091-2110)
  mtf_confirm            <- mode dispatcher honoring config.MTF_30M_MODE
  mtf_cascade_check      <- mtfCascadeCheck_         (L3156)

STRICT pipeline (evaluate_symbol):
  1. Liquidity: 24h quote volume >= $2M — FAIL-CLOSED when unavailable
     (GAS applies the floor at universe time; audit D6/M5)
  2. 15m analysis -> BULLISH WATCH + trade plan + strength >= 3
     (GAS: BULLISH_ONLY L84, AUTO_MIN_STRENGTH L7535)
  3. MTF cascade: 4h bullish + 1d bullish + 30m per MTF_30M_MODE (L3156).
     Default STRICT: 30m must be a full BULLISH WATCH — deliberately stricter
     than the GAS default LEAN (audit D5). Any MTF fetch exception rejects.
  4. Derivatives positioning: funding + open-interest REQUIRED (audit H6/D4);
     missing required components HOLD the signal, never auto-neutral.
     Optional components (long/short, taker) are flagged on the signal dict.
  5. Deterministic hard vetoes (ROADMAP ITEM 2; GAS V9-V13, V15):
     leverage-risk, funding-extreme, squeeze-risk, whale-divergence,
     liquidity-wall, BTC-dump circuit breaker. Any fired veto — or any
     veto whose required inputs are unavailable — REJECTS the signal.
     Vetoes are never compensated by the quality score.
  6. Quality = 40 + strength*10 + delta, delta in [-40, 40]
     (GAS: deepCheckQualityScore_ L3532, scoreDelta clamp L3250)
  7. STRICT gate: quality >= 70 (GAS: getMinQuality_ L3137)
  8. Freshness: LIVE ticker price vs entry zone — reject if >0.4% above the
     zone; FAIL-CLOSED when the live price is unavailable (audit C2/C3/D1).
STRICT is effectively LONG-only (cascade requires bullish everywhere).
"""

from __future__ import annotations

import math
import time

import config
from indicators import adx, atr_wilder, average, ema_series, macd, rsi_wilder
from quality_deltas import (
    btc_breadth_warn_delta,
    final_quality,
    funding_delta,
    h1_trend_delta,
    leverage_warn_delta,
    liq_event_delta,
    liq_wall_warn_delta,
    market_regime,
    mtf_1d_delta,
    mtf_30m_delta,
    mtf_4h_delta,
    smart_money_delta,
    smart_money_score,
    squeeze_warn_delta,
    whale_warn_delta,
)
from vetoes import btc_1h_change_pct, leverage_risk_index, run_vetoes, squeeze_risk_index


def _finite(v) -> bool:
    """True for a usable (non-None, non-NaN/Inf) number."""
    return v is not None and math.isfinite(float(v))


# ------------------------------------------------------------------ regimes
def regime_bonus(regime: str, signal: str) -> float:
    """GAS: regimeBonus_ (L1878)."""
    if signal == "BULLISH WATCH" and regime == "TRENDING BULLISH":
        return 12
    if signal == "BEARISH WATCH" and regime == "TRENDING BEARISH":
        return 12
    if regime == "HIGH VOLATILITY":
        return -4
    if regime == "RANGING":
        return -2
    return 2


def classify_market_regime(price, e9, e21, e50, atr, adx_val) -> str:
    """GAS: classifyMarketRegime_ (L1889)."""
    price, atr, adx_val = float(price), float(atr or 0), float(adx_val or 0)
    atr_pct = atr / price * 100 if price else 0
    aligned_bull = e9 > e21 and e21 > e50
    aligned_bear = e9 < e21 and e21 < e50
    if atr_pct >= 3.5:
        return "HIGH VOLATILITY"
    if adx_val >= 23 and aligned_bull:
        return "TRENDING BULLISH"
    if adx_val >= 23 and aligned_bear:
        return "TRENDING BEARISH"
    if adx_val < 18:
        return "RANGING"
    return "MIXED / TRANSITION"


# -------------------------------------------------------------- trade plan
def build_trade_plan(signal: str, price: float, ema9: float, ema21: float,
                     atr: float, candles: list) -> dict:
    """GAS: buildTradePlan_ (L1984). ATR-based entry / stop / TP1-3."""
    price, ema9, ema21 = float(price), float(ema9), float(ema21)
    atr = float(atr)
    if not math.isfinite(atr) or atr <= 0:
        atr = max(price * 0.005, 1e-8)
    recent = candles[-config.SWING_LOOKBACK:]
    swing_low = min(float(c["low"]) for c in recent) if recent else price - atr
    swing_high = max(float(c["high"]) for c in recent) if recent else price + atr

    if signal == "BULLISH WATCH":
        pullback = min(price, max(ema9, ema21))
        entry = max(price - config.ENTRY_MAX_PULLBACK_ATR * atr, pullback)
        stop = min(entry - config.STOP_ATR_MIN * atr, swing_low - 0.10 * atr)
        stop = max(stop, entry - config.STOP_ATR_MAX * atr)
    else:  # BEARISH WATCH (kept for parity; STRICT never trades it)
        pullback = max(price, min(ema9, ema21))
        entry = min(price + config.ENTRY_MAX_PULLBACK_ATR * atr, pullback)
        stop = max(entry + config.STOP_ATR_MIN * atr, swing_high + 0.10 * atr)
        stop = min(stop, entry + config.STOP_ATR_MAX * atr)

    risk = max(abs(entry - stop), atr * 0.5)
    direction = 1 if signal == "BULLISH WATCH" else -1
    tp1 = entry + direction * risk * config.TP1_RR
    tp2 = entry + direction * risk * config.TP2_RR
    tp3 = entry + direction * risk * config.TP3_RR
    zone = config.ENTRY_ZONE_ATR * atr
    return {
        "entry": entry,
        "entry_low": entry - zone,
        "entry_high": entry + zone,
        "stop_loss": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": abs(tp1 - entry) / risk,
        "rr2": abs(tp2 - entry) / risk,
        "rr3": abs(tp3 - entry) / risk,
        "risk_pct": risk / entry * 100 if entry else 0,
        "atr": atr,
        "support": swing_low,
        "resistance": swing_high,
    }


# ---------------------------------------------------------------- analysis
def analyze_candles(symbol: str, interval: str, candles: list) -> dict:
    """GAS: analyzeCandles_ (L1904). Returns setup analysis + trade plan."""
    if not isinstance(candles, list) or len(candles) < config.MIN_CANDLES:
        raise ValueError(f"not enough candles: {len(candles) if candles else 0}")

    opens = [float(c["open"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]

    e9a = ema_series(closes, 9)
    e21a = ema_series(closes, 21)
    e50a = ema_series(closes, 50)
    m = macd(closes, 12, 26, 9)
    rsi = rsi_wilder(closes, 14)
    atr = atr_wilder(candles, config.ATR_PERIOD)
    adx_pack = adx(candles, 14)

    i = len(closes) - 1
    price, open_ = closes[i], opens[i]
    prev = closes[i - 1]
    e9, e21, e50 = e9a[i], e21a[i], e50a[i]
    avg_vol20 = average(volumes[max(0, i - 20): i])  # GAS: excludes current candle
    volume_ratio = volumes[i] / avg_vol20 if avg_vol20 > 0 else 1.0
    change_pct = (price / prev - 1) * 100 if prev else 0.0

    bull = bear = 0
    rb, rs = [], []
    if e9 > e21:
        bull += 1; rb.append("EMA9 > EMA21")
    else:
        bear += 1; rs.append("EMA9 < EMA21")
    if e21 > e50:
        bull += 1; rb.append("EMA21 > EMA50")
    else:
        bear += 1; rs.append("EMA21 < EMA50")
    if price > e21:
        bull += 1; rb.append("price > EMA21")
    else:
        bear += 1; rs.append("price < EMA21")
    if e21 > e21a[i - 1]:
        bull += 1; rb.append("EMA21 rising")
    elif e21 < e21a[i - 1]:
        bear += 1; rs.append("EMA21 falling")
    if 52 <= rsi <= 72:
        bull += 1; rb.append("RSI bullish")
    if 28 <= rsi <= 48:
        bear += 1; rs.append("RSI bearish")
    if m["histogram"] > 0:
        bull += 1; rb.append("MACD +")
    elif m["histogram"] < 0:
        bear += 1; rs.append("MACD -")
    if price > open_:
        bull += 1; rb.append("green candle")
    elif price < open_:
        bear += 1; rs.append("red candle")
    if volume_ratio >= 1.15:
        if price >= open_:
            bull += 1; rb.append("volume confirmation")
        else:
            bear += 1; rs.append("volume confirmation")

    signal, score, reasons = "NEUTRAL", max(bull, bear), []
    if bull >= config.SIGNAL_MIN_SCORE and bull - bear >= 2:
        signal, score, reasons = "BULLISH WATCH", bull, rb
    elif bear >= config.SIGNAL_MIN_SCORE and bear - bull >= 2:
        signal, score, reasons = "BEARISH WATCH", bear, rs

    # GAS: 5 points -> 3/5, 6 -> 4/5, 7+ -> 5/5
    strength = 0 if signal == "NEUTRAL" else min(5, max(1, round(score / 1.5)))
    warning = "RSI overbought" if rsi >= 72 else ("RSI oversold" if rsi <= 28 else "")

    plan = None
    if signal != "NEUTRAL":
        plan = build_trade_plan(signal, price, e9, e21, atr, candles)

    regime = classify_market_regime(price, e9, e21, e50, atr, adx_pack["adx"])
    return {
        "symbol": symbol, "interval": interval, "signal": signal,
        "strength": strength, "regime": regime, "score": score,
        "bull_score": bull, "bear_score": bear, "price": price,
        "change_pct": change_pct, "ema9": e9, "ema21": e21, "ema50": e50,
        "rsi": rsi, "macd_histogram": m["histogram"],
        "volume_ratio": volume_ratio, "atr": atr, "adx": adx_pack["adx"],
        "reasons": reasons, "warning": warning, "trade_plan": plan,
    }


# ------------------------------------------------------------------ scoring
def smart_crypto_score(a: dict) -> float:
    """GAS: smartCryptoScore_ (L2026)."""
    rr = float((a.get("trade_plan") or {}).get("rr1") or 0)
    return ((a.get("strength") or 0) * 20
            + min(15, (a.get("volume_ratio") or 0) * 6)
            + regime_bonus(a.get("regime"), a.get("signal"))
            + min(10, rr * 4)
            - (5 if a.get("warning") else 0))


def signal_quality_rank(a: dict) -> int:
    """GAS: signalNowCryptoQuality_ (L2031) — smart score normalized 0-100."""
    raw = float(smart_crypto_score(a) or 0)
    return max(0, min(100, round(raw / 1.37)))


def derivatives_score(snap: dict) -> dict:
    """GAS: calculateDerivativesScore_ (L879). Neutral base 50; 0-100."""
    score, notes = 50, []
    long_pct = snap.get("aggregate_long")
    funding = snap.get("aggregate_funding")
    oi = snap.get("aggregate_oi_change")
    taker = snap.get("taker_buy_sell_ratio")

    if _finite(long_pct):
        long_pct = float(long_pct)
        if long_pct >= 65:
            score -= 10; notes.append("crowded longs")
        elif long_pct <= 35:
            score += 10; notes.append("crowded shorts")
        elif long_pct >= 55:
            score -= 3; notes.append("mild long crowding")
        elif long_pct <= 45:
            score += 3; notes.append("mild short crowding")
    if _finite(funding):
        funding = float(funding)
        if funding >= 0.0005:
            score -= 10; notes.append("high positive funding")
        elif funding <= -0.0005:
            score += 10; notes.append("negative funding")
        elif funding > 0:
            score -= 2
        elif funding < 0:
            score += 2
    if _finite(oi):
        oi = float(oi)
        if oi >= 8:
            score += 8; notes.append("OI expanding fast")
        elif oi >= 3:
            score += 4; notes.append("OI rising")
        elif oi <= -8:
            score -= 8; notes.append("OI contracting fast")
        elif oi <= -3:
            score -= 4; notes.append("OI falling")
    if _finite(taker):
        br = float(taker)
        if br >= 1.2:
            score += 6; notes.append("aggressive buyers")
        elif 0 < br <= 0.8:
            score -= 6; notes.append("aggressive sellers")
    score = max(0, min(100, score))
    bias = ("BULLISH DERIVATIVES" if score > 55 else
            "BEARISH DERIVATIVES" if score < 45 else "NEUTRAL / BALANCED")
    return {"value": round(score), "bias": bias, "notes": notes}


def quality_score(strength: int, components: dict) -> dict:
    """Final STRICT quality — ROADMAP ITEM 3.

    Keeps the GAS base formula 40 + strength*10 (deepCheckQualityScore_, L3532)
    and builds scoreDelta from explicit, named components (quality_deltas.py),
    each ported from the GAS deep check (prePublishDeepCheck_, L3172-3262):

      mtf_30m   +8  (30m STRICT confirm, deepCheckMtf_ L3292-3298)
      mtf_4h    +6/-10/+3/-5 (mtfConfirm4hPass_ L2074-2085)
      h1        +8/-10/0 (deepCheckH1Trend_ L3605)
      btc_warn  -15 when BTC <= -2%/1h (deepCheckBtcBreadth_ L3634; veto at -3%
                stays in vetoes.py V15)
      lev_warn  -10 when leverage-risk 70-89 (deepCheckDerivatives_ L3341;
                veto >= 90 stays V9)
      funding   +5 when extreme & aligned (L3344-3354; against stays V10)
      squeeze   -8 when squeeze-risk 65-84 (L3356-3363; veto >= 85 stays V11)
      whale     -12 on whale-divergence penalty tier (L3365-3380; extreme stays V12)
      smart     +6/-6 smart-money alignment (L3381-3386)
      liq_event -15 on LIQUIDATION EVENT regime (L3388-3391)
      liq_wall  -8 on warn-tier wall 0.5-1.0% (deepCheckLiquidity_ L3428-3436,
                implemented correctly — the GAS version was dead code, audit M1)

    delta is clamped to [-40, +40] (GAS L3250); quality is clamped to [5, 98]
    (L3532-3535). STRICT passes at quality >= 70 (getMinQuality_, L3137).
    Vetoes are evaluated separately and can never be compensated by the score.
    """
    return final_quality(strength, components)


# ---------------------------------------------------------------------- MTF
def mtf_trend_direction(closes: list) -> str:
    """GAS: mtfTrendDirection_ (L3144). Needs >= 55 closes."""
    if len(closes) < 55:
        return "NEUTRAL"
    e21a = ema_series(closes, 21)
    e50a = ema_series(closes, 50)
    i = len(closes) - 1
    price = float(closes[i])
    if price > e21a[i] > e50a[i] and e21a[i] >= e21a[i - 1]:
        return "BULLISH"
    if price < e21a[i] < e50a[i] and e21a[i] <= e21a[i - 1]:
        return "BEARISH"
    return "NEUTRAL"


def mtf_confirm_strict(c30: dict, direction: str = "BULLISH") -> dict:
    """GAS: mtfConfirmPass_ STRICT branch (L2091-2101)."""
    if not c30:
        return {"pass": False, "reason": "no 30m data"}
    sig = str(c30.get("signal") or "")
    want_bear = direction == "BEARISH"
    aligned = sig.startswith("BEAR") if want_bear else sig.startswith("BULL")
    if aligned and c30.get("trade_plan"):
        return {"pass": True, "boost": config.MTF_CONFIRM_BOOST}
    need = "BEARISH" if want_bear else "BULLISH"
    return {"pass": False,
            "reason": f"30m {sig} (STRICT requires {need} WATCH)"}


def mtf_confirm_lean(c30: dict, direction: str = "BULLISH") -> dict:
    """GAS: mtfConfirmPass_ LEAN branch — the GAS *default* behavior (L2091-2110).

    BULLISH direction: bearish 30m fails; full BULLISH WATCH passes with
    boost 8; a NEUTRAL 30m with bull_score >= bear_score passes as a 'lean'
    with boost 3; otherwise passes weak with a -5 penalty. Kept for
    MTF_30M_MODE='LEAN' parity with GAS; the STRICT default below is
    deliberately stricter (audit D5). Mirrored for BEARISH direction.
    """
    if not c30:
        return {"pass": False, "reason": "no 30m data"}
    sig = str(c30.get("signal") or "")
    is_bull = sig.startswith("BULL")
    is_bear = sig.startswith("BEAR")
    want_bear = direction == "BEARISH"
    # A 30m hostile to the signal direction fails outright.
    if (is_bull and want_bear) or (is_bear and not want_bear):
        return {"pass": False, "reason": f"30m {sig.lower()} against signal"}
    # Full aligned setup passes with boost.
    if ((is_bear and want_bear) or (is_bull and not want_bear)):
        if c30.get("trade_plan"):
            return {"pass": True, "boost": config.MTF_CONFIRM_BOOST}
    bull_score = float(c30.get("bull_score") or 0)
    bear_score = float(c30.get("bear_score") or 0)
    if (bear_score >= bull_score) if want_bear else (bull_score >= bear_score):
        return {"pass": True, "boost": config.MTF_LEAN_BOOST, "lean": True}
    return {"pass": True, "boost": 0, "penalty": -5, "weak": True}


def mtf_confirm(c30: dict, mode: str | None = None,
                direction: str = "BULLISH") -> dict:
    """30m confirmation honoring config.MTF_30M_MODE (default 'STRICT')."""
    mode = str(mode or config.MTF_30M_MODE).upper()
    if mode == "LEAN":
        return mtf_confirm_lean(c30, direction)
    return mtf_confirm_strict(c30, direction)


def mtf_cascade_check(klines_4h: list, klines_1d: list, a30: dict,
                      direction: str = "BULLISH") -> dict:
    """GAS: mtfCascadeCheck_ (L3156). 4h trend aligned with the signal
    direction (hard gate) + 1d trend aligned (hard gate when
    config.MTF_REQUIRE_1D, else soft-scored via quality_deltas.mtf_1d_delta)
    + 30m per MTF_30M_MODE."""
    want = "BEARISH" if direction == "BEARISH" else "BULLISH"
    try:
        t4 = mtf_trend_direction([float(c["close"]) for c in klines_4h])
        if t4 != want:
            return {"pass": False, "stage": "4h",
                    "reason": f"4h trend {t4.lower()} (need {want.lower()})"}
        t1 = mtf_trend_direction([float(c["close"]) for c in klines_1d])
        if t1 != want and config.MTF_REQUIRE_1D:
            return {"pass": False, "stage": "1d",
                    "reason": f"1d trend {t1.lower()} (need {want.lower()})"}
        chk = mtf_confirm(a30, direction=direction)
        if not chk["pass"]:
            return {"pass": False, "stage": "30m", "reason": "30m: " + chk["reason"]}
        # The 30m check result is threaded through so the quality gate can
        # apply its delta (deepCheckMtf_ L3292-3298). In GAS STRICT the cascade
        # boost reaches scoreDelta only via this leg — there is no separate
        # cascade +8 in the delta sum. t1d is threaded for the soft 1d delta
        # in relaxed modes (when the 1d leg is not a hard gate).
        return {"pass": True, "t4h": t4, "t1d": t1, "chk": chk,
                "boost": config.MTF_CASCADE_BOOST}
    except Exception as e:  # noqa: BLE001
        return {"pass": False, "stage": "error", "reason": str(e)}


# ------------------------------------------------- mode-based signal layers
# User spec 2026-10-15 (+ aturan MTF 2026-10-15: SEMUA mode cek MTF 4h/1h/30m/15m):
#   biasa : 15m indicators + MTF alignment (4h, 1h, 30m, 15m all same
#           direction). Bidirectional, no BTC/veto/derivatives.
#   sedang: biasa + BTC direction must match.
#           + BTC 15m direction must match.
#   ketat : sedang + pattern review + news review + liquidation review
#           + whale review + full veto/derivatives/quality machinery.


def _dir_of_signal(sig: str) -> str:
    s = str(sig or "")
    if s.startswith("BULL"):
        return "BULLISH"
    if s.startswith("BEAR"):
        return "BEARISH"
    return "NEUTRAL"


def mtf_alignment_check(symbol: str, market, direction: str,
                        a15: dict | None = None,
                        a30: dict | None = None) -> tuple[bool, str]:
    """sedang/ketat: 4h, 1h, 30m, 15m must ALL point the signal direction.

    15m/30m use the indicator setup direction; 4h/1h use the EMA21/50
    trend. Fail-closed on any data error — never assume alignment.
    """
    want = "BULLISH" if direction == "BULLISH" else "BEARISH"
    try:
        if a15 is None:
            a15 = analyze_candles(symbol, "15m",
                                  market.get_klines(symbol, "15m"))
        if a30 is None:
            a30 = analyze_candles(symbol, "30m",
                                  market.get_klines(symbol, "30m"))
        k4h = market.get_klines(symbol, "4h")
        k1h = market.get_klines(symbol, "1h")
    except Exception as e:  # noqa: BLE001
        return False, f"MTF data error: {e} — holding (fail-closed)"
    legs = {
        "15m": _dir_of_signal(a15.get("signal")),
        "30m": _dir_of_signal(a30.get("signal")),
        "4h": mtf_trend_direction([float(c["close"]) for c in k4h]),
        "1h": mtf_trend_direction([float(c["close"]) for c in k1h]),
    }
    bad = [tf for tf, d in legs.items() if d != want]
    if bad:
        detail = ", ".join(f"{tf}={legs[tf].lower()}" for tf in
                           ("15m", "30m", "1h", "4h"))
        return False, (f"MTF tak selaras ({detail}; "
                       f"butuh {want.lower()} semua)")
    return True, f"MTF selaras {want.lower()} (15m/30m/1h/4h)"


def btc_direction_check(market, direction: str) -> tuple[bool, str]:
    """sedang/ketat: BTC 15m indicator direction must match the signal
    direction. Fail-closed when BTC data is unavailable."""
    want = "BULLISH" if direction == "BULLISH" else "BEARISH"
    try:
        a = analyze_candles("BTCUSDT", "15m",
                            market.get_klines("BTCUSDT", "15m"))
    except Exception as e:  # noqa: BLE001
        return False, f"arah BTC tak tersedia ({e}) — holding (fail-closed)"
    got = _dir_of_signal(a.get("signal"))
    if got != want:
        return False, (f"arah BTC tak selaras (BTC 15m {got.lower()}, "
                       f"sinyal {want.lower()})")
    return True, f"BTC selaras {want.lower()} (15m {a.get('signal')})"


def _candle_parts(c: dict) -> tuple:
    o = float(c["open"]); h = float(c["high"])
    l = float(c["low"]); cl = float(c["close"])
    body = abs(cl - o)
    return o, h, l, cl, body, h - l


def detect_pattern(candles: list, direction: str) -> str | None:
    """Ketat pattern review: find a candlestick pattern supporting the
    direction in recent 15m candles. Returns the pattern name or None.

    Bullish: bullish-engulfing, hammer. Bearish: bearish-engulfing,
    shooting-star.
    """
    if not candles or len(candles) < 2:
        return None
    want_bull = direction == "BULLISH"
    recent = candles[-5:]
    # Single-candle patterns: hammer / shooting star.
    for c in recent:
        o, h, l, cl, body, rng = _candle_parts(c)
        if rng <= 0:
            continue
        lower_wick = min(o, cl) - l
        upper_wick = h - max(o, cl)
        is_hammer = (body < rng * 0.35 and lower_wick > body * 2
                     and upper_wick < body)
        is_star = (body < rng * 0.35 and upper_wick > body * 2
                   and lower_wick < body)
        if want_bull and is_hammer:
            return "hammer"
        if not want_bull and is_star:
            return "shooting-star"
    # Two-candle patterns: engulfing.
    for i in range(max(0, len(candles) - 5), len(candles) - 1):
        po, _ph, _pl, pcl, _pb, _pr = _candle_parts(candles[i])
        o, _h, _l, cl, _b, _r = _candle_parts(candles[i + 1])
        bull_eng = pcl < po and cl > o and o <= pcl and cl >= po
        bear_eng = pcl > po and cl < o and o >= pcl and cl <= po
        if want_bull and bull_eng:
            return "bullish-engulfing"
        if not want_bull and bear_eng:
            return "bearish-engulfing"
    return None


def news_review(market, symbol: str, direction: str,
                hours: int = 24) -> tuple[bool, str]:
    """Ketat: coin news sentiment must not oppose the direction.

    Rejects LONG when strongly bearish headlines dominate the window,
    SHORT when strongly bullish headlines dominate. Fail-closed when the
    feed is unreachable.
    """
    if not market.news_feed_ok():
        return False, "news feed tak tersedia — holding (fail-closed)"
    try:
        items = market.get_coin_news(symbol, hours=hours)
    except Exception as e:  # noqa: BLE001
        return False, f"news review error ({e}) — holding (fail-closed)"
    if not items:
        return True, "news: tak ada headline coin 24h terakhir"
    bear = [n for n in items if n["sentiment"] <= -2]
    bull = [n for n in items if n["sentiment"] >= 2]
    severe_bear = any(n["sentiment"] <= -4 for n in items)
    severe_bull = any(n["sentiment"] >= 4 for n in items)
    if direction == "BULLISH" and (severe_bear or len(bear) >= 2):
        worst = min(items, key=lambda n: n["sentiment"])
        return False, (f"news bearish menolak LONG: "
                       f"\"{worst['title'][:80]}\"")
    if direction == "BEARISH" and (severe_bull or len(bull) >= 2):
        best = max(items, key=lambda n: n["sentiment"])
        return False, (f"news bullish menolak SHORT: "
                       f"\"{best['title'][:80]}\"")
    return True, f"news: {len(items)} headline, tak ada sentimen berlawanan"


def liquidation_review(market, symbol: str,
                       direction: str) -> tuple[bool, str]:
    """Ketat: reject when a liquidation spike is dominated by the trade's
    own side (weak hands being wiped in our direction). Fail-closed."""
    try:
        adv = market.get_advanced_derivatives(symbol)
    except Exception as e:  # noqa: BLE001
        return False, f"data likuidasi tak tersedia ({e}) — holding"
    spike = adv.get("spike") or 0.0
    long_liq = adv.get("long_liq_usd") or 0.0
    short_liq = adv.get("short_liq_usd") or 0.0
    total = long_liq + short_liq
    if total <= 0 or spike <= 2.0:
        return True, "likuidasi: tak ada spike"
    if direction == "BULLISH" and long_liq / total > 0.6:
        return False, (f"likuidasi: spike {spike:.1f}x didominasi long "
                       f"({long_liq / total:.0%}) — buyer lemah tersapu")
    if direction == "BEARISH" and short_liq / total > 0.6:
        return False, (f"likuidasi: spike {spike:.1f}x didominasi short "
                       f"({short_liq / total:.0%}) — seller lemah tersapu")
    return True, f"likuidasi: spike {spike:.1f}x tak melawan arah"


def whale_review(market, symbol: str, direction: str) -> tuple[bool, str]:
    """Ketat: reject when top traders are extremely positioned against
    the direction (whale-crowd divergence). Fail-closed."""
    try:
        adv = market.get_advanced_derivatives(symbol)
    except Exception as e:  # noqa: BLE001
        return False, f"data whale tak tersedia ({e}) — holding"
    top_long = adv.get("top_long")
    div = adv.get("divergence")
    if not (_finite(top_long) and _finite(div)):
        return False, "posisi top-trader tak tersedia — holding (fail-closed)"
    ext = config.DERIV_TOP_TRADER_EXTREME_LONG
    against = (
        (direction == "BULLISH" and top_long <= ext and div <= -15)
        or (direction == "BEARISH" and top_long >= 100 - ext and div >= 15))
    if against:
        return False, (f"whale melawan {direction.lower()} "
                       f"(top long {top_long:.1f}%, divergen {div:+.1f}pp)")
    return True, f"whale: tak ada divergen ekstrem (top long {top_long:.1f}%)"


def _layered_quality(strength: int, mode: str) -> int:
    """Deterministic quality for biasa/sedang from 15m strength + earned
    layer bonuses. Capped at the 98 GAS quality cap."""
    base = {3: 60, 4: 75, 5: 90}.get(int(strength), 50)
    if mode in ("sedang", "ketat"):
        base += 5  # MTF + BTC alignment earned
    return min(98, base)


# ---------------------------------------------------------- STRICT pipeline
def _signal_mode() -> str:
    m = str(getattr(config, "SIGNAL_MODE", "ketat") or "ketat").lower()
    return m if m in ("biasa", "sedang", "ketat") else "ketat"


def _build_signal(symbol: str, direction: str, a15: dict, live: float,
                  quality: int, mode: str, reviews: list[str],
                  deriv: dict | None = None, snap: dict | None = None,
                  deriv_missing: list | None = None,
                  delta_notes: list | None = None) -> tuple[dict, str]:
    plan = a15["trade_plan"]
    sig_dir = "SHORT" if direction == "BEARISH" else "LONG"
    signal = {
        "symbol": symbol,
        "direction": sig_dir,
        "interval": "15m",
        "entry": plan["entry"],
        "entry_low": plan["entry_low"],
        "entry_high": plan["entry_high"],
        "stop_loss": plan["stop_loss"],
        "tp1": plan["tp1"], "tp2": plan["tp2"], "tp3": plan["tp3"],
        "rr1": plan["rr1"],
        "quality": quality,
        "mode": mode,
        "reviews": reviews,
        "strength": a15["strength"],
        "rsi": round(a15["rsi"], 1),
        "volume_ratio": round(a15["volume_ratio"], 2),
        "regime": a15["regime"],
        "deriv_score": (deriv["value"] if deriv else 50),
        "deriv_bias": (deriv["bias"] if deriv else "neutral"),
        "deriv_notes": (deriv["notes"] if deriv else []),
        "deriv_missing": deriv_missing or [],
        "deriv_provider": (snap.get("provider") if snap else "n/a"),
        "reasons": a15["reasons"],
        "price": a15["price"],
        "live_price": live,
        "delta_notes": delta_notes or [],
        "timestamp": int(time.time() * 1000),
    }
    note = (f"{mode.upper()} PASS quality {quality}/100 "
            f"(strength {a15['strength']}/5; "
            f"{'; '.join(reviews)})")
    return signal, note


def _freshness_check(symbol: str, market, direction: str,
                     plan: dict) -> tuple[float | None, str | None]:
    """Shared freshness gate (all modes). Returns (live, None) or
    (None, reason)."""
    try:
        live = market.get_live_price(symbol)
    except Exception:  # noqa: BLE001 - ticker failure = unavailable
        live = None
    if live is None or not _finite(live) or live <= 0:
        return None, "freshness: live price unavailable — holding (fail-closed)"
    if direction == "BEARISH":
        drift_pct = ((plan["entry_low"] - live) / plan["entry_low"] * 100
                     if plan["entry_low"] > 0 else 0.0)
        if drift_pct > config.FRESHNESS_MAX_DRIFT_PCT:
            return None, (f"freshness: live price -{drift_pct:.2f}% below entry "
                          f"zone — stale entry for short")
    else:
        drift_pct = (live - plan["entry_high"]) / plan["entry_high"] * 100
        if drift_pct > config.FRESHNESS_MAX_DRIFT_PCT:
            return None, (f"freshness: live price +{drift_pct:.2f}% above entry "
                          f"zone — stale entry")
    return live, None


def evaluate_symbol(symbol: str, market) -> tuple[dict | None, str]:
    """Mode-dispatched signal pipeline (user spec 2026-10-15).

    - biasa : 15m indicators (MACD, MA/EMA, RSI) + MTF alignment
      4h/1h/30m/15m. Bidirectional.
    - sedang: biasa + BTC 15m direction must match.
    - ketat : sedang + pattern review + news review + liquidation review
      + whale review + derivatives coverage + deterministic vetoes +
      quality gate (>=70).

    Returns (signal_dict, note) or (None, reason).
    `market` is a data.MarketData instance (duck-typed for tests).
    """
    symbol = symbol.upper()
    mode = _signal_mode()
    degraded = bool(getattr(market, "degraded_derivatives", False))

    # ---- Stage 0: liquidity filter — FAIL-CLOSED (all modes).
    vol = market.get_24h_quote_volume_usd(symbol)
    if vol is None or not _finite(vol):
        return None, "24h volume unavailable — holding (fail-closed)"
    if vol < config.MIN_24H_VOLUME_USD:
        return None, f"24h volume ${vol:,.0f} below ${config.MIN_24H_VOLUME_USD:,}"

    # ---- Stage 1: 15m indicator setup — MACD, MA/EMA, RSI (all modes).
    # BULLISH WATCH -> LONG; BEARISH WATCH -> SHORT. In bullish-only modes
    # (ketat/sedang) non-bullish setups are rejected here.
    try:
        k15 = market.get_klines(symbol, "15m")
        a15 = analyze_candles(symbol, "15m", k15)
    except Exception as e:  # noqa: BLE001
        return None, f"15m data error: {e}"
    sig15 = str(a15["signal"])
    is_bull = sig15.startswith("BULL")
    is_bear = sig15.startswith("BEAR")
    if config.BULLISH_ONLY and not is_bull:
        return None, f"bullish-only: 15m signal {sig15}"
    if not is_bull and not is_bear:
        return None, f"15m: no directional setup ({sig15})"
    direction = "BULLISH" if is_bull else "BEARISH"
    if not a15["trade_plan"]:
        return None, "15m: no trade plan"
    if a15["strength"] < config.MIN_STRENGTH:
        return None, f"15m strength {a15['strength']}/5 below {config.MIN_STRENGTH}"

    # ---- Stage 1b: freshness (all modes).
    live, frej = _freshness_check(symbol, market, direction,
                                  a15["trade_plan"])
    if frej:
        return None, frej

    reviews = [f"15m {sig15.lower()} (strength {a15['strength']}/5)"]

    # ---- Stage 2: MTF alignment — 4h, 1h, 30m, 15m same direction
    # (SEMUA mode — aturan user 2026-10-15: setiap signal cek MTF
    # 4h-1h-30m-15m). Fail-closed.
    try:
        a30 = analyze_candles(symbol, "30m", market.get_klines(symbol, "30m"))
    except Exception as e:  # noqa: BLE001
        return None, f"30m data error: {e} — holding (fail-closed)"
    ok, note = mtf_alignment_check(symbol, market, direction, a15, a30)
    if not ok:
        return None, note
    reviews.append(note)

    # ================= biasa: 15m + MTF 4h/1h/30m/15m =================
    if mode == "biasa":
        quality = _layered_quality(a15["strength"], mode)
        if quality < config.MIN_QUALITY_SCORE:
            return None, (f"quality {quality}/100 below biasa minimum "
                           f"{config.MIN_QUALITY_SCORE}")
        return _build_signal(symbol, direction, a15, live, quality, mode,
                             reviews)

    # ---- Stage 3: BTC direction must match (sedang, ketat).
    if config.BTC_ALIGN_REQUIRED:
        ok, note = btc_direction_check(market, direction)
        if not ok:
            return None, note
        reviews.append(note)

    # ================= sedang: MTF + BTC =================
    if mode == "sedang":
        quality = _layered_quality(a15["strength"], mode)
        if quality < config.MIN_QUALITY_SCORE:
            return None, (f"quality {quality}/100 below sedang minimum "
                           f"{config.MIN_QUALITY_SCORE}")
        return _build_signal(symbol, direction, a15, live, quality, mode,
                             reviews)

    # ================= ketat: full review stack =================
    # Stage 4: pattern review.
    if config.PATTERN_REQUIRED:
        pat = detect_pattern(k15, direction)
        if not pat:
            return None, ("pattern: tak ada pola candlestick 15m yang "
                          f"mendukung {direction.lower()} (5 candle terakhir)")
        reviews.append(f"pattern {pat}")

    # Stage 5: news review.
    if config.NEWS_CHECK:
        ok, note = news_review(market, symbol, direction)
        if not ok:
            return None, note
        reviews.append(note)

    # Stage 6: liquidation review.
    if config.LIQ_REVIEW:
        ok, note = liquidation_review(market, symbol, direction)
        if not ok:
            return None, note
        reviews.append(note)

    # Stage 7: whale review.
    if config.WHALE_REVIEW:
        ok, note = whale_review(market, symbol, direction)
        if not ok:
            return None, note
        reviews.append(note)

    # Stage 8: derivatives coverage — FAIL-CLOSED. Required: funding +
    # open interest.
    snap = market.get_derivatives_snapshot(symbol)
    missing = [name for name, key in config.DERIV_COMPONENT_KEYS.items()
               if not _finite(snap.get(key))]
    required_missing = [m for m in missing
                        if m in config.DERIV_REQUIRED_COMPONENTS]
    if required_missing and not degraded:
        return None, (f"derivatives coverage insufficient "
                       f"({', '.join(required_missing)} unavailable) — holding")
    deriv_missing = [m for m in missing
                     if m in config.DERIV_OPTIONAL_COMPONENTS]

    # Stage 9: deterministic hard vetoes. Any fired veto — or any veto
    # whose required inputs are unavailable — rejects the signal.
    try:
        book = market.get_order_book(symbol)
    except Exception:  # noqa: BLE001
        book = None
    blocks = run_vetoes(symbol, direction, market, snap, book=book,
                        skip_deriv_vetoes=degraded,
                        disabled=getattr(config, "DISABLED_VETOES", ()))
    if blocks:
        why = "; ".join(f"[{code}/{kind}] {reason}"
                        for code, kind, reason in blocks)
        return None, f"veto: {why}"
    reviews.append("vetoes bersih")

    # Stage 10: quality gate — warn-tier deltas score 0-100.
    components: dict[str, int] = {}
    delta_notes: list[str] = []

    def _add(name: str, res: tuple[int, str]):
        d, note = res
        components[name] = int(d)
        delta_notes.append(f"{name}: {note}")

    _add("mtf_30m", mtf_30m_delta(mtf_confirm(a30, direction=direction),
                                 direction))
    try:
        a4h = analyze_candles(symbol, "4h", market.get_klines(symbol, "4h"))
    except Exception as e:  # noqa: BLE001
        components["mtf_4h"] = 0
        delta_notes.append(f"mtf_4h: data tidak tersedia ({e})")
    else:
        _add("mtf_4h", mtf_4h_delta(a4h, direction))
    try:
        h1_closes = [float(c["close"]) for c in market.get_klines(symbol, "1h")]
    except Exception as e:  # noqa: BLE001
        components["h1"] = 0
        delta_notes.append(f"h1: data tidak tersedia ({e})")
    else:
        _add("h1", h1_trend_delta(h1_closes, direction))

    if degraded:
        for _dname in ("lev_warn", "funding", "squeeze_warn", "whale_warn",
                       "smart_money", "liq_event", "liq_wall_warn"):
            components[_dname] = 0
            delta_notes.append(f"{_dname}: degraded — no historical "
                               f"derivatives feed (backtest limitation)")
    else:
        adv = market.get_advanced_derivatives(symbol)
        lev = leverage_risk_index(adv["retail_long"], adv["funding"],
                                  adv["oi_usd"], adv["volume_24h"],
                                  adv.get("spike") or 0.0)
        _add("lev_warn", leverage_warn_delta(lev))
        _add("funding", funding_delta(snap.get("aggregate_funding"), direction))
        sq = squeeze_risk_index(adv["retail_long"], adv["funding"],
                                adv["oi_change"], adv.get("long_liq_usd") or 0.0,
                                adv.get("short_liq_usd") or 0.0,
                                adv.get("spike") or 0.0, direction)
        _add("squeeze_warn", squeeze_warn_delta(sq))
        _add("whale_warn", whale_warn_delta(adv.get("top_long"),
                                            adv.get("divergence"), direction))
        smart = smart_money_score(adv)
        _add("smart_money", smart_money_delta(smart, direction))
        regime = market_regime(adv)
        _add("liq_event", liq_event_delta(regime))
        _add("liq_wall_warn", liq_wall_warn_delta(book, direction))

    deriv = derivatives_score(snap)
    q = quality_score(a15["strength"], components)
    if not q["pass"]:
        return None, (f"quality {q['quality']}/100 below ketat minimum "
                       f"{config.MIN_QUALITY_SCORE} "
                       f"(delta {q['delta']:+d}: "
                       + ", ".join(f"{k}{v:+d}" for k, v in components.items()
                                   if v) + ")")
    reviews.append(f"quality {q['quality']}/100")

    return _build_signal(symbol, direction, a15, live, q["quality"], mode,
                         reviews, deriv=deriv, snap=snap,
                         deriv_missing=deriv_missing, delta_notes=delta_notes)
