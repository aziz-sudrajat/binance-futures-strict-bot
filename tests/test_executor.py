# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for the Phase 2 executor (mocked exchange — NO real orders)."""

import itertools
import json
import os

import pytest

import config
import executor as ex
from executor import Executor, compute_qty, compute_test_qty, split_tp_qty, make_signal_id
from lifecycle import LifecycleLogger
from risk import PaperBroker, RiskManager, floor_to_step, utc_today
from run_executor import require_testnet_keys
from state import StateStore
import risk as risk_mod


# ------------------------------------------------------------------ fakes

class FakeMarket:
    def __init__(self, live=100.0, close_ms=1_728_900_000_000 - 1):
        self.live = live
        self.close_ms = close_ms

    def get_live_price(self, symbol):
        return self.live

    def get_klines(self, symbol, interval, limit=220):
        return [{"close_time": self.close_ms, "close": self.live}]


class FakeExchange:
    """Controllable stand-in for ExchangeClient."""

    def __init__(self, balance=10_000.0):
        self._balance = balance
        self.market_price = {}
        self.orders = {}
        self._pos = {}
        self._ids = itertools.count(1000)
        self.filters = {"status": "TRADING", "tickSize": 0.1, "stepSize": 0.001,
                        "minQty": 0.001, "maxQty": 100000.0, "minNotional": 5.0}
        self.margin_calls = []
        self.leverage_calls = []
        self.user_trades = []  # ground-truth fills for _classify_vanished_close

    def get_balance(self, asset="USDT"):
        return self._balance

    def get_positions(self):
        return list(self.exchange_positions)

    def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return True

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        self.margin_calls.append((symbol, margin_type))
        return True

    def get_symbol_filters(self, symbol):
        return dict(self.filters)

    def place_order(self, symbol, side, type, quantity=None, price=None,
                    stop_price=None, time_in_force="GTC", reduce_only=False,
                    working_type="MARK_PRICE", client_order_id=None):
        oid = next(self._ids)
        o = {"orderId": oid, "symbol": symbol, "side": side, "type": type,
             "origQty": quantity or 0.0, "price": price or 0.0,
             "stopPrice": stop_price or 0.0, "reduceOnly": reduce_only,
             "clientOrderId": client_order_id or f"fake_{oid}",
             "status": "NEW", "executedQty": 0.0, "avgPrice": 0.0}
        if type == "MARKET":
            px = self.market_price.get(symbol, price or 0.0)
            o.update(status="FILLED", executedQty=quantity, avgPrice=px)
            self._apply_fill(symbol, quantity, reduce_only)
        self.orders[oid] = o
        return {"orderId": oid, "clientOrderId": o["clientOrderId"],
                "status": o["status"]}

    def _apply_fill(self, symbol, qty, reduce_only):
        if reduce_only:
            self._pos[symbol] = self._pos.get(symbol, 0.0) - (qty or 0.0)
        else:
            self._pos[symbol] = self._pos.get(symbol, 0.0) + (qty or 0.0)

    def get_positions(self):
        return [{"symbol": s, "side": "LONG", "qty": q, "entry_price": 0.0,
                 "unrealized_pnl": 0.0, "leverage": 3}
                for s, q in self._pos.items() if q > 0]

    def get_user_trades(self, symbol, limit=20, start_time=None):
        trades = [t for t in self.user_trades if t.get("symbol") == symbol]
        if start_time:
            trades = [t for t in trades if t.get("time", 0) >= start_time]
        return trades[:limit]

    def get_order(self, symbol, order_id=None, orig_client_order_id=None):
        for o in self.orders.values():
            if "orderId" not in o:  # algo orders live here too; skip them
                continue
            if order_id is not None and o["orderId"] == order_id:
                return dict(o)
            if orig_client_order_id and o["clientOrderId"] == orig_client_order_id:
                return dict(o)
        return None

    def cancel_order(self, symbol, order_id=None, orig_client_order_id=None):
        for o in self.orders.values():
            if "orderId" not in o:
                continue
            if ((order_id is not None and o["orderId"] == order_id) or
                    (orig_client_order_id and
                     o["clientOrderId"] == orig_client_order_id)):
                if o["status"] == "NEW":
                    o["status"] = "CANCELED"
                return True
        return True

    def get_open_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values()
                if "orderId" in o and o["status"] == "NEW"
                and (symbol is None or o["symbol"] == symbol)]

    # algo-order stand-ins (mirror ExchangeClient.place/cancel/get_algo_order)
    def place_algo_order(self, symbol, side, type, quantity=None,
                         trigger_price=None, price=None, reduce_only=False,
                         working_type="MARK_PRICE", time_in_force="GTE_GTC",
                         client_algo_id=None):
        aid = next(self._ids)
        o = {"algoId": aid, "symbol": symbol, "side": side, "type": type,
             "origQty": quantity or 0.0, "triggerPrice": trigger_price or 0.0,
             "price": price or 0.0, "reduceOnly": reduce_only,
             "clientAlgoId": client_algo_id or f"fake_algo_{aid}",
             "status": "NEW"}
        self.orders[aid] = o
        return {"algoId": aid, "clientAlgoId": o["clientAlgoId"],
                "status": o["status"]}

    def cancel_algo_order(self, symbol, algo_id):
        o = self.orders.get(algo_id)
        if o is not None and o["status"] == "NEW":
            o["status"] = "CANCELED"
        return True

    def get_algo_order(self, symbol, algo_id):
        o = self.orders.get(algo_id)
        return dict(o) if o is not None else None

    # test helper: force an order to FILLED
    def fill(self, order_id, px, qty=None):
        o = self.orders[order_id]
        o["status"] = "FILLED"
        o["executedQty"] = qty if qty is not None else o["origQty"]
        o["avgPrice"] = px
        self._apply_fill(o["symbol"], o["executedQty"], o["reduceOnly"])
        return o


def _signal(symbol="BTCUSDT", entry=100.0):
    return {"symbol": symbol, "direction": "LONG", "interval": "15m",
            "entry": entry, "entry_low": 99.5, "entry_high": 100.5,
            "stop_loss": 98.0, "tp1": 103.0, "tp2": 104.5, "tp3": 106.0,
            "quality": 80}


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def rig(workdir):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ev = lambda symbol, m: (_signal(symbol), "stub")  # noqa: E731
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"], evaluate_fn=ev)
    return executor, market, exchange, state, lifecycle, risk


# ------------------------------------------------------------------- sizing

def test_sizing_math():
    # $10k balance, 1% risk = $100; stop distance $2 -> raw qty 50.
    # Per-symbol exposure cap (20% of $10k = $2000 notional) binds:
    # 50 * $100 = $5000 -> capped to $2000 / $100 = 20.
    filters = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
    qty, note = compute_qty(10_000.0, 100.0, 98.0, filters)
    assert qty == 20.0, note
    assert qty * 100.0 <= 2000.0 + 1e-9
    # Without the cap binding: wide stop -> small qty, cap not hit.
    qty2, _ = compute_qty(10_000.0, 100.0, 90.0, filters)  # $10 dist
    assert qty2 == 10.0  # $100 / $10, notional $1000 < $2000 cap


def test_sizing_floors_down():
    filters = {"stepSize": 0.5, "minQty": 0.5, "minNotional": 5.0}
    qty, _ = compute_qty(10_000.0, 100.0, 98.0, filters)
    # raw 50 -> cap 2000/100=20 -> floor(20/0.5)*0.5 = 20.0
    assert qty == 20.0
    assert floor_to_step(7.89, 0.5) == 7.5  # never rounds up


def test_sizing_rounds_to_zero_skips():
    filters = {"stepSize": 1.0, "minQty": 1.0, "minNotional": 5.0}
    qty, reason = compute_qty(100.0, 100.0, 99.99, filters)
    assert qty == 0.0 and "minQty" in reason


def test_tp_split_ratios():
    splits, note = split_tp_qty(20.0, 0.001, 0.001)
    assert splits == [8.0, 6.0, 6.0], note
    assert abs(sum(splits) - 20.0) < 1e-9


def test_tp_split_step_aligned():
    splits, _ = split_tp_qty(7.0, 0.5, 0.5)
    assert splits == [2.5, 2.0, 2.5]
    for s in splits:
        assert abs(s / 0.5 - round(s / 0.5)) < 1e-9


def test_tp_split_unexitable_aborts():
    # qty so small that even TP3 < minQty after merging -> abort.
    splits, reason = split_tp_qty(0.0015, 0.001, 0.001)
    assert splits is None and "abort" in reason


# ------------------------------------------------------------- idempotency

def test_idempotent_rescan(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1
    sid = make_signal_id("BTCUSDT", market.close_ms, "LONG")
    assert state.seen_signal(sid)
    summary2 = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
                "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary2)
    assert summary2["opened"] == 0 and summary2["rejected"] == 1
    events = lifecycle.read_all()
    states = [e["state"] for e in events if e["signal_id"] == sid]
    assert "approved" in states and "rejected" in states


def test_signal_id_changes_with_candle(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    sid1 = make_signal_id("BTCUSDT", market.close_ms, "LONG")
    market.close_ms += 900_000  # next 15m candle
    sid2 = make_signal_id("BTCUSDT", market.close_ms, "LONG")
    assert sid1 != sid2


# --------------------------------------------------------------- open flow

def test_open_flow_places_brackets(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1
    positions = executor._open_positions()
    assert len(positions) == 1
    pos_id, pos = next(iter(positions.items()))
    assert pos["brackets_placed"] is True
    assert pos["sl_order_id"] is not None
    assert all(pos["tp"][leg]["order_id"] for leg in ("tp1", "tp2", "tp3"))
    # margin was set to ISOLATED
    assert ("BTCUSDT", "ISOLATED") in exchange.margin_calls
    # lifecycle trail
    states = [e["state"] for e in lifecycle.read_all()]
    assert "position_opened" in states


def test_breakeven_after_tp1(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    old_sl = pos["sl_order_id"]
    # Simulate TP1 filling on the exchange.
    exchange.fill(pos["tp"]["tp1"]["order_id"], 103.0)
    executor._manage_one(pos_id, pos, 10_000.0, summary)
    pos = executor._open_positions()[pos_id]
    assert pos["breakeven_moved"] is True
    assert pos["sl_order_id"] != old_sl
    # breakeven SL goes through the algo endpoint (HTTP 400 -4120 forbids
    # STOP_MARKET on /fapi/v1/order)
    assert pos["sl_is_algo"] is True
    new_sl = exchange.get_algo_order("BTCUSDT", pos["sl_order_id"])
    assert new_sl["triggerPrice"] == pos["entry_price"]  # == 100.0 (breakeven)
    # the original SL was an algo order -> must be canceled via the algo route
    assert exchange.get_algo_order("BTCUSDT", old_sl)["status"] == "CANCELED"
    states = [e["state"] for e in lifecycle.read_all()]
    assert "tp1_hit" in states


def test_breakeven_failure_keeps_original_sl(rig, monkeypatch):
    """Regression: if the breakeven algo SL fails (-4120-style), the original
    SL must stay live — the position must never be left unprotected."""
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    old_sl = pos["sl_order_id"]
    exchange.fill(pos["tp"]["tp1"]["order_id"], 103.0)
    # Simulate the -4120 rejection on the breakeven placement only.
    orig_place_algo = exchange.place_algo_order
    def failing_place_algo(*a, **k):
        return None
    monkeypatch.setattr(exchange, "place_algo_order", failing_place_algo)
    executor._manage_one(pos_id, pos, 10_000.0, summary)
    pos = executor._open_positions()[pos_id]
    assert pos["breakeven_moved"] is False
    assert pos["sl_order_id"] == old_sl
    # original SL still live — never canceled, never orphaned
    assert exchange.get_algo_order("BTCUSDT", old_sl)["status"] == "NEW"


# ------------------------------------------------------------- risk manager

def test_kill_switch_triggers_and_resets(workdir, monkeypatch):
    state = StateStore(os.path.join(workdir, "state.json"))
    balance = {"v": 10_000.0}
    rm = RiskManager(state, lambda: balance["v"])
    rm.ensure_day()
    # -4% day -> breach of the 3% cap.
    balance["v"] = 9_600.0
    ok, reason = rm.check_daily_loss()
    assert ok is False and rm.kill_switch_active()
    ok2, _ = rm.check_can_open("BTCUSDT", 100.0, 0)
    assert ok2 is False
    # Next UTC day -> reset.
    monkeypatch.setattr(risk_mod, "utc_today", lambda: "2099-01-02")
    rm.ensure_day()
    assert rm.kill_switch_active() is False
    ok3, _ = rm.check_daily_loss()
    assert ok3 is True


def test_widened_loss_cap_keeps_trading_alive(workdir, monkeypatch):
    # User 2026-10-15 "habiskan saja saldonya": with the cap widened to
    # 100 via the dashboard knob, a -17.5% day no longer trips the kill
    # switch and new entries stay allowed.
    state = StateStore(os.path.join(workdir, "state.json"))
    balance = {"v": 5.32}
    rm = RiskManager(state, lambda: balance["v"])
    rm.ensure_day()
    balance["v"] = 4.39  # -17.48% day
    monkeypatch.setattr(config, "DAILY_LOSS_CAP_PCT", 100)
    ok, _ = rm.check_daily_loss()
    assert ok is True and not rm.kill_switch_active()
    ok2, _ = rm.check_can_open("BTCUSDT", 0.5, 0)
    assert ok2 is True


def test_max_positions_cap(workdir):
    state = StateStore(os.path.join(workdir, "state.json"))
    rm = RiskManager(state, lambda: 10_000.0, max_positions=1)
    rm.ensure_day()
    ok, _ = rm.check_can_open("BTCUSDT", 100.0, 1)
    assert ok is False
    ok, _ = rm.check_can_open("BTCUSDT", 100.0, 0)
    assert ok is True


def test_balance_unavailable_fail_closed(workdir):
    state = StateStore(os.path.join(workdir, "state.json"))
    rm = RiskManager(state, lambda: None)
    rm.ensure_day()
    ok, reason = rm.check_can_open("BTCUSDT", 100.0, 0)
    assert ok is False and "fail-closed" in reason


# ---------------------------------------------------------------- reconcile

def test_restart_reconcile_adopts_and_closes(workdir):
    state = StateStore(os.path.join(workdir, "state.json"))
    state.get("positions", {})["p1"] = {"symbol": "BTCUSDT", "qty": 1.0}
    state.save()
    rec = state.reconcile([
        {"symbol": "ETHUSDT", "side": "LONG", "qty": 2.0,
         "entry_price": 50.0, "unrealized_pnl": 0.0, "leverage": 3},
    ])
    assert len(rec["adopted"]) == 1 and len(rec["closed"]) == 1
    adopted = state.get("positions")[rec["adopted"][0]]
    assert adopted["unmanaged"] is True and adopted["symbol"] == "ETHUSDT"
    assert state.get("positions")["p1"]["closed"] is True
    # state file round-trips
    state2 = StateStore(os.path.join(workdir, "state.json"))
    assert state2.get("positions")["p1"]["closed"] is True


# --------------------------------------------------------------- paper sim

def test_paper_fill_simulation():
    prices = {"BTCUSDT": 100.0}
    broker = PaperBroker(10_000.0, price_fn=lambda s: prices[s])
    o = broker.place_order("BTCUSDT", "BUY", "LIMIT", quantity=1.0, price=99.0)
    assert o["status"] == "NEW"  # rests: live 100 > 99
    prices["BTCUSDT"] = 98.5
    fills = broker.poll(["BTCUSDT"])
    assert len(fills) == 1 and fills[0]["order"]["status"] == "FILLED"
    assert broker.positions["BTCUSDT"]["qty"] == 1.0
    # Take-profit rests until touched.
    tp = broker.place_order("BTCUSDT", "SELL", "LIMIT", quantity=0.5,
                            price=103.0, reduce_only=True)
    assert tp["status"] == "NEW"
    prices["BTCUSDT"] = 104.0
    fills = broker.poll(["BTCUSDT"])
    assert len(fills) == 1
    assert broker.realized_pnl > 0
    # Stop-loss triggers below the stop price.
    sl = broker.place_order("BTCUSDT", "SELL", "STOP_MARKET", quantity=0.5,
                            stop_price=97.0, reduce_only=True)
    prices["BTCUSDT"] = 96.0
    fills = broker.poll(["BTCUSDT"])
    assert len(fills) == 1
    assert broker.positions["BTCUSDT"]["qty"] == 0.0


def test_paper_market_fills_immediately():
    broker = PaperBroker(10_000.0, price_fn=lambda s: 100.0)
    o = broker.place_order("BTCUSDT", "BUY", "MARKET", quantity=2.0)
    assert o["status"] == "FILLED"
    assert broker.positions["BTCUSDT"]["qty"] == 2.0


def test_paper_broker_wallet_and_equity_split():
    """PaperBroker must expose the H1 balance split: wallet = realized only,
    equity = wallet + unrealized. run_executor wires both into RiskManager,
    so their absence used to crash paper mode at startup (AttributeError)."""
    prices = {"BTCUSDT": 100.0}
    broker = PaperBroker(10_000.0, price_fn=lambda s: prices[s])
    assert broker.get_wallet_balance() == 10_000.0
    assert broker.get_margin_balance() == 10_000.0
    broker.place_order("BTCUSDT", "BUY", "MARKET", quantity=2.0)
    fee = 100.0 * 2.0 * broker.fee_pct / 100
    # wallet moved only by the entry fee (realized); unrealized is flat.
    assert broker.get_wallet_balance() == pytest.approx(10_000.0 - fee)
    assert broker.get_margin_balance() == pytest.approx(10_000.0 - fee)
    # price runs +10%: equity picks up the unrealized gain, wallet doesn't.
    prices["BTCUSDT"] = 110.0
    assert broker.get_wallet_balance() == pytest.approx(10_000.0 - fee)
    assert broker.get_margin_balance() == pytest.approx(10_000.0 - fee + 20.0)
    # short side mirrors.
    broker2 = PaperBroker(10_000.0, price_fn=lambda s: prices[s])
    broker2.place_order("BTCUSDT", "SELL", "MARKET", quantity=1.0)
    fee2 = 110.0 * 1.0 * broker2.fee_pct / 100
    assert broker2.get_margin_balance() == pytest.approx(10_000.0 - fee2)
    prices["BTCUSDT"] = 100.0  # short gains 10
    assert broker2.get_margin_balance() == pytest.approx(
        10_000.0 - fee2 + 10.0)


# ------------------------------------------------------------------- CLI

def test_testnet_without_keys_refuses():
    with pytest.raises(SystemExit) as ei:
        require_testnet_keys({})
    assert "BINANCE_TESTNET_KEY" in str(ei.value)
    with pytest.raises(SystemExit):
        require_testnet_keys({"BINANCE_TESTNET_KEY": "x"})
    key, secret = require_testnet_keys(
        {"BINANCE_TESTNET_KEY": "k", "BINANCE_TESTNET_SECRET": "s"})
    assert (key, secret) == ("k", "s")


def test_lifecycle_jsonl_appends(workdir):
    path = os.path.join(workdir, "lifecycle.jsonl")
    lc = LifecycleLogger(path)
    lc.log("sid1", "BTCUSDT", "approved", quality=80)
    lc.log("sid1", "BTCUSDT", "rejected", reason="dup")
    events = lc.read_all()
    assert len(events) == 2 and events[0]["state"] == "approved"
    with open(path) as f:
        assert len(f.readlines()) == 2  # one JSON object per line


# ------------------------------------------- fixed-margin test sizing (test mode)

def test_test_sizing_math():
    filters = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
    qty, note = compute_test_qty(10.0, 10, 100.0, filters)
    assert qty == 1.0, note  # $10 x10 = $100 notional / $100 = 1.0
    qty20, _ = compute_test_qty(10.0, 20, 100.0, filters)
    assert qty20 == 2.0  # $10 x20 = $200 notional


def test_test_sizing_floors_down():
    filters = {"stepSize": 0.5, "minQty": 0.5, "minNotional": 5.0}
    qty, _ = compute_test_qty(10.0, 10, 100.0, filters)
    assert qty == 1.0
    # raw 100/300 = 0.333 -> floors to 0.0 -> below minQty -> skip
    qty2, reason = compute_test_qty(10.0, 10, 300.0, filters)
    assert qty2 == 0.0 and "minQty" in reason


def test_test_sizing_min_notional_skips():
    filters = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
    qty, _ = compute_test_qty(10.0, 1, 100.0, filters)
    assert qty == 0.1  # $10 notional >= $5 minNotional
    qty2, reason = compute_test_qty(0.1, 1, 100.0, filters)
    assert qty2 == 0.0 and "minNotional" in reason


def test_test_sizing_invalid_inputs():
    filters = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
    assert compute_test_qty(0, 10, 100.0, filters)[0] == 0.0
    assert compute_test_qty(-5, 10, 100.0, filters)[0] == 0.0
    assert compute_test_qty(10.0, 0, 100.0, filters)[0] == 0.0
    assert compute_test_qty(10.0, 10, 0.0, filters)[0] == 0.0


def test_leverage_override_and_fixed_margin_in_open_flow(workdir):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ev = lambda symbol, m: (_signal(symbol), "stub")  # noqa: E731
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"], evaluate_fn=ev,
                        fixed_margin_usdt=10.0, leverage_override=10)
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1, summary
    assert ("BTCUSDT", 10) in exchange.leverage_calls  # override, not default 3
    assert ("BTCUSDT", "ISOLATED") in exchange.margin_calls
    positions = executor._open_positions()
    pos = next(iter(positions.values()))
    assert pos["qty"] == 1.0  # $10 x10 / $100 entry
    assert pos["leverage"] == 10
    assert pos["margin_usdt"] == 10.0


def test_default_sizing_unchanged_without_overrides(workdir):
    # Without overrides the executor must still use risk-based sizing + 3x.
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ev = lambda symbol, m: (_signal(symbol), "stub")  # noqa: E731
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"], evaluate_fn=ev)
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1, summary
    assert ("BTCUSDT", config.MAX_LEVERAGE) in exchange.leverage_calls
    pos = next(iter(executor._open_positions().values()))
    # risk-based: 1% of $10k = $100 / $2 stop dist = 50 -> capped to
    # 20% of $10k = $2000 / $100 = 20.0
    assert pos["qty"] == 20.0


# ------------------------------------------------- leverage tiers (test mode)

def _executor_with(workdir, **kw):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ev = lambda symbol, m: (_signal(symbol), "stub")  # noqa: E731
    return Executor(market, exchange, risk, state, lifecycle,
                    universe=["BTCUSDT"], evaluate_fn=ev, **kw)


def test_resolve_leverage_tiers_from_config(workdir):
    ex = _executor_with(workdir, fixed_margin_usdt=10.0)
    assert ex._resolve_leverage("BTCUSDT") == 20
    assert ex._resolve_leverage("ETHUSDT") == 20
    assert ex._resolve_leverage("BNBUSDT") == 20
    assert ex._resolve_leverage("SOLUSDT") == 10
    assert ex._resolve_leverage("PEPEUSDT") == 10


def test_resolve_leverage_single_override_wins(workdir):
    ex = _executor_with(workdir, fixed_margin_usdt=10.0, leverage_override=15)
    assert ex._resolve_leverage("BTCUSDT") == 15
    assert ex._resolve_leverage("SOLUSDT") == 15


def test_resolve_leverage_normal_mode_uses_max(workdir):
    ex = _executor_with(workdir)  # no test mode at all
    assert ex._resolve_leverage("BTCUSDT") == config.MAX_LEVERAGE
    assert ex._resolve_leverage("SOLUSDT") == config.MAX_LEVERAGE


def test_resolve_leverage_cli_tiers_merge_over_config(workdir):
    ex = _executor_with(workdir, fixed_margin_usdt=10.0,
                        leverage_tiers={"SOLUSDT": 25}, leverage_default=5)
    assert ex._resolve_leverage("SOLUSDT") == 25   # CLI wins
    assert ex._resolve_leverage("BTCUSDT") == 20   # config tier kept
    assert ex._resolve_leverage("DOGEUSDT") == 5    # CLI default


def test_parse_leverage_tiers():
    from run_executor import _parse_leverage_tiers
    assert _parse_leverage_tiers(None) is None
    assert _parse_leverage_tiers("BTCUSDT=20, ethusdt=15") == {
        "BTCUSDT": 20, "ETHUSDT": 15}
    with pytest.raises(SystemExit):
        _parse_leverage_tiers("BTCUSDT=200")   # out of range
    with pytest.raises(SystemExit):
        _parse_leverage_tiers("BTCUSDT")       # missing =LEV


def test_tier_leverage_used_in_open_flow(workdir):
    # SOLUSDT is not in the 20x tier -> 10x; BTCUSDT -> 20x.
    for symbol, want_lev in (("SOLUSDT", 10), ("BTCUSDT", 20)):
        market = FakeMarket(live=100.0)
        exchange = FakeExchange(balance=10_000.0)
        exchange.market_price = {symbol: 100.0}
        sub = os.path.join(workdir, symbol)
        os.makedirs(sub, exist_ok=True)
        state = StateStore(os.path.join(sub, "state.json"))
        lifecycle = LifecycleLogger(os.path.join(sub, "lifecycle.jsonl"))
        risk = RiskManager(state, exchange.get_balance)
        ev = lambda s, m, _sym=symbol: (_signal(_sym), "stub")  # noqa: E731
        executor = Executor(market, exchange, risk, state, lifecycle,
                            universe=[symbol], evaluate_fn=ev,
                            fixed_margin_usdt=10.0)
        summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
                   "errors": []}
        executor._scan_symbol(symbol, 10_000.0, summary)
        assert summary["opened"] == 1, (symbol, summary)
        assert (symbol, want_lev) in exchange.leverage_calls, (
            symbol, exchange.leverage_calls)
        pos = next(iter(executor._open_positions().values()))
        assert pos["leverage"] == want_lev
        # margin $10 fixed; qty reflects tier: $10*10/100=1.0, $10*20/100=2.0
        assert pos["qty"] == (1.0 if want_lev == 10 else 2.0)


# ------------------------------------------------------- manual entry (test)

class CandleMarket(FakeMarket):
    """FakeMarket with a synthetic 15m candle history for ATR."""
    def __init__(self, live=2.0, n=30):
        super().__init__(live=live)
        base = 1_728_900_000_000
        self.candles = []
        px = live * 0.98
        for i in range(n):
            o = px
            h = px * 1.01
            l = px * 0.99
            c = px * 1.002
            self.candles.append({
                "open_time": base + i * 900_000,
                "close_time": base + (i + 1) * 900_000 - 1,
                "open": o, "high": h, "low": l, "close": c,
            })
            px = c

    def get_klines(self, symbol, interval, limit=220):
        return self.candles[-limit:]


def _manual_rig(workdir, symbol="GRASSUSDT", **kw):
    market = CandleMarket(live=2.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {symbol: 2.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    return Executor(market, exchange, risk, state, lifecycle,
                    universe=[symbol], fixed_margin_usdt=10.0, **kw), exchange


def test_manual_entry_opens_with_brackets(workdir):
    executor, exchange = _manual_rig(workdir)
    ok, note = executor.manual_entry("GRASSUSDT")
    assert ok, note
    assert "MANUAL LONG GRASSUSDT" in note
    assert (("GRASSUSDT", 10) in exchange.leverage_calls)  # default tier
    assert ("GRASSUSDT", "ISOLATED") in exchange.margin_calls
    positions = executor._open_positions()
    assert len(positions) == 1
    pos = next(iter(positions.values()))
    assert pos["leverage"] == 10
    assert pos["margin_usdt"] == 10.0
    assert pos["brackets_placed"] is True
    assert pos["stop_loss"] < pos["entry_price"] < pos["tp"]["tp1"]["price"]


def test_manual_entry_idempotent_per_candle(workdir):
    executor, _ = _manual_rig(workdir)
    ok1, _ = executor.manual_entry("GRASSUSDT")
    assert ok1
    ok2, note2 = executor.manual_entry("GRASSUSDT")
    assert not ok2 and "already attempted" in note2
    assert len(executor._open_positions()) == 1


def test_manual_entry_no_live_price_fail_closed(workdir):
    executor, _ = _manual_rig(workdir)
    executor.market.live = 0  # price feed dead
    ok, note = executor.manual_entry("GRASSUSDT")
    assert not ok and "live price" in note.lower()
    assert len(executor._open_positions()) == 0


def test_manual_entry_thin_candles_fail_closed(workdir):
    executor, exchange = _manual_rig(workdir)
    executor.market.candles = executor.market.candles[:5]
    ok, note = executor.manual_entry("GRASSUSDT")
    assert not ok and "candles" in note.lower()
    assert len(executor._open_positions()) == 0


def test_manual_entry_unknown_symbol(workdir):
    executor, _ = _manual_rig(workdir)
    ok, note = executor.manual_entry("!!!")
    assert not ok


def test_manual_entry_short_mirrors_brackets(workdir):
    executor, exchange = _manual_rig(workdir)
    ok, note = executor.manual_entry("GRASSUSDT", direction="SHORT")
    assert ok, note
    assert "MANUAL SHORT GRASSUSDT" in note
    positions = executor._open_positions()
    assert len(positions) == 1
    pos = next(iter(positions.values()))
    assert pos["direction"] == "SHORT"
    # mirrored: SL above entry, TPs below
    assert pos["tp"]["tp1"]["price"] < pos["entry_price"] < pos["stop_loss"]
    assert pos["brackets_placed"] is True


def test_manual_entry_bad_direction(workdir):
    executor, _ = _manual_rig(workdir)
    ok, note = executor.manual_entry("GRASSUSDT", direction="SIDEWAYS")
    assert not ok and "LONG atau SHORT" in note
    assert len(executor._open_positions()) == 0


def test_preview_manual_entry_places_no_orders(workdir):
    executor, exchange = _manual_rig(workdir)
    plan, err = executor.preview_manual_entry("GRASSUSDT", direction="SHORT")
    assert err == "", err
    assert plan["direction"] == "SHORT"
    assert plan["symbol"] == "GRASSUSDT"
    assert plan["stop_loss"] > plan["entry"] > plan["tp1"] > plan["tp2"] > plan["tp3"]
    assert plan["risk_pct"] > 0
    # no orders placed, no positions opened
    assert exchange.orders == {}
    assert len(executor._open_positions()) == 0


def test_preview_manual_entry_fail_closed(workdir):
    executor, _ = _manual_rig(workdir)
    executor.market.live = 0
    plan, err = executor.preview_manual_entry("GRASSUSDT")
    assert plan is None and "live price" in err.lower()


def _short_signal(symbol="BTCUSDT", entry=100.0):
    return {"symbol": symbol, "direction": "SHORT", "interval": "15m",
            "entry": entry, "entry_low": 99.5, "entry_high": 100.5,
            "stop_loss": 102.0, "tp1": 97.0, "tp2": 95.5, "tp3": 94.0,
            "quality": 80}


@pytest.fixture
def short_rig(workdir):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ev = lambda symbol, m: (_short_signal(symbol), "stub")  # noqa: E731
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"], evaluate_fn=ev)
    return executor, market, exchange, state, lifecycle, risk


def test_short_open_flow_sides_and_direction(short_rig):
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1
    pos_id, pos = next(iter(executor._open_positions().items()))
    assert pos["direction"] == "SHORT"
    # entry leg is a SELL (open short)
    entry = exchange.get_order("BTCUSDT", order_id=pos["entry_order_id"])
    assert entry["side"] == "SELL"
    # brackets exit with BUY
    sl = exchange.get_algo_order("BTCUSDT", pos["sl_order_id"])
    assert sl["side"] == "BUY"
    for leg in ("tp1", "tp2", "tp3"):
        o = exchange.get_order("BTCUSDT", order_id=pos["tp"][leg]["order_id"])
        assert o["side"] == "BUY" and o["reduceOnly"] is True


def test_short_tp_fill_pnl_sign(short_rig):
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    entry_px = pos["entry_price"]
    # TP1 fills below the entry -> profit for a short.
    exchange.fill(pos["tp"]["tp1"]["order_id"], entry_px - 3.0)
    executor._manage_one(pos_id, pos, 10_000.0, summary)
    pos = executor._open_positions()[pos_id]
    assert pos["tp"]["tp1"]["filled"] is True
    assert pos["realized"] > 0, pos["realized"]


def test_short_sl_fill_pnl_sign(short_rig):
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    entry_px = pos["entry_price"]
    qty = pos["qty"]
    # SL fills above the entry -> loss for a short (mirrored PnL sign).
    # Ground truth now comes from userTrades (close classified via exchange).
    sl_px = pos["stop_loss"]
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "BUY", "qty": qty, "price": sl_px,
        "realizedPnl": -(sl_px - entry_px) * qty, "commission": 0.001,
        "time": 9_999_999_999_999,
    }]
    executor._close_position(pos_id, pos, pos["signal_id"], summary)
    assert pos["closed_reason"] == "sl_hit"
    assert pos["realized"] < 0, pos["realized"]
    # gross loss ~= -(sl-entry) * qty; allow fee slippage in the bound
    assert pos["realized"] > -((sl_px - entry_px) * qty) * 1.1


def test_algo_sl_vanished_records_trigger_estimate(rig):
    """Regression: a triggered algo STOP_MARKET vanishes from the algo-order
    endpoint. Classification now uses userTrades ground truth: the SL fill
    is found there and recorded with its real PnL (not 0.0, not estimated)."""
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    assert pos["sl_is_algo"] is True
    entry_px = pos["entry_price"]
    qty = pos["qty"]
    trigger = pos["stop_loss"]
    # Simulate Binance: triggered algo order is gone (-2011), position closed.
    # The real SL fill lives in userTrades.
    del exchange.orders[pos["sl_order_id"]]
    exchange.get_positions = lambda: []
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "SELL", "qty": qty, "price": trigger,
        "realizedPnl": (trigger - entry_px) * qty, "commission": 0.001,
        "time": 9_999_999_999_999,
    }]
    executor._close_position(pos_id, pos, pos["signal_id"], summary,
                             reason="position gone from exchange")
    assert pos["closed"] is True
    assert pos["closed_reason"] == "sl_hit"
    # LONG SL below entry -> real loss from the exchange fill, not 0.0.
    expected = (trigger - entry_px) * qty - 0.001
    assert pos["realized"] == pytest.approx(expected, abs=1e-6)
    assert pos["realized"] < 0, pos["realized"]


def test_algo_sl_cancelled_no_phantom_estimate(rig):
    """A cancelled (not triggered) algo SL must not produce a phantom
    estimate: only a vanished order implies the trigger fired."""
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    exchange.orders[pos["sl_order_id"]]["status"] = "CANCELED"
    executor._close_position(pos_id, pos, pos["signal_id"], summary,
                             reason="position gone from exchange")
    assert pos["closed"] is True
    assert pos["realized"] == 0.0, pos["realized"]


def test_gone_check_fail_closed_on_positions_error(rig, monkeypatch):
    """Regression (live 2026-10-15): a transient get_positions() failure
    (laptop waking from sleep) made the bot log false 'position gone from
    exchange' sl_hit events and corrupt state. get_positions() must raise
    on API failure, and _manage_one must skip the gone-check instead of
    closing a live position."""
    from exchange import ExchangeError
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    assert pos.get("brackets_placed") is True

    def boom():
        raise ExchangeError("GET /fapi/v2/account network error")
    monkeypatch.setattr(exchange, "get_positions", boom)
    executor._manage_one(pos_id, pos, 10_000.0, summary)
    assert pos.get("closed") is not True
    states = [e["state"] for e in lifecycle.read_all()]
    assert "sl_hit" not in states


def test_get_positions_raises_on_api_error(monkeypatch):
    """ExchangeClient.get_positions must propagate API failures (fail-closed),
    never return a phantom empty list."""
    from exchange import ExchangeClient, ExchangeError
    ex = ExchangeClient("k", "s", base_url="https://example.invalid")

    def boom(*a, **k):
        raise ExchangeError("boom")
    monkeypatch.setattr(ex, "_request", boom)
    with pytest.raises(ExchangeError):
        ex.get_positions()


def test_paper_fill_simulation_short():
    prices = {"BTCUSDT": 100.0}
    broker = PaperBroker(10_000.0, price_fn=lambda s: prices[s])
    # Short entry: SELL LIMIT above the market rests until touched.
    o = broker.place_order("BTCUSDT", "SELL", "LIMIT", quantity=1.0,
                           price=101.0)
    assert o["status"] == "NEW"  # rests: live 100 < 101
    prices["BTCUSDT"] = 101.5
    fills = broker.poll(["BTCUSDT"])
    assert len(fills) == 1 and fills[0]["order"]["status"] == "FILLED"
    assert broker.positions["BTCUSDT"]["qty"] == -1.0
    assert broker.get_positions()[0]["side"] == "SHORT"
    # Short take-profit (BUY LIMIT below) fills when price drops to it.
    tp = broker.place_order("BTCUSDT", "BUY", "LIMIT", quantity=0.5,
                            price=97.0, reduce_only=True)
    assert tp["status"] == "NEW"
    prices["BTCUSDT"] = 96.0
    fills = broker.poll(["BTCUSDT"])
    assert len(fills) == 1
    assert broker.realized_pnl > 0  # short TP below entry = profit
    assert broker.positions["BTCUSDT"]["qty"] == -0.5
    # Short stop (BUY STOP_MARKET above) triggers when price rallies to it.
    sl = broker.place_order("BTCUSDT", "BUY", "STOP_MARKET", quantity=0.5,
                            stop_price=103.0, reduce_only=True)
    prices["BTCUSDT"] = 104.0
    fills = broker.poll(["BTCUSDT"])
    assert len(fills) == 1
    assert broker.positions["BTCUSDT"]["qty"] == 0.0


def test_short_full_stack_with_paper_broker(workdir):
    """End-to-end SHORT through the real PaperBroker: SELL entry fills,
    BUY brackets placed, TP fills below entry books a profit."""
    prices = {"BTCUSDT": 100.0}
    market = FakeMarket(live=100.0)
    broker = PaperBroker(10_000.0, price_fn=lambda s: prices[s])
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, broker.get_balance)
    ev = lambda symbol, m: (_short_signal(symbol), "stub")  # noqa: E731
    ex = Executor(market, broker, risk, state, lifecycle,
                  universe=["BTCUSDT"], evaluate_fn=ev,
                  fixed_margin_usdt=10.0,
                  leverage_override=10)
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    ex._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1, summary
    pos_id, pos = next(iter(ex._open_positions().items()))
    assert pos["direction"] == "SHORT"
    assert pos["brackets_placed"] is True
    # Drive the simulation: TP1 (BUY LIMIT @97) fills when price drops.
    prices["BTCUSDT"] = 96.0
    broker.poll(["BTCUSDT"])
    ex._manage_one(pos_id, pos, 10_000.0, summary)
    pos = ex._open_positions()[pos_id]
    assert pos["tp"]["tp1"]["filled"] is True
    assert pos["realized"] > 0


def test_floor_to_step_strips_float_artifacts():
    # Regression: 70*0.1 == 7.000000000000001 in IEEE-754; Binance rejects
    # such values with -1111 (precision over maximum). The hardened
    # floor_to_step must emit exactly representable, step-aligned values.
    from risk import floor_to_step, quantize_price, _step_decimals
    assert _step_decimals(0.001) == 3
    assert _step_decimals(1) == 0
    assert _step_decimals(0.00000001) == 8
    assert floor_to_step(7.0, 0.1) == 7.0
    assert repr(floor_to_step(7.0, 0.1)) == "7.0"
    # still floors down, never up
    assert floor_to_step(7.09, 0.1) == 7.0
    assert floor_to_step(0.0, 0.001) == 0.0
    assert floor_to_step(5.5, 0) == 5.5  # no step -> passthrough
    # quantize_price: the 0.05599/0.00001 case that failed live (-1111)
    assert quantize_price(0.05599, 0.00001) == 0.05599
    assert repr(quantize_price(0.05599, 0.00001)) == "0.05599"
    assert quantize_price(123.456, 0.1) == 123.5  # rounds to tick
    assert quantize_price(100.0, 0) == 100.0
    # fuzz: never emit more decimals than the step allows
    import random
    rng = random.Random(42)
    for step in (0.1, 0.01, 0.001, 0.00001, 1, 0.5):
        dec = _step_decimals(step)
        for _ in range(200):
            v = rng.uniform(0.0001, 99999)
            q = floor_to_step(v, step)
            assert abs(q - v) < step + 1e-9  # within one step below
            assert round(q, dec) == q, (v, step, repr(q))
            p = quantize_price(v, step)
            assert round(p, dec) == p, (v, step, repr(p))


def test_last_event_ts_tail_read(tmp_path):
    """last_event_ts reads only the tail: newest ts, empty, missing file."""
    from lifecycle import LifecycleLogger
    p = tmp_path / "lc.jsonl"
    lc = LifecycleLogger(str(p))
    assert lc.last_event_ts() is None  # missing file
    p.write_text("")
    assert lc.last_event_ts() is None  # empty file
    lc.log("s1", "AAAUSDT", "generated")
    t1 = lc.last_event_ts()
    assert isinstance(t1, int) and t1 > 0
    lc.log("s1", "AAAUSDT", "position_opened", qty=1.0, entry=2.0)
    t2 = lc.last_event_ts()
    assert t2 >= t1
    # garbage lines are skipped
    with open(p, "a") as f:
        f.write("not json\n{\"no_ts\": 1}\n")
    assert lc.last_event_ts() == t2


def test_write_last_run(tmp_path):
    """_write_last_run emits a worker-friendly JSON with notable events."""
    import json
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lifecycle import LifecycleLogger
    from run_executor import _write_last_run, _NOTABLE_STATES
    lc = LifecycleLogger(str(tmp_path / "lifecycle.jsonl"))
    start = lc.last_event_ts() or 0
    lc.log("s1", "WALUSDT", "generated")
    lc.log("s1", "WALUSDT", "position_opened", qty=10.0, entry=0.06)
    lc.log("s2", "AAAUSDT", "rejected", reason="x")  # not notable
    _write_last_run("TESTNET", lc, start, {"approved": 1, "rejected": 1,
                                           "opened": 1, "managed": 0,
                                           "errors": []})
    d = json.loads((tmp_path / "last_run.json").read_text())
    assert d["mode"] == "TESTNET"
    assert d["summary"]["opened"] == 1
    assert d["last_event_ts"] == lc.last_event_ts()
    assert len(d["notable"]) == 1
    n = d["notable"][0]
    assert n["symbol"] == "WALUSDT" and n["state"] == "position_opened"
    assert n["qty"] == 10.0 and n["entry"] == 0.06
    assert "rejected" not in _NOTABLE_STATES


def test_tradable_symbols_filters_non_trading():
    from exchange import ExchangeClient
    ex = ExchangeClient("k", "s")
    ex._exchange_info = {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        {"symbol": "PEPEUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        {"symbol": "OLDUSDT", "status": "BREAK", "contractType": "PERPETUAL"},
        {"symbol": "QUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER"},
    ]}
    ex._exchange_info_ts = 9999999999.0  # don't refetch
    got = ex.tradable_symbols()
    assert got == {"BTCUSDT", "PEPEUSDT"}


def test_tradable_symbols_none_on_failure():
    from exchange import ExchangeClient
    ex = ExchangeClient("k", "s")
    ex._request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    ex._exchange_info = None
    assert ex.tradable_symbols() is None


def test_prefilter_drops_untradable():
    from executor import Executor
    from unittest.mock import MagicMock
    ex = MagicMock()
    ex.tradable_symbols.return_value = {"BTCUSDT", "ETHUSDT"}
    executor = Executor.__new__(Executor)
    executor.exchange = ex
    got = executor._apply_tradability_prefilter(
        ["BTCUSDT", "PEPEUSDT", "ETHUSDT", "SOXSUSDT"])
    assert got == ["BTCUSDT", "ETHUSDT"]


def test_prefilter_fail_open_no_method():
    # PaperBroker has no tradable_symbols -> universe untouched
    from executor import Executor
    from unittest.mock import MagicMock
    ex = MagicMock(spec=[])  # no tradable_symbols attr
    executor = Executor.__new__(Executor)
    executor.exchange = ex
    uni = ["BTCUSDT", "PEPEUSDT"]
    assert executor._apply_tradability_prefilter(uni) == uni


def test_prefilter_fail_open_on_none():
    from executor import Executor
    from unittest.mock import MagicMock
    ex = MagicMock()
    ex.tradable_symbols.return_value = None
    executor = Executor.__new__(Executor)
    executor.exchange = ex
    uni = ["BTCUSDT", "PEPEUSDT"]
    assert executor._apply_tradability_prefilter(uni) == uni


# ------------------------------------------- _classify_vanished_close
def _vanished_short_pos(executor, exchange, summary, symbol="BTCUSDT"):
    executor._scan_symbol(symbol, 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    return pos_id, pos


def test_manual_profitable_close_never_labeled_sl(short_rig):
    """Live 2026-10-15: user force-closed LIT (+$0.079) and BOME (+$0.074)
    shorts in profit; the old code labeled both sl_hit with realized $0.00.
    A profitable close must never be called an SL."""
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    pos_id, pos = _vanished_short_pos(executor, exchange, summary)
    entry_px, qty = pos["entry_price"], pos["qty"]
    close_px = entry_px * 0.991  # profitable for a short, near no bracket
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "BUY", "qty": qty, "price": close_px,
        "realizedPnl": (entry_px - close_px) * qty, "commission": 0.002,
        "time": 9_999_999_999_999,
    }]
    exchange.get_positions = lambda: []
    executor._close_position(pos_id, pos, pos["signal_id"], summary,
                             reason="position gone from exchange")
    assert pos["closed_reason"] == "manual_close (profit)"
    assert pos["realized"] == pytest.approx((entry_px - close_px) * qty - 0.002)
    assert pos["close_price"] == pytest.approx(close_px)


def test_manual_loss_close_labeled_honestly(short_rig):
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    pos_id, pos = _vanished_short_pos(executor, exchange, summary)
    entry_px, qty = pos["entry_price"], pos["qty"]
    close_px = entry_px * 1.004  # small loss, far from the SL trigger
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "BUY", "qty": qty, "price": close_px,
        "realizedPnl": (entry_px - close_px) * qty, "commission": 0.002,
        "time": 9_999_999_999_999,
    }]
    exchange.get_positions = lambda: []
    executor._close_position(pos_id, pos, pos["signal_id"], summary,
                             reason="position gone from exchange")
    assert pos["closed_reason"] == "manual_close (loss)"
    assert pos["realized"] < 0


def test_vanished_close_near_tp1_classified_tp(short_rig):
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    pos_id, pos = _vanished_short_pos(executor, exchange, summary)
    entry_px, qty = pos["entry_price"], pos["qty"]
    tp1_px = pos["tp"]["tp1"]["price"]
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "BUY", "qty": qty, "price": tp1_px,
        "realizedPnl": (entry_px - tp1_px) * qty, "commission": 0.002,
        "time": 9_999_999_999_999,
    }]
    exchange.get_positions = lambda: []
    executor._close_position(pos_id, pos, pos["signal_id"], summary,
                             reason="position gone from exchange")
    assert pos["closed_reason"] == "tp1_hit (remainder)"
    assert pos["realized"] > 0


def test_vanished_close_trades_unavailable_fails_closed(short_rig):
    """If userTrades can't be fetched, fail-closed: sl_hit with no PnL
    rather than a fabricated classification."""
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    pos_id, pos = _vanished_short_pos(executor, exchange, summary)

    def boom(symbol, limit=20, start_time=None):
        raise RuntimeError("timeout")

    exchange.get_user_trades = boom
    exchange.get_positions = lambda: []
    executor._close_position(pos_id, pos, pos["signal_id"], summary,
                             reason="position gone from exchange")
    assert pos["closed_reason"] == "sl_hit (trades unavailable)"
    assert pos["realized"] == 0.0


# --------------------------------------- reconcile terminal attribution (2026-10-15)

def _bracketed_pos(sid="s1", qty=1.0):
    return {
        "symbol": "BTCUSDT", "signal_id": sid, "direction": "LONG",
        "qty": qty, "entry_price": 100.0, "stop_loss": 95.0,
        "tp": {"tp1": {"price": 105.0, "qty": 0.4, "order_id": None,
                       "filled": False},
               "tp2": {"price": 110.0, "qty": 0.3, "order_id": None,
                       "filled": False},
               "tp3": {"price": 115.0, "qty": 0.3, "order_id": None,
                       "filled": False}},
        "sl_order_id": None, "breakeven_moved": False,
        "brackets_placed": True, "opened_at": 1_000,
        "realized": 0.0,
    }


def test_reconcile_reports_newly_closed_only(workdir):
    state = StateStore(os.path.join(workdir, "state.json"))
    state.get("positions", {})["p1"] = _bracketed_pos()
    state.save()
    rec1 = state.reconcile([])
    assert rec1["closed"] == ["p1"]
    # Second startup: already-closed positions are not re-reported, so no
    # duplicate terminal attribution can happen.
    rec2 = state.reconcile([])
    assert rec2["closed"] == []


def test_reconcile_close_bracketed_logs_terminal_sl(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    state.get("positions", {})["p1"] = _bracketed_pos(sid="s1")
    state.save()
    # Vanished from the exchange while the bot was down; userTrades shows
    # the stop filled.
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "price": 95.0,
        "realizedPnl": -5.0, "commission": 0.05, "time": 2_000,
    }]
    rec = state.reconcile([])
    assert rec["closed"] == ["p1"]
    executor._close_reconciled_positions(rec["closed"])
    events = [e for e in lifecycle.read_all()
              if e.get("signal_id") == "s1"]
    terminal = [e for e in events if e.get("terminal")]
    assert len(terminal) == 1
    assert terminal[0]["state"] == "sl_hit"
    assert terminal[0]["realized"] == pytest.approx(-5.05)


def test_reconcile_close_unbracketed_cancels_entry(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    pos = _bracketed_pos(sid="s2")
    pos["brackets_placed"] = False
    pos["entry_order_id"] = 4242
    exchange.orders[4242] = {"orderId": 4242, "symbol": "BTCUSDT",
                             "status": "NEW", "clientOrderId": "en1"}
    state.get("positions", {})["p2"] = pos
    state.save()
    rec = state.reconcile([])
    assert rec["closed"] == ["p2"]
    executor._close_reconciled_positions(rec["closed"])
    assert exchange.orders[4242]["status"] == "CANCELED"  # no naked fill
    events = [e for e in lifecycle.read_all()
              if e.get("signal_id") == "s2" and e.get("terminal")]
    assert len(events) == 1 and events[0]["state"] == "cancelled"
    assert state.get("positions")["p2"]["closed"] is True


def test_reconcile_close_uses_report_outcome(rig):
    # End-to-end into report.py: the attributed close shows up with PnL.
    import datetime
    from report import daily_report
    executor, market, exchange, state, lifecycle, risk = rig
    state.get("positions", {})["p1"] = _bracketed_pos(sid="s9")
    state.save()
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "price": 95.0,
        "realizedPnl": -5.0, "commission": 0.05, "time": 2_000,
    }]
    rec = state.reconcile([])
    executor._close_reconciled_positions(rec["closed"])
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out = daily_report(day, lifecycle.path,
                       os.path.join(os.path.dirname(state.path),
                                    "state.json"))
    assert "closed trades: 1 | wins: 0 | realized PnL: -5.05 USDT" in out


# --------------------------- reviewer findings, cycle 4 (2026-10-15)

def test_reconcile_close_entry_filled_no_brackets_classifies(rig):
    # Reviewer finding #3: entry filled while the bot was down, crash
    # before brackets -> must be classified via userTrades, not CANCELLED.
    executor, market, exchange, state, lifecycle, risk = rig
    pos = _bracketed_pos(sid="s3")
    pos["brackets_placed"] = False
    pos["entry_order_id"] = 555
    exchange.orders[555] = {"orderId": 555, "symbol": "BTCUSDT",
                            "status": "FILLED", "executedQty": 1.0,
                            "avgPrice": 100.0, "clientOrderId": "en2"}
    exchange.user_trades = [{
        "symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "price": 95.0,
        "realizedPnl": -5.0, "commission": 0.05, "time": 2_000,
    }]
    state.get("positions", {})["p3"] = pos
    state.save()
    rec = state.reconcile([])
    executor._close_reconciled_positions(rec["closed"])
    events = [e for e in lifecycle.read_all()
              if e.get("signal_id") == "s3" and e.get("terminal")]
    assert len(events) == 1 and events[0]["state"] == "sl_hit"
    assert events[0]["realized"] == pytest.approx(-5.05)


def test_reconcile_close_failure_retries_next_startup(rig):
    # Reviewer finding #2: a failed attribution clears the closed flag so
    # the next startup retries instead of losing the trade forever.
    executor, market, exchange, state, lifecycle, risk = rig
    state.get("positions", {})["p4"] = _bracketed_pos(sid="s4")
    state.save()
    rec = state.reconcile([])
    assert rec["closed"] == ["p4"]
    orig_save = state.save
    orig_close = executor._close_position
    def fail_close(*a, **k):
        raise RuntimeError("close failed")
    executor._close_position = fail_close
    try:
        executor._close_reconciled_positions(rec["closed"])
    finally:
        executor._close_position = orig_close
        state.save = orig_save
    pos = state.get("positions")["p4"]
    assert pos.get("closed") is None  # cleared -> reconcile re-lists it
    rec2 = state.reconcile([])
    assert rec2["closed"] == ["p4"]


def test_classify_uses_start_time_window(rig):
    # Reviewer finding #1: close fills must not be pushed out of the
    # userTrades window by later trades on the same symbol.
    executor, market, exchange, state, lifecycle, risk = rig
    seen = {}
    orig = exchange.get_user_trades
    def spy(symbol, limit=20, start_time=None):
        seen["start_time"] = start_time
        seen["limit"] = limit
        return orig(symbol, limit=limit, start_time=start_time)
    exchange.get_user_trades = spy
    pos = _bracketed_pos(sid="s5")
    pos["opened_at"] = 5_000
    # an old close fill from a previous position + the real close fill
    exchange.user_trades = [
        {"symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "price": 90.0,
         "realizedPnl": -10.0, "commission": 0.05, "time": 1_000},
        {"symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "price": 95.0,
         "realizedPnl": -5.0, "commission": 0.05, "time": 6_000},
    ]
    event, why, net, avg_px = executor._classify_vanished_close(
        "BTCUSDT", pos, "s5", "p5")
    assert seen["start_time"] == 5_000
    assert event == "sl_hit" and net == pytest.approx(-5.05)
    assert avg_px == pytest.approx(95.0)  # old fill excluded by the window


def test_reconcile_close_skips_already_attributed_sid(rig):
    # Review 2026-10-16 (F1): a crash between lifecycle.log and the later
    # state.save leaves the terminal event logged but the position
    # un-closed. The retry must NOT attribute it again — no second terminal
    # event, no second PnL recording.
    executor, market, exchange, state, lifecycle, risk = rig
    pos = _bracketed_pos(sid="s9")
    state.get("positions", {})["p9"] = pos
    state.save()
    # First attempt already logged the terminal outcome.
    lifecycle.log("s9", "BTCUSDT", "sl_hit", reason="x", realized=-5.05,
                  terminal=True)
    rec = state.reconcile([])
    assert rec["closed"] == ["p9"]
    executor._close_reconciled_positions(rec["closed"])
    terminal = [e for e in lifecycle.read_all()
                if e.get("signal_id") == "s9" and e.get("terminal")]
    assert len(terminal) == 1  # no double terminal event
    assert pos["realized"] == 0.0  # no double PnL


def test_has_terminal_event(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    assert not lifecycle.has_terminal_event("nope")
    assert not lifecycle.has_terminal_event("")
    lifecycle.log("sA", "BTCUSDT", "tp1_hit", realized=0.0)  # leg, no marker
    assert not lifecycle.has_terminal_event("sA")
    lifecycle.log("sA", "BTCUSDT", "tp1_hit", realized=1.25, terminal=True)
    assert lifecycle.has_terminal_event("sA")


def test_one_position_per_coin_cap(rig):
    # 2026-10-16 WCTUSDT incident: two SHORT entries stacked $2 margin on
    # one coin. The second entry on an already-positioned symbol must be
    # rejected, regardless of direction or signal quality.
    executor, market, exchange, state, lifecycle, risk = rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1
    assert len(executor._open_positions()) == 1
    # Second signal on the same coin -> rejected, no new position.
    summary2 = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
                "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary2)
    assert summary2["opened"] == 0
    assert summary2["rejected"] == 1
    assert len(executor._open_positions()) == 1
    # A different coin is unaffected.
    summary3 = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
                "errors": []}
    executor._scan_symbol("ETHUSDT", 10_000.0, summary3)
    assert summary3["opened"] == 1
    assert len(executor._open_positions()) == 2


# ------------------------------------------------------- profit take (+% TP)
def _profit_take_pos():
    return {
        "symbol": "BTCUSDT", "signal_id": "BTCUSDT:1:LONG", "direction": "LONG",
        "qty": 1.0, "entry_price": 100.0, "margin_usdt": 1.0,
        "leverage": 10, "brackets_placed": True, "realized": 0.0,
        "tp": {leg: {"price": 110.0 + i, "qty": 0.34,
                     "order_id": 1001 + i, "filled": False}
               for i, leg in enumerate(("tp1", "tp2", "tp3"))},
        "sl_order_id": 1004,
    }


def _mgmt_summary():
    return {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
            "errors": []}


def test_profit_take_closes_at_plus_20pct(rig, monkeypatch):
    # User 2026-10-16: "+20% langsung TP" (live). The whole position is
    # market-closed once unrealized PnL reaches +PROFIT_TAKE_PCT% of margin.
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 120.0                      # +$20 unrealized on $1 margin
    exchange.market_price["BTCUSDT"] = 120.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos["closed_reason"] == "profit_take"
    assert pos["qty"] == 0.0
    evts = [e for e in lifecycle.read_all() if e["state"] == "profit_take"]
    assert len(evts) == 1
    assert evts[0]["terminal"] is True
    assert evts[0]["pnl"] > 0
    # brackets cancelled; one reduce-only MARKET close on the right side
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert len(closes) == 1 and closes[0]["side"] == "SELL"


def test_profit_take_not_triggered_below_threshold(rig, monkeypatch):
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 100.10                     # +$0.10 < $0.20 threshold
    exchange.market_price["BTCUSDT"] = 100.10
    exchange._pos["BTCUSDT"] = 1.0           # keep the gone-check quiet
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert "profit_take" not in [e["state"] for e in lifecycle.read_all()]
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert closes == []


def test_profit_take_short_direction(rig, monkeypatch):
    # SHORT: profit when the price FALLS 20% of margin.
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    pos = _profit_take_pos()
    pos["direction"] = "SHORT"
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 80.0                       # -$20 move = +$20 for a short
    exchange.market_price["BTCUSDT"] = 80.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos["closed_reason"] == "profit_take"
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert len(closes) == 1 and closes[0]["side"] == "BUY"


def test_profit_take_off_by_default(rig):
    # PROFIT_TAKE_PCT = 0 (default) -> no behavior change for paper/testnet.
    executor, market, exchange, state, lifecycle, risk = rig
    assert config.PROFIT_TAKE_PCT == 0.0
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 200.0                      # huge profit, still no trigger
    exchange.market_price["BTCUSDT"] = 200.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert "profit_take" not in [e["state"] for e in lifecycle.read_all()]


# ------------------------------------------------------- trailing stop
def _trail_pos(**kw):
    pos = _profit_take_pos()
    pos.update(kw)
    return pos


def test_trail_stop_activates_and_closes_on_pullback(rig, monkeypatch):
    # Trigger +10% of margin, distance 2%: LONG activates at 110, trails
    # the peak to 115, then a >2% pullback closes the whole position.
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "TRAIL_TRIGGER_PCT", 10.0)
    monkeypatch.setattr(config, "TRAIL_DISTANCE_PCT", 2.0)
    pos = _trail_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    # Cycle 1: +$10 unrealized on $1 margin -> activates, peak = 110.
    market.live = 110.0
    exchange.market_price["BTCUSDT"] = 110.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("trail_active") is True
    assert pos["trail_peak"] == 110.0
    assert pos.get("closed") is not True
    # Cycle 2: new peak 115 -> peak trails up, no close.
    market.live = 115.0
    exchange.market_price["BTCUSDT"] = 115.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["trail_peak"] == 115.0
    assert pos.get("closed") is not True
    # Cycle 3: pullback to 112.0 (2.6% from peak) -> market close.
    market.live = 112.0
    exchange.market_price["BTCUSDT"] = 112.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos["closed_reason"] == "trail_stop"
    evts = [e for e in lifecycle.read_all() if e["state"] == "trail_stop"]
    assert len(evts) == 1 and evts[0]["terminal"] is True
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert len(closes) == 1 and closes[0]["side"] == "SELL"


def test_trail_stop_not_before_trigger(rig, monkeypatch):
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "TRAIL_TRIGGER_PCT", 10.0)
    monkeypatch.setattr(config, "TRAIL_DISTANCE_PCT", 2.0)
    pos = _trail_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 100.05                     # +$0.05 < $0.10 trigger
    exchange.market_price["BTCUSDT"] = 100.05
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("trail_active") is not True
    assert pos.get("closed") is not True
    assert "trail_stop" not in [e["state"] for e in lifecycle.read_all()]


def test_trail_stop_short_direction(rig, monkeypatch):
    # SHORT: activates when price FALLS 10% of margin; peak trails down;
    # closes on a 2% bounce up from the trough.
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "TRAIL_TRIGGER_PCT", 10.0)
    monkeypatch.setattr(config, "TRAIL_DISTANCE_PCT", 2.0)
    pos = _trail_pos(direction="SHORT")
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 90.0
    exchange.market_price["BTCUSDT"] = 90.0
    exchange._pos["BTCUSDT"] = -1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("trail_active") is True
    assert pos["trail_peak"] == 90.0
    market.live = 92.0                       # 2.2% bounce from trough
    exchange.market_price["BTCUSDT"] = 92.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos["closed_reason"] == "trail_stop"
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert len(closes) == 1 and closes[0]["side"] == "BUY"


def test_trail_stop_off_by_default(rig):
    # Both knobs 0 (default) -> no activation even deep in profit.
    executor, market, exchange, state, lifecycle, risk = rig
    assert config.TRAIL_TRIGGER_PCT == 0.0
    assert config.TRAIL_DISTANCE_PCT == 0.0
    pos = _trail_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 200.0
    exchange.market_price["BTCUSDT"] = 200.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("trail_active") is not True
    assert pos.get("closed") is not True
    assert "trail_stop" not in [e["state"] for e in lifecycle.read_all()]


# ------------------------------------------------------- time stop
def _time_stop_pos(age_hours):
    import time as _t
    pos = _profit_take_pos()
    pos["opened_at"] = int(_t.time() * 1000) - int(age_hours * 3_600_000)
    return pos


def test_time_stop_closes_stale_position(rig, monkeypatch):
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "TIME_STOP_HOURS", 4.0)
    pos = _time_stop_pos(age_hours=5)
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 101.0
    exchange.market_price["BTCUSDT"] = 101.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos["closed_reason"] == "time_stop"
    evts = [e for e in lifecycle.read_all() if e["state"] == "time_stop"]
    assert len(evts) == 1 and evts[0]["terminal"] is True
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert len(closes) == 1 and closes[0]["side"] == "SELL"


def test_time_stop_not_before_hours(rig, monkeypatch):
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "TIME_STOP_HOURS", 4.0)
    pos = _time_stop_pos(age_hours=2)
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 101.0
    exchange.market_price["BTCUSDT"] = 101.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert "time_stop" not in [e["state"] for e in lifecycle.read_all()]


def test_time_stop_missing_opened_at_skips(rig, monkeypatch):
    # Review 2026-10-16: unknown age must skip, never instant-close.
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "TIME_STOP_HOURS", 4.0)
    pos = _profit_take_pos()                # no opened_at key at all
    assert "opened_at" not in pos
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 101.0
    exchange.market_price["BTCUSDT"] = 101.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert "time_stop" not in [e["state"] for e in lifecycle.read_all()]
    closes = [o for o in exchange.orders.values()
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert closes == []


def test_time_stop_off_by_default(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    assert config.TIME_STOP_HOURS == 0.0
    pos = _time_stop_pos(age_hours=100)
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 101.0
    exchange.market_price["BTCUSDT"] = 101.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert "time_stop" not in [e["state"] for e in lifecycle.read_all()]


# ------------------------------------------------- stale entry limit orders
def _stale_entry_pos(entry_order_id, age_min):
    import time as _t
    return {
        "symbol": "BTCUSDT", "signal_id": "BTCUSDT:9:LONG", "direction": "LONG",
        "qty": 1.0, "entry_price": 99.0, "margin_usdt": 1.0,
        "leverage": 10, "brackets_placed": False, "realized": 0.0,
        "entry_order_id": entry_order_id, "entry_type": "LIMIT",
        "opened_at": int(_t.time() * 1000) - int(age_min * 60_000),
    }


def test_stale_entry_cancelled_after_30min(rig):
    # User 2026-10-16: limit entry unfilled after 30 min -> cancel.
    import executor as ex_mod
    assert ex_mod._ENTRY_TIMEOUT_MINUTES == 30
    executor, market, exchange, state, lifecycle, risk = rig
    o = exchange.place_order("BTCUSDT", "BUY", "LIMIT", quantity=1.0,
                             price=99.0)
    oid = o["orderId"]
    pos = _stale_entry_pos(oid, age_min=31)
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert "30m" in pos["closed_reason"]
    assert exchange.get_order("BTCUSDT", order_id=oid)["status"] == "CANCELED"
    evts = [e for e in lifecycle.read_all() if e["state"] == "expired"]
    assert len(evts) == 1 and evts[0]["terminal"] is True


def test_fresh_entry_kept_within_30min(rig):
    executor, market, exchange, state, lifecycle, risk = rig
    o = exchange.place_order("BTCUSDT", "BUY", "LIMIT", quantity=1.0,
                             price=99.0)
    oid = o["orderId"]
    pos = _stale_entry_pos(oid, age_min=10)
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert exchange.get_order("BTCUSDT", order_id=oid)["status"] == "NEW"
    assert "expired" not in [e["state"] for e in lifecycle.read_all()]


# ------------------------------------------- _flat_position partial fills
def _partial_fill_order(executed_qty, avg_price, status="PARTIALLY_FILLED"):
    return {"orderId": 2001, "clientOrderId": "fake_2001",
            "status": status, "executedQty": executed_qty,
            "avgPrice": avg_price}


def test_flat_position_partial_fill_keeps_residual(rig, monkeypatch):
    # A partially filled market close must NOT mark a phantom flat: only the
    # filled qty is accounted, the residual stays open, event non-terminal.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: _partial_fill_order(0.4, 120.0))
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert pos["qty"] == 0.6
    # PnL only for the filled 0.4: (120-100)*0.4 - fee(120*0.4*0.05%)
    assert pos["realized"] == (20 * 0.4 - 120 * 0.4 * 0.0005)
    evts = [e for e in lifecycle.read_all() if e["state"] == "profit_take"]
    assert len(evts) == 1
    assert evts[0]["terminal"] is False
    assert evts[0]["qty"] == 0.4
    assert "partial fill" in evts[0]["reason"]


def test_flat_position_partial_then_full_single_terminal(rig, monkeypatch):
    # Residual from a partial fill closes fully on retry: exactly one
    # terminal event, realized = sum of both fills, no double count.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    calls = {"n": 0}

    def _place(symbol, side, type, quantity=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _partial_fill_order(0.4, 120.0)
        return _partial_fill_order(0.6, 121.0, status="FILLED")

    monkeypatch.setattr(exchange, "place_order", _place)
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    flat = lambda: executor._flat_position(
        "p1", pos, pos["signal_id"], _mgmt_summary(), 120.0,
        "profit_take", ex_mod.PROFIT_TAKE, reason="profit_take +20% of margin")
    flat()   # partial: 0.4 filled, 0.6 remains
    assert state.get("positions")["p1"].get("closed") is not True
    flat()   # retry: remaining 0.6 fully filled
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos["qty"] == 0.0
    assert pos["closed_reason"] == "profit_take"
    evts = [e for e in lifecycle.read_all() if e["state"] == "profit_take"]
    assert len(evts) == 2
    assert [e["terminal"] for e in evts] == [False, True]
    exp = ((20 * 0.4 - 120 * 0.4 * 0.0005)
           + (21 * 0.6 - 121 * 0.6 * 0.0005))
    assert pos["realized"] == exp


def test_flat_position_zero_fill_retries(rig, monkeypatch):
    # executedQty 0 (e.g. immediately expired): nothing accounted, position
    # untouched, no lifecycle event — retry next cycle.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: _partial_fill_order(0.0, 0.0,
                                                           status="EXPIRED"))
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert pos["qty"] == 1.0
    assert pos["realized"] == 0.0
    assert "profit_take" not in [e["state"] for e in lifecycle.read_all()]


def test_flat_position_missing_fill_info_trusts_filled(rig, monkeypatch):
    # Wrappers that omit executedQty (test fakes): status FILLED -> full
    # close as before; any other status -> no fill. Never invent a qty.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: {"orderId": 2001, "status": "FILLED"})
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True and pos["qty"] == 0.0


# ------------------------------------------- pending_close retry
def _residual_pos():
    # Simulates the state right after a partial _flat_position: brackets
    # cancelled, 0.6 residual open, pending_close armed.
    import executor as ex_mod
    pos = _profit_take_pos()
    pos["qty"] = 0.6
    pos["realized"] = 7.976  # from the earlier 0.4 partial fill
    pos["pending_close"] = {"closed_reason": "profit_take",
                            "lifecycle_state": ex_mod.PROFIT_TAKE,
                            "reason": "profit_take +20% of margin"}
    return pos


def test_flat_position_partial_arms_pending_close(rig, monkeypatch):
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: _partial_fill_order(0.4, 120.0))
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pc = state.get("positions")["p1"].get("pending_close")
    assert pc is not None
    assert pc["closed_reason"] == "profit_take"
    assert pc["lifecycle_state"] == ex_mod.PROFIT_TAKE


def test_flat_position_zero_fill_arms_pending_close(rig, monkeypatch):
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: _partial_fill_order(0.0, 0.0,
                                                           status="EXPIRED"))
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True and pos["qty"] == 1.0
    assert pos.get("pending_close", {}).get("closed_reason") == "profit_take"


def test_flat_position_order_failure_arms_pending_close(rig, monkeypatch):
    # place_order -> None AFTER brackets were cancelled: still arm the retry.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order", lambda *a, **k: None)
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert pos.get("pending_close") is not None


def test_pending_close_retries_without_trigger(rig, monkeypatch):
    # Reviewer scenario: profit-take partial fill, then price retreats BELOW
    # the +20% trigger. The residual must STILL be closed — the retry is
    # unconditional, never gated on the trigger firing again.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    pos = _residual_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    # trigger cold: unrealized 0.6*(100.20-100) = $0.12 < $0.20 threshold —
    # any close below must come from the pending_close retry, not the trigger
    market.live = 100.20
    exchange.market_price["BTCUSDT"] = 100.20
    exchange._pos["BTCUSDT"] = 0.6           # keep the gone-check quiet
    n_orders = len(exchange.orders)
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    # a NEW market close was placed despite the trigger being cold...
    closes = [o for o in list(exchange.orders.values())[n_orders:]
              if o.get("type") == "MARKET" and o.get("reduceOnly")]
    assert len(closes) == 1, "pending_close retry did not fire"
    # ...and the FakeExchange full-fills it -> position closed, flag cleared
    assert pos["closed"] is True
    assert pos.get("pending_close") is None
    assert pos["qty"] == 0.0
    terminals = [e for e in lifecycle.read_all()
                 if e.get("terminal") is True]
    assert len(terminals) == 1 and terminals[0]["state"] == "profit_take"


def test_flat_position_string_executed_qty(rig, monkeypatch):
    # Real Binance returns executedQty as a STRING — must be handled.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: _partial_fill_order("0.4", 120.0))
    pos = _profit_take_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            120.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True
    assert pos["qty"] == 0.6


def test_flat_position_short_partial_fill(rig, monkeypatch):
    # SHORT direction: sign flip applies to the partial qty as well.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(exchange, "place_order",
                        lambda *a, **k: _partial_fill_order(0.4, 80.0))
    pos = _profit_take_pos()
    pos["direction"] = "SHORT"
    state._data["positions"] = {"p1": pos}
    state.save()
    executor._flat_position("p1", pos, pos["signal_id"], _mgmt_summary(),
                            80.0, "profit_take", ex_mod.PROFIT_TAKE,
                            reason="profit_take +20% of margin")
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True and pos["qty"] == 0.6
    # SHORT profit: (entry - fill) * qty - fee = (100-80)*0.4 - 80*0.4*0.0005
    assert pos["realized"] == (20 * 0.4 - 80 * 0.4 * 0.0005)
    assert pos.get("pending_close") is not None


# ------------------------------------------- pending_close fall-through
def test_pending_close_vanished_residual_goes_via_close_position(
        rig, monkeypatch):
    # Residual vanished externally (manual close / liquidation) while
    # pending_close is armed: the retry's market order fails, but instead of
    # looping forever the fall-through reaches the end-of-cycle cross-check,
    # which routes via _close_position (ground-truth classification).
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    monkeypatch.setattr(config, "TIME_STOP_HOURS", 0.0)
    pos = _residual_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    # trigger cold: unrealized 0.6*(100.20-100) = $0.12 < $0.20 threshold
    market.live = 100.20
    exchange.market_price["BTCUSDT"] = 100.20
    exchange._pos = {}                       # nothing on the exchange
    monkeypatch.setattr(exchange, "place_order", lambda *a, **k: None)
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True
    assert pos.get("pending_close") is None  # cleared on close
    terminals = [e for e in lifecycle.read_all()
                 if e.get("terminal") is True]
    assert len(terminals) == 1
    # no close fills on the exchange -> fail-closed sl_hit classification
    assert terminals[0]["state"] == "sl_hit"
    assert "no close fills found" in terminals[0]["reason"]


def test_pending_close_retry_partial_then_trigger_completes(rig, monkeypatch):
    # Fall-through: pending retry partial-fills again, then the still-hot
    # trigger completes the close in the SAME cycle (bounded: max 2
    # attempts). Exactly one terminal event.
    import executor as ex_mod
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    calls = {"n": 0}

    def _place(symbol, side, type, quantity=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _partial_fill_order(0.3, 120.0)   # pending retry: partial
        return _partial_fill_order(0.3, 120.0, status="FILLED")  # trigger: rest

    monkeypatch.setattr(exchange, "place_order", _place)
    pos = _residual_pos()
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 120.0                      # trigger HOT for the 0.6 residual
    exchange.market_price["BTCUSDT"] = 120.0
    exchange._pos["BTCUSDT"] = 0.6
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert calls["n"] == 2, "expected retry + trigger attempt in one cycle"
    assert pos["closed"] is True and pos["qty"] == 0.0
    assert pos.get("pending_close") is None
    terminals = [e for e in lifecycle.read_all()
                 if e.get("terminal") is True]
    assert len(terminals) == 1 and terminals[0]["state"] == "profit_take"


# ---------------------------------------------------------------- H2 regression
def test_h2_sl_resized_after_tp_fill(short_rig, monkeypatch):
    """H2: after a TP fill, the live SL must be resized to the remainder.

    An oversized reduce-only stop can be rejected at trigger time, leaving
    the tail unprotected. (Breakeven disabled here to isolate the resize.)
    """
    import config as config_mod
    monkeypatch.setattr(config_mod, "EXEC_BREAKEVEN_AFTER_TP1", False)
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    entry_px = pos["entry_price"]
    full_qty = pos["qty"]
    old_sl_id = pos["sl_order_id"]
    assert pos["sl_qty"] == full_qty
    # Fill TP1 -> remaining shrinks.
    exchange.fill(pos["tp"]["tp1"]["order_id"], entry_px - 3.0)
    executor._manage_one(pos_id, pos, 10_000.0, summary)
    pos = executor._open_positions()[pos_id]
    remaining = pos["qty"]
    assert 0 < remaining < full_qty
    assert pos["sl_order_id"] != old_sl_id, "SL was not replaced after TP fill"
    new_sl = exchange.get_algo_order("BTCUSDT", pos["sl_order_id"])
    assert new_sl is not None and new_sl["status"] == "NEW"
    assert abs(new_sl["origQty"] - remaining) < 1e-9, (
        f"SL qty {new_sl['origQty']} != remaining {remaining}")
    assert pos["sl_qty"] == remaining
    old_sl = exchange.get_algo_order("BTCUSDT", old_sl_id)
    assert old_sl["status"] == "CANCELED"


def test_h2_dead_sl_emergency_replaced(short_rig):
    """H2: a dead SL leg (CANCELED) must be re-placed, not left naked."""
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    pos_id, pos = next(iter(executor._open_positions().items()))
    old_sl_id = pos["sl_order_id"]
    # Simulate the SL dying on the exchange (rejected/cancelled externally).
    exchange.cancel_algo_order("BTCUSDT", old_sl_id)
    executor._manage_one(pos_id, pos, 10_000.0, summary)
    pos = executor._open_positions().get(pos_id)
    assert pos is not None and not pos.get("closed"), \
        "position should stay managed with a fresh SL"
    assert pos["sl_order_id"] != old_sl_id, "dead SL was not replaced"
    new_sl = exchange.get_algo_order("BTCUSDT", pos["sl_order_id"])
    assert new_sl is not None and new_sl["status"] == "NEW"
    assert abs(new_sl["origQty"] - pos["qty"]) < 1e-9
    assert any(e.get("state") == "sl_replaced"
               for e in lifecycle.read_all()), "no sl_replaced lifecycle event"


# ---------------------------------------------------------------- H4 regression
def test_h4_flat_position_terminal_carries_realized(rig, monkeypatch):
    """H4: _flat_position's terminal event must carry realized PnL.

    metrics/report/notify read e["realized"]; logging only pnl= left them
    at 0 for profit-take / trail / time-stop closes.
    """
    executor, market, exchange, state, lifecycle, risk = rig
    monkeypatch.setattr(config, "PROFIT_TAKE_PCT", 20.0)
    pos = _profit_take_pos()
    # Give the test position a stop_loss so H2 helpers don't skip it.
    pos["stop_loss"] = 99.0
    state._data["positions"] = {"p1": pos}
    state.save()
    market.live = 120.0                      # +$20 unrealized on $1 margin
    exchange.market_price["BTCUSDT"] = 120.0
    exchange._pos["BTCUSDT"] = 1.0
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    terminals = [e for e in lifecycle.read_all()
                 if e.get("terminal") is True]
    assert len(terminals) == 1, f"expected 1 terminal event, got {terminals}"
    ev = terminals[0]
    assert "realized" in ev, f"terminal event lacks realized: {ev}"
    assert ev["realized"] > 0, f"expected positive realized, got {ev}"
    # realized must equal the position's total (prior fills + this close).
    stored = state.get("positions")["p1"]
    assert abs(ev["realized"] - round(stored["realized"], 4)) < 1e-9


# ---------------------------------------------------------------- H5 regression
def test_h5_tp_dust_closes_position_and_frees_slot(rig):
    """H5: sub-minQty residual after all TP legs fill must close, not block
    the one-position-per-coin slot forever."""
    executor, market, exchange, state, lifecycle, risk = rig
    pos = _profit_take_pos()
    pos["stop_loss"] = 99.0
    for leg in ("tp1", "tp2", "tp3"):
        pos["tp"][leg]["filled"] = True
    pos["qty"] = 0.0005  # dust < minQty (0.001)
    state._data["positions"] = {"p1": pos}
    state.save()
    # Keep the exchange-side position so the gone-check doesn't mask the
    # dust logic.
    exchange._pos["BTCUSDT"] = 0.0005
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos["closed"] is True, "dust position was not closed"
    assert executor._open_positions() == {}, "symbol slot still blocked"
    assert any(e.get("state") == "dust_closed" for e in lifecycle.read_all())


# ---------------------------------------------------------------- H6 regression
def test_h6_orphan_cleanup_keeps_adopted_position_orders(rig):
    """H6: cleanup_orphan_orders must NOT cancel live orders belonging to an
    adopted (unmanaged) position's symbol — reconcile() can't know their
    order IDs, so they'd all look like orphans."""
    executor, market, exchange, state, lifecycle, risk = rig
    # An adopted position: unmanaged, no known order IDs (as reconcile()
    # stores it).
    state._data["positions"] = {
        "adopted_1": {"symbol": "BTCUSDT", "qty": 1.0, "unmanaged": True,
                      "tp": {}, "sl_order_id": None, "closed": False},
    }
    state.save()
    # A live order on that symbol (e.g. the manual trader's stop) + a real
    # orphan on another symbol.
    exchange.orders[9001] = {"orderId": 9001, "symbol": "BTCUSDT",
                             "side": "SELL", "price": 95.0, "status": "NEW"}
    exchange.orders[9002] = {"orderId": 9002, "symbol": "ETHUSDT",
                             "side": "SELL", "price": 3000.0, "status": "NEW"}
    cancelled = executor.cleanup_orphan_orders()
    assert exchange.orders[9001]["status"] == "NEW", \
        "adopted position's order was cancelled!"
    assert "BTCUSDT:9001" not in cancelled
    assert exchange.orders[9002]["status"] == "CANCELED", \
        "genuine orphan was not cancelled"
    assert "ETHUSDT:9002" in cancelled


# ---------------------------------------------------------------- H7 regression
def test_h7_entry_client_id_deterministic_from_sid():
    """H7: the entry clientOrderId must derive from the signal id so a retry
    collides instead of double-submitting."""
    from executor import Executor
    sid = "BTCUSDT:1234567890000:LONG"
    cid1 = Executor._entry_client_id(sid)
    cid2 = Executor._entry_client_id(sid)
    assert cid1 == cid2, "entry clientOrderId not deterministic"
    assert len(cid1) <= 36
    assert all(c.isalnum() or c in "-_" for c in cid1)
    other = Executor._entry_client_id("ETHUSDT:1234567890000:LONG")
    assert other != cid1, "different signals must not share a clientOrderId"


def test_h7_signal_marked_before_place_order(short_rig, monkeypatch):
    """H7: if place_order blows up, the signal must already be marked so the
    next cycle does not open a second position (at-most-once)."""
    executor, market, exchange, state, lifecycle, risk = short_rig
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}

    def boom(*a, **k):
        raise RuntimeError("simulated crash between mark and fill")

    monkeypatch.setattr(exchange, "place_order", boom)
    import pytest
    with pytest.raises(RuntimeError):
        executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    # The signal id for this candle/direction must be marked despite the crash.
    assert len(state._data.get("signal_ids", {})) == 1, \
        "signal was not marked before place_order"
    assert executor._open_positions() == {}


# ---------------------------------------------------------------- H8 regression
def test_h8_partial_fill_at_timeout_is_adopted_not_stranded(rig, monkeypatch):
    """H8: if the entry partially fills around the timeout cancel, the filled
    portion must be adopted (resized + bracketed), never marked closed as a
    naked position."""
    import time as time_mod
    executor, market, exchange, state, lifecycle, risk = rig
    # Build a position whose entry is stale (past timeout) and partially
    # filled on the exchange.
    pos = _profit_take_pos()
    pos.update({
        "qty": 1.0, "entry_price": 100.0, "brackets_placed": False,
        "opened_at": int(time_mod.time() * 1000) - 31 * 60_000,
        "entry_order_id": 7777, "closed": False, "stop_loss": 99.0,
    })
    for leg in ("tp1", "tp2", "tp3"):
        pos["tp"][leg]["order_id"] = None
    state._data["positions"] = {"p1": pos}
    state.save()
    # Exchange: entry order partially filled (0.4 of 1.0), still NEW.
    exchange.orders[7777] = {"orderId": 7777, "symbol": "BTCUSDT",
                             "status": "PARTIALLY_FILLED",
                             "executedQty": 0.4, "avgPrice": 100.1,
                             "origQty": 1.0}
    # Force the timeout path: make _check_entry_fill see it as not-fillable
    # by having get_order report the partial state (it returns False for
    # PARTIALLY_FILLED), then _expire_stale_entry runs.
    executor._manage_one("p1", pos, 10_000.0, _mgmt_summary())
    pos = state.get("positions")["p1"]
    assert pos.get("closed") is not True, \
        "partially filled entry was stranded as closed!"
    assert abs(pos["qty"] - 0.4) < 1e-9, f"qty not resized to fill: {pos['qty']}"
    assert pos.get("brackets_placed") is True, "brackets not placed for adopted fill"
    assert pos.get("sl_order_id") is not None, "no SL for adopted fill"
