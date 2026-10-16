# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Empirical threshold study — ROADMAP ITEM 3, step 3.

OFFLINE: historical OKX klines only. No orders, no live trading, no API keys.

For 5 majors, every 6h over the last 7 days, run the deterministic quality
pipeline on data that was available at that time and record the quality
scores of signals that reach the quality gate. Report the score distribution
and pass rates at 60/65/70/75 to ground the MIN_QUALITY_SCORE decision.

Known limitation (documented, not hidden): derivatives warn tiers
(lev/funding/squeeze/whale/smart/liq-event/liq-wall) have no historical
feed, so they are treated as 0 here. Real warns can only push scores DOWN,
therefore every pass rate below is an UPPER BOUND on the true pass rate.

Usage:
    .venv/bin/python tools/threshold_study.py [--days 7] [--step-h 6]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quality_deltas import (
    btc_breadth_warn_delta,
    final_quality,
    h1_trend_delta,
    mtf_30m_delta,
    mtf_4h_delta,
)
from signal_engine import (
    analyze_candles,
    mtf_confirm,
    mtf_trend_direction,
)

_OKX_INTERVAL = {"15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}
_INTERVAL_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
                "4h": 14_400_000, "1d": 86_400_000}
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "AVAXUSDT"]
CACHE = "/tmp/threshold_study_cache.json"


def _inst(symbol: str) -> str:
    base = symbol[:-4]
    return f"{base}-USDT"


def fetch_history(symbol: str, interval: str, days: int,
                  session: requests.Session) -> list[dict]:
    """Paginated OKX history candles, closed only, oldest-first.

    Uses /market/history-candles with the `after` cursor: empirically,
    `after=<oldest_ts>` returns the next page backward in time with exact
    1-candle continuity (verified live 2026-10-14). The `before` cursor is
    ignored by this edge.
    """
    need = int(days * 86_400_000 / _INTERVAL_MS[interval]) + 10
    pages = (need + 299) // 300
    out, after = [], None
    for _ in range(pages):
        params = {"instId": _inst(symbol), "bar": _OKX_INTERVAL[interval],
                  "limit": "300"}
        if after:
            params["after"] = str(after)
        r = session.get("https://www.okx.com/api/v5/market/history-candles",
                        params=params, timeout=20)
        r.raise_for_status()
        rows = (r.json() or {}).get("data") or []
        if not rows:
            break
        for k in rows:
            if str(k[8]) != "1":  # GAS parseOkxRows_ L644: confirmed only
                continue
            t = int(k[0])
            out.append({"open_time": t, "open": float(k[1]),
                        "high": float(k[2]), "low": float(k[3]),
                        "close": float(k[4]), "volume": float(k[5]),
                        "close_time": t + _INTERVAL_MS[interval] - 1})
        after = int(rows[-1][0])
        time.sleep(0.15)
    # Dedupe by open_time (cursor pages touch at the boundary).
    seen = {}
    for c in out:
        seen[c["open_time"]] = c
    out = sorted(seen.values(), key=lambda c: c["open_time"])
    return out


def load_all(days: int) -> dict:
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            cached = json.load(f)
        if cached.get("days") == days:
            print(f"using cached history ({CACHE})")
            return cached["data"]
    session = requests.Session()
    session.headers.update({"User-Agent": "binance-futures-bot/threshold-study"})
    data = {}
    for sym in SYMBOLS:
        data[sym] = {}
        for iv in ("15m", "30m", "1h", "4h", "1d"):
            data[sym][iv] = fetch_history(sym, iv, days, session)
            print(f"  {sym} {iv}: {len(data[sym][iv])} closed candles")
    with open(CACHE, "w") as f:
        json.dump({"days": days, "data": data}, f)
    return data


def slice_at(series: list[dict], t_ms: int, n: int) -> list[dict]:
    past = [c for c in series if c["close_time"] <= t_ms]
    return past[-n:] if len(past) >= n else past


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--step-h", type=int, default=6)
    args = ap.parse_args()

    data = load_all(args.days)
    step_ms = args.step_h * 3_600_000
    window_ms = args.days * 86_400_000

    scores: list[int] = []
    per_symbol: dict[str, list[int]] = {s: [] for s in SYMBOLS}
    evaluated = reached = 0
    funnel = {"15m_setup": 0, "cascade_4h1d": 0, "cascade_30m": 0}

    for sym in SYMBOLS:
        s15 = data[sym]["15m"]
        if not s15:
            continue
        t_end = s15[-1]["close_time"]
        t = t_end - window_ms
        points = []
        while t <= t_end:
            points.append(t)
            t += step_ms
        for tp in points:
            k15 = slice_at(s15, tp, 220)
            if len(k15) < 80:
                continue
            evaluated += 1
            try:
                a15 = analyze_candles(sym, "15m", k15)
            except Exception:
                continue
            # Same pre-gates as the pipeline: bullish-only, plan, strength>=3.
            if (not str(a15["signal"]).startswith("BULL") or not a15["trade_plan"]
                    or a15["strength"] < 3):
                continue
            funnel["15m_setup"] += 1
            k4h = slice_at(data[sym]["4h"], tp, 100)
            k1d = slice_at(data[sym]["1d"], tp, 100)
            if (mtf_trend_direction([c["close"] for c in k4h]) != "BULLISH"
                    or mtf_trend_direction([c["close"] for c in k1d]) != "BULLISH"):
                continue
            funnel["cascade_4h1d"] += 1
            k30 = slice_at(data[sym]["30m"], tp, 220)
            if len(k30) < 80:
                continue
            try:
                a30 = analyze_candles(sym, "30m", k30)
            except Exception:
                continue
            chk = mtf_confirm(a30)  # STRICT mode
            if not chk["pass"]:
                continue
            funnel["cascade_30m"] += 1
            reached += 1
            comps: dict[str, int] = {}
            comps["mtf_30m"] = mtf_30m_delta(chk)[0]
            try:
                a4h = analyze_candles(sym, "4h", k4h)
                comps["mtf_4h"] = mtf_4h_delta(a4h)[0]
            except Exception:
                comps["mtf_4h"] = 0
            k1h = slice_at(data[sym]["1h"], tp, 100)
            comps["h1"] = h1_trend_delta(
                [c["close"] for c in k1h] if len(k1h) >= 55 else [])[0]
            # BTC 1h breadth from the BTC series sliced at the same time.
            btc1h = slice_at(data["BTCUSDT"]["1h"], tp, 3)
            if len(btc1h) >= 2 and btc1h[-2]["close"] > 0:
                chg = ((btc1h[-1]["close"] - btc1h[-2]["close"])
                       / btc1h[-2]["close"] * 100)
            else:
                chg = None
            comps["btc_warn"] = btc_breadth_warn_delta(chg)[0]
            # Derivatives warn tiers: no historical feed -> 0 (documented;
            # real warns only push DOWN, so pass rates are upper bounds).
            comps.update({k: 0 for k in ("lev_warn", "funding", "squeeze_warn",
                                         "whale_warn", "smart_money",
                                         "liq_event", "liq_wall_warn")})
            q = final_quality(a15["strength"], comps)
            scores.append(q["quality"])
            per_symbol[sym].append(q["quality"])

    print(f"\nevaluated points: {evaluated}, reached quality gate: {reached}")
    print("funnel: " + ", ".join(f"{k}={v}" for k, v in funnel.items()))
    if not scores:
        print("no signals reached the gate in this window")
        return 1
    scores.sort()
    n = len(scores)

    def pct(p):
        return scores[min(n - 1, int(p / 100 * n))]

    print(f"\nquality distribution (n={n}):")
    print(f"  min {scores[0]}, p10 {pct(10)}, p25 {pct(25)}, "
          f"median {pct(50)}, p75 {pct(75)}, p90 {pct(90)}, max {scores[-1]}")
    print("  per symbol (n, median): " +
          ", ".join(f"{s} ({len(v)}, {sorted(v)[len(v)//2] if v else '-'})"
                    for s, v in per_symbol.items()))
    buckets: dict[int, int] = {}
    for s in scores:
        b = (s // 5) * 5
        buckets[b] = buckets.get(b, 0) + 1
    print("  histogram (5pt buckets): " +
          " ".join(f"{b}-{b+4}:{c}" for b, c in sorted(buckets.items())))
    print("\npass rate (UPPER BOUND — deriv warns unavailable historically):")
    for thr in (60, 65, 70, 75):
        passed = sum(1 for s in scores if s >= thr)
        print(f"  >= {thr}: {passed}/{n} = {passed/n*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
