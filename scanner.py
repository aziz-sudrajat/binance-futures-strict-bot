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

Parallel evaluation: stage 2 runs in a ThreadPoolExecutor (max_workers
from config.SCAN_MAX_WORKERS). Each worker thread gets its own MarketData
(thread-local; the TTL caches are not thread-safe); the shared
ThrottledSession keeps the global OKX rate limit intact. Results are
yielded in input order — deterministic, identical DB contents to the
sequential path. max_workers=1 takes the sequential stage-2 path.

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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scanner")

import config
import httputil
from data import MarketData, normalize_symbol
from signal_engine import evaluate_symbol, prefilter_symbol, analyze_candles, quality_score_rank
from executor import signal_candle_close_ms, make_signal_id
from signals_db import SignalDB

# One MarketData per worker THREAD (thread-local): the in-memory TTL
# caches inside MarketData are not thread-safe, so they are never shared
# between threads — but reusing one instance per thread keeps the
# cross-symbol caches useful (e.g. the CoinTelegraph news cache, TTL 900s,
# is then fetched ~once per thread per pass instead of once per symbol).
_thread_markets = threading.local()


def _worker_market(session) -> MarketData:
    md = getattr(_thread_markets, "market", None)
    if md is None or md.session is not session:
        md = MarketData(session=session)
        _thread_markets.market = md
    return md

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
    Returns a SUPERSET of the approved set — candidates are sorted descending
    by lightweight quality_score_rank() for faster scoring.
    """
    results = []
    for i, symbol in enumerate(symbols):
        try:
            ok, _reason = prefilter_symbol(symbol, market, tickers)
        except Exception:  # noqa: BLE001 - fail-closed, like the engine
            ok = False
        if ok:
            # Get the 15m analysis + regime for ranking (cheap vs full engine)
            try:
                k15 = market.get_klines(symbol, "15m")
                a15 = analyze_candles(symbol, "15m", k15)
                regime = a15.get("regime", "MIXED / TRANSITION")
                vol_ratio = a15.get("volume_ratio") or 1.0
                direction = "BULLISH" if a15["signal"].startswith("BULL") else "BEARISH"
                rank = quality_score_rank(a15, regime, vol_ratio, direction)
            except Exception:  # noqa: BLE001 - rank fallback
                rank = 0
            results.append((symbol, rank))
        if (i + 1) % 50 == 0:
            log.info("prefilter progress %d/%d", i + 1, len(symbols))
        time.sleep(THROTTLE_SEC)
    # Sort by quality score descending: highest-quality candidates first
    results.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _rank in results]


def _evaluate_priority(symbols: list[str], market: MarketData,
                       db: SignalDB, expiry_min: int,
                       max_workers: int) -> tuple[int, int, int]:
    """Priority scan: sort candidates by quality rank and evaluate the
    highest-priority symbols first. Returns stats."""
    approved = rejected = errors = 0
    session = getattr(market, "session", None) or httputil.ThrottledSession()
    workers = max(1, int(max_workers))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as ex:
        for res in ex.map(_scan_one_symbol, symbols, [session] * len(symbols)):
            symbol = res["symbol"]
            try:
                if res["error"] is not None:
                    errors += 1
                    log.debug("scan %s failed: %s", symbol, res["error"])
                elif res["signal"] is None:
                    rejected += 1
                else:
                    db.upsert_signal(res["signal"],
                                     res["signal_id"],
                                     expiry_min=expiry_min)
                    approved += 1
                    log.info("APPROVED %s Q=%s -> db", symbol,
                             res["signal"].get("quality"))
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.debug("scan %s failed: %s", symbol, e)
            if (approved + rejected + errors) % 50 == 0:
                log.info("progress %d/%d", approved + rejected + errors,
                         len(symbols))
    return approved, rejected, errors


def _scan_one_symbol(symbol: str, session) -> dict:
    """Evaluate ONE symbol inside a worker thread.

    The worker uses its thread's own MarketData(session=session) — see
    _worker_market: the in-memory TTL caches inside MarketData are NOT
    thread-safe, so they are never shared between threads. The shared
    httputil.ThrottledSession keeps the global OKX rate limit intact
    (its token bucket is lock-protected).

    Returns {"symbol", "signal", "note", "signal_id", "error"}; "error" is
    None on success ("signal" None = rejected). Per-symbol exceptions never
    propagate — fail-closed, exactly like the sequential path.
    """
    result = {"symbol": symbol, "signal": None, "note": "",
              "signal_id": None, "error": None}
    try:
        worker_market = _worker_market(session)
        signal, note = evaluate_symbol(symbol, worker_market)
        result["note"] = note
        if signal is None:
            return result
        close_ms = signal_candle_close_ms(worker_market, symbol)
        result["signal"] = signal
        result["signal_id"] = make_signal_id(
            symbol, close_ms, signal.get("direction", "LONG"))
    except Exception as e:  # noqa: BLE001 - one symbol never kills a scan
        result["error"] = str(e)
    return result


def evaluate_symbols_ordered(symbols: list[str], market,
                             max_workers: int):
    """Evaluate symbols in a ThreadPoolExecutor.

    Yields per-symbol result dicts (see _scan_one_symbol) in INPUT order —
    deterministic regardless of which thread finishes first. DB writes and
    logging stay in the caller (single thread) in that same order, so
    downstream code sees the same ordering as the sequential scan.
    """
    session = getattr(market, "session", None) or httputil.ThrottledSession()
    workers = max(1, int(max_workers))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="scan") as ex:
        yield from ex.map(_scan_one_symbol, symbols,
                          [session] * len(symbols))


def _evaluate_sequential(symbols: list[str], market: MarketData,
                         db: SignalDB, expiry_min: int) -> tuple[int, int, int]:
    """Stage 2, sequential: full engine per symbol, one at a time."""
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
    return approved, rejected, errors


def _evaluate_parallel(symbols: list[str], market: MarketData,
                       db: SignalDB, expiry_min: int,
                       max_workers: int) -> tuple[int, int, int]:
    """Stage 2, parallel: full engine in worker threads.

    Evaluation runs in threads (each with its own MarketData); DB upserts
    and logging happen here in the main thread, in symbol order, so stats
    and DB contents match the sequential path. Pacing is handled by the
    shared ThrottledSession's rate limiter instead of the fixed
    per-symbol sleep.
    """
    approved = rejected = errors = 0
    for i, res in enumerate(
            evaluate_symbols_ordered(symbols, market, max_workers)):
        symbol = res["symbol"]
        try:
            if res["error"] is not None:
                errors += 1
                log.debug("scan %s failed: %s", symbol, res["error"])
            elif res["signal"] is None:
                rejected += 1
            else:
                db.upsert_signal(res["signal"], res["signal_id"],
                                 expiry_min=expiry_min)
                approved += 1
                log.info("APPROVED %s Q=%s -> db", symbol,
                         res["signal"].get("quality"))
        except Exception as e:  # noqa: BLE001 - one symbol never kills a pass
            errors += 1
            log.debug("scan %s failed: %s", symbol, e)
        if (i + 1) % 50 == 0:
            log.info("progress %d/%d", i + 1, len(symbols))
    return approved, rejected, errors


def run_pass(market: MarketData, db: SignalDB,
             expiry_min: int = 30, two_sided: bool = False,
             two_stage: bool = True,
             max_workers: int | None = None) -> dict:
    """One full pass over all symbols. Returns stats.

    two_sided=True mirrors the LIVE bot (bullish_only=False): both LONG
    and SHORT signals are evaluated. Default is long-only (ketat preset).

    two_stage=True (default): stage 1 runs the cheap prefilter
    (prefilter_symbol: engine stages 0-2) over the whole universe with one
    bulk tickers fetch, then stage 2 runs the full engine only on the
    candidates. Provably identical approvals to a single-stage pass
    (candidates are a superset of approvable symbols) at a fraction of
    the HTTP cost. two_stage=False keeps the legacy single pass.

    max_workers: stage-2 thread-pool size; None -> config.SCAN_MAX_WORKERS.
    max_workers=1 takes the sequential stage-2 path (identical behavior).

    Priority scan: after prefilter, candidates are ranked by lightweight
    quality_score_rank() and evaluated in that order. Highest-quality
    signals are found first (useful for UI/DB top-N displays).
    """
    # Studio settings apply to the scanner too (quality threshold etc.).
    from dashboard import load_settings
    config.apply_dashboard_overrides(load_settings(mode="live"))
    if two_sided:
        config.BULLISH_ONLY = False
    t0 = time.time()
    symbols = list_symbols(market)
    prefilter_rejected = 0
    eval_symbols = symbols
    if two_stage and symbols:
        tickers = _prime_tickers(market)
        eval_symbols = _select_candidates(symbols, market, tickers)
        prefilter_rejected = len(symbols) - len(eval_symbols)
        log.info("two-stage: %d/%d candidates survive prefilter",
                 len(eval_symbols), len(symbols))
    workers = config.SCAN_MAX_WORKERS if max_workers is None else max_workers
    if max(1, int(workers)) == 1:
        approved, rejected, errors = _evaluate_sequential(
            eval_symbols, market, db, expiry_min)
    else:
        log.info("parallel stage-2: %d workers on %d candidates",
                 max(1, int(workers)), len(eval_symbols))
        approved, rejected, errors = _evaluate_priority(
            eval_symbols, market, db, expiry_min, max(1, int(workers)))
    expired = db.expire_old()
    stats = {
        "at": int(time.time() * 1000),
        "symbols": len(symbols),
        "two_stage": two_stage,
        "two_sided": two_sided,
        "workers": max(1, int(workers)),
        "candidates": len(eval_symbols),
        "prefilter_rejected": prefilter_rejected,
        "approved": approved,
        "rejected": rejected,
        "errors": errors,
        "expired": expired,
        "duration_s": round(time.time() - t0, 1),
    }
    import tempfile
    directory = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scan_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
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
    ap.add_argument("--workers", type=int, default=None,
                    help="stage-2 parallel workers (default: "
                         "config.SCAN_MAX_WORKERS; 1 = sequential)")
    args = ap.parse_args(argv)

    market = MarketData()
    db = SignalDB(args.db)
    try:
        if args.once:
            run_pass(market, db, expiry_min=args.expiry_min,
                     two_sided=args.two_sided, two_stage=args.two_stage,
                     max_workers=args.workers)
            return 0
        interval = max(5, args.interval)
        log.info("scanner loop every %d min (Ctrl+C to stop)", interval)
        while True:
            try:
                run_pass(market, db, expiry_min=args.expiry_min,
                         two_sided=args.two_sided, two_stage=args.two_stage,
                         max_workers=args.workers)
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
