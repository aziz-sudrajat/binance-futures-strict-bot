# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# Tests for V19 Funding Window Proximity Veto.

import time
import pytest
import config
import vetoes


class MockMarket:
    def get_order_book(self, symbol):
        return {"mid": 100.0, "asks": [], "bids": []}

    def get_klines(self, symbol, interval, limit=100):
        return [{"low": 50.0, "high": 150.0, "close": 100.0} for _ in range(limit)]

    def get_advanced_derivatives(self, symbol):
        return {
            "retail_long": 50.0,
            "funding": 0.0001,
            "oi_usd": 10_000_000,
            "volume_24h": 50_000_000,
            "spike": 0.0,
            "oi_change": 0.0,
            "top_long": 50.0,
            "divergence": 0.0,
            "long_liq_usd": 0.0,
            "short_liq_usd": 0.0,
        }


def test_funding_proximity_vetoes_long_near_snapshot(monkeypatch):
    """Test LONG is vetoed when 10 min before 8h funding and funding is +0.06%."""
    market = MockMarket()
    # 8h = 28800s. Set time to 28800 - 600 = 28200 (10 mins before snapshot)
    monkeypatch.setattr(time, "time", lambda: 28200.0)

    snap = {"aggregate_funding": 0.0006}  # +0.06%
    blocks = vetoes.run_vetoes("BTCUSDT", "BULLISH", market, snap,
                               skip_deriv_vetoes=False)
    codes = [b[0] for b in blocks]
    assert "funding_window" in codes
    reason = [b[2] for b in blocks if b[0] == "funding_window"][0]
    assert "immediate funding drag" in reason


def test_funding_proximity_vetoes_short_near_snapshot(monkeypatch):
    """Test SHORT is vetoed when 5 min before 8h funding and funding is -0.06%."""
    market = MockMarket()
    # 5 mins before snapshot = 28800 - 300 = 28500
    monkeypatch.setattr(time, "time", lambda: 28500.0)

    snap = {"aggregate_funding": -0.0006}  # -0.06%
    blocks = vetoes.run_vetoes("BTCUSDT", "BEARISH", market, snap,
                               skip_deriv_vetoes=False)
    codes = [b[0] for b in blocks]
    assert "funding_window" in codes


def test_funding_proximity_allows_benign_funding_near_snapshot(monkeypatch):
    """Test LONG is allowed when 10 min before snapshot but funding is mild (+0.01%)."""
    market = MockMarket()
    monkeypatch.setattr(time, "time", lambda: 28200.0)

    snap = {"aggregate_funding": 0.0001}  # +0.01% < 0.05% threshold
    blocks = vetoes.run_vetoes("BTCUSDT", "BULLISH", market, snap,
                               skip_deriv_vetoes=False)
    codes = [b[0] for b in blocks]
    assert "funding_window" not in codes


def test_funding_proximity_allows_high_funding_far_from_snapshot(monkeypatch):
    """Test LONG is allowed when 2 hours away from funding snapshot even with high funding."""
    market = MockMarket()
    # 2 hours away = 28800 - 7200 = 21600 (120 mins before snapshot > 15m)
    monkeypatch.setattr(time, "time", lambda: 21600.0)

    snap = {"aggregate_funding": 0.0008}  # +0.08%
    blocks = vetoes.run_vetoes("BTCUSDT", "BULLISH", market, snap,
                               skip_deriv_vetoes=False)
    codes = [b[0] for b in blocks]
    assert "funding_window" not in codes


def test_funding_proximity_honors_disabled_veto(monkeypatch):
    """Test funding_window veto does not fire if disabled in config or overrides."""
    market = MockMarket()
    monkeypatch.setattr(time, "time", lambda: 28200.0)
    snap = {"aggregate_funding": 0.0008}

    # Pass disabled set
    blocks = vetoes.run_vetoes("BTCUSDT", "BULLISH", market, snap,
                               disabled=frozenset({"funding_window"}),
                               skip_deriv_vetoes=False)
    codes = [b[0] for b in blocks]
    assert "funding_window" not in codes
