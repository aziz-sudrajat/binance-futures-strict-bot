"""Tests for GET /api/health (dashboard.py build_health)."""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))  # noqa: E402

import pytest  # noqa: E402

import config  # noqa: E402
import dashboard  # noqa: E402
from dashboard import build_health  # noqa: E402
from executor import Executor  # noqa: E402
from lifecycle import LifecycleLogger  # noqa: E402
from risk import RiskManager  # noqa: E402
from state import StateStore  # noqa: E402
from test_executor import FakeExchange, FakeMarket  # noqa: E402


@pytest.fixture
def health_files(tmp_path, monkeypatch):
    """Redirect mode_files + settings at a tmp state dir."""
    state_path = str(tmp_path / "state.json")
    life_path = str(tmp_path / "lifecycle.jsonl")
    settings_path = str(tmp_path / "dashboard_settings.json")
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump({"poll_interval_min": 5}, f)
    monkeypatch.setattr(
        dashboard, "mode_files",
        lambda mode="live": (state_path, life_path))
    monkeypatch.setattr(config, "DASHBOARD_SETTINGS_FILE", settings_path)
    monkeypatch.setattr(dashboard, "SETTINGS_PATH", settings_path)
    return state_path


def _write_state(path, **kw):
    base = {
        "signal_ids": {},
        "positions": {},
        "daily": {"date": "", "start_balance": 0.0, "realized_pnl": 0.0},
        "kill_switch": {"date": "", "active": False, "reason": ""},
        "last_cycle_ts": 0,
    }
    base.update(kw)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(base, f)


REQUIRED = {"status", "mode", "loop_alive", "last_cycle_ts",
            "last_cycle_age_sec", "open_positions", "uptime_sec",
            "version", "commit"}


def test_health_ok_when_heartbeat_fresh(health_files):
    now_ms = int(time.time() * 1000)
    _write_state(health_files, last_cycle_ts=now_ms, positions={
        "p1": {"symbol": "BTCUSDT", "closed": False},
        "p2": {"symbol": "ETHUSDT", "closed": True},
    })
    h = build_health("live")
    assert REQUIRED <= set(h), f"missing fields: {REQUIRED - set(h)}"
    assert h["status"] == "ok"
    assert h["loop_alive"] is True
    assert h["last_cycle_ts"] == now_ms
    assert 0 <= h["last_cycle_age_sec"] < 60
    assert h["open_positions"] == 1  # closed position not counted
    assert h["uptime_sec"] >= 0
    assert "commit" in h


def test_health_degraded_when_heartbeat_stale(health_files):
    stale_ms = int(time.time() * 1000) - 24 * 3600 * 1000
    _write_state(health_files, last_cycle_ts=stale_ms)
    h = build_health("live")
    assert h["status"] == "degraded"
    assert h["loop_alive"] is False
    assert h["last_cycle_age_sec"] > 3600


def test_health_degraded_when_no_state_file(health_files):
    h = build_health("live")  # state file never written
    assert h["status"] == "degraded"
    assert h["loop_alive"] is False
    assert h["last_cycle_ts"] is None
    assert h["last_cycle_age_sec"] is None
    assert h["open_positions"] == 0


def test_health_is_read_only(health_files):
    now_ms = int(time.time() * 1000)
    _write_state(health_files, last_cycle_ts=now_ms,
                 positions={"p1": {"symbol": "BTCUSDT"}})
    before = open(health_files, "rb").read()
    before_mtime = os.stat(health_files).st_mtime_ns
    h1 = build_health("live")
    h2 = build_health("live")
    after = open(health_files, "rb").read()
    after_mtime = os.stat(health_files).st_mtime_ns
    assert before == after, "build_health must not modify the state file"
    assert before_mtime == after_mtime
    assert h1 == h2


def test_health_never_calls_trading_functions(health_files, monkeypatch):
    """Health must not touch the exchange: any exchange call fails loudly."""
    now_ms = int(time.time() * 1000)
    _write_state(health_files, last_cycle_ts=now_ms)
    def boom(*a, **k):
        pytest.fail("build_health must not call the exchange")

    import exchange as exchange_mod
    monkeypatch.setattr(exchange_mod.ExchangeClient, "get_balance",
                        lambda self: boom())
    monkeypatch.setattr(exchange_mod.ExchangeClient, "get_positions",
                        lambda self: boom())
    h = build_health("live")
    assert h["status"] == "ok"


def test_health_http_endpoint(health_files):
    """Serve the real handler on an ephemeral port; GET /api/health."""
    now_ms = int(time.time() * 1000)
    _write_state(health_files, last_cycle_ts=now_ms,
                 positions={"p1": {"symbol": "BTCUSDT"}})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health?mode=live",
                timeout=10) as r:
            assert r.status == 200
            assert "application/json" in r.headers.get("Content-Type", "")
            h = json.loads(r.read().decode())
        assert REQUIRED <= set(h)
        assert h["status"] == "ok" and h["loop_alive"] is True
        assert h["open_positions"] == 1
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_executor_writes_cycle_heartbeat(tmp_path):
    """run_loop stamps last_cycle_ts into the state file each cycle."""
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state_path = os.path.join(str(tmp_path), "state.json")
    state = StateStore(state_path)
    lifecycle = LifecycleLogger(os.path.join(str(tmp_path), "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"],
                        poll_interval_min=0,
                        evaluate_fn=lambda s, m: (None, "stub"))
    before = int(time.time() * 1000)
    executor.run_loop(stop_after=2)
    after = int(time.time() * 1000)
    with open(state_path, encoding="utf-8") as f:
        saved = json.load(f)
    ts = saved.get("last_cycle_ts")
    assert isinstance(ts, int) and before <= ts <= after, ts
    # and the dashboard sees the loop as alive from that heartbeat
    orig = dashboard.mode_files
    dashboard.mode_files = lambda mode="live": (state_path,
                                                "/nonexistent.jsonl")
    try:
        h = build_health("live")
    finally:
        dashboard.mode_files = orig
    assert h["loop_alive"] is True and h["last_cycle_ts"] == ts
