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
    # terminal event per signal_id (tp3_hit > tp2_hit > tp1_hit > sl_hit).
    terminal = {}
    rank = {"sl_hit": 0, "tp1_hit": 1, "tp2_hit": 2, "tp3_hit": 3}
    for e in events:
        st = e.get("state")
        if st in rank:
            sid = e.get("signal_id")
            if sid not in terminal or rank[st] > rank[terminal[sid]["state"]]:
                terminal[sid] = e
    n_trades = len(terminal)
    wins = sum(1 for e in terminal.values() if e["state"] != "sl_hit")
    pnl = sum(float(e.get("realized", 0) or 0) for e in terminal.values())
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
