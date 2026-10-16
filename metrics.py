# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Live performance metrics from lifecycle events (item 3.3).

Reads the executor's lifecycle log (one terminal event per closed trade,
with a numeric ``realized`` PnL field) and computes trader-standard
metrics. Pure function over event dicts — no I/O except the optional
``from_lifecycle_file`` helper. No trading logic touched.
"""
from __future__ import annotations

import json
import math


def _terminal_pnls(events: list[dict]) -> list[float]:
    """Realized PnL per closed trade, in event order."""
    pnls = []
    for e in events:
        if not e.get("terminal"):
            continue
        try:
            pnl = float(e.get("realized", 0.0))
        except (TypeError, ValueError):
            continue
        pnls.append(pnl)
    return pnls


def compute_metrics(events: list[dict]) -> dict:
    """Win rate, profit factor, Sharpe (per-trade), max drawdown, totals.

    Returns {} when there are no closed trades yet. Sharpe here is the
    per-trade Sharpe (mean/std of trade PnLs); it is NOT annualized —
    the field is named ``sharpe_per_trade`` to avoid confusion.
    """
    pnls = _terminal_pnls(events)
    n = len(pnls)
    if n == 0:
        return {}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total = sum(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0)

    # Max drawdown on the cumulative PnL curve (peak-to-trough).
    peak = trough = 0.0
    max_dd = 0.0
    equity = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        trough = min(trough, equity)

    # Per-trade Sharpe (mean / stdev, ddof=1); undefined for n < 2.
    sharpe = 0.0
    if n >= 2:
        mean = total / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        if var > 0:
            sharpe = mean / math.sqrt(var)

    return {
        "n_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 2),
        "total_pnl": round(total, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 3)
        if profit_factor != float("inf") else "inf",
        "max_drawdown": round(max_dd, 4),
        "sharpe_per_trade": round(sharpe, 3),
    }


def from_lifecycle_file(path: str) -> dict:
    """Convenience: read a lifecycle .jsonl file and compute metrics."""
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        return {}
    return compute_metrics(events)
