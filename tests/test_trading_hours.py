# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for the trading-hours gate (LIVE entries 05:00-18:00 WIB)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
from config import within_trading_hours


WIB = ZoneInfo("Asia/Jakarta")


def wib(h, m):
    return datetime(2026, 10, 15, h, m, tzinfo=WIB)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(config, "TRADING_HOURS_ENABLED", True)
    monkeypatch.setattr(config, "TRADING_HOURS_START", "05:00")
    monkeypatch.setattr(config, "TRADING_HOURS_END", "18:00")
    monkeypatch.setattr(config, "TRADING_HOURS_TZ", "Asia/Jakarta")


def test_inside_window():
    assert within_trading_hours(wib(5, 0)) is True
    assert within_trading_hours(wib(12, 30)) is True
    assert within_trading_hours(wib(17, 59)) is True


def test_outside_window():
    assert within_trading_hours(wib(4, 59)) is False
    assert within_trading_hours(wib(18, 0)) is False
    assert within_trading_hours(wib(23, 15)) is False
    assert within_trading_hours(wib(0, 0)) is False


def test_aware_utc_converted_to_wib():
    # 2026-10-15 00:30 UTC == 07:30 WIB -> inside
    assert within_trading_hours(
        datetime(2026, 10, 15, 0, 30, tzinfo=ZoneInfo("UTC"))) is True
    # 2026-10-14 21:30 UTC == 2026-10-15 04:30 WIB -> outside
    assert within_trading_hours(
        datetime(2026, 10, 14, 21, 30, tzinfo=ZoneInfo("UTC"))) is False


def test_naive_assumed_wib():
    assert within_trading_hours(datetime(2026, 10, 15, 10, 0)) is True
    assert within_trading_hours(datetime(2026, 10, 15, 22, 0)) is False


def test_disabled_gate_always_open(monkeypatch):
    monkeypatch.setattr(config, "TRADING_HOURS_ENABLED", False)
    assert within_trading_hours(wib(3, 0)) is True
    assert within_trading_hours(wib(23, 0)) is True


def test_overnight_window(monkeypatch):
    monkeypatch.setattr(config, "TRADING_HOURS_START", "22:00")
    monkeypatch.setattr(config, "TRADING_HOURS_END", "06:00")
    assert within_trading_hours(wib(23, 0)) is True
    assert within_trading_hours(wib(5, 59)) is True
    assert within_trading_hours(wib(6, 0)) is False
    assert within_trading_hours(wib(12, 0)) is False


def test_dashboard_override_disables_gate():
    config.TRADING_HOURS_ENABLED = True
    config.apply_dashboard_overrides({"trading_hours_enabled": False})
    try:
        assert config.TRADING_HOURS_ENABLED is False
        assert within_trading_hours(wib(3, 0)) is True
    finally:
        config.apply_dashboard_overrides({"trading_hours_enabled": True})
