#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""All-coin signal scanner — OKX perpetuals -> signals.db.

This is stage 1 of the two-stage pipeline:
  scanner.py --loop   (this file: scans ALL OKX coins, writes good signals)
  run_executor.py --from-db  (stage 2: Binance execution from the DB)

A full pass evaluates every USDT-margined OKX perpetual with the real
STRICT engine. APPROVED signals are upserted into signals.db; the executor
picks the freshest ones up on its own cycle. One bad symbol never kills a
pass; a small sleep between symbols keeps us under OKX rate limits.

Two-stage scan (default): a cheap prefilter (engine stages 0-2: volume,
15m setup, MTF alignment) screens the whole universe with one bulk
tickers fetch, then the full engine runs only on the surviving
candidates — identical approvals, a fraction of the HTTP cost.
Use --no-two-stage for the legacy single pass.

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
from signal_engine import evaluate_symbol, prefilter_symbol
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


def _prime_tickers(market: MarketData) -> dict | None:
    """One bulk tickers fetch for the whole universe.

    Returns {symbol: {"vol_usd_24h": ..., "last": ...}} or None on any
    failure — the caller then falls back to per-symbol volume lookups.
    """
    try:
        rows = market.get_swap_tickers()
    except Exception as e:  # noqa: BLE001
        log.warning("bulk tickers failed, per-symbol fallback: %s", e)
        return None
    return {r["symbol"]: r for r in rows if r.get("symbol")}


def _select_candidates(symbols: list[str], market: MarketData,
                       tickers: dict | None) -> list[str]:
    """Stage 1 of the two-stage scan: keep only symbols passing the cheap
    necessary conditions (engine stages 0-2 via prefilter_symbol).

    One bad symbol never kills the selection; every symbol is fail-closed.
    """
    candidates = []
    for i, symbol in enumerate(symbols):
        try:
            ok, _reason = prefilter_symbol(symbol, market, tickers)
        except Exception:  # noqa: BLE001 - fail-closed, like the engine
            ok = False
        if ok:
            candidates.append(symbol)
        if (i + 1) % 50 == 0:
            log.info("prefilter progress %d/%d", i + 1, len(symbols))
        time.sleep(THROTTLE_SEC)
    return candidates


def run_pass(market: MarketData, db: SignalDB,
             expiry_min: int = 30, two_sided: bool = False,
             two_stage: bool = True) -> dict:
    """One full pass over all symbols. Returns stats.

    two_sided=True mirrors the LIVE bot (bullish_only=False): both LONG
    and SHORT signals are evaluated. Default is long-only (ketat preset).

    two_stage=True (default): stage 1 runs the cheap prefilter
    (prefilter_symbol: engine stages 0-2) over the whole universe with one
    bulk tickers fetch, then stage 2 runs the full engine only on the
    candidates. Provably identical approvals to a single-stage pass
    (candidates are a superset of approvable symbols) at a fraction of
    the HTTP cost. two_stage=False keeps the legacy single pass.
    """
    # Studio settings apply to the scanner too (quality threshold etc.).
    from dashboard import load_settings
    config.apply_dashboard_overrides(load_settings(mode="live"))
    if two_sided:
        config.BULLISH_ONLY = False
    t0 = time.time()
    symbols = list_symbols(market)
    approved = rejected = errors = 0
    prefilter_rejected = 0
    eval_symbols = symbols
    if two_stage and symbols:
        tickers = _prime_tickers(market)
        eval_symbols = _select_candidates(symbols, market, tickers)
        prefilter_rejected = len(symbols) - len(eval_symbols)
        log.info("two-stage: %d/%d candidates survive prefilter",
                 len(eval_symbols), len(symbols))
    for i, symbol in enumerate(eval_symbols):
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
            log.info("progress %d/%d", i + 1, len(eval_symbols))
        time.sleep(THROTTLE_SEC)
    expired = db.expire_old()
    stats = {
        "at": int(time.time() * 1000),
        "symbols": len(symbols),
        "two_stage": two_stage,
        "candidates": len(eval_symbols),
        "prefilter_rejected": prefilter_rejected,
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
    ap.add_argument("--two-sided", action="store_true",
                    help="evaluate LONG and SHORT (mirror live bot); "
                         "default is long-only ketat preset")
    ap.add_argument("--two-stage", dest="two_stage",
                    action=argparse.BooleanOptionalAction, default=True,
                    help="two-stage scan: cheap prefilter over all symbols, "
                         "full engine on candidates only (default on; "
                         "--no-two-stage for the legacy single pass)")
    args = ap.parse_args(argv)

    market = MarketData()
    db = SignalDB(args.db)
    try:
        if args.once:
            run_pass(market, db, expiry_min=args.expiry_min,
                     two_sided=args.two_sided, two_stage=args.two_stage)
            return 0
        interval = max(5, args.interval)
        log.info("scanner loop every %d min (Ctrl+C to stop)", interval)
        while True:
            try:
                run_pass(market, db, expiry_min=args.expiry_min,
                         two_sided=args.two_sided, two_stage=args.two_stage)
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
