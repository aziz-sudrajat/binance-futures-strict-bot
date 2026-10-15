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
    assert load_settings(dash_env) == config.DASHBOARD_DEFAULTS


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


# ------------------------------------------------- studio overrides (config)
@pytest.fixture(autouse=True)
def _reset_config():
    yield
    config.reset_dashboard_overrides()


def test_apply_numeric_overrides(dash_env):
    save_settings({"min_quality_score": 80, "tp1_rr": 2.0,
                   "stop_atr_min": 1.0}, dash_env)
    from executor import Executor as _E  # noqa
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.MIN_QUALITY_SCORE == 80
    assert config.TP1_RR == 2.0
    assert config.STOP_ATR_MIN == 1.0


def test_apply_clamps_out_of_range(dash_env):
    with open(dash_env, "w", encoding="utf-8") as f:
        json.dump({"min_quality_score": 200, "tp1_rr": 99.0}, f)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.MIN_QUALITY_SCORE == 95
    assert config.TP1_RR == 5.0


def test_daily_loss_cap_override(dash_env):
    # Kill-switch cap is a per-cycle dashboard knob (0-100, 100 = off).
    # User 2026-10-15 "habiskan saja saldonya": cap=100 must flow through.
    save_settings({"daily_loss_cap_pct": 100}, dash_env)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.DAILY_LOSS_CAP_PCT == 100


def test_validate_daily_loss_cap(dash_env):
    assert validate_settings({})["daily_loss_cap_pct"] == 3.0
    assert validate_settings({"daily_loss_cap_pct": 100})[
        "daily_loss_cap_pct"] == 100
    assert validate_settings({"daily_loss_cap_pct": 0})[
        "daily_loss_cap_pct"] == 0
    with pytest.raises(ValueError):
        validate_settings({"daily_loss_cap_pct": 101})
    with pytest.raises(ValueError):
        validate_settings({"daily_loss_cap_pct": -1})


def test_apply_tp_split_normalized(dash_env):
    save_settings({"tp_split_1": 1.0, "tp_split_2": 1.0, "tp_split_3": 2.0},
                  dash_env)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.TP_SPLIT == pytest.approx((0.25, 0.25, 0.5))
    assert abs(sum(config.TP_SPLIT) - 1.0) < 1e-9


def test_apply_veto_toggles(dash_env):
    save_settings({"veto_funding": False, "veto_whale": False}, dash_env)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.DISABLED_VETOES == frozenset({"funding", "whale"})


def test_apply_mtf_and_bools(dash_env):
    save_settings({"mtf_30m_mode": "LEAN", "bullish_only": False,
                   "breakeven_after_tp1": False}, dash_env)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.MTF_30M_MODE == "LEAN"
    assert config.BULLISH_ONLY is False
    assert config.EXEC_BREAKEVEN_AFTER_TP1 is False


def test_apply_ignores_invalid_mtf(dash_env):
    with open(dash_env, "w", encoding="utf-8") as f:
        json.dump({"mtf_30m_mode": "YOLO"}, f)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.MTF_30M_MODE == "STRICT"


def test_scan_once_applies_studio_settings(erig):
    executor, _ = erig
    save_settings({"min_quality_score": 95, "tp2_rr": 3.0},
                  config.DASHBOARD_SETTINGS_FILE)
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor.scan_once()
    assert config.MIN_QUALITY_SCORE == 95
    assert config.TP2_RR == 3.0


def test_vetoes_disabled_param():
    from unittest.mock import MagicMock
    import vetoes
    m = MagicMock()
    m.get_advanced_derivatives.side_effect = Exception("no feed")
    snap = {"aggregate_funding": 0.0001}
    full = vetoes.run_vetoes("BTCUSDT", "BULLISH", m, snap, book={"a": 1})
    codes = {b[0] for b in full}
    assert "adv_deriv" in codes  # hold present when vetoes enabled
    off = vetoes.run_vetoes("BTCUSDT", "BULLISH", m, snap, book={"a": 1},
                            disabled={"leverage_risk", "squeeze", "whale"})
    assert "adv_deriv" not in {b[0] for b in off}


def test_studio_validation_accepts_full_settings(dash_env):
    full = dict(config.DASHBOARD_DEFAULTS)
    full.update({"min_quality_score": 80, "mtf_30m_mode": "LEAN",
                 "tp1_rr": 2.0, "veto_funding": False})
    out = save_settings(full, dash_env)
    assert out["min_quality_score"] == 80
    assert out["veto_funding"] is False


@pytest.mark.parametrize("bad", [
    {"mtf_30m_mode": "YOLO"},
    {"tp_split_1": 0.0}, {"tp_split_2": 50.0},
    {"veto_funding": "yes"},
    {"bullish_only": 1},
    {"min_quality_score": 49}, {"min_quality_score": 96},
    {"tp1_rr": 0.1}, {"stop_atr_min": 10},
])
def test_studio_validation_rejects(dash_env, bad):
    with pytest.raises(ValueError):
        save_settings(bad, dash_env)


# ------------------------------------------------- order config + modes
def test_order_fields_validate(dash_env):
    out = save_settings({"margin_mode": "CROSS", "leverage_mode": "fixed",
                         "leverage": 20, "entry_mode": "fixed_usdt",
                         "fixed_usdt": 50}, dash_env)
    assert out["margin_mode"] == "CROSS"
    assert out["leverage"] == 20
    assert out["fixed_usdt"] == 50.0


@pytest.mark.parametrize("bad", [
    {"margin_mode": "MARGIN"},
    {"leverage_mode": "yolo"},
    {"leverage": 0}, {"leverage": 126},
    {"entry_mode": "random"},
    {"fixed_usdt": 0}, {"fixed_usdt": 200_000},
])
def test_order_fields_reject(dash_env, bad):
    with pytest.raises(ValueError):
        save_settings(bad, dash_env)


def test_per_mode_sections(dash_env):
    save_settings({"leverage_mode": "fixed", "leverage": 20}, dash_env,
                  mode="testnet")
    save_settings({"leverage_mode": "auto"}, dash_env, mode="live")
    assert load_settings(dash_env, mode="testnet")["leverage"] == 20
    assert load_settings(dash_env, mode="live")["leverage_mode"] == "auto"
    # untouched mode falls back to defaults
    assert load_settings(dash_env, mode="paper")["auto_trade"] is True


def test_legacy_flat_file_is_live_section(dash_env):
    # legacy flat file: one profile shared by all modes
    with open(dash_env, "w", encoding="utf-8") as f:
        json.dump({"auto_trade": False}, f)
    assert load_settings(dash_env, mode="live")["auto_trade"] is False
    assert load_settings(dash_env, mode="testnet")["auto_trade"] is False


def test_executor_fixed_leverage(erig):
    executor, _ = erig
    save_settings({"leverage_mode": "fixed", "leverage": 20},
                  config.DASHBOARD_SETTINGS_FILE, mode="live")
    assert executor._resolve_leverage("BTCUSDT") == 20


def test_executor_margin_mode_cross(erig):
    executor, exchange = erig
    save_settings({"margin_mode": "CROSS"},
                  config.DASHBOARD_SETTINGS_FILE, mode="live")
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1
    assert ("BTCUSDT", "CROSS") in exchange.margin_calls


def test_executor_fixed_usdt_entry(erig):
    executor, exchange = erig
    save_settings({"entry_mode": "fixed_usdt", "fixed_usdt": 50,
                   "leverage_mode": "fixed", "leverage": 10},
                  config.DASHBOARD_SETTINGS_FILE, mode="live")
    summary = {"approved": 0, "rejected": 0, "opened": 0, "managed": 0,
               "errors": []}
    executor._scan_symbol("BTCUSDT", 10_000.0, summary)
    assert summary["opened"] == 1
    # 50 USDT * 10x / $100 entry = 5.0 qty (floored to step)
    pos = next(iter(executor._open_positions().values()))
    assert pos["qty"] == pytest.approx(5.0)


def test_settings_mode_picks_section(tmp_path, dash_env):
    market = FakeMarket(live=100.0)
    exchange = FakeExchange(balance=10_000.0)
    state = StateStore(os.path.join(str(tmp_path), "s2.json"))
    lifecycle = LifecycleLogger(os.path.join(str(tmp_path), "l2.jsonl"))
    risk = RiskManager(state, exchange.get_balance)
    ex = Executor(market, exchange, risk, state, lifecycle,
                  universe=["BTCUSDT"], settings_mode="testnet")
    save_settings({"auto_trade": False}, config.DASHBOARD_SETTINGS_FILE,
                  mode="testnet")
    save_settings({"auto_trade": True}, config.DASHBOARD_SETTINGS_FILE,
                  mode="live")
    assert ex._dash_settings()["auto_trade"] is False


def test_bot_mode_env_selects_testnet(monkeypatch):
    import run_executor
    from run_executor import main
    monkeypatch.setenv("BOT_MODE", "demo")
    # demo -> testnet -> needs testnet keys -> refuses without them
    with pytest.raises(SystemExit):
        main(["--balance"])


def test_bot_mode_env_selects_live(monkeypatch):
    from run_executor import main
    monkeypatch.setenv("BOT_MODE", "live")
    with pytest.raises(SystemExit):
        main(["--balance"])


def test_explicit_flag_beats_bot_mode(monkeypatch):
    import run_executor
    from run_executor import main
    monkeypatch.setenv("BOT_MODE", "demo")
    # explicit --live ignores BOT_MODE=demo -> needs live keys
    with pytest.raises(SystemExit) as ei:
        main(["--live", "--balance"])
    assert "BINANCE_LIVE_KEY" in str(ei.value)


def test_validate_preserves_mtf_require_1d(dash_env):
    # Regression: a new bool knob must survive validate_settings, otherwise
    # the biasa preset's 1d relaxation would be silently dropped on save.
    out = validate_settings({"mtf_require_1d": False})
    assert out["mtf_require_1d"] is False
    out = validate_settings({"mtf_require_1d": True})
    assert out["mtf_require_1d"] is True
    assert validate_settings({})["mtf_require_1d"] is True  # default
    with __import__("pytest").raises(ValueError):
        validate_settings({"mtf_require_1d": "nope"})


def test_apply_mtf_require_1d_override(dash_env):
    save_settings({"mtf_require_1d": False}, dash_env)
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.settings_mode = "live"
    config.apply_dashboard_overrides(e._dash_settings())
    assert config.MTF_REQUIRE_1D is False
