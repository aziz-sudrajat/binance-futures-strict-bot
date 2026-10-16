# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Phase-1 scanner: run the STRICT pipeline over a coin universe.

Paper mode only — passing signals are recorded to paper_trades.jsonl.
No orders are placed (there is no order-placement code in Phase 1).

Usage:
    python scan.py                        # scan config.UNIVERSE
    python scan.py --symbols BTCUSDT,ETHUSDT
    python scan.py --log paper_trades.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import config
from data import MarketData, normalize_symbol
from signal_engine import evaluate_symbol


def scan(symbols: list[str], log_path: str, market: MarketData,
         max_workers: int = 1) -> list[dict]:
    """Scan symbols with the STRICT engine; approved signals -> paper log.

    max_workers=1 (default) evaluates sequentially — exactly the legacy
    behavior. max_workers>1 evaluates symbols in a ThreadPoolExecutor and
    reports/logs results in input order, so approvals are provably identical
    to the sequential path (each symbol is evaluated independently).
    """
    from concurrent.futures import ThreadPoolExecutor

    normed: list[str] = []
    for raw in symbols:
        symbol = normalize_symbol(raw)
        if symbol:
            normed.append(symbol)

    def _evaluate(indexed):
        i, symbol = indexed
        t0 = time.time()
        try:
            signal, note = evaluate_symbol(symbol, market)
        except Exception as e:  # noqa: BLE001 - one bad symbol must not kill the scan
            return i, symbol, None, f"ERROR {e}", 0.0
        return i, symbol, signal, note, time.time() - t0

    workers = max(1, int(max_workers or 1))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            evaluated = list(pool.map(_evaluate, enumerate(normed)))
    else:
        evaluated = [_evaluate((i, s)) for i, s in enumerate(normed)]
    # pool.map already preserves order; sort defensively so reporting and
    # the paper log are always in input order on every path.
    evaluated.sort(key=lambda r: r[0])

    passed = []
    for _i, symbol, signal, note, dt in evaluated:
        if signal:
            passed.append(signal)
            print(f"  {symbol:12s} ✅ {note}  ({dt:.1f}s)")
            print(f"               entry {signal['entry']:.6g} "
                  f"[{signal['entry_low']:.6g} - {signal['entry_high']:.6g}]  "
                  f"SL {signal['stop_loss']:.6g}")
            print(f"               TP1 {signal['tp1']:.6g} / TP2 {signal['tp2']:.6g} / "
                  f"TP3 {signal['tp3']:.6g}   RSI {signal['rsi']}  "
                  f"{signal['regime']}  deriv {signal['deriv_score']} "
                  f"({signal['deriv_provider']})")
        elif note.startswith("ERROR "):
            print(f"  {symbol:12s} {note}", flush=True)
        else:
            print(f"  {symbol:12s} -- {note}  ({dt:.1f}s)", flush=True)
    if passed:
        with open(log_path, "a", encoding="utf-8") as f:
            for s in passed:
                f.write(json.dumps(s) + "\n")
        print(f"\nLogged {len(passed)} paper signal(s) to {log_path}")
    else:
        print("\nNo STRICT signals this scan. The bot does not force entries.")
    return passed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="STRICT signal scanner (paper mode)")
    ap.add_argument("--symbols", default=",".join(config.UNIVERSE),
                    help="comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--log", default="paper_trades.jsonl",
                    help="paper-trade log path")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel evaluation workers (>1 = ThreadPoolExecutor)")
    args = ap.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"Scanning {len(symbols)} symbol(s) — STRICT (quality >= "
          f"{config.MIN_QUALITY_SCORE}) — paper mode, no orders")
    market = MarketData()
    scan(symbols, args.log, market, max_workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
