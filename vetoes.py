# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Deterministic hard vetoes — ROADMAP ITEM 2.

Ports the GAS deep-check hard vetoes as non-compensable hard gates: any
single fired veto — or any veto whose required inputs are unavailable —
REJECTS the signal. Scores can never compensate for a veto.

GAS source: workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs

Ported (GAS line refs):
  leverage risk index   <- derivGateAdvanced_      (L1016-1022); veto L3338
  squeeze risk index    <- derivGateAdvanced_      (L1024-1034); veto L3360
  funding extreme       <- deepCheckDerivatives_   (L3346-3354); CFG L72
  whale/top-trader veto <- deepCheckDerivatives_   (L3366-3379); CFG L90
  liquidity wall veto   <- deepCheckLiquidity_      (L3395-3425); CFG L75-76
  BTC circuit breaker   <- deepCheckBtcBreadth_     (L3634-3662); CFG L106

Deliberately NOT ported (documented in README):
  - warn-tier penalties (funding +5, whale-divergence -12, liq-warn -8,
    BTC -2% -15): score deltas belong to roadmap item 3 (quality recalibration).
  - AI news-sentiment veto and chart-pattern veto (audit: keep out until
    calibrated).
  - the GAS liq-warn NaN bug (audit M1): r.delta was never initialized and
    never added to scoreDelta — the warn tier is recorded as a note only.
"""

from __future__ import annotations

import math
import time

import config


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _finite(v) -> bool:
    return v is not None and math.isfinite(float(v))


# ------------------------------------------------------------- pure indices
def leverage_risk_index(retail_long_pct: float, funding: float,
                        oi_usd: float, volume_24h_usd: float,
                        spike_ratio: float = 0.0) -> int:
    """GAS: derivGateAdvanced_ (L1016-1022). Crowded-trade / flush-risk index.

    crowding     = |retailLong - 50| * 2
    fundingCrowd = min(100, |funding| * 100000)
    oiVolRisk    = min(100, oiUsd / volume24h * 100)
    spikeRisk    = min(100, max(0, spike - 1) * 25)
    leverageRisk = round(clamp(crowding*.28 + fundingCrowd*.22
                               + oiVolRisk*.25 + spikeRisk*.25, 0, 100))
    """
    crowding = abs(float(retail_long_pct) - 50) * 2
    funding_crowd = min(100.0, abs(float(funding)) * 100000)
    oi_vol_risk = (min(100.0, float(oi_usd) / float(volume_24h_usd) * 100)
                   if volume_24h_usd and volume_24h_usd > 0 else 0.0)
    spike_risk = min(100.0, max(0.0, float(spike_ratio) - 1) * 25)
    return round(_clamp(crowding * 0.28 + funding_crowd * 0.22
                       + oi_vol_risk * 0.25 + spike_risk * 0.25, 0, 100))


def squeeze_risk_index(retail_long_pct: float, funding: float,
                       oi_change_pct: float, long_liq_usd: float,
                       short_liq_usd: float, spike_ratio: float,
                       direction: str = "BULLISH") -> int:
    """GAS: derivGateAdvanced_ (L1024-1034). Returns the squeeze risk *for*
    the signal direction (longSqueeze for BULLISH, shortSqueeze for BEARISH).

    longSqueeze = round(clamp(
        max(0, retailLong-50)*1.25 + max(0, funding)*90000
        + max(0, oiChange)*1.2
        + (longLiq > shortLiq ? min(25, spike*6) : 0), 0, 100))
    (shortSqueeze mirrors with the signs flipped.)
    """
    rl, f, oic = (float(retail_long_pct), float(funding),
                  float(oi_change_pct))
    liq_term = min(25.0, float(spike_ratio) * 6)
    if direction == "BULLISH":
        base = (max(0.0, rl - 50) * 1.25 + max(0.0, f) * 90000
                + max(0.0, oic) * 1.2)
        if not (long_liq_usd or 0) > (short_liq_usd or 0):
            liq_term = 0.0
    else:
        base = (max(0.0, 50 - rl) * 1.25 + max(0.0, -f) * 90000
                + max(0.0, oic) * 1.2)
        if not (short_liq_usd or 0) > (long_liq_usd or 0):
            liq_term = 0.0
    return round(_clamp(base + liq_term, 0, 100))


# ------------------------------------------------------- market-wide breaker
_btc_cache: dict = {}


def reset_caches() -> None:
    """Clear module caches (tests)."""
    _btc_cache.clear()


def btc_1h_change_pct(market) -> float:
    """% change between the last two CLOSED BTC 1h candles.

    GAS: deepCheckBtcBreadth_ (L3634) — "compare only the two latest closed
    1h candles so the percentage really represents about one hour".
    Cached config.BTC_BREAKER_TTL_SEC seconds. Raises on data failure —
    the caller must fail closed, never skip the breaker.
    """
    now = time.time()
    hit = _btc_cache.get("v1")
    if hit and now - hit[0] < config.BTC_BREAKER_TTL_SEC:
        return hit[1]
    candles = market.get_klines("BTCUSDT", "1h", 2)
    if len(candles) < 2:
        raise ValueError("BTC 1h: insufficient candles")
    first, last = float(candles[-2]["close"]), float(candles[-1]["close"])
    if not (first > 0 and last > 0):
        raise ValueError("BTC 1h: invalid closes")
    chg = (last - first) / first * 100
    _btc_cache["v1"] = (now, chg)
    return chg


# ------------------------------------------------------------------ vetoes
def run_vetoes(symbol: str, direction: str, market, snap: dict,
               book: dict | None = None,
               skip_deriv_vetoes: bool = False,
               disabled: object = None,
               entry_px: float | None = None) -> list:
    """Evaluate every deterministic hard veto.

    Returns a list of blocks; each block is a (code, kind, reason) tuple
    with kind in {"veto", "hold"}:
      veto — the veto condition fired; the signal is REJECTED.
      hold — required veto inputs were unavailable; the signal is HELD
             (fail-closed: never passed on missing data).

    An empty list means no veto fired and every veto was evaluable.

    `book` is an optional pre-fetched order book (from market.get_order_book);
    when None the book is fetched here. Callers that also need the book for
    the liquidity-wall *warn* tier (quality_deltas.liq_wall_warn_delta) should
    fetch it once and pass it in.

    `skip_deriv_vetoes` (ROADMAP ITEM 4, backtester only): the historical
    feed has no derivatives/order-book data, so V9/V10/V11/V12/V13 cannot be
    evaluated honestly and are SKIPPED — never fabricated, never fail-closed
    into a blanket hold (that would make backtesting impossible). The V15 BTC
    circuit breaker still runs on real historical BTC 1h klines. Every skip
    is documented in the backtest report: backtest results are UPPER BOUNDS
    on what the full production pipeline would admit.

    `disabled`: veto codes the operator turned off via the dashboard
    (config.DISABLED_VETOES). A disabled veto is SKIPPED entirely — not even
    its fail-closed hold applies. This is an explicit operator choice;
    by default every veto is enabled.
    """
    symbol = symbol.upper()
    direction = direction.upper()
    blocks = []
    off = set(disabled or ())

    def _block(code: str, kind: str, reason: str) -> None:
        if code not in off:
            blocks.append((code, kind, reason))

    # V15 — BTC circuit breaker (GAS: deepCheckBtcBreadth_ L3634).
    # Guards the traded direction: a BTC dump kills new LONGs, a vertical
    # BTC pump kills new SHORTs (shorting alts into a BTC squeeze is the
    # mirrored hostile regime). Runs on real historical klines in backtest —
    # never skipped.
    try:
        chg = btc_1h_change_pct(market)
    except Exception as e:  # noqa: BLE001 - unevaluable breaker -> hold
        _block("btc_breaker", "hold",
                       f"BTC circuit breaker unevaluable ({e}) — holding (fail-closed)")
    else:
        if direction == "BULLISH" and chg <= -config.BTC_DUMP_VETO_PCT:
            _block("btc_breaker", "veto",
                           f"BTC dump {chg:.2f}%/1h <= -{config.BTC_DUMP_VETO_PCT:g}% "
                           f"— circuit breaker, LONG held")
        elif direction == "BEARISH" and chg >= config.BTC_DUMP_VETO_PCT:
            _block("btc_breaker", "veto",
                           f"BTC pump +{chg:.2f}%/1h >= +{config.BTC_DUMP_VETO_PCT:g}% "
                           f"— circuit breaker, SHORT held")

    if skip_deriv_vetoes:
        return blocks  # V9-V12, V13 skipped: no historical feed (documented)

    # Advanced derivatives (Gate) feed V9/V11/V12.
    try:
        adv = market.get_advanced_derivatives(symbol)
    except Exception as e:  # noqa: BLE001 - unevaluable -> hold
        adv = None
        # The adv feed serves leverage_risk/squeeze/whale: if the operator
        # disabled all three, its unavailability holds nothing.
        if not {"leverage_risk", "squeeze", "whale"} <= off:
            _block("adv_deriv", "hold",
                   f"advanced derivatives unavailable ({e}) — holding (fail-closed)")

    if adv is not None:
        # V9 — leverage-risk veto (GAS L3338; index L1016-1022).
        lev_req = (adv.get("retail_long"), adv.get("funding"),
                   adv.get("oi_usd"), adv.get("volume_24h"))
        if not all(_finite(v) for v in lev_req):
            _block("leverage_risk", "hold",
                           "leverage-risk inputs incomplete — holding (fail-closed)")
        else:
            lev = leverage_risk_index(*lev_req, adv.get("spike") or 0.0)
            if lev >= config.DERIV_LEVERAGE_RISK_VETO:
                _block("leverage_risk", "veto",
                               f"leverage risk index {lev}/100 >= "
                               f"{config.DERIV_LEVERAGE_RISK_VETO} — crowded, flush risk")

        # V10 — funding extreme against the signal (GAS L3346-3354).
        # Positive funding = longs pay shorts = crowded long (GAS L3347).
        funding = snap.get("aggregate_funding")
        if not _finite(funding):
            _block("funding", "hold",
                           "funding unavailable — holding (fail-closed)")
        else:
            funding = float(funding)
            crowded_long = funding > 0
            against = ((direction == "BULLISH" and crowded_long)
                       or (direction == "BEARISH" and not crowded_long))
            if abs(funding) >= config.DERIV_FUNDING_EXTREME and against:
                _block("funding", "veto",
                               f"funding {funding:+.4%} extreme and against {direction} "
                               f"(crowded {'long' if crowded_long else 'short'}, squeeze risk)")

        # V11 — squeeze risk in the signal direction (GAS L3360).
        sq_req = (adv.get("retail_long"), adv.get("funding"), adv.get("oi_change"))
        if not all(_finite(v) for v in sq_req):
            _block("squeeze", "hold",
                           "squeeze-risk inputs incomplete — holding (fail-closed)")
        else:
            sq = squeeze_risk_index(
                sq_req[0], sq_req[1], sq_req[2],
                adv.get("long_liq_usd") or 0.0, adv.get("short_liq_usd") or 0.0,
                adv.get("spike") or 0.0, direction)
            if sq >= config.DERIV_SQUEEZE_RISK_VETO:
                side = "long" if direction == "BULLISH" else "short"
                _block("squeeze", "veto",
                               f"{side} squeeze risk {sq}/100 >= "
                               f"{config.DERIV_SQUEEZE_RISK_VETO} — stop-hunt risk")

        # V12 — top-trader / whale extreme divergence (GAS L3366-3379).
        top_long, div = adv.get("top_long"), adv.get("divergence")
        if not (_finite(top_long) and _finite(div)):
            _block("whale", "hold",
                           "top-trader positioning unavailable — holding (fail-closed)")
        else:
            ext = config.DERIV_TOP_TRADER_EXTREME_LONG
            whale_against = (
                (direction == "BULLISH" and top_long <= ext and div <= -15)
                or (direction == "BEARISH" and top_long >= 100 - ext and div >= 15))
            if whale_against:
                _block("whale", "veto",
                               f"top traders extremely against {direction} "
                               f"(top long {top_long:.1f}%, whale-crowd divergence "
                               f"{div:+.1f}pp)")

    # V13 — liquidity wall blocking the move (GAS: deepCheckLiquidity_ L3395).
    if book is None:
        try:
            book = market.get_order_book(symbol)
        except Exception as e:  # noqa: BLE001 - unevaluable -> hold
            book = None
            _block("liq_wall", "hold",
                           f"order book unavailable ({e}) — holding (fail-closed)")
    wall_reason = _blocking_wall(book, direction) if book is not None else None
    if wall_reason:
        _block("liq_wall", "veto", wall_reason)

    # V16 — S/R location (user 2026-10-15): never SHORT into 4h support,
    # never LONG into 4h resistance. Support/resistance are the 4h swing
    # extremes over SR_ZONE_LOOKBACK_4H candles (~2-3 months); the zone is
    # SR_ZONE_ATR_MULT x ATR(4h) inside the level (volatility-adaptive).
    # Only entries sitting *inside the zone edge* are vetoed: a breakdown
    # already below support (or breakout above resistance) is allowed.
    # Without an entry price the veto is not applicable (skipped); a failed
    # 4h history fetch holds the signal (fail-closed).
    if entry_px is not None and entry_px > 0:
        try:
            from indicators import atr_wilder
            k4 = market.get_klines(symbol, "4h",
                                   limit=config.SR_ZONE_LOOKBACK_4H)
            lows = [float(c["low"]) for c in k4 if c.get("low") is not None]
            highs = [float(c["high"]) for c in k4 if c.get("high") is not None]
            if len(lows) < 50:
                raise ValueError("insufficient 4h history for S/R zone")
            support, resistance = min(lows), max(highs)
            zone = atr_wilder(k4, 14) * config.SR_ZONE_ATR_MULT
            if zone <= 0:
                raise ValueError("ATR(4h) unavailable")
            if (direction == "BEARISH"
                    and support < entry_px <= support + zone):
                _block("sr_zone", "veto",
                       f"SHORT into 4h support {support:.6g} (entry "
                       f"{entry_px:.6g} within {zone:.6g} = "
                       f"{config.SR_ZONE_ATR_MULT:g}x ATR) — bad location")
            elif (direction == "BULLISH"
                    and resistance - zone <= entry_px < resistance):
                _block("sr_zone", "veto",
                       f"LONG into 4h resistance {resistance:.6g} (entry "
                       f"{entry_px:.6g} within {zone:.6g} = "
                       f"{config.SR_ZONE_ATR_MULT:g}x ATR) — bad location")
        except Exception as e:  # noqa: BLE001 - unevaluable -> hold
            _block("sr_zone", "hold",
                   f"4h S/R zone unevaluable ({e}) — holding (fail-closed)")

    # V17 — CVD / Taker aggressor imbalance veto (Phase 4).
    # Rejects when aggressive market orders strongly oppose the signal direction.
    # Best-effort by design (M1): taker_volume is an OPTIONAL derivatives
    # component — Stage 8 of the engine flags its absence on the signal and
    # lets the signal through (test_derivatives_optional_gap_flagged_on_signal).
    # This veto fires only when the feed is present; a missing feed is NOT a
    # hold here, it is recorded as deriv_missing by the engine.
    taker = snap.get("taker_buy_sell_ratio")  # H3: was "taker_ratio" (dead key)
    if _finite(taker):
        taker = float(taker)
        if direction == "BULLISH" and taker < 0.70:
            _block("cvd_imbalance", "veto",
                   f"taker buy/sell ratio {taker:.2f} < 0.70 — heavy seller aggression opposes LONG")
        elif direction == "BEARISH" and taker > 1.43:
            _block("cvd_imbalance", "veto",
                   f"taker buy/sell ratio {taker:.2f} > 1.43 — heavy buyer aggression opposes SHORT")

    # V18 — Open Interest Flush / Liquidation cascade veto (Phase 4).
    # Rejects if a rapid OI dump happens concurrently with a liquidation spike.
    if adv is not None:
        oi_chg = adv.get("oi_change")
        spike = adv.get("spike")
        if not _finite(oi_chg) or not _finite(spike):
            # M1: fail-closed — open_interest is a REQUIRED derivatives
            # component; the OI-flush cascade veto cannot be evaluated on
            # partial data, so hold instead of silently skipping.
            _block("oi_flush", "hold",
                   "OI/liquidation inputs incomplete — holding (fail-closed)")
        elif float(oi_chg) <= -5.0 and float(spike) >= 2.5:
            _block("oi_flush", "veto",
                   f"OI flush cascade (OI {float(oi_chg):+.1f}% with {float(spike):.1f}x liq spike) — high slippage & cascade risk")

    # V19 — Funding Window Proximity Veto:
    # Protects against entering right before 8h funding fee snapshot (00:00, 08:00, 16:00 UTC)
    # when funding rate is high against the position, preventing immediate fee drag.
    if getattr(config, "FUNDING_PROXIMITY_VETO_ENABLED", True) and snap is not None:
        funding_val = snap.get("aggregate_funding")
        if _finite(funding_val):
            f_val = float(funding_val)
            max_rate = getattr(config, "FUNDING_PROXIMITY_MAX_RATE", 0.0005)
            window_min = getattr(config, "FUNDING_PROXIMITY_WINDOW_MIN", 15)
            now_sec = time.time()
            sec_until_funding = 28800 - (now_sec % 28800)
            min_until_funding = sec_until_funding / 60.0

            if min_until_funding <= window_min:
                if direction == "BULLISH" and f_val >= max_rate:
                    _block("funding_window", "veto",
                           f"Within {min_until_funding:.1f}m of 8h funding snapshot with high funding "
                           f"{f_val*100:+.3f}% (>= +{max_rate*100:.3f}%) paying shorts — immediate funding drag")
                elif direction == "BEARISH" and f_val <= -max_rate:
                    _block("funding_window", "veto",
                           f"Within {min_until_funding:.1f}m of 8h funding snapshot with negative funding "
                           f"{f_val*100:+.3f}% (<= -{max_rate*100:.3f}%) paying longs — immediate funding drag")

    # V20 — Volatility Shock / Extreme Candle Range Veto (Phase 4).
    # Blocks entries when the latest 15m candle true range is an extreme outlier
    # (> config.VOLATILITY_SHOCK_MAX_RATIO * 14-period ATR), indicating erratic wick,
    # illiquidity sweep, or sudden news volatility.
    if getattr(config, "VOLATILITY_SHOCK_VETO_ENABLED", True):
        if market is None:
            _block("volatility_shock", "hold",
                   "market data unavailable for volatility shock check — holding (fail-closed)")
        else:
            try:
                from indicators import atr_wilder
                k15 = market.get_klines(symbol, "15m", limit=30)
                if not k15 or len(k15) < 16:
                    raise ValueError("insufficient 15m klines for volatility shock check")
                last_c = k15[-1]
                prev_c = k15[-2]
                h = float(last_c["high"])
                l = float(last_c["low"])
                pc = float(prev_c["close"])
                tr = max(h - l, abs(h - pc), abs(l - pc))
                atr_14 = atr_wilder(k15[:-1], 14)
                if atr_14 and atr_14 > 0:
                    ratio = tr / atr_14
                    max_ratio = getattr(config, "VOLATILITY_SHOCK_MAX_RATIO", 2.5)
                    if ratio >= max_ratio:
                        _block("volatility_shock", "veto",
                               f"15m candle true range {tr:.6g} is {ratio:.2f}x ATR_14 "
                               f"({atr_14:.6g} >= {max_ratio:.1f}x) — extreme volatility shock")
                else:
                    raise ValueError("ATR_14 unavailable or zero")
            except Exception as e:
                _block("volatility_shock", "hold",
                       f"volatility shock check unevaluable ({e}) — holding (fail-closed)")

    # V21 — Trend Strength gate (2026-10-17).
    # Blocks entries when the 4h timeframe has no real trend. Post-mortem of
    # 2026-10-17: 4 of 5 SL-hit SHORTs were taken in flat markets (|4h
    # displacement| < 0.6%) with quality 92-98 — the engine's MA-alignment
    # trend definition passes in chop, then normal oscillation wicked out
    # the tight ATR stops. Requires |displacement| >=
    # TREND_STRENGTH_MIN_DISPLACEMENT_PCT over TREND_STRENGTH_LOOKBACK_4H
    # 4h candles *in the signal direction* (also blocks counter-trend
    # entries). Fail-closed: unevaluable 4h history holds the signal.
    if getattr(config, "TREND_STRENGTH_VETO_ENABLED", True):
        if market is None:
            _block("trend_strength", "hold",
                   "market data unavailable for trend strength check — holding (fail-closed)")
        else:
            try:
                lookback = int(getattr(config, "TREND_STRENGTH_LOOKBACK_4H", 6))
                k4 = market.get_klines(symbol, "4h", limit=lookback + 1)
                closes = [float(c["close"]) for c in k4
                          if c.get("close") is not None]
                if len(closes) < lookback + 1:
                    raise ValueError("insufficient 4h history for trend strength check")
                first, last = closes[-lookback - 1], closes[-1]
                if first <= 0:
                    raise ValueError("bad 4h close for trend strength check")
                disp = (last - first) / first * 100.0
                min_disp = float(getattr(config, "TREND_STRENGTH_MIN_DISPLACEMENT_PCT", 1.5))
                ok = (disp >= min_disp) if direction == "BULLISH" else (disp <= -min_disp)
                if not ok:
                    _block("trend_strength", "veto",
                           f"4h displacement {disp:+.2f}% over {lookback} candles < "
                           f"{min_disp:g}% in {direction} direction — no real trend, chop risk")
            except Exception as e:  # noqa: BLE001 - unevaluable -> hold
                _block("trend_strength", "hold",
                       f"trend strength check unevaluable ({e}) — holding (fail-closed)")


    return blocks


def _blocking_wall(book: dict, direction: str) -> str | None:
    """Find an opposing wall big enough to block the move.

    GAS: deepCheckLiquidity_ (L3395-3425) — for a LONG, the ask side blocks;
    a wall vetoes when |distancePct| <= LIQ_WALL_VETO_DISTANCE_PCT (0.5) and
    notional >= LIQ_WALL_MIN_NOTIONAL_USD ($250k). The warn tier (0.5-1.0%,
    the GAS -8 delta) is recorded as a note only: the GAS penalty was dead
    code twice over (audit M1: r.delta never initialized, never summed) and
    score deltas belong to roadmap item 3.
    """
    mid = book.get("mid")
    if not _finite(mid) or mid <= 0:
        return None
    mid = float(mid)
    side = book.get("asks") if direction == "BULLISH" else book.get("bids")
    side_name = "ask" if direction == "BULLISH" else "bid"
    blockers = []
    for price, size in side or []:
        if not (_finite(price) and _finite(size)):
            continue
        dist = abs(float(price) - mid) / mid * 100
        notional = float(price) * float(size)
        if (dist <= config.LIQ_WALL_VETO_DISTANCE_PCT
                and notional >= config.LIQ_WALL_MIN_NOTIONAL_USD):
            blockers.append((notional, dist, float(price)))
    if not blockers:
        return None
    notional, dist, price = max(blockers)
    return (f"liquidity wall {side_name} ${notional:,.0f} at {price:g} "
            f"({dist:.2f}% from mid) blocks {direction}")
