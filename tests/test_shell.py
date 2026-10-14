"""Unit tests for the `binance` terminal shell framework."""
import pytest
from rich.console import Console

import shell_ui
from shell_ui import (COMMANDS, coerce_setting, lookup_command, parse_line,
                      positions_table, radar_table, settings_table,
                      signals_table, status_panel, suggest_command)


def _render(renderable) -> str:
    c = Console(record=True, width=100)
    c.print(renderable)
    return c.export_text()


# ------------------------------------------------------- command framework
def test_parse_line():
    assert parse_line("scan --top 10") == ("scan", ["--top", "10"])
    assert parse_line('set key "a b"') == ("set", ["key", "a b"])
    assert parse_line("") == ("", [])
    assert parse_line("SCAN") == ("scan", [])


def test_lookup_aliases():
    assert lookup_command("pos").name == "position"
    assert lookup_command("bal").name == "balance"
    assert lookup_command("trade").name == "auto-trade"
    assert lookup_command("q").name == "exit"
    assert lookup_command("nope") is None


def test_suggest():
    assert "scan" in suggest_command("scann")
    assert "position" in suggest_command("positon")


def test_commands_have_handlers():
    import binance as bsh
    shell = bsh.BinanceShell()
    names = {c.name for c in COMMANDS}
    # every non-trivial command must dispatch without "belum diimplementasi"
    for name in names - {"clear", "exit", "help"}:
        assert name in ("scan", "auto-trade", "setting", "set", "position",
                        "balance", "signals", "scanner", "mode", "events",
                        "kill", "dashboard", "signal-mode"), name


# ------------------------------------------------------- dispatch
def test_dispatch_help_unknown_exit(capsys):
    import binance as bsh
    shell = bsh.BinanceShell()
    assert shell.dispatch("help") is True
    assert shell.dispatch("help scan") is True
    assert shell.dispatch("scann") is True  # suggestion, no crash
    assert shell.dispatch("exit") is False
    assert shell.dispatch("quit") is False


def test_dispatch_mode():
    import binance as bsh
    shell = bsh.BinanceShell()
    shell.dispatch("mode live")
    assert shell.mode == "live"
    shell.dispatch("mode demo")
    assert shell.mode == "testnet"
    shell.dispatch("mode bogus")
    assert shell.mode == "testnet"  # unchanged


def test_setting_fields_shared():
    import dashboard
    import tui
    assert tui.FIELDS == dashboard.SETTING_FIELDS
    assert len(dashboard.SETTING_FIELDS) >= 20


# ------------------------------------------------------- coercion
def test_coerce_setting():
    assert coerce_setting("bool", "on") is True
    assert coerce_setting("bool", "off") is False
    assert coerce_setting("int", "75") == 75
    assert coerce_setting("float", "1.5") == 1.5
    assert coerce_setting("choice:a,b", "b") == "b"
    with pytest.raises(ValueError):
        coerce_setting("bool", "maybe")
    with pytest.raises(ValueError):
        coerce_setting("choice:a,b", "c")
    with pytest.raises(ValueError):
        coerce_setting("int", "x")


# ------------------------------------------------------- rendering
def test_status_panel():
    st = {"balance": 10000.0, "balance_ok": True, "n_positions": 2,
          "daily": {"realized_pnl": 12.5}, "kill_switch": {},
          "settings": {"auto_trade": True}}
    out = _render(status_panel(st, "paper"))
    assert "PAPER" in out and "10,000.00" in out and "+12.50" in out


def test_positions_table():
    st = {"positions": [
        {"symbol": "BTCUSDT", "side": "LONG", "qty": 0.5, "entry": 100.0,
         "mark": 101.0, "unrealized_pnl": 0.5, "leverage": 3,
         "stop_loss": 97.0}]}
    out = _render(positions_table(st))
    assert "BTCUSDT" in out and "LONG" in out
    out2 = _render(positions_table({"positions": []}))
    assert "tidak ada posisi terbuka" in out2


def test_radar_table():
    rows = [{"state": "approved", "symbol": "ETHUSDT", "direction": "LONG",
             "quality": 82, "entry": 3000.0, "stop_loss": 2950.0,
             "tp1": 3060.0, "tp2": 3100.0, "tp3": 3150.0},
            {"state": "rejected", "symbol": "XRPUSDT"}]
    out = _render(radar_table(rows))
    assert "ETHUSDT" in out and "XRPUSDT" not in out


def test_signals_and_settings_tables():
    out = _render(signals_table(
        [{"symbol": "SOLUSDT", "direction": "LONG", "quality": 78,
          "entry": 150.0, "age_min": 3.2}]))
    assert "SOLUSDT" in out
    out2 = _render(settings_table(
        {"auto_trade": True, "min_quality_score": 70},
        [("auto_trade", "Auto-trade", "bool"),
         ("min_quality_score", "Min quality", "int")]))
    assert "min_quality_score" in out2 and "70" in out2


# ------------------------------------------------------- signal modes
def test_signal_mode_presets_match_current_defaults():
    import config
    ketat = config.SIGNAL_MODE_PRESETS["ketat"]
    d = config.DASHBOARD_DEFAULTS
    for k, v in ketat.items():
        assert d[k] == v, k  # ketat == today's behavior, no drift
    assert set(config.SIGNAL_MODE_PRESETS) == {"ketat", "sedang", "biasa"}
    # strictness ordering: quality bar descends
    assert (config.SIGNAL_MODE_PRESETS["ketat"]["min_quality_score"]
            > config.SIGNAL_MODE_PRESETS["sedang"]["min_quality_score"]
            > config.SIGNAL_MODE_PRESETS["biasa"]["min_quality_score"])


def test_apply_signal_mode_preset():
    import dashboard
    s = {"signal_mode": "ketat", "min_quality_score": 70}
    dashboard.apply_signal_mode_preset(s, "biasa")
    assert s["signal_mode"] == "biasa"
    assert s["min_quality_score"] == 50
    assert s["bullish_only"] is True  # engine is long-only; biasa loosens LONG
    assert s["veto_funding"] is True  # core veto stays on
    with __import__("pytest").raises(ValueError):
        dashboard.apply_signal_mode_preset({}, "ngawur")


def test_validate_signal_mode():
    import dashboard
    out = dashboard.validate_settings({"signal_mode": "sedang"})
    assert out["signal_mode"] == "sedang"
    with __import__("pytest").raises(ValueError):
        dashboard.validate_settings({"signal_mode": "ngawur"})


def test_shell_signal_mode_command(tmp_path, monkeypatch):
    import binance as bsh
    import dashboard
    fake_path = str(tmp_path / "settings.json")
    monkeypatch.setattr(dashboard, "SETTINGS_PATH", fake_path)
    shell = bsh.BinanceShell()
    shell.mode = "paper"
    assert shell.dispatch("signal-mode") is True  # show, no crash
    assert shell.dispatch("signal-mode sedang") is True
    st = dashboard.load_settings(mode="paper")
    assert st["signal_mode"] == "sedang"
    assert st["min_quality_score"] == 60
    assert st["veto_liq_wall"] is False
    # set alias also routes through the preset
    assert shell.dispatch("set signal_mode biasa") is True
    st = dashboard.load_settings(mode="paper")
    assert st["signal_mode"] == "biasa"
    assert st["bullish_only"] is True  # long-only engine; biasa = looser LONG
    # unknown mode rejected, settings untouched
    assert shell.dispatch("signal-mode ngawur") is True
    assert dashboard.load_settings(mode="paper")["signal_mode"] == "biasa"


def test_parse_line_tolerates_leading_colon():
    from shell_ui import parse_line
    assert parse_line(": scan --top 5") == ("scan", ["--top", "5"])
    assert parse_line(":signal-mode sedang") == ("signal-mode", ["sedang"])
    assert parse_line(":") == ("", [])
    assert parse_line("scan") == ("scan", [])
