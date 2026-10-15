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
                 per_symbol_cap_pct: float = config.EXEC_MAX_EXPOSURE_PCT_PER_SYMBOL):
        self.state = state
        self.get_balance = get_balance  # () -> float | None
        self.max_positions = max_positions
        self.daily_loss_cap_pct = daily_loss_cap_pct
        self.per_symbol_cap_pct = per_symbol_cap_pct

    # ------------------------------------------------------------- day roll
    def ensure_day(self) -> str:
        """Roll the daily bookkeeping to today (UTC); returns today's date."""
        today = utc_today()
        daily = self.state.get("daily", {})
        if daily.get("date") != today:
            balance = self.get_balance() or 0.0
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
            return True, "no start-of-day balance recorded yet"
        balance = self.get_balance()
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

    # ------------------------------------------------------------ new entry
    def check_can_open(self, symbol: str, notional: float,
                       open_count: int) -> tuple[bool, str]:
        if self.kill_switch_active():
            return False, "kill switch active - no new entries today"
        ok, reason = self.check_daily_loss()
        if not ok:
            return False, reason
        if open_count >= self.max_positions:
            return False, (f"max concurrent positions reached "
                           f"({open_count}/{self.max_positions})")
        balance = self.get_balance()
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
        return True, "ok"

    def record_realized(self, pnl: float) -> None:
        daily = self.state.get("daily", {})
        daily["realized_pnl"] = (daily.get("realized_pnl") or 0.0) + pnl
        self.state.set("daily", daily)
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

    def get_positions(self) -> list[dict]:
        out = []
        for symbol, p in self.positions.items():
            if p["qty"] == 0:
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
            self.balance += pnl
            self.realized_pnl += pnl
            order["realizedPnl"] = pnl
        return {"order": dict(order), "fill_price": fill_px, "fill_qty": qty}
