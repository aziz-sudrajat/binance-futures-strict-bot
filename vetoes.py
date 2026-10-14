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
               skip_deriv_vetoes: bool = False) -> list:
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
    """
    symbol = symbol.upper()
    direction = direction.upper()
    blocks = []

    # V15 — BTC circuit breaker (GAS: deepCheckBtcBreadth_ L3634).
    # The breaker guards LONG exposure only (GAS comment L3655-3656).
    # Runs on real historical klines in backtest — never skipped.
    try:
        chg = btc_1h_change_pct(market)
    except Exception as e:  # noqa: BLE001 - unevaluable breaker -> hold
        blocks.append(("btc_breaker", "hold",
                       f"BTC circuit breaker unevaluable ({e}) — holding (fail-closed)"))
    else:
        if direction == "BULLISH" and chg <= -config.BTC_DUMP_VETO_PCT:
            blocks.append(("btc_breaker", "veto",
                           f"BTC dump {chg:.2f}%/1h <= -{config.BTC_DUMP_VETO_PCT:g}% "
                           f"— circuit breaker, LONG held"))

    if skip_deriv_vetoes:
        return blocks  # V9-V12, V13 skipped: no historical feed (documented)

    # Advanced derivatives (Gate) feed V9/V11/V12.
    try:
        adv = market.get_advanced_derivatives(symbol)
    except Exception as e:  # noqa: BLE001 - unevaluable -> hold
        adv = None
        blocks.append(("adv_deriv", "hold",
                       f"advanced derivatives unavailable ({e}) — holding (fail-closed)"))

    if adv is not None:
        # V9 — leverage-risk veto (GAS L3338; index L1016-1022).
        lev_req = (adv.get("retail_long"), adv.get("funding"),
                   adv.get("oi_usd"), adv.get("volume_24h"))
        if not all(_finite(v) for v in lev_req):
            blocks.append(("leverage_risk", "hold",
                           "leverage-risk inputs incomplete — holding (fail-closed)"))
        else:
            lev = leverage_risk_index(*lev_req, adv.get("spike") or 0.0)
            if lev >= config.DERIV_LEVERAGE_RISK_VETO:
                blocks.append(("leverage_risk", "veto",
                               f"leverage risk index {lev}/100 >= "
                               f"{config.DERIV_LEVERAGE_RISK_VETO} — crowded, flush risk"))

        # V10 — funding extreme against the signal (GAS L3346-3354).
        # Positive funding = longs pay shorts = crowded long (GAS L3347).
        funding = snap.get("aggregate_funding")
        if not _finite(funding):
            blocks.append(("funding", "hold",
                           "funding unavailable — holding (fail-closed)"))
        else:
            funding = float(funding)
            crowded_long = funding > 0
            against = ((direction == "BULLISH" and crowded_long)
                       or (direction == "BEARISH" and not crowded_long))
            if abs(funding) >= config.DERIV_FUNDING_EXTREME and against:
                blocks.append(("funding", "veto",
                               f"funding {funding:+.4%} extreme and against {direction} "
                               f"(crowded {'long' if crowded_long else 'short'}, squeeze risk)"))

        # V11 — squeeze risk in the signal direction (GAS L3360).
        sq_req = (adv.get("retail_long"), adv.get("funding"), adv.get("oi_change"))
        if not all(_finite(v) for v in sq_req):
            blocks.append(("squeeze", "hold",
                           "squeeze-risk inputs incomplete — holding (fail-closed)"))
        else:
            sq = squeeze_risk_index(
                sq_req[0], sq_req[1], sq_req[2],
                adv.get("long_liq_usd") or 0.0, adv.get("short_liq_usd") or 0.0,
                adv.get("spike") or 0.0, direction)
            if sq >= config.DERIV_SQUEEZE_RISK_VETO:
                side = "long" if direction == "BULLISH" else "short"
                blocks.append(("squeeze", "veto",
                               f"{side} squeeze risk {sq}/100 >= "
                               f"{config.DERIV_SQUEEZE_RISK_VETO} — stop-hunt risk"))

        # V12 — top-trader / whale extreme divergence (GAS L3366-3379).
        top_long, div = adv.get("top_long"), adv.get("divergence")
        if not (_finite(top_long) and _finite(div)):
            blocks.append(("whale", "hold",
                           "top-trader positioning unavailable — holding (fail-closed)"))
        else:
            ext = config.DERIV_TOP_TRADER_EXTREME_LONG
            whale_against = (
                (direction == "BULLISH" and top_long <= ext and div <= -15)
                or (direction == "BEARISH" and top_long >= 100 - ext and div >= 15))
            if whale_against:
                blocks.append(("whale", "veto",
                               f"top traders extremely against {direction} "
                               f"(top long {top_long:.1f}%, whale-crowd divergence "
                               f"{div:+.1f}pp)"))

    # V13 — liquidity wall blocking the move (GAS: deepCheckLiquidity_ L3395).
    if book is None:
        try:
            book = market.get_order_book(symbol)
        except Exception as e:  # noqa: BLE001 - unevaluable -> hold
            book = None
            blocks.append(("liq_wall", "hold",
                           f"order book unavailable ({e}) — holding (fail-closed)"))
    wall_reason = _blocking_wall(book, direction) if book is not None else None
    if wall_reason:
        blocks.append(("liq_wall", "veto", wall_reason))

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
