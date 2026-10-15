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
    sl_id = pos["sl_order_id"]
    exchange.orders[sl_id]["status"] = "FILLED"
    exchange.orders[sl_id]["avgPrice"] = entry_px + 2.0
    executor._close_position(pos_id, pos, pos["signal_id"], summary)
    assert pos["realized"] < 0, pos["realized"]
    # gross loss ~= -2.0 * qty; allow fee slippage in the bound
    assert pos["realized"] > -(2.0 * qty) * 1.1


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
