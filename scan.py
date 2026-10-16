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


def scan(symbols: list[str], log_path: str, market: MarketData) -> list[dict]:
    passed = []
    for raw in symbols:
        symbol = normalize_symbol(raw)
        if not symbol:
            continue
        t0 = time.time()
        try:
            signal, note = evaluate_symbol(symbol, market)
        except Exception as e:  # noqa: BLE001 - one bad symbol must not kill the scan
            print(f"  {symbol:12s} ERROR {e}", flush=True)
            continue
        dt = time.time() - t0
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
    args = ap.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"Scanning {len(symbols)} symbol(s) — STRICT (quality >= "
          f"{config.MIN_QUALITY_SCORE}) — paper mode, no orders")
    market = MarketData()
    scan(symbols, args.log, market)
    return 0


if __name__ == "__main__":
    sys.exit(main())
