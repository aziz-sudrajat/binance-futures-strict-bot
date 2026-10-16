# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Funding-veto calibration study — backlog #2 (2026-10-15).

OFFLINE: historical OKX data only. No orders, no live trading, no API keys.

Question: is DERIV_FUNDING_EXTREME = 0.001 (veto when |funding| >= 0.1%
against the signal direction) empirically grounded? The veto's thesis is
that extreme funding against the signal predicts an adverse move
(squeeze risk): crowded longs get squeezed down, crowded shorts up.

Method: for 10 liquid USDT-SWAP perps, last 90 days of 8h funding rates
(/api/v5/public/funding-rate-history) + 4h klines (1H retention is only
~60d; 4H covers the full window). At each funding
timestamp T with |f| >= tau, measure forward returns at +4h and +24h.
Report, per threshold tau in {0.05%, 0.10%, 0.15%, 0.20%}:
  - fire count (how often the veto would trigger)
  - P(adverse move | extreme funding): f>0 -> P(ret<0); f<0 -> P(ret>0)
  - mean/median forward return, vs the same stats over ALL funding
    timestamps (base rate, no threshold)

Honest limitations (documented, not hidden):
  - Observational, not causal. Funding follows price (momentum confound):
    extreme funding often marks strong trends that CONTINUE. A high
    adverse-move rate is evidence FOR the veto; a low rate is evidence
    the veto mostly blocks momentum.
  - 90 days / 10 symbols = one market-regime sample. Not a proof.
  - Funding prints every 8h are AUTOCORRELATED: an extreme-funding episode
    arrives in clusters (one mania week ~= 21 correlated prints), so the
    effective sample size is far below the nominal event count. Treat any
    z-score as overstated precision, not a discovery.
  - Forward returns are mark-to-horizon, no costs, no position sizing.
  - This calibrates the THRESHOLD only; it says nothing about veto P&L.
  - 2026-10-15 finding: no funding print reached even 0.05% in 90d/10
    symbols, so the 0.1% veto is empirically UNTESTABLE on available data.
    Sub-threshold exploration (0.01%) showed no adverse edge, which mildly
    WEAKENS the squeeze thesis in normal regimes. The veto is therefore
    kept as cheap mania insurance (GAS parity, zero idle cost) — NOT as a
    validated result. Do not cite this study as validation.

Usage:
    python tools/funding_veto_study.py [--days 90] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "ARBUSDT"]
OKX = "https://www.okx.com"
THRESHOLDS = [0.0005, 0.001, 0.0015, 0.002]
HORIZONS_H = [4, 24]
CACHE_DIR = os.environ.get("FUNDING_VETO_CACHE",
                             "/tmp/funding_veto_study")

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (research)"})


def _get(url, params, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            r.raise_for_status()
            body = r.json()
            if str(body.get("code")) == "0":
                return body.get("data", [])
        except Exception as e:  # noqa: BLE001 - retry
            print(f"    retry {i + 1}: {e}", file=sys.stderr)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url} {params}")


def _cache(name):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def funding_history(sym: str, days: int) -> list:
    """[(funding_time_ms, rate)] oldest-first, 8h cadence."""
    path = _cache(f"fund_{sym}_{days}.json")
    if os.path.exists(path):
        return [tuple(x) for x in json.load(open(path))]
    inst = sym.replace("USDT", "-USDT-SWAP")
    # NOTE (2026-10-15): on these public endpoints `after` pages into the
    # past (records older than the cursor); `before` behaves as a lower
    # bound. Verified empirically against OKX.
    out, seen, after, cutoff = [], set(), None, \
        int(time.time() * 1000) - days * 86400000
    for _page in range(40):  # hard guard: never loop forever
        params = {"instId": inst, "limit": 100}
        if after:
            params["after"] = str(after)
        data = _get(f"{OKX}/api/v5/public/funding-rate-history", params)
        if not data:
            break
        for row in data:
            t = int(row["fundingTime"])
            if t in seen:
                continue
            seen.add(t)
            if t >= cutoff:
                out.append((t, float(row["fundingRate"])))
        oldest = min(int(r["fundingTime"]) for r in data)
        if oldest < cutoff:
            break
        after = oldest
        time.sleep(0.25)
    out.sort()
    json.dump(out, open(path, "w"))
    return out


BAR_MS = 4 * 3600000  # 4H candles: deep enough history (~1y+ retained)


def klines_4h(sym: str, days: int) -> list:
    """[(open_ms, close)] oldest-first, closed 4h candles.

    NOTE: OKX retains ~60d of 1H candles but 4H goes much deeper; 4H is
    sufficient for the 4h/24h forward horizons used here.
    """
    path = _cache(f"k4h_{sym}_{days}.json")
    if os.path.exists(path):
        return [tuple(x) for x in json.load(open(path))]
    inst = sym.replace("USDT", "-USDT-SWAP")
    out, seen, after = [], set(), None
    cutoff = int(time.time() * 1000) - days * 86400000
    for _page in range(40):  # hard guard: never loop forever
        params = {"instId": inst, "bar": "4H", "limit": 100}
        if after:
            params["after"] = str(after)
        data = _get(f"{OKX}/api/v5/market/candles", params)
        if not data:
            break
        for k in data:
            if str(k[8]) != "1":
                continue
            t = int(k[0])
            if t in seen:
                continue
            seen.add(t)
            if t >= cutoff - BAR_MS:
                out.append((t, float(k[4])))
        oldest = min(int(k[0]) for k in data)
        if oldest < cutoff - BAR_MS:
            break
        after = oldest
        time.sleep(0.25)
    out.sort()
    json.dump(out, open(path, "w"))
    return out


def fwd_return(klines: list, t_ms: int, horizon_h: int):
    """Forward return over [t_ms, t_ms+h]: ratio of the 4h-candle closes
    bracketing the window. (A candle opening at X closes at X+4h, so the
    target candle opens at tgt-4h — not at tgt.)"""
    px0 = next((c for ts, c in klines if ts >= t_ms - BAR_MS), None)
    tgt = t_ms + horizon_h * 3600000 - BAR_MS
    px1 = min(klines, key=lambda kc: abs(kc[0] - tgt))[1] if klines else None
    if px0 is None or px1 is None or px0 <= 0:
        return None
    return px1 / px0 - 1


def _z_vs_base(p1, n1, p0, n0):
    """Two-proportion z for (extreme funding adverse rate) vs base rate."""
    if not n1 or not n0:
        return None
    se = (p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0) ** 0.5
    return (p1 - p0) / se if se > 0 else 0.0


def _check_coverage(funds_all, klines_all, days):
    """Fail loudly if kline coverage can't serve the funding range.

    A silent truncation (page guard hit) would otherwise produce wrong
    forward returns from nearest-candle lookups on a cut series.
    """
    if not funds_all or not klines_all:
        raise RuntimeError("empty funding or kline series")
    t_min = min(t for sym in funds_all for t, _ in funds_all[sym])
    t_max = max(t for sym in funds_all for t, _ in funds_all[sym])
    k_min = min(t for sym in klines_all for t, _ in klines_all[sym])
    k_max = max(t for sym in klines_all for t, _ in klines_all[sym])
    # Forward windows must be fully measurable: events younger than the
    # longest horizon are skipped in main(), so klines only need to reach
    # the latest funding timestamp.
    need_lo, need_hi = t_min - BAR_MS, t_max
    if k_min > need_lo or k_max < need_hi:
        raise RuntimeError(
            f"kline coverage [{k_min}, {k_max}] does not serve funding "
            f"range [{need_lo}, {need_hi}]: increase the page guard or "
            f"reduce --days")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    per_tau = {t: {"n": 0, "long_crowd": {h: [] for h in HORIZONS_H},
                   "short_crowd": {h: [] for h in HORIZONS_H}}
               for t in THRESHOLDS}
    base = {"long_crowd": {h: [] for h in HORIZONS_H},
            "short_crowd": {h: [] for h in HORIZONS_H}}
    funds_all, klines_all = {}, {}

    for sym in SYMBOLS:
        print(f"{sym}: funding...", flush=True)
        funds = funding_history(sym, args.days)
        print(f"  {len(funds)} funding points; klines...", flush=True)
        kl = klines_4h(sym, args.days)
        print(f"  {len(kl)} candles", flush=True)
        funds_all[sym], klines_all[sym] = funds, kl
    _check_coverage(funds_all, klines_all, args.days)

    max_h = max(HORIZONS_H)
    for sym in SYMBOLS:
        funds, kl = funds_all[sym], klines_all[sym]
        k_max = kl[-1][0]
        for t, f in funds:
            if t + max_h * 3600000 > k_max:
                continue  # forward window not yet fully measurable
            rets = {h: fwd_return(kl, t, h) for h in HORIZONS_H}
            if any(r is None for r in rets.values()):
                continue
            crowd = "long_crowd" if f > 0 else "short_crowd" if f < 0 else None
            if crowd is None:
                continue
            for h in HORIZONS_H:
                base[crowd][h].append(rets[h])
            for tau in THRESHOLDS:
                if abs(f) >= tau:
                    per_tau[tau]["n"] += 1
                    for h in HORIZONS_H:
                        per_tau[tau][crowd][h].append(rets[h])

    report = {"days": args.days, "symbols": SYMBOLS,
              "thresholds": {}, "base": {}}
    for crowd in ("long_crowd", "short_crowd"):
        # adverse for a crowded long = negative fwd return (squeeze down)
        report["base"][crowd] = {}
        for h in HORIZONS_H:
            rs = base[crowd][h]
            adv = ([-r for r in rs] if crowd == "short_crowd" else rs)
            report["base"][crowd][h] = {
                "n": len(rs),
                "mean_ret": statistics.fmean(rs) if rs else None,
                "p_adverse": (sum(1 for r in adv if r < 0) / len(adv)
                              if adv else None),
            }
    for tau in THRESHOLDS:
        d = per_tau[tau]
        entry = {"n_events": d["n"], "crowds": {}}
        for crowd in ("long_crowd", "short_crowd"):
            entry["crowds"][crowd] = {}
            for h in HORIZONS_H:
                rs = d[crowd][h]
                adv = ([-r for r in rs] if crowd == "short_crowd" else rs)
                p_adv = (sum(1 for r in adv if r < 0) / len(adv)
                         if adv else None)
                b = report["base"][crowd][h]
                entry["crowds"][crowd][h] = {
                    "n": len(rs),
                    "mean_ret": statistics.fmean(rs) if rs else None,
                    "median_ret": statistics.median(rs) if rs else None,
                    "p_adverse": p_adv,
                    "z_vs_base": (_z_vs_base(p_adv, len(adv),
                                             b["p_adverse"], b["n"])
                                  if p_adv is not None and
                                  b["p_adverse"] is not None else None),
                }
        report["thresholds"][tau] = entry

    text = ["=== Funding veto calibration (OKX, "
            f"{args.days}d, {len(SYMBOLS)} symbols) ==="]
    for crowd in ("long_crowd", "short_crowd"):
        b = report["base"][crowd]
        text.append(
            f"base [{crowd}]: " +
            ", ".join(f"{h}h n={b[h]['n']} "
                      f"p_adverse={b[h]['p_adverse']:.3f}"
                      for h in HORIZONS_H))
    for tau in THRESHOLDS:
        e = report["thresholds"][tau]
        parts = []
        for crowd in ("long_crowd", "short_crowd"):
            for h in HORIZONS_H:
                c = e["crowds"][crowd][h]
                pa = f"{c['p_adverse']:.3f}" if c["p_adverse"] is not None else "n/a"
                mr = f"{c['mean_ret']:+.4f}" if c["mean_ret"] is not None else "n/a"
                parts.append(f"{crowd}/{h}h n={c['n']} p_adv={pa} mean={mr}")
        text.append(f"tau={tau:.4f} (n={e['n_events']}): " + "; ".join(parts))
    out = "\n".join(text)
    print(out)
    if args.out:
        json.dump(report, open(args.out, "w"), indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
