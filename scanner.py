#!/usr/bin/env python3
"""All-coin signal scanner — OKX perpetuals -> signals.db.

This is stage 1 of the two-stage pipeline:
  scanner.py --loop   (this file: scans ALL OKX coins, writes good signals)
  run_executor.py --from-db  (stage 2: Binance execution from the DB)

A full pass evaluates every USDT-margined OKX perpetual with the real
STRICT engine. APPROVED signals are upserted into signals.db; the executor
picks the freshest ones up on its own cycle. One bad symbol never kills a
pass; a small sleep between symbols keeps us under OKX rate limits.

Run:
    .venv/bin/python scanner.py --once          # single full pass
    .venv/bin/python scanner.py --loop          # continuous (default 15 min)
    .venv/bin/python scanner.py --loop --interval 10
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scanner")

import config
from data import MarketData, normalize_symbol
from signal_engine import evaluate_symbol
from executor import signal_candle_close_ms, make_signal_id
from signals_db import SignalDB

STATE_FILE = "scanner_state.json"
THROTTLE_SEC = 0.15  # polite gap between symbols (OKX rate limits)


def list_symbols(market: MarketData) -> list[str]:
    """Every USDT-margined OKX perpetual, Binance-style symbols."""
    try:
        syms = market.list_okx_swap_symbols()
    except Exception as e:  # noqa: BLE001
        log.error("symbol list failed: %s", e)
        return []
    log.info("universe: %d OKX USDT perpetuals", len(syms))
    return syms


def run_pass(market: MarketData, db: SignalDB,
             expiry_min: int = 30) -> dict:
    """One full pass over all symbols. Returns stats."""
    # Studio settings apply to the scanner too (quality threshold etc.).
    from dashboard import load_settings
    config.apply_dashboard_overrides(load_settings(mode="live"))
    t0 = time.time()
    symbols = list_symbols(market)
    approved = rejected = errors = 0
    for i, symbol in enumerate(symbols):
        try:
            signal, note = evaluate_symbol(symbol, market)
            if signal is None:
                rejected += 1
            else:
                close_ms = signal_candle_close_ms(market, symbol)
                sid = make_signal_id(
                    symbol, close_ms, signal.get("direction", "LONG"))
                db.upsert_signal(signal, sid, expiry_min=expiry_min)
                approved += 1
                log.info("APPROVED %s Q=%s -> db", symbol,
                         signal.get("quality"))
        except Exception as e:  # noqa: BLE001 - one symbol never kills a pass
            errors += 1
            log.debug("scan %s failed: %s", symbol, e)
        if (i + 1) % 50 == 0:
            log.info("progress %d/%d", i + 1, len(symbols))
        time.sleep(THROTTLE_SEC)
    expired = db.expire_old()
    stats = {
        "at": int(time.time() * 1000),
        "symbols": len(symbols),
        "approved": approved,
        "rejected": rejected,
        "errors": errors,
        "expired": expired,
        "duration_s": round(time.time() - t0, 1),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    log.info("pass done: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="All-coin OKX signal scanner")
    ap.add_argument("--once", action="store_true", help="single full pass")
    ap.add_argument("--loop", action="store_true", help="continuous passes")
    ap.add_argument("--interval", type=int, default=15,
                    help="minutes between passes in --loop (default 15)")
    ap.add_argument("--expiry-min", type=int, default=30,
                    help="signal expiry in minutes (default 30)")
    ap.add_argument("--db", default="signals.db")
    args = ap.parse_args(argv)

    market = MarketData()
    db = SignalDB(args.db)
    try:
        if args.once:
            run_pass(market, db, expiry_min=args.expiry_min)
            return 0
        interval = max(5, args.interval)
        log.info("scanner loop every %d min (Ctrl+C to stop)", interval)
        while True:
            try:
                run_pass(market, db, expiry_min=args.expiry_min)
            except Exception:  # noqa: BLE001 - loop never dies on a pass
                log.exception("pass failed")
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
