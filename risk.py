# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Risk management: kill switch, position caps, and the paper-trading broker.

RiskManager is exchange-agnostic: it works off the StateStore plus a
balance callback, so the same rules guard paper, testnet, and (later) live.

PaperBroker mirrors the ExchangeClient interface (duck-typed) and simulates
fills from live prices. Fills are honest: MARKET fills immediately at the
live price; LIMIT/STOP_MARKET rest until the live price touches them; a
0.05% taker fee is charged per fill (same assumption as backtest.py).
"""

from __future__ import annotations

import datetime
import itertools
import logging
import math
import time

import config

log = logging.getLogger("executor.risk")

_TAKER_FEE_PCT = 0.05  # per fill, matches BACKTEST_TAKER_FEE_PCT


def utc_today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _step_decimals(step: float) -> int:
    """Decimal places implied by a step/tick size (0.001 -> 3, 1 -> 0)."""
    s = f"{step:.12f}".rstrip("0").rstrip(".")
    return len(s.partition(".")[2]) if "." in s else 0


def floor_to_step(qty: float, step: float) -> float:
    """Round quantity DOWN to the exchange step size (never up).

    Also strips IEEE-754 artifacts (e.g. 70*0.1 -> 7.000000000000001):
    the result is quantized to the step's own decimal places, so the
    value sent to the exchange never exceeds its precision (-1111).
    """
    if step <= 0:
        return qty
    floored = math.floor(qty / step + 1e-12) * step
    return round(floored, _step_decimals(step))


def quantize_price(px: float, tick: float) -> float:
    """Round a price to the exchange tick size, stripping float artifacts.

    Replaces the naive round(px/tick)*tick pattern, which can emit values
    like 0.055990000000000005 that Binance rejects with -1111.
    """
    if not tick or tick <= 0 or px is None:
        return px
    return round(round(px / tick) * tick, _step_decimals(tick))


class RiskManager:
    """Daily-loss kill switch + concurrency/exposure caps."""

    def __init__(self, state, get_balance,
                 max_positions: int = config.MAX_CONCURRENT_POSITIONS,
                 daily_loss_cap_pct: float = config.DAILY_LOSS_CAP_PCT,
                 per_symbol_cap_pct: float = config.EXEC_MAX_EXPOSURE_PCT_PER_SYMBOL,
                 get_wallet_balance=None, get_equity=None):
        self.state = state
        self.get_balance = get_balance  # () -> float | None (available)
        # H1: loss metrics must NEVER use availableBalance — locked initial
        # margin moves it without any real loss. Wallet = realized value
        # only; equity = wallet + unrealized PnL. Default to get_balance
        # when the caller provides no dedicated getters (tests, simple
        # setups).
        self.get_wallet_balance = get_wallet_balance or get_balance
        self.get_equity = get_equity or get_balance
        self.max_positions = max_positions
        self.daily_loss_cap_pct = daily_loss_cap_pct
        self.per_symbol_cap_pct = per_symbol_cap_pct

    # ------------------------------------------------------------- day roll
    def ensure_day(self) -> str:
        """Roll the daily bookkeeping to today (UTC); returns today's date."""
        today = utc_today()
        daily = self.state.get("daily", {})
        if daily.get("date") != today:
            balance = self.get_wallet_balance()
            if balance is None:
                # M3: never roll the bookkeeping on a failed read. Keeping
                # yesterday's record means the loss guard keeps working off
                # the last known start balance instead of going blind.
                log.warning("day rollover: wallet balance unavailable - "
                            "keeping previous day record")
                return today
            self.state.set("daily", {
                "date": today,
                "start_balance": balance,
                "realized_pnl": 0.0,
            })
            # A new UTC day lifts the kill switch.
            ks = self.state.get("kill_switch", {})
            if ks.get("active") and ks.get("date") != today:
                self.state.set("kill_switch", {
                    "date": today, "active": False, "reason": "",
                })
                log.info("kill switch reset for new UTC day %s", today)
            self.state.save()
        return today

    def kill_switch_active(self) -> bool:
        ks = self.state.get("kill_switch", {})
        return bool(ks.get("active")) and ks.get("date") == utc_today()

    def engage_kill_switch(self, reason: str) -> None:
        self.state.set("kill_switch", {
            "date": utc_today(), "active": True, "reason": reason,
        })
        self.state.save()
        log.warning("KILL SWITCH ENGAGED: %s", reason)

    def check_daily_loss(self) -> tuple[bool, str]:
        """Returns (ok, reason). Engages the kill switch on breach."""
        if self.kill_switch_active():
            return False, "kill switch already active today"
        daily = self.state.get("daily", {})
        start = daily.get("start_balance") or 0.0
        if start <= 0:
            # M3 fail-closed: without a start-of-day anchor we cannot prove
            # we are within the loss cap, so block new entries.
            return False, ("no start-of-day balance recorded yet - "
                           "holding (fail-closed)")
        # H1: wallet balance, not availableBalance (locked margin is not a loss).
        balance = self.get_wallet_balance()
        if balance is None:
            # Fail-closed: without a balance we cannot prove we are safe.
            return False, "balance unavailable - holding (fail-closed)"
        # Read the cap live from config so dashboard overrides apply;
        # constructor value is the fallback when config is unavailable.
        cap_pct = getattr(config, "DAILY_LOSS_CAP_PCT",
                          self.daily_loss_cap_pct)
        loss_pct = (start - balance) / start * 100
        if loss_pct >= cap_pct:
            reason = (f"daily loss {loss_pct:.2f}% >= cap "
                      f"{cap_pct}% "
                      f"(start ${start:,.2f} -> now ${balance:,.2f})")
            self.engage_kill_switch(reason)
            return False, reason
        return True, f"daily PnL {balance - start:+.2f} USDT ({-loss_pct:+.2f}%)"

    def check_max_drawdown(self) -> tuple[bool, str]:
        """Circuit breaker: stops trading if equity drops N% from peak balance.

        Tracks a high-watermark of account balance in state["risk_metrics"].
        When drawdown from the peak reaches MAX_DRAWDOWN_CAP_PCT, engages
        the kill switch. Disabled when the knob is 0 (default).
        """
        cap_pct = float(getattr(config, "MAX_DRAWDOWN_CAP_PCT", 0.0) or 0.0)
        if cap_pct <= 0:
            return True, "drawdown guard disabled"
        metrics = self.state.get("risk_metrics", {})
        hwm = float(metrics.get("high_watermark") or 0.0)
        # H1: drawdown is measured on equity, not availableBalance.
        balance = self.get_equity()
        if balance is None:
            return False, "balance unavailable - holding (fail-closed)"
        if balance > hwm:
            metrics["high_watermark"] = balance
            self.state.set("risk_metrics", metrics)
            self.state.save()
            return True, f"new high watermark: ${balance:,.2f}"
        if hwm <= 0:
            metrics["high_watermark"] = balance
            self.state.set("risk_metrics", metrics)
            self.state.save()
            return True, f"initial high watermark: ${balance:,.2f}"
        dd_pct = (hwm - balance) / hwm * 100.0
        if dd_pct >= cap_pct:
            reason = (f"max drawdown breach: {dd_pct:.2f}% >= cap {cap_pct:g}% "
                      f"(peak ${hwm:,.2f} -> now ${balance:,.2f})")
            self.engage_kill_switch(reason)
            return False, reason
        return True, f"drawdown from peak: {dd_pct:.2f}% (safe < {cap_pct:g}%)"

    def check_consecutive_losses(self) -> tuple[bool, str]:
        """True if not currently paused by consecutive-losses cooldown.

        After MAX_CONSECUTIVE_LOSSES losing trades in a row, new entries
        are paused for CONSECUTIVE_LOSS_COOLDOWN_HOURS. The pause is
        recorded in state["risk_metrics"]["cooldown_until_ms"].
        """
        metrics = self.state.get("risk_metrics", {})
        cooldown_until = int(metrics.get("cooldown_until_ms") or 0)
        now_ms = int(time.time() * 1000)
        if cooldown_until > now_ms:
            rem_m = (cooldown_until - now_ms) / 60_000
            losses = metrics.get("consecutive_losses", 0)
            return False, (f"consecutive loss cooldown active after {losses} losses "
                           f"({rem_m:.1f}m remaining)")
        return True, "no cooldown active"

    # ------------------------------------------------------------ new entry
    def check_can_open(self, symbol: str, notional: float,
                       open_count: int) -> tuple[bool, str]:
        # Ensure day bookkeeping is current on every entry path (scan_once,
        # scan_from_db, manual_entry). Idempotent; required for the
        # fail-closed start_balance semantics in check_daily_loss.
        self.ensure_day()
        if self.kill_switch_active():
            return False, "kill switch active - no new entries today"
        ok, reason = self.check_daily_loss()
        if not ok:
            return False, reason
        ok_dd, reason_dd = self.check_max_drawdown()
        if not ok_dd:
            return False, reason_dd
        ok_cool, reason_cool = self.check_consecutive_losses()
        if not ok_cool:
            return False, reason_cool
        if open_count >= self.max_positions:
            return False, (f"max concurrent positions reached "
                           f"({open_count}/{self.max_positions})")
        # Exposure cap is measured against account equity (wallet + unrealized),
        # falling back to balance when equity getter is unavailable.
        balance = (self.get_equity() if callable(self.get_equity) else None) or self.get_balance()
        if balance is None or balance <= 0:
            return False, "balance unavailable - holding (fail-closed)"
        # Read the cap live from config so dashboard overrides apply;
        # constructor value is the fallback when config is unavailable.
        cap_pct = getattr(config, "EXEC_MAX_EXPOSURE_PCT_PER_SYMBOL",
                          self.per_symbol_cap_pct)
        cap = balance * cap_pct / 100
        if notional > cap:
            return False, (f"notional ${notional:,.2f} exceeds per-symbol cap "
                           f"${cap:,.2f} ({cap_pct}% of balance)")

        # Sector / Cluster Concentration Guard:
        # Prevents opening multiple positions in the same sector (e.g. multiple Meme coins)
        # to avoid highly correlated liquidation cascades.
        max_per_sector = int(getattr(config, "MAX_POSITIONS_PER_SECTOR", 1) or 0)
        if max_per_sector > 0:
            get_sec = getattr(config, "get_symbol_sector", None)
            sector = get_sec(symbol) if callable(get_sec) else None
            if sector and self.state and hasattr(self.state, "get"):
                open_positions = self.state.get("positions", {}) or {}
                sector_positions = []
                for p in open_positions.values():
                    if not p.get("closed") and p.get("symbol"):
                        p_sym = p.get("symbol")
                        if callable(get_sec) and get_sec(p_sym) == sector:
                            sector_positions.append(p_sym)
                if len(sector_positions) >= max_per_sector:
                    return False, (f"sector concentration limit reached for '{sector}': "
                                   f"already holding {', '.join(sector_positions)} "
                                   f"(max {max_per_sector} per sector)")

        return True, "ok"

    def record_realized(self, pnl: float) -> None:
        daily = self.state.get("daily", {})
        daily["realized_pnl"] = (daily.get("realized_pnl") or 0.0) + pnl
        self.state.set("daily", daily)

        # Update consecutive losses & cooldown (user feature 2026-10-16):
        # after MAX_CONSECUTIVE_LOSSES losing trades, pause new entries
        # for CONSECUTIVE_LOSS_COOLDOWN_HOURS. A winning trade resets
        # the counter. Breakeven (pnl == 0) leaves the counter unchanged.
        metrics = self.state.get("risk_metrics", {})
        losses = int(metrics.get("consecutive_losses") or 0)
        max_losses = int(getattr(config, "MAX_CONSECUTIVE_LOSSES", 3) or 3)
        cooldown_h = float(getattr(config, "CONSECUTIVE_LOSS_COOLDOWN_HOURS", 4.0) or 4.0)

        if pnl < 0:
            losses += 1
            metrics["consecutive_losses"] = losses
            if max_losses > 0 and losses >= max_losses and cooldown_h > 0:
                until_ms = int(time.time() * 1000 + cooldown_h * 3600 * 1000)
                metrics["cooldown_until_ms"] = until_ms
                log.warning("CONSECUTIVE LOSS COOLDOWN: %d losses in a row -> paused for %gh",
                            losses, cooldown_h)
        elif pnl > 0:
            metrics["consecutive_losses"] = 0

        self.state.set("risk_metrics", metrics)
        self.state.save()


# ---------------------------------------------------------------------------
# Paper broker - duck-typed mirror of ExchangeClient for --paper runs.
# ---------------------------------------------------------------------------

class PaperBroker:
    """Simulated futures broker. Supports LONG and SHORT positions.

    Orders rest until the live price touches them:
      BUY  MARKET        -> fills immediately at live price
      SELL MARKET        -> fills immediately at live price
      BUY  LIMIT @p      -> fills when live <= p
      SELL LIMIT @p      -> fills when live >= p
      BUY  STOP_MARKET @s (reduceOnly) -> fills when live >= s (short stop)
      SELL STOP_MARKET @s (reduceOnly) -> fills when live <= s (long stop)

    Position qty is signed: positive = LONG, negative = SHORT.
    """

    _ids = itertools.count(1)

    def __init__(self, start_balance: float, price_fn, fee_pct: float = _TAKER_FEE_PCT):
        self.balance = float(start_balance)
        self._price_fn = price_fn  # (symbol) -> float | None
        self.fee_pct = fee_pct
        self.positions: dict[str, dict] = {}   # symbol -> {qty, avg_price}
        self.orders: dict[int, dict] = {}      # orderId -> order
        self.realized_pnl = 0.0

    # ------------------------------------------------- ExchangeClient iface
    def get_balance(self, asset: str = "USDT") -> float | None:
        return self.balance

    def get_wallet_balance(self, asset: str = "USDT") -> float | None:
        """Realized wallet balance (excludes unrealized PnL).

        Paper mirror of ExchangeClient.get_wallet_balance: self.balance only
        moves on realized PnL (fills, fees), never on unrealized moves — the
        exact semantics the H1 risk fix requires for loss metrics. Without
        this method run_executor.py crashes at paper-mode startup
        (AttributeError wiring get_wallet_balance into RiskManager).
        """
        return self.balance

    def get_margin_balance(self) -> float | None:
        """Account equity: wallet balance + open unrealized PnL."""
        eq = self.balance
        for symbol, p in self.positions.items():
            qty = p.get("qty", 0.0)
            if abs(qty) < 1e-12:
                continue
            live = self._price_fn(symbol)
            if not live:
                continue
            sign = 1.0 if qty > 0 else -1.0
            eq += sign * (live - p["avg_price"]) * abs(qty)
        return eq

    def get_positions(self) -> list[dict]:
        out = []
        for symbol, p in self.positions.items():
            if abs(p.get("qty", 0.0)) < 1e-12:
                continue
            out.append({
                "symbol": symbol,
                "side": "LONG" if p["qty"] > 0 else "SHORT",
                "qty": abs(p["qty"]),
                "entry_price": p["avg_price"], "unrealized_pnl": 0.0,
                "leverage": config.MAX_LEVERAGE,
            })
        return out


    def set_leverage(self, symbol: str, leverage: int) -> bool:
        return True

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        return True

    def get_symbol_filters(self, symbol: str) -> dict | None:
        # Paper mode still needs realistic filters; the executor injects the
        # real ones in testnet mode. Permissive default here.
        return {"status": "TRADING", "tickSize": 0.01, "stepSize": 0.001,
                "minQty": 0.001, "maxQty": 1e9, "minNotional": 5.0}

    def place_order(self, symbol: str, side: str, type: str,
                    quantity: float | None = None,
                    price: float | None = None,
                    stop_price: float | None = None,
                    time_in_force: str = "GTC",
                    reduce_only: bool = False,
                    working_type: str = "MARK_PRICE",
                    client_order_id: str | None = None) -> dict | None:
        oid = next(self._ids)
        order = {
            "orderId": oid, "symbol": symbol, "side": side, "type": type,
            "origQty": quantity or 0.0, "price": price or 0.0,
            "stopPrice": stop_price or 0.0,
            "reduceOnly": reduce_only,
            "clientOrderId": client_order_id or f"paper_{oid}",
            "status": "NEW",
        }
        live = self._price_fn(symbol)
        if type == "MARKET" and live:
            self._fill(order, live)
        # Always store the order: the executor polls entry fills via
        # get_order(), and an immediately-filled MARKET order must be
        # visible there too (otherwise brackets are never placed).
        self.orders[oid] = order
        return {"orderId": oid, "clientOrderId": order["clientOrderId"],
                "status": order["status"]}

    def get_order(self, symbol: str, order_id: int | None = None,
                  orig_client_order_id: str | None = None) -> dict | None:
        for o in self.orders.values():
            if order_id is not None and o["orderId"] == order_id:
                return dict(o)
            if orig_client_order_id and o["clientOrderId"] == orig_client_order_id:
                return dict(o)
        return None

    def cancel_order(self, symbol: str, order_id: int | None = None,
                     orig_client_order_id: str | None = None) -> bool:
        for oid, o in list(self.orders.items()):
            if ((order_id is not None and oid == order_id) or
                    (orig_client_order_id and
                     o["clientOrderId"] == orig_client_order_id)):
                if o["status"] == "NEW":
                    o["status"] = "CANCELED"
                return True
        return True  # already gone

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        return [dict(o) for o in self.orders.values()
                if o["status"] == "NEW"
                and (symbol is None or o["symbol"] == symbol)]

    # ------------------------------------------- algo (conditional) orders
    # Paper mirror of the Algo Order API. Simulated fills treat conditional
    # orders like their regular counterparts (STOP_MARKET fills like the
    # paper STOP_MARKET branch in poll()).
    def place_algo_order(self, symbol: str, side: str, type: str,
                         quantity: float | None = None,
                         trigger_price: float | None = None,
                         price: float | None = None,
                         reduce_only: bool = False,
                         working_type: str = "MARK_PRICE",
                         time_in_force: str = "GTE_GTC",
                         client_algo_id: str | None = None) -> dict | None:
        o = self.place_order(symbol, side, type, quantity=quantity,
                             price=price,
                             stop_price=trigger_price,
                             reduce_only=reduce_only,
                             working_type=working_type,
                             client_order_id=client_algo_id)
        if not o:
            return None
        oid = o["orderId"]
        self.orders[oid]["algoId"] = oid
        return {"algoId": oid, "symbol": symbol, "side": side, "type": type,
                "status": self.orders[oid]["status"]}

    def get_algo_order(self, symbol: str, algo_id: int) -> dict | None:
        o = self.orders.get(algo_id)
        if o and o["symbol"] == symbol:
            d = dict(o)
            d["algoStatus"] = d.get("status")
            return d
        return None

    def cancel_algo_order(self, symbol: str, algo_id: int) -> bool:
        return self.cancel_order(symbol, order_id=algo_id)

    def get_open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        return [dict(o) for o in self.orders.values()
                if o["status"] == "NEW" and o.get("algoId") is not None
                and (symbol is None or o["symbol"] == symbol)]

    # ------------------------------------------------------------- matching
    def poll(self, symbols: list[str]) -> list[dict]:
        """Advance the simulation: fill resting orders against live prices.

        Returns fill events: {order, fill_price, fill_qty}.
        """
        fills = []
        for symbol in symbols:
            live = self._price_fn(symbol)
            if not live or live <= 0:
                continue
            for o in list(self.orders.values()):
                if o["symbol"] != symbol or o["status"] != "NEW":
                    continue
                fill_px = None
                if o["type"] == "LIMIT" and o["side"] == "BUY" and live <= o["price"]:
                    fill_px = o["price"]
                elif (o["type"] == "LIMIT" and o["side"] == "SELL"
                      and live >= o["price"]):
                    fill_px = o["price"]
                elif (o["type"] == "STOP_MARKET" and o["side"] == "SELL"
                      and live <= o["stopPrice"]):
                    fill_px = o["stopPrice"]
                elif (o["type"] == "STOP_MARKET" and o["side"] == "BUY"
                      and live >= o["stopPrice"]):
                    fill_px = o["stopPrice"]
                if fill_px:
                    fills.append(self._fill(o, fill_px))
        return fills

    def _fill(self, order: dict, fill_px: float) -> dict:
        order["status"] = "FILLED"
        order["avgPrice"] = fill_px
        qty = order["origQty"]
        fee = fill_px * qty * self.fee_pct / 100
        symbol = order["symbol"]
        signed = qty if order["side"] == "BUY" else -qty
        if not order["reduceOnly"]:
            pos = self.positions.setdefault(
                symbol, {"qty": 0.0, "avg_price": 0.0})
            new_qty = pos["qty"] + signed
            pos["avg_price"] = (
                (pos["avg_price"] * pos["qty"] + fill_px * signed) / new_qty
                if new_qty != 0 else 0.0)
            pos["qty"] = new_qty
            self.balance -= fee
        else:
            pos = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
            close_qty = min(qty, abs(pos["qty"]))
            # LONG (qty>0): (fill - avg) * qty; SHORT (qty<0): (avg - fill) * qty
            sign = 1.0 if pos["qty"] > 0 else -1.0
            pnl = sign * (fill_px - pos["avg_price"]) * close_qty - fee
            if pos["qty"] > 0:
                pos["qty"] -= close_qty  # LONG: sells reduce toward zero
            else:
                pos["qty"] += close_qty  # SHORT: buys reduce toward zero
            if abs(pos["qty"]) < 1e-12:
                pos["qty"] = 0.0
                pos["avg_price"] = 0.0
            self.balance += pnl

            self.realized_pnl += pnl
            order["realizedPnl"] = pnl
        return {"order": dict(order), "fill_price": fill_px, "fill_qty": qty}
