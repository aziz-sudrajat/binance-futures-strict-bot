"""Tests for graceful shutdown (1.3): SIGINT/SIGTERM finish the in-flight
cycle, flush state, and exit cleanly — never interrupting an order."""
import os
import signal
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_executor import FakeExchange, FakeMarket  # noqa: E402

from executor import Executor  # noqa: E402
from lifecycle import LifecycleLogger  # noqa: E402
from risk import RiskManager  # noqa: E402
from state import StateStore  # noqa: E402

import run_executor  # noqa: E402


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def _rig(workdir):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(workdir, "state.json"))
    lifecycle = LifecycleLogger(os.path.join(workdir, "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"],
                        poll_interval_min=0,
                        evaluate_fn=lambda s, m: (None, "stub"))
    return executor, state


def test_executor_has_shutdown_event(workdir):
    executor, _ = _rig(workdir)
    assert isinstance(executor.shutdown_event, threading.Event)
    assert not executor.shutdown_event.is_set()


def test_install_handlers_sets_event_on_signal(workdir):
    """Simulated SIGINT/SIGTERM only set the event (never raise)."""
    executor, _ = _rig(workdir)
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        event = run_executor._install_shutdown_handlers(executor)
        assert executor.shutdown_event is event
        assert not event.is_set()

        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)  # simulate Ctrl+C landing mid-cycle
        assert event.is_set(), "SIGINT must set the shutdown event"

        event.clear()
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # simulate systemd/docker stop
        assert event.is_set(), "SIGTERM must set the shutdown event"
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def test_run_loop_finishes_inflight_cycle_then_exits(workdir, monkeypatch):
    """Signal arriving DURING a cycle: the cycle completes (not cut off),
    the loop does not start another cycle, and state is flushed."""
    executor, state = _rig(workdir)
    state_path = os.path.join(workdir, "state.json")

    calls = []

    def fake_cycle():
        calls.append(1)
        executor.shutdown_event.set()  # signal lands mid-cycle
        return {"opened": 0}

    monkeypatch.setattr(executor, "scan_once", fake_cycle)
    executor.run_loop()

    assert len(calls) == 1, \
        "in-flight cycle must complete exactly once, no second cycle started"
    assert os.path.exists(state_path), \
        "state must be flushed on clean shutdown"


def test_run_loop_exits_immediately_when_event_preset(workdir, monkeypatch):
    """Event set before the loop starts: zero cycles, clean exit."""
    executor, _ = _rig(workdir)
    executor.shutdown_event.set()

    def boom():
        raise AssertionError("no cycle should run after shutdown requested")

    monkeypatch.setattr(executor, "scan_once", boom)
    executor.run_loop()  # must return, not hang, not run a cycle


def test_run_loop_stop_after_still_works(workdir, monkeypatch):
    """Regression: normal loop behavior (no signal) is unchanged."""
    executor, _ = _rig(workdir)
    calls = []

    def fake_cycle():
        calls.append(1)
        return {"opened": 0}

    monkeypatch.setattr(executor, "scan_once", fake_cycle)
    executor.run_loop(stop_after=3)
    assert len(calls) == 3
