# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for tui.py pure logic (no curses screen needed)."""
import os
import pytest
import dashboard
import tui


@pytest.fixture()
def tui_env(tmp_path, monkeypatch):
    p = os.path.join(str(tmp_path), "tui_settings.json")
    monkeypatch.setattr(dashboard, "SETTINGS_PATH", p)
    return p


def test_toggle_bool(tui_env):
    s = dashboard.load_settings()
    out = tui.toggle_bool(s, "auto_trade")
    assert out["auto_trade"] is False
    out2 = tui.toggle_bool(out, "auto_trade")
    assert out2["auto_trade"] is True


def test_cycle_choice(tui_env):
    s = dashboard.load_settings()
    out = tui.cycle_choice(s, "margin_mode", ["ISOLATED", "CROSS"])
    assert out["margin_mode"] == "CROSS"
    out2 = tui.cycle_choice(out, "margin_mode", ["ISOLATED", "CROSS"])
    assert out2["margin_mode"] == "ISOLATED"


def test_set_number_ok(tui_env):
    s = dashboard.load_settings()
    out = tui.set_number(s, "leverage", "20", "int")
    assert out["leverage"] == 20
    out = tui.set_number(s, "risk_pct_per_trade", "0.5", "float")
    assert out["risk_pct_per_trade"] == 0.5


def test_set_number_bad_input(tui_env):
    s = dashboard.load_settings()
    with pytest.raises(ValueError):
        tui.set_number(s, "leverage", "abc", "int")


def test_set_number_rejected_by_validation(tui_env):
    s = dashboard.load_settings()
    with pytest.raises(ValueError):
        tui.set_number(s, "leverage", "999", "int")


def test_fields_all_known_keys():
    known = set(dashboard.DEFAULTS)
    for key, _label, _kind in tui.FIELDS:
        assert key in known, f"unknown settings key {key}"


def test_fmt_helpers():
    st = {"positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01,
                         "leverage": 3, "entry_price": 100.0,
                         "unrealized_pnl": 5.5}],
          "events": [{"at_human": "20:00", "event": "position_opened",
                      "symbol": "BTCUSDT"}]}
    assert "BTCUSDT" in tui.fmt_positions(st)[0]
    assert "position_opened" in tui.fmt_events(st)[0]
    assert tui.fmt_positions({"positions": []}) == ["(tidak ada posisi terbuka)"]
    rows = [{"symbol": "ETHUSDT", "state": "approved", "quality": 80,
             "entry": 100.0, "stop_loss": 98.0},
            {"symbol": "SOLUSDT", "state": "rejected", "reason": "nope"}]
    f = tui.fmt_radar(rows)
    assert "APPROVED" in f[0] and "Q=80" in f[0]
    assert "nope" in f[1]
