"""Tests for dashboard.py settings + executor integration."""
import json
import os

import pytest

import config
import dashboard
from dashboard import (load_settings, save_settings, validate_settings,
                       build_status, read_events, mode_files)
from executor import Executor, compute_qty
from lifecycle import LifecycleLogger
from risk import RiskManager
from state import StateStore
from tests.test_executor import FakeExchange, FakeMarket, _signal


@pytest.fixture
def dash_env(tmp_path, monkeypatch):
    """Point the dashboard settings file at a tmp path."""
    p = str(tmp_path / "dashboard_settings.json")
    monkeypatch.setattr(config, "DASHBOARD_SETTINGS_FILE", p)
    monkeypatch.setattr(dashboard, "SETTINGS_PATH", p)
    return p


@pytest.fixture
def erig(tmp_path, dash_env):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    exchange.market_price = {"BTCUSDT": 100.0}
    state = StateStore(os.path.join(str(tmp_path), "state.json"))
    lifecycle = LifecycleLogger(os.path.join(str(tmp_path), "lifecycle.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ev = lambda symbol, m: (_signal(symbol), "stub")  # noqa: E731
    executor = Executor(market, exchange, risk, state, lifecycle,
                        universe=["BTCUSDT"], evaluate_fn=ev)
    return executor, exchange


# ------------------------------------------------------------ validation
def test_defaults_when_missing(dash_env):
    s = load_settings(dash_env)
    assert s == {"auto_trade": True, "risk_pct_per_trade": 1.0,
                "max_leverage": 3, "poll_interval_min": 5}


def test_save_load_roundtrip(dash_env):
    out = save_settings({"auto_trade": False, "risk_pct_per_trade": 0.5,
                         "max_leverage": 2, "poll_interval_min": 10},
                        dash_env)
    assert out["auto_trade"] is False
    assert load_settings(dash_env) == out


def test_partial_update_keeps_rest(dash_env):
    save_settings({"risk_pct_per_trade": 0.5}, dash_env)
    merged = load_settings(dash_env)
    merged.update({"auto_trade": False})
    out = save_settings(merged, dash_env)
    assert out["risk_pct_per_trade"] == 0.5 and out["auto_trade"] is False


@pytest.mark.parametrize("bad", [
    {"risk_pct_per_trade": 0}, {"risk_pct_per_trade": 1.5},
    {"risk_pct_per_trade": 99}, {"risk_pct_per_trade": "x"},
    {"max_leverage": 0}, {"max_leverage": 4}, {"max_leverage": 125},
    {"max_leverage": "x"}, {"auto_trade": "yes"}, {"auto_trade": 1},
    {"poll_interval_min": 0}, {"poll_interval_min": 61},
])
def test_rejects_bad_settings(dash_env, bad):
    with pytest.raises(ValueError):
        save_settings(bad, dash_env)


def test_tighten_only_never_exceeds_code_defaults(dash_env):
    # Even if the file is hand-edited above the caps, the executor clamps.
    with open(dash_env, "w", encoding="utf-8") as f:
        json.dump({"risk_pct_per_trade": 50.0, "max_leverage": 125}, f)
    assert load_settings(dash_env)["risk_pct_per_trade"] == 50.0  # raw read
    # ...but validate_ on save refuses it
    with pytest.raises(ValueError):
        save_settings({"risk_pct_per_trade": 50.0}, dash_env)


# ------------------------------------------------------------ executor hook
def test_auto_trade_off_blocks_new_positions(erig):
    executor, exchange = erig
    save_settings({"auto_trade": False}, config.DASHBOARD_SETTINGS_FILE)
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["approved"] == 1
    assert summary["opened"] == 0
    assert summary["rejected"] == 1
    assert executor._open_positions() == {}


def test_auto_trade_on_opens(erig):
    executor, exchange = erig
    save_settings({"auto_trade": True}, config.DASHBOARD_SETTINGS_FILE)
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1


def test_dashboard_tightens_leverage(erig):
    executor, exchange = erig
    assert executor._resolve_leverage("BTCUSDT") == 3  # default path
    save_settings({"max_leverage": 2}, config.DASHBOARD_SETTINGS_FILE)
    assert executor._resolve_leverage("BTCUSDT") == 2
    # hand-edited above the code cap -> clamped to the code default
    with open(config.DASHBOARD_SETTINGS_FILE, "w",
              encoding="utf-8") as f:
        json.dump({"max_leverage": 125}, f)
    assert executor._resolve_leverage("BTCUSDT") == 3


def test_dashboard_tightens_risk(erig):
    executor, exchange = erig
    assert executor._effective_risk_pct() == 1.0
    save_settings({"risk_pct_per_trade": 0.5},
                  config.DASHBOARD_SETTINGS_FILE)
    assert executor._effective_risk_pct() == 0.5
    filters = {"stepSize": 0.001, "minQty": 0.001, "minNotional": 5.0}
    # wide stop so the 20%-of-balance exposure cap doesn't bind
    q_full, _ = compute_qty(10_000.0, 100.0, 90.0, filters, risk_pct=1.0)
    q_half, _ = compute_qty(10_000.0, 100.0, 90.0, filters, risk_pct=0.5)
    assert q_full == pytest.approx(10.0)
    assert q_half == pytest.approx(5.0)


# ------------------------------------------------------------------ status
def test_mode_files():
    s, l = mode_files("live")
    assert "live" in s and "live" in l
    s2, _ = mode_files("testnet")
    assert "live" not in s2


def test_build_status_paper(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_STATE_FILE",
                        str(tmp_path / "state.json"))
    st = build_status("paper")
    assert st["mode"] == "paper"
    assert st["balance_ok"] is True  # PaperBroker needs no keys
    assert st["positions"] == []
    assert st["settings"]["auto_trade"] is True


def test_read_events_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "nope.jsonl"))
    assert read_events("live") == []
