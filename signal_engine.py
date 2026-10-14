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


def mtf_confirm_strict(c30: dict) -> dict:
    """GAS: mtfConfirmPass_ STRICT branch (L2091-2101)."""
    if not c30:
        return {"pass": False, "reason": "no 30m data"}
    sig = str(c30.get("signal") or "")
    if sig.startswith("BULL") and c30.get("trade_plan"):
        return {"pass": True, "boost": config.MTF_CONFIRM_BOOST}
    return {"pass": False, "reason": f"30m {sig} (STRICT requires BULLISH WATCH)"}


def mtf_confirm_lean(c30: dict) -> dict:
    """GAS: mtfConfirmPass_ LEAN branch — the GAS *default* behavior (L2091-2110).

    Bearish 30m fails; full BULLISH WATCH passes with boost 8; a NEUTRAL 30m
    with bull_score >= bear_score passes as a 'lean' with boost 3; otherwise
    passes weak with a -5 penalty. Kept for MTF_30M_MODE='LEAN' parity with
    GAS; the STRICT default below is deliberately stricter (audit D5).
    """
    if not c30:
        return {"pass": False, "reason": "no 30m data"}
    sig = str(c30.get("signal") or "")
    is_bull = sig.startswith("BULL")
    is_bear = sig.startswith("BEAR")
    if is_bear:
        return {"pass": False, "reason": f"30m bearish ({sig})"}
    if is_bull and c30.get("trade_plan"):
        return {"pass": True, "boost": config.MTF_CONFIRM_BOOST}
    bull_score = float(c30.get("bull_score") or 0)
    bear_score = float(c30.get("bear_score") or 0)
    if bull_score >= bear_score:
        return {"pass": True, "boost": config.MTF_LEAN_BOOST, "lean": True}
    return {"pass": True, "boost": 0, "penalty": -5, "weak": True}


def mtf_confirm(c30: dict, mode: str | None = None) -> dict:
    """30m confirmation honoring config.MTF_30M_MODE (default 'STRICT')."""
    mode = str(mode or config.MTF_30M_MODE).upper()
    if mode == "LEAN":
        return mtf_confirm_lean(c30)
    return mtf_confirm_strict(c30)


def mtf_cascade_check(klines_4h: list, klines_1d: list, a30: dict) -> dict:
    """GAS: mtfCascadeCheck_ (L3156). 4h + 1d bullish, 30m per MTF_30M_MODE."""
    try:
        t4 = mtf_trend_direction([float(c["close"]) for c in klines_4h])
        if t4 != "BULLISH":
            return {"pass": False, "stage": "4h", "reason": f"4h trend {t4.lower()}"}
        t1 = mtf_trend_direction([float(c["close"]) for c in klines_1d])
        if t1 != "BULLISH":
            return {"pass": False, "stage": "1d", "reason": f"1d trend {t1.lower()}"}
        chk = mtf_confirm(a30)
        if not chk["pass"]:
            return {"pass": False, "stage": "30m", "reason": "30m: " + chk["reason"]}
        # The 30m check result is threaded through so the quality gate can
        # apply its delta (deepCheckMtf_ L3292-3298). In GAS STRICT the cascade
        # boost reaches scoreDelta only via this leg — there is no separate
        # cascade +8 in the delta sum.
        return {"pass": True, "t4h": t4, "t1d": t1, "chk": chk,
                "boost": config.MTF_CASCADE_BOOST}
    except Exception as e:  # noqa: BLE001
        return {"pass": False, "stage": "error", "reason": str(e)}


# ---------------------------------------------------------- STRICT pipeline
def evaluate_symbol(symbol: str, market) -> tuple[dict | None, str]:
    """Run the full STRICT pipeline. Returns (signal_dict, note) or (None, reason).

    `market` is a data.MarketData instance (duck-typed for tests).
    """
    symbol = symbol.upper()
    # Degraded-derivatives mode (ROADMAP ITEM 4, backtester only): the market
    # object declares it has no historical derivatives feed. Derivative-
    # dependent gates/deltas are then SKIPPED — never fabricated — and every
    # skip is recorded on the signal dict and in the backtest report, so
    # backtest pass rates are honest UPPER BOUNDS. Production markets never
    # set this flag (default False): missing derivatives data fails closed.
    degraded = bool(getattr(market, "degraded_derivatives", False))
    # 1. Liquidity filter — FAIL-CLOSED (audit D6/M5). GAS applies the
    # MIN_24H_VOLUME_USD floor at universe-selection time and lets favorites
    # bypass it; for trading, unavailable or thin volume must block the signal.
    vol = market.get_24h_quote_volume_usd(symbol)
    if vol is None or not _finite(vol):
        return None, "24h volume unavailable — holding (fail-closed)"
    if vol < config.MIN_24H_VOLUME_USD:
        return None, f"24h volume ${vol:,.0f} below ${config.MIN_24H_VOLUME_USD:,}"

    # 2. 15m setup: BULLISH WATCH + plan + strength >= 3
    try:
        a15 = analyze_candles(symbol, "15m", market.get_klines(symbol, "15m"))
    except Exception as e:  # noqa: BLE001
        return None, f"15m data error: {e}"
    if config.BULLISH_ONLY and not str(a15["signal"]).startswith("BULL"):
        return None, f"bullish-only: 15m signal {a15['signal']}"
    if not a15["trade_plan"]:
        return None, "15m: no trade plan"
    if a15["strength"] < config.MIN_STRENGTH:
        return None, f"15m strength {a15['strength']}/5 below {config.MIN_STRENGTH}"

    # 3. MTF cascade: 4h + 1d trend, 30m per MTF_30M_MODE (default STRICT).
    # Any fetch/analysis exception rejects the symbol — never skip-and-pass.
    try:
        k4h = market.get_klines(symbol, "4h")
        k1d = market.get_klines(symbol, "1d")
        a30 = analyze_candles(symbol, "30m", market.get_klines(symbol, "30m"))
    except Exception as e:  # noqa: BLE001
        return None, f"MTF data error: {e}"
    casc = mtf_cascade_check(k4h, k1d, a30)
    if not casc["pass"]:
        return None, f"STRICT cascade: {casc['reason']}"

    # 4. Derivatives coverage — FAIL-CLOSED (audit D4/H6). A snapshot with no
    # positioning visibility must HOLD the signal, never auto-neutralize to
    # score 50 and pass. Required: funding + open interest. Optional
    # (long/short, taker) gaps are recorded on the signal dict but do not block.
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

    # 5. Deterministic hard vetoes (ROADMAP ITEM 2). Same check order as GAS
    # prePublishDeepCheck_ (vetoes before the quality gate): any fired veto —
    # or any veto whose required inputs are unavailable — rejects the signal.
    # A high quality score can never compensate for a veto.
    # The order book is fetched once and shared with the liquidity-wall
    # *warn* tier in the quality gate (item 3); a fetch failure holds here.
    try:
        book = market.get_order_book(symbol)
    except Exception:  # noqa: BLE001 - run_vetoes retries and holds on failure
        book = None
    blocks = run_vetoes(symbol, "BULLISH", market, snap, book=book,
                        skip_deriv_vetoes=degraded)
    if blocks:
        why = "; ".join(f"[{code}/{kind}] {reason}"
                        for code, kind, reason in blocks)
        return None, f"veto: {why}"

    # 6. Quality gate — ROADMAP ITEM 3. Every scoreDelta contributor is an
    # explicit, named component (quality_deltas.py), ported from the GAS deep
    # check. Veto tiers already rejected above; the warn tiers subtract here.
    components: dict[str, int] = {}
    delta_notes: list[str] = []

    def _add(name: str, res: tuple[int, str]):
        d, note = res
        components[name] = int(d)
        delta_notes.append(f"{name}: {note}")

    _add("mtf_30m", mtf_30m_delta(casc["chk"]))
    try:
        a4h = analyze_candles(symbol, "4h", k4h)
    except Exception as e:  # noqa: BLE001 - soft scoring (GAS L3304)
        components["mtf_4h"] = 0
        delta_notes.append(f"mtf_4h: data tidak tersedia ({e})")
    else:
        _add("mtf_4h", mtf_4h_delta(a4h))
    try:
        h1_closes = [float(c["close"]) for c in market.get_klines(symbol, "1h")]
    except Exception as e:  # noqa: BLE001 - soft scoring (GAS L3228)
        components["h1"] = 0
        delta_notes.append(f"h1: data tidak tersedia ({e})")
    else:
        _add("h1", h1_trend_delta(h1_closes))
    try:
        btc_chg = btc_1h_change_pct(market)  # cached by the V15 veto check
    except Exception as e:  # noqa: BLE001 - veto already held on failure
        components["btc_warn"] = 0
        delta_notes.append(f"btc_warn: data tidak tersedia ({e})")
    else:
        _add("btc_warn", btc_breadth_warn_delta(btc_chg))

    # Derivatives warn tiers. Veto tiers (V9-V12) already rejected above, so
    # the required inputs are present here; optional inputs (smart money,
    # regime) degrade to delta 0 with a note when unavailable.
    # (Cached by run_vetoes above — no extra API call.)
    #
    # DEGRADED MODE (backtester): there is no historical derivatives feed, so
    # every derivatives-dependent delta is 0 with an explicit note — never a
    # fabricated value. Real warns can only push scores DOWN, therefore
    # backtest quality scores (and pass rates) are UPPER BOUNDS. The MTF / h1 /
    # BTC deltas above ran on real historical klines and are unaffected.
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
        _add("funding", funding_delta(snap.get("aggregate_funding"), "BULLISH"))
        sq = squeeze_risk_index(adv["retail_long"], adv["funding"],
                                adv["oi_change"], adv.get("long_liq_usd") or 0.0,
                                adv.get("short_liq_usd") or 0.0,
                                adv.get("spike") or 0.0, "BULLISH")
        _add("squeeze_warn", squeeze_warn_delta(sq))
        _add("whale_warn", whale_warn_delta(adv.get("top_long"),
                                            adv.get("divergence"), "BULLISH"))
        smart = smart_money_score(adv)
        _add("smart_money", smart_money_delta(smart, "BULLISH"))
        regime = market_regime(adv)
        _add("liq_event", liq_event_delta(regime))
        _add("liq_wall_warn", liq_wall_warn_delta(book, "BULLISH"))

    deriv = derivatives_score(snap)  # informational (scan-time heuristic)
    q = quality_score(a15["strength"], components)
    if not q["pass"]:
        return None, (f"quality {q['quality']}/100 below STRICT minimum "
                       f"{config.MIN_QUALITY_SCORE} "
                       f"(delta {q['delta']:+d}: "
                       + ", ".join(f"{k}{v:+d}" for k, v in components.items()
                                   if v) + ")")

    # 7. Freshness (GAS: freshnessCheck_, L5075): LIVE ticker price, one-sided —
    # reject only when price ran >0.4% ABOVE the entry zone (stale entry for
    # longs). Price below/at the zone is fine (pullback entry). FAIL-CLOSED:
    # an unavailable live price holds the signal (audit C2/C3/D1 — GAS returned
    # ok:true on price_unavailable; we deliberately do not reproduce that).
    plan = a15["trade_plan"]
    try:
        live = market.get_live_price(symbol)
    except Exception:  # noqa: BLE001 - ticker failure = unavailable
        live = None
    if live is None or not _finite(live) or live <= 0:
        return None, "freshness: live price unavailable — holding (fail-closed)"
    drift_pct = (live - plan["entry_high"]) / plan["entry_high"] * 100
    if drift_pct > config.FRESHNESS_MAX_DRIFT_PCT:
        return None, (f"freshness: live price +{drift_pct:.2f}% above entry zone "
                       f"(limit +{config.FRESHNESS_MAX_DRIFT_PCT}%) — stale entry")

    signal = {
        "symbol": symbol,
        "direction": "LONG",  # STRICT is bullish-only by construction
        "interval": "15m",
        "entry": plan["entry"],
        "entry_low": plan["entry_low"],
        "entry_high": plan["entry_high"],
        "stop_loss": plan["stop_loss"],
        "tp1": plan["tp1"], "tp2": plan["tp2"], "tp3": plan["tp3"],
        "rr1": plan["rr1"],
        "quality": q["quality"],
        "score_delta": q["delta"],
        "delta_breakdown": q["components"],
        "delta_notes": delta_notes,
        "strength": a15["strength"],
        "rsi": round(a15["rsi"], 1),
        "volume_ratio": round(a15["volume_ratio"], 2),
        "regime": a15["regime"],
        "deriv_score": deriv["value"],
        "deriv_bias": deriv["bias"],
        "deriv_notes": deriv["notes"],
        "deriv_missing": deriv_missing,
        "deriv_provider": snap.get("provider"),
        "deriv_degraded": degraded,  # True only in backtest degraded mode
        "reasons": a15["reasons"],
        "price": a15["price"],
        "live_price": live,
        "timestamp": int(time.time() * 1000),
    }
    note = (f"STRICT PASS quality {q['quality']}/100 "
            f"(strength {a15['strength']}/5, deriv {deriv['value']})")
    return signal, note
