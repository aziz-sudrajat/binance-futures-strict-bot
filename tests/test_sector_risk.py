# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# Tests for Sector & Cluster Concentration Risk Guard.

import pytest
import config
from risk import RiskManager
from state import StateStore


class FakeState:
    def __init__(self, positions=None):
        self.data = {"positions": positions or {}, "daily": {}, "risk_metrics": {}}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        pass


def test_get_symbol_sector_mapping():
    """Verify sector clustering maps known symbols to correct sectors."""
    assert config.get_symbol_sector("DOGEUSDT") == "MEME"
    assert config.get_symbol_sector("PEPEUSDT") == "MEME"
    assert config.get_symbol_sector("FETUSDT") == "AI_DATA"
    assert config.get_symbol_sector("BTCUSDT") == "L1_MAJORS"
    assert config.get_symbol_sector("SOLUSDT") == "L1_MAJORS"
    assert config.get_symbol_sector("AAVEUSDT") == "DEFI"
    assert config.get_symbol_sector("GALAUSDT") == "GAMING_METAVERSE"
    assert config.get_symbol_sector("UNKNOWNCOIN") is None


def test_sector_concentration_blocks_duplicate_sector_entry(monkeypatch):
    """Verify check_can_open rejects entry when sector limit (1) is reached."""
    monkeypatch.setattr(config, "MAX_POSITIONS_PER_SECTOR", 1)
    # Holding active DOGEUSDT (MEME)
    positions = {
        "pos_1": {"symbol": "DOGEUSDT", "closed": False, "qty": 100.0}
    }
    state = FakeState(positions)
    rm = RiskManager(state, lambda: 10_000.0)

    # Attempting to open PEPEUSDT (also MEME) -> should reject
    can_open, reason = rm.check_can_open("PEPEUSDT", notional=100.0, open_count=1)
    assert can_open is False
    assert "sector concentration limit reached for 'MEME'" in reason
    assert "already holding DOGEUSDT" in reason


def test_sector_concentration_allows_different_sector_entry(monkeypatch):
    """Verify check_can_open permits opening position from a different sector."""
    monkeypatch.setattr(config, "MAX_POSITIONS_PER_SECTOR", 1)
    positions = {
        "pos_1": {"symbol": "DOGEUSDT", "closed": False, "qty": 100.0}
    }
    state = FakeState(positions)
    rm = RiskManager(state, lambda: 10_000.0)

    # Attempting to open FETUSDT (AI_DATA) -> should pass
    can_open, reason = rm.check_can_open("FETUSDT", notional=100.0, open_count=1)
    assert can_open is True
    assert reason == "ok"


def test_sector_concentration_ignores_closed_positions(monkeypatch):
    """Verify check_can_open ignores previously closed positions in same sector."""
    monkeypatch.setattr(config, "MAX_POSITIONS_PER_SECTOR", 1)
    # DOGEUSDT is closed
    positions = {
        "pos_1": {"symbol": "DOGEUSDT", "closed": True, "qty": 0.0}
    }
    state = FakeState(positions)
    rm = RiskManager(state, lambda: 10_000.0)

    can_open, reason = rm.check_can_open("PEPEUSDT", notional=100.0, open_count=0)
    assert can_open is True
    assert reason == "ok"


def test_sector_concentration_disabled_when_zero(monkeypatch):
    """Verify check_can_open allows multiple sector positions when MAX_POSITIONS_PER_SECTOR=0."""
    monkeypatch.setattr(config, "MAX_POSITIONS_PER_SECTOR", 0)
    positions = {
        "pos_1": {"symbol": "DOGEUSDT", "closed": False, "qty": 100.0}
    }
    state = FakeState(positions)
    rm = RiskManager(state, lambda: 10_000.0)

    can_open, reason = rm.check_can_open("PEPEUSDT", notional=100.0, open_count=1)
    assert can_open is True
    assert reason == "ok"


def test_sector_concentration_permits_uncategorized_symbols(monkeypatch):
    """Verify check_can_open permits uncategorized symbols without sector error."""
    monkeypatch.setattr(config, "MAX_POSITIONS_PER_SECTOR", 1)
    positions = {
        "pos_1": {"symbol": "DOGEUSDT", "closed": False, "qty": 100.0}
    }
    state = FakeState(positions)
    rm = RiskManager(state, lambda: 10_000.0)

    can_open, reason = rm.check_can_open("SOMENEWCOINUSDT", notional=100.0, open_count=1)
    assert can_open is True
    assert reason == "ok"
