# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Honest backtester — ROADMAP ITEM 4.

Runs the PRODUCTION evaluate_symbol() pipeline (same hard gates, vetoes,
quality gate, freshness) against historical klines through BacktestMarket,
then simulates entries/exits with costs. Paper mode: no orders, no API keys.

NON-NEGOTIABLE PROPERTIES (each documented where implemented):

1. Production-gate parity: the backtester calls the SAME evaluate_symbol()
   from signal_engine.py — not a simplified replica. All hard gates, the
   MTF cascade, the 6 deterministic vetoes (where evaluable), the quality
   gate (>=70) and the freshness gate run exactly as in production.
2. Live-only data approximations (documented, never hidden):
   - live ticker -> close of the signal candle (data <= t).
   - derivatives snapshot / advanced derivatives / order book -> DEGRADED.
     There is no historical derivatives feed, so derivative-dependent vetoes
     (V9/V10/V11/V12/V13) and warn deltas are SKIPPED (never fabricated).
     The V15 BTC circuit breaker runs on REAL historical BTC 1h klines.
     => backtest pass rates are UPPER BOUNDS on production selectivity.
3. Costs: taker fee per side + slippage per side (config) on every fill;
   funding estimate per 8h while in position (config, assumed paid by the
   long — conservative; historical funding is NOT used: degraded).
4. Same-candle SL+TP rule: on any candle, the stop is checked BEFORE targets
   (conservative for longs). Documented in simulate_trade().
5. Timeouts/expiry: a signal never entered within BACKTEST_SIGNAL_EXPIRY_HOURS
   is NO_ENTRY (not a win, not a loss — excluded from win rate). A position
   open past BACKTEST_MAX_HOLD_HOURS closes at market and IS counted.
6. No look-ahead: a signal at candle-close t uses only candles with
   close_time <= t; entries are evaluated from candle t+1 onward.
7. Walk-forward / out-of-sample: --in-sample / --out-of-sample date ranges
   (or an automatic 70/30 split); both are reported separately.
8. Metrics: win rate, net expectancy (R), profit factor, avg win/loss,
   max drawdown, realized RR, post-cost return, sample size, per-asset and
   per-regime breakdowns, consecutive wins/losses, Sharpe/Sortino on daily
   returns. Sample < BACKTEST_MIN_TRADES or unstable OOS -> the report SAYS SO.

Usage:
    .venv/bin/python backtest.py --symbols BTCUSDT --days 30
    .venv/bin/python backtest.py --symbols BTCUSDT,ETHUSDT \\
        --in-sample 2025-01-01 2025-06-30 --out-of-sample 2025-07-01 2025-09-30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import vetoes
from data import normalize_symbol
from signal_engine import evaluate_symbol

_INTERVAL_MS = {
    "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "4h": 14_400_000, "1d": 86_400_000,
}
_OKX_BAR = {"15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}
_GATE_INTERVALS = ("15m", "30m", "1h", "4h", "1d")
_EXIT_INTERVAL = "15m"  # exits evaluated on the signal timeframe (documented)
_MIN_15M_FOR_VOLUME = 48  # need >=12h of 15m candles to trust 24h volume


def _inst(symbol: str) -> str:
    base = symbol[:-4]
    return f"{base}-USDT"


def fetch_history(symbol: str, interval: str, days: int,
                  session: requests.Session) -> list[dict]:
    """Paginated OKX history candles, closed only, oldest-first.

    Uses /market/history-candles with the `after` cursor: empirically,
    `after=<oldest_ts>` returns the next page backward in time with exact
    1-candle continuity (verified live 2026-10-14; same pattern as
    tools/threshold_study.py). The `before` cursor is ignored by this edge.
    """
    need = int(days * 86_400_000 / _INTERVAL_MS[interval]) + 10
    pages = (need + 299) // 300
    out, after = [], None
    for _ in range(pages):
        params = {"instId": _inst(symbol), "bar": _OKX_BAR[interval],
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
    seen = {}
    for c in out:  # dedupe: cursor pages touch at the boundary
        seen[c["open_time"]] = c
    return sorted(seen.values(), key=lambda c: c["open_time"])


def load_history(symbols: list[str], days: int,
                 cache_path: str) -> dict[str, dict[str, list[dict]]]:
    """Fetch (or reuse cached) history for every symbol x gate interval."""
    key = {"symbols": sorted(symbols), "days": days,
           "intervals": list(_GATE_INTERVALS)}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get("key") == key:
            print(f"using cached history ({cache_path})")
            return cached["data"]
    session = requests.Session()
    session.headers.update({"User-Agent": "binance-futures-bot/backtest"})
    need_symbols = list(dict.fromkeys(symbols + ["BTCUSDT"]))  # BTC 1h: breaker
    data: dict[str, dict[str, list[dict]]] = {}
    for sym in need_symbols:
        data[sym] = {}
        for iv in _GATE_INTERVALS:
            # FIX (B1/B2): ALL gate-interval history for every symbol
            # (incl. BTCUSDT). Non-1h history was intentionally skipped,
            # which broke multi-symbol backtests. Historical depth is what
            # the pipeline gates on, not the symbol list.
            data[sym][iv] = fetch_history(sym, iv, days, session)
            print(f"  {sym} {iv}: {len(data[sym][iv])} closed candles",
                  flush=True)
    with open(cache_path, "w") as f:
        json.dump({"key": key, "data": data}, f)
    return data


class BacktestMarket:
    """Duck-typed market for evaluate_symbol(), time-sliced at t.

    degraded_derivatives = True declares the historical feed has no
    derivatives/order-book data. signal_engine honors it by SKIPPING the
    derivative-dependent gates/deltas (never fabricating values); every skip
    is recorded on the signal dict and in the backtest report.
    """

    degraded_derivatives = True

    def __init__(self, history: dict[str, dict[str, list[dict]]]):
        self.history = history
        self.t_ms = 0

    def set_time(self, t_ms: int) -> None:
        self.t_ms = int(t_ms)

    def _slice(self, symbol: str, interval: str, limit: int) -> list[dict]:
        series = self.history.get(symbol, {}).get(interval, [])
        # NO LOOK-AHEAD: only candles closed at or before t.
        past = [c for c in series if c["close_time"] <= self.t_ms]
        return past[-limit:]

    def get_klines(self, symbol: str, interval: str,
                   limit: int = 220) -> list[dict]:
        symbol = normalize_symbol(symbol)
        out = self._slice(symbol, interval, max(1, int(limit)))
        need = min(config.MIN_CANDLES, int(limit))
        if len(out) < need:
            raise RuntimeError(
                f"backtest: only {len(out)} {interval} candles at t "
                f"(need {need}) — signal held, no fabrication")
        return out

    def get_24h_quote_volume_usd(self, symbol: str) -> float | None:
        symbol = normalize_symbol(symbol)
        c15 = self._slice(symbol, "15m", 96)  # 96 x 15m = 24h
        if len(c15) < _MIN_15M_FOR_VOLUME:
            return None  # fail-closed: thin history -> no signal
        # OKX candle volume is base-asset volume; quote ~= sum(close*volume).
        return sum(c["close"] * c["volume"] for c in c15)

    def get_live_price(self, symbol: str) -> float | None:
        """DOCUMENTED APPROXIMATION: production uses a live ticker for the
        freshness gate; the backtester uses the close of the signal candle
        (data <= t). Slightly optimistic vs a real ticker at t+epsilon, but
        the gate only rejects drift ABOVE the entry zone, and the close is
        the best timestamp-consistent proxy available."""
        symbol = normalize_symbol(symbol)
        c15 = self._slice(symbol, "15m", 1)
        return c15[-1]["close"] if c15 else None

    def get_derivatives_snapshot(self, symbol: str,
                                 period: str = "4h") -> dict:
        symbol = normalize_symbol(symbol)
        return {"symbol": symbol, "period": period, "at": self.t_ms,
                "provider": "backtest-degraded",
                "aggregate_long": None, "aggregate_funding": None,
                "aggregate_oi_change": None, "taker_buy_sell_ratio": None}

    def get_advanced_derivatives(self, symbol: str,
                                 period: str = "4h") -> dict:
        raise RuntimeError("backtest degraded: no historical derivatives feed")

    def get_order_book(self, symbol: str, limit: int = 100) -> dict:
        raise RuntimeError("backtest degraded: no historical order book")

# ------------------------------------------------------- trade simulation
def simulate_trade(signal: dict, candles: list[dict], i_signal: int,
                   risk_usd: float) -> dict:
    """Simulate one LONG signal forward in time. Conservative rules:

    - Entry: first 15m candle AFTER the signal candle (t+1 onward) whose low
      touches <= entry fills at the signal's entry price (limit-order
      semantics), timed at that candle's close. Never entered within
      BACKTEST_SIGNAL_EXPIRY_HOURS -> NO_ENTRY (not a win/loss).
    - Same-candle ordering: on EVERY candle (including the entry candle) the
      stop is checked BEFORE any take-profit. If a candle's range touches
      both SL and TP, the SL is assumed hit first (conservative for longs).
    - Scale-out per config.TP_SPLIT at TP1/TP2/TP3; after TP1 fills the stop
      moves to breakeven (config.BACKTEST_BREAKEVEN_AFTER_TP1).
    - Costs: (taker fee + slippage) per side on every fill, applied to the
      fill notional. Funding estimate per 8h on the open notional, assumed
      paid by the long (conservative; historical funding NOT used).
    - Position open past BACKTEST_MAX_HOLD_HOURS closes at that candle's
      close (market) and IS counted as a trade.
    """
    entry = float(signal["entry"])
    sl0 = float(signal["stop_loss"])
    tps = [(float(signal[f"tp{i}"]), config.TP_SPLIT[i - 1]) for i in (1, 2, 3)]
    per_side = (config.BACKTEST_TAKER_FEE_PCT
                + config.BACKTEST_SLIPPAGE_PCT) / 100.0
    funding_8h = config.BACKTEST_FUNDING_PER_8H_PCT / 100.0

    base = {
        "symbol": signal["symbol"], "direction": "LONG",
        "signal_time": candles[i_signal]["close_time"],
        "signal_quality": signal["quality"], "strength": signal["strength"],
        "regime": signal.get("regime"), "entry": entry, "stop_loss": sl0,
        "tp1": tps[0][0], "tp2": tps[1][0], "tp3": tps[2][0],
        "risk_usd": float(risk_usd), "deriv_degraded": True,
        "status": "NO_ENTRY", "exit_reason": None, "tp_fills": [False] * 3,
        "entry_time": None, "exit_time": None, "qty": 0.0,
        "pnl_usd": 0.0, "r_multiple": 0.0, "fees_usd": 0.0,
        "funding_usd": 0.0, "candles_held": 0,
    }
    if not (entry > sl0 > 0 and risk_usd > 0):
        base["status"] = "INVALID"
        return base
    qty = risk_usd / (entry - sl0)
    base["qty"] = qty

    n = len(candles)
    expiry_idx = i_signal + int(config.BACKTEST_SIGNAL_EXPIRY_HOURS * 4)
    entry_idx = None
    for j in range(i_signal + 1, min(expiry_idx + 1, n)):
        if candles[j]["low"] <= entry:
            entry_idx = j
            break
    if entry_idx is None:
        return base  # NO_ENTRY: expired without entry — not a trade

    base["entry_time"] = candles[entry_idx]["close_time"]
    fees = qty * entry * per_side  # entry fill cost
    max_hold_idx = entry_idx + int(config.BACKTEST_MAX_HOLD_HOURS * 4)
    last_k = min(max_hold_idx, n - 1)

    sl = sl0
    remaining = qty
    realized = 0.0
    funding = 0.0
    tp_done = [False, False, False]
    breakeven = bool(config.BACKTEST_BREAKEVEN_AFTER_TP1)
    exit_reason, exit_k = None, None

    for k in range(entry_idx, last_k + 1):
        c = candles[k]
        funding += remaining * entry * funding_8h / 32.0  # 32 x 15m per 8h
        # 1. stop first — conservative same-candle SL+TP ordering for longs.
        if c["low"] <= sl:
            realized += remaining * (sl - entry)
            fees += remaining * sl * per_side
            remaining = 0.0
            exit_reason, exit_k = "SL", k
            break
        # 2. take-profits in order.
        for ti, (tpx, frac) in enumerate(tps):
            if not tp_done[ti] and c["high"] >= tpx:
                q = qty * frac
                realized += q * (tpx - entry)
                fees += q * tpx * per_side
                tp_done[ti] = True
                remaining -= q
                if ti == 0 and breakeven:
                    sl = entry  # move stop to breakeven after TP1
                elif ti == 1 and getattr(config, "BACKTEST_LOCK_PROFIT_AFTER_TP2", True):
                    sl = tps[0][0]  # move stop to TP1 price after TP2 (lock profit)
        if remaining <= 1e-12:
            exit_reason, exit_k = "TP_ALL", k
            break

    if remaining > 1e-12:
        # Max hold (or end of data): close the rest at market.
        c = candles[last_k]
        px = c["close"]
        realized += remaining * (px - entry)
        fees += remaining * px * per_side
        remaining = 0.0
        exit_reason, exit_k = "MAX_HOLD", last_k

    pnl = realized - fees - funding
    base.update({
        "status": "CLOSED", "exit_reason": exit_reason,
        "tp_fills": tp_done, "exit_time": candles[exit_k]["close_time"],
        "pnl_usd": pnl, "r_multiple": pnl / risk_usd,
        "fees_usd": fees, "funding_usd": funding,
        "candles_held": exit_k - entry_idx + 1,
    })
    return base


# ------------------------------------------------------------------ metrics
def compute_metrics(trades: list[dict], start_equity: float) -> dict:
    """Metrics over CLOSED trades only. NO_ENTRY outcomes are counted
    separately and never enter win rate / expectancy denominators."""
    closed = [t for t in trades if t["status"] == "CLOSED"]
    n = len(closed)
    m: dict = {
        "n_trades": n,
        "n_no_entry": sum(1 for t in trades if t["status"] == "NO_ENTRY"),
        "n_invalid": sum(1 for t in trades if t["status"] == "INVALID"),
    }
    if n == 0:
        m["note"] = "no closed trades"
        return m
    pnls = [t["pnl_usd"] for t in closed]
    rs = [t["r_multiple"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)

    # Equity curve for drawdown + daily Sharpe/Sortino.
    equity = start_equity
    peak = start_equity
    max_dd = 0.0
    daily: dict[str, float] = {}
    for t in sorted(closed, key=lambda x: x["exit_time"] or 0):
        equity += t["pnl_usd"]
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        day = datetime.fromtimestamp((t["exit_time"] or 0) / 1000,
                                     tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + t["pnl_usd"]
    rets = []
    eq = start_equity
    for day in sorted(daily):
        new_eq = eq + daily[day]
        rets.append((new_eq - eq) / eq if eq > 0 else 0.0)
        eq = new_eq

    def _sharpe(rs_):
        if len(rs_) < 2:
            return None
        mu = sum(rs_) / len(rs_)
        var = sum((x - mu) ** 2 for x in rs_) / (len(rs_) - 1)
        return mu / math.sqrt(var) * math.sqrt(365) if var > 0 else None

    def _sortino(rs_):
        if len(rs_) < 2:
            return None
        mu = sum(rs_) / len(rs_)
        down = [x for x in rs_ if x < 0]
        if not down:
            return None
        var = sum(x * x for x in down) / len(down)
        return mu / math.sqrt(var) * math.sqrt(365) if var > 0 else None

    max_consec_loss = max_consec_win = 0
    cl = cw = 0
    for p in pnls:
        if p <= 0:
            cl += 1
            cw = 0
            max_consec_loss = max(max_consec_loss, cl)
        else:
            cw += 1
            cl = 0
            max_consec_win = max(max_consec_win, cw)

    per_asset: dict[str, dict] = {}
    per_regime: dict[str, dict] = {}
    for t in closed:
        for key, bucket in (("symbol", per_asset),
                            ("regime", per_regime)):
            b = bucket.setdefault(str(t.get(key) or "unknown"),
                                  {"n": 0, "pnl": 0.0, "wins": 0})
            b["n"] += 1
            b["pnl"] += t["pnl_usd"]
            if t["pnl_usd"] > 0:
                b["wins"] += 1
    for bucket in (per_asset, per_regime):
        for b in bucket.values():
            b["win_rate"] = b["wins"] / b["n"] if b["n"] else 0.0

    m.update({
        "win_rate": len(wins) / n,
        "expectancy_r": sum(rs) / n,
        "expectancy_usd": sum(pnls) / n,
        "profit_factor": (gross_profit / gross_loss
                          if gross_loss > 0 else float("inf")),
        "avg_win_r": (sum(t["r_multiple"] for t in closed if t["pnl_usd"] > 0)
                      / len(wins) if wins else 0.0),
        "avg_loss_r": (sum(t["r_multiple"] for t in closed if t["pnl_usd"] <= 0)
                       / len(losses) if losses else 0.0),
        "avg_win_usd": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_usd": sum(losses) / len(losses) if losses else 0.0,
        "max_drawdown_pct": max_dd,
        "total_return_pct": (equity - start_equity) / start_equity * 100,
        "end_equity": equity,
        "total_fees_usd": sum(t["fees_usd"] for t in closed),
        "total_funding_usd": sum(t["funding_usd"] for t in closed),
        "max_consec_losses": max_consec_loss,
        "max_consec_wins": max_consec_win,
        "sharpe_daily": _sharpe(rets),
        "sortino_daily": _sortino(rets),
        "per_asset": per_asset,
        "per_regime": per_regime,
        "exit_reasons": {r: sum(1 for t in closed if t["exit_reason"] == r)
                         for r in ("TP_ALL", "SL", "MAX_HOLD")},
    })
    return m


# ------------------------------------------------------------------- runner
def run_backtest(symbols: list[str], start_ms: int, end_ms: int, step_h: int,
                 history: dict) -> tuple[list[dict], dict]:
    """Evaluate the production pipeline every step_h over [start_ms, end_ms].

    No pyramiding: a new signal for a symbol is skipped while a simulated
    position from an earlier signal on the same symbol is still open.
    """
    market = BacktestMarket(history)
    trades: list[dict] = []
    stats = {"n_evaluated": 0, "n_signals": 0, "reject_reasons": {}}
    equity = config.BACKTEST_START_EQUITY

    for symbol in symbols:
        c15 = history.get(symbol, {}).get("15m", [])
        if not c15:
            continue
        idx = {c["close_time"]: i for i, c in enumerate(c15)}
        # Evaluation points are ACTUAL 15m candle closes every step_h —
        # a wall-clock-aligned t would never match a candle close_time
        # (xx:14:59.999) and every signal would be silently skipped.
        step_candles = max(1, int(step_h * 4))
        points = [c["close_time"] for c in c15
                  if start_ms <= c["close_time"] <= end_ms][::step_candles]
        open_until: list[int] = []  # exit times of still-open positions
        for t in points:
            market.set_time(t)
            vetoes.reset_caches()  # wall-clock cache must not leak across t
            stats["n_evaluated"] += 1
            try:
                signal, note = evaluate_symbol(symbol, market)
            except Exception as e:  # noqa: BLE001 - never crash the sweep
                stats["reject_reasons"][f"EXC:{type(e).__name__}"] = \
                    stats["reject_reasons"].get(f"EXC:{type(e).__name__}", 0) + 1
                continue
            if signal is None:
                key = (note or "?").split("—")[0].strip()[:60]
                stats["reject_reasons"][key] = \
                    stats["reject_reasons"].get(key, 0) + 1
                continue
            stats["n_signals"] += 1
            i = idx[t]  # t is a real 15m close_time: always present
            if any(x > t for x in open_until):
                stats["reject_reasons"]["skip: position already open"] = \
                    stats["reject_reasons"].get("skip: position already open",
                                                0) + 1
                continue
            risk_usd = equity * config.RISK_PCT_PER_TRADE / 100.0
            trade = simulate_trade(signal, c15, i, risk_usd)
            trades.append(trade)
            if trade["status"] == "CLOSED":
                equity += trade["pnl_usd"]
                open_until.append(trade["exit_time"])
    return trades, stats


def _parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000)


def print_report(label: str, trades: list[dict], stats: dict) -> None:
    m = compute_metrics(trades, config.BACKTEST_START_EQUITY)
    print(f"\n===== BACKTEST [{label}] =====")
    print(f"evaluated points: {stats['n_evaluated']}, "
          f"signals: {stats['n_signals']}, "
          f"closed trades: {m.get('n_trades', 0)}, "
          f"no-entry: {m.get('n_no_entry', 0)}")
    if m.get("n_trades", 0) == 0:
        print("NO TRADES — nothing to measure.")
        return
    n = m["n_trades"]
    print(f"win rate: {m['win_rate']*100:.1f}% ({m['n_trades']} trades)")
    print(f"net expectancy: {m['expectancy_r']:+.3f} R "
          f"(${m['expectancy_usd']:+.2f}/trade)")
    pf = m["profit_factor"]
    print(f"profit factor: {'inf' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"avg win: {m['avg_win_r']:+.3f} R (${m['avg_win_usd']:+.2f}) | "
          f"avg loss: {m['avg_loss_r']:+.3f} R (${m['avg_loss_usd']:+.2f})")
    print(f"max drawdown: {m['max_drawdown_pct']:.2f}% | "
          f"total return (post-cost): {m['total_return_pct']:+.2f}% "
          f"(equity ${m['end_equity']:,.2f})")
    print(f"costs: fees ${m['total_fees_usd']:.2f}, "
          f"funding(est) ${m['total_funding_usd']:.2f}")
    print(f"max consec losses: {m['max_consec_losses']} | "
          f"max consec wins: {m['max_consec_wins']}")
    sh, so = m["sharpe_daily"], m["sortino_daily"]
    print(f"sharpe (daily, ann.): {'n/a' if sh is None else f'{sh:.2f}'} | "
          f"sortino: {'n/a' if so is None else f'{so:.2f}'}")
    print(f"exit reasons: {m['exit_reasons']}")
    print("per asset (n, win%, pnl): " +
          ", ".join(f"{a} ({b['n']}, {b['win_rate']*100:.0f}%, "
                    f"${b['pnl']:+.0f})"
                    for a, b in sorted(m["per_asset"].items())))
    print("per regime (n, win%, pnl): " +
          ", ".join(f"{r} ({b['n']}, {b['win_rate']*100:.0f}%, "
                    f"${b['pnl']:+.0f})"
                    for r, b in sorted(m["per_regime"].items())))
    if n < config.BACKTEST_MIN_TRADES:
        print(f"*** SAMPLE TOO SMALL: {n} < {config.BACKTEST_MIN_TRADES} "
              f"trades — metrics are NOT meaningful. ***")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Honest backtester (paper mode)")
    ap.add_argument("--symbols", default=",".join(config.UNIVERSE),
                    help="comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--days", type=int, default=config.BACKTEST_DEFAULT_DAYS,
                    help="lookback days ending now (default: split 70/30 IS/OOS)")
    ap.add_argument("--start", help="history start YYYY-MM-DD")
    ap.add_argument("--end", help="history end YYYY-MM-DD (default: today)")
    ap.add_argument("--in-sample", nargs=2, metavar=("START", "END"),
                    help="in-sample range YYYY-MM-DD YYYY-MM-DD")
    ap.add_argument("--out-of-sample", nargs=2, metavar=("START", "END"),
                    help="out-of-sample range YYYY-MM-DD YYYY-MM-DD")
    ap.add_argument("--step-h", type=int,
                    default=config.BACKTEST_DEFAULT_STEP_H,
                    help="signal evaluation step in hours")
    ap.add_argument("--cache", default="/tmp/backtest_history_cache.json")
    args = ap.parse_args(argv)

    symbols = [normalize_symbol(s) for s in args.symbols.split(",") if s.strip()]
    symbols = [s for s in symbols if s]
    if not symbols:
        print("no symbols")
        return 1

    now_ms = int(time.time() * 1000)
    if args.start:
        hist_start = _parse_date(args.start)
        hist_end = _parse_date(args.end) if args.end else now_ms
        days = max(1, int((hist_end - hist_start) / 86_400_000) + 2)
    else:
        days = args.days
        hist_end = now_ms
        hist_start = hist_end - days * 86_400_000

    history = load_history(symbols, days, args.cache)

    # Resolve in-sample / out-of-sample windows.
    if args.in_sample and args.out_of_sample:
        is_start, is_end = _parse_date(args.in_sample[0]), _parse_date(args.in_sample[1])
        oos_start, oos_end = (_parse_date(args.out_of_sample[0]),
                              _parse_date(args.out_of_sample[1]))
    else:
        is_start, is_end = hist_start, hist_start + int(0.7 * (hist_end - hist_start))
        oos_start, oos_end = is_end, hist_end

    windows = [("IN-SAMPLE", is_start, is_end),
               ("OUT-OF-SAMPLE", oos_start, oos_end)]
    results = []
    for label, ws, we in windows:
        print(f"\n>>> running {label}: "
              f"{datetime.fromtimestamp(ws/1000, tz=timezone.utc):%Y-%m-%d} "
              f"-> {datetime.fromtimestamp(we/1000, tz=timezone.utc):%Y-%m-%d}")
        trades, stats = run_backtest(symbols, ws, we, args.step_h, history)
        print_report(label, trades, stats)
        results.append((label, trades, stats))

    # ---- explicit caveats: never let the numbers speak alone ----
    print("\n===== CAVEATS (read before trusting any number above) =====")
    print("1. Derivative-dependent vetoes (V9 leverage, V10 funding, V11 "
          "squeeze, V12 whale, V13 liquidity wall) and warn deltas were "
          "SKIPPED: no historical derivatives feed exists. Real vetoes can "
          "only REJECT more signals, so every pass rate above is an UPPER "
          "BOUND on production selectivity.")
    print("2. Freshness used the signal-candle close as the live-price proxy.")
    print("3. Funding is a flat estimate (config), not historical funding.")
    print("4. Exits evaluated on 15m candles; intra-candle paths are "
          "approximated with the conservative SL-first rule.")
    print("5. No pyramiding: one open simulated position per symbol.")
    (_, is_trades, _), (_, oos_trades, _) = results
    is_m = compute_metrics(is_trades, config.BACKTEST_START_EQUITY)
    oos_m = compute_metrics(oos_trades, config.BACKTEST_START_EQUITY)
    is_n = is_m.get("n_trades", 0)
    oos_n = oos_m.get("n_trades", 0)
    if is_n >= config.BACKTEST_MIN_TRADES and oos_n >= config.BACKTEST_MIN_TRADES:
        same_sign = (is_m["expectancy_r"] > 0) == (oos_m["expectancy_r"] > 0)
        print(f"6. Walk-forward stability: IS expectancy "
              f"{is_m['expectancy_r']:+.3f} R vs OOS {oos_m['expectancy_r']:+.3f} R "
              f"-> {'SAME SIGN (stable)' if same_sign else 'SIGN FLIP (UNSTABLE)'}")
    else:
        print(f"6. Walk-forward stability: CANNOT ASSESS — IS trades={is_n}, "
              f"OOS trades={oos_n} (need >= {config.BACKTEST_MIN_TRADES} each).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
