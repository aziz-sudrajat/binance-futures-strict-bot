# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Daily lifecycle report (honest accounting).

Reads lifecycle.jsonl (+ state.json) and prints the day's signals, trades,
fills and PnL. Win rate is reported ONLY when the closed-trade sample is
large enough (>= BACKTEST_MIN_TRADES); otherwise the report says
"SAMPLE TOO SMALL" and refuses to claim anything — per the quant-trading
skill: NO VERIFIED RESULTS, NO PERFORMANCE CLAIMS.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import sys

import config
from lifecycle import LifecycleLogger


def _fnum(v) -> float:
    """Lenient float for lifecycle fields: a corrupt/hand-edited value
    must not kill the whole report."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _day_bounds_utc(date_str: str) -> tuple[int, int]:
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc)
    start = int(d.timestamp() * 1000)
    return start, start + 86_400_000


def daily_report(date_str: str | None = None,
                 lifecycle_path: str = config.EXEC_LIFECYCLE_FILE,
                 state_path: str = config.EXEC_STATE_FILE) -> str:
    if date_str is None:
        date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    start, end = _day_bounds_utc(date_str)
    events = [e for e in LifecycleLogger(lifecycle_path).read_all()
              if start <= e.get("ts", 0) < end]

    lines = [f"=== Daily report {date_str} (UTC) ===",
             f"lifecycle events: {len(events)}"]
    by_state = collections.Counter(e.get("state") for e in events)
    for st, n in sorted(by_state.items()):
        lines.append(f"  {st}: {n}")

    # Rejection reasons (top 5).
    rej = [e.get("reason", "?") for e in events if e.get("state") == "rejected"]
    if rej:
        lines.append("top rejection reasons:")
        for reason, n in collections.Counter(rej).most_common(5):
            lines.append(f"  x{n} {reason[:90]}")

    # Terminal trade outcomes. One signal = one trade outcome: the LAST
    # terminal event per signal_id. Terminal events carry terminal=True
    # (the executor marks them) because per-leg TP fill events reuse the
    # same state names (tp1_hit ...) — a highest-rank pick would keep the
    # leg fill (PnL 0) instead of the true outcome. Legacy logs without the
    # marker fall back to "last outcome event wins".
    # expired/cancelled = the entry never became a position: they stay
    # visible in the state counts above but are excluded from trade stats
    # (no PnL is possible, and counting them would distort the win rate).
    TERMINAL_STATES = {"tp1_hit", "tp2_hit", "tp3_hit", "sl_hit",
                       "manual_close", "profit_take", "trail_stop", "time_stop",
                       "expired", "cancelled"}
    TRADE_STATES = {"tp1_hit", "tp2_hit", "tp3_hit", "sl_hit", "manual_close",
                    "profit_take", "trail_stop", "time_stop"}
    terminal = {}
    for e in events:
        if e.get("state") not in TERMINAL_STATES:
            continue
        sid = e.get("signal_id")
        prev = terminal.get(sid)
        if prev is None:
            terminal[sid] = e
        elif e.get("terminal"):
            terminal[sid] = e      # explicit marker always wins
        elif not prev.get("terminal"):
            terminal[sid] = e      # legacy logs: last outcome event wins
        # else: keep the marked terminal event over later non-marked noise
    # Day-boundary edge (reviewer siklus 3, temuan opsional): a position that
    # is STILL OPEN per state.json may already have leg-fill events inside
    # this day's window. Those are not outcomes — counting them inflates
    # n_trades and mislabels an open trade as closed (with PnL 0).
    # signal_id is unique per trade ({symbol}:{close_ms}:{direction},
    # deduped via seen_signal), so "open now" soundly implies "not closed on
    # any report date": the exclusion applies for every date, not just today.
    # Closed tombstones (closed=True) are NOT excluded — they are real trades.
    open_sids: set = set()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            st = json.load(f) or {}
        open_sids = {
            p.get("signal_id") for p in st.get("positions", {}).values()
            if isinstance(p, dict) and p.get("signal_id")
            and not p.get("closed")
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    trades = {sid: e for sid, e in terminal.items()
              if e.get("state") in TRADE_STATES and sid not in open_sids}
    n_trades = len(trades)
    # A win is decided by money, not by the event name: manual_close (loss)
    # or a tp1_hit (remainder) stopped at a net loss must not count as wins.
    wins = sum(1 for e in trades.values() if _fnum(e.get("realized")) > 0)
    pnl = sum(_fnum(e.get("realized")) for e in trades.values())
    lines.append(f"closed trades: {n_trades} | wins: {wins} | "
                 f"realized PnL: {pnl:+.2f} USDT")
    if n_trades < config.BACKTEST_MIN_TRADES:
        lines.append(f"SAMPLE TOO SMALL (n={n_trades} < "
                     f"{config.BACKTEST_MIN_TRADES}): no win-rate claims.")
    else:
        lines.append(f"win rate: {wins / n_trades * 100:.1f}% "
                     f"(n={n_trades}; partial TPs counted once per trade)")

    # Open positions from state.json.
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        open_pos = [p for p in state.get("positions", {}).values()
                    if not p.get("closed")]
        lines.append(f"open positions: {len(open_pos)}")
        for p in open_pos:
            lines.append(f"  {p.get('symbol')} qty={p.get('qty')} "
                         f"entry={p.get('entry_price')} sl={p.get('stop_loss')} "
                         f"{'UNMANAGED' if p.get('unmanaged') else ''}")
        ks = state.get("kill_switch", {})
        if ks.get("active"):
            lines.append(f"KILL SWITCH ACTIVE: {ks.get('reason', '')}")
    except (FileNotFoundError, json.JSONDecodeError):
        lines.append("state.json not found/unreadable")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily executor report")
    ap.add_argument("--daily", action="store_true",
                    help="print today's (UTC) report")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--lifecycle", default=config.EXEC_LIFECYCLE_FILE)
    ap.add_argument("--state", default=config.EXEC_STATE_FILE)
    args = ap.parse_args(argv)
    print(daily_report(args.date, args.lifecycle, args.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
