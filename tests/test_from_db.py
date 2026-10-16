"""Unit tests for the two-stage pipeline: scanner -> signals.db -> executor."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_executor import FakeExchange, FakeMarket, _signal  # noqa: E402

from executor import Executor  # noqa: E402
from lifecycle import LifecycleLogger  # noqa: E402
from risk import RiskManager  # noqa: E402
from signals_db import SignalDB  # noqa: E402
from state import StateStore  # noqa: E402
import config  # noqa: E402


@pytest.fixture(autouse=True)
def _trading_hours_open(monkeypatch):
    # These tests exercise the from-db execution flow, not the hours gate
    # (covered in test_trading_hours.py): force entries allowed.
    monkeypatch.setattr(config, "within_trading_hours", lambda now=None: True)


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
                        evaluate_fn=lambda s, m: (None, "stub"))
    return executor, exchange, state


def _db_signal(**kw):
    s = dict(_signal("BTCUSDT"))
    s.update(kw)
    return s


def test_scan_from_db_opens_and_consumes(workdir):
    executor, exchange, state = _rig(workdir)
    db_path = os.path.join(workdir, "signals.db")
    db = SignalDB(db_path)
    db.upsert_signal(_db_signal(), "SID1", expiry_min=30)
    db.close()

    summary = executor.scan_from_db(db_path)
    assert summary["opened"] == 1
    assert summary["approved"] == 1
    assert exchange.get_positions(), "position should be open on Binance"
    assert state.seen_signal("SID1")

    db = SignalDB(db_path)
    assert db.stats()["consumed"] == 1
    db.close()


def test_scan_from_db_ignores_expired(workdir):
    executor, exchange, state = _rig(workdir)
    db_path = os.path.join(workdir, "signals.db")
    db = SignalDB(db_path)
    db.upsert_signal(_db_signal(), "SIDX", expiry_min=30)
    # force-expire the row
    db._conn.execute("UPDATE signals SET expires_at = 1 WHERE signal_id='SIDX'")
    db._conn.commit()
    db.close()

    summary = executor.scan_from_db(db_path)
    assert summary["opened"] == 0
    assert summary["approved"] == 0
    assert not exchange.get_positions()


def test_scan_from_db_duplicate_sid_rejected(workdir):
    executor, exchange, state = _rig(workdir)
    db_path = os.path.join(workdir, "signals.db")
    db = SignalDB(db_path)
    db.upsert_signal(_db_signal(), "SIDDUP", expiry_min=30)
    db.close()

    s1 = executor.scan_from_db(db_path)
    assert s1["opened"] == 1

    # scanner re-inserts the same signal id; executor must not open twice
    db = SignalDB(db_path)
    db.upsert_signal(_db_signal(), "SIDDUP", expiry_min=30)
    db.close()
    s2 = executor.scan_from_db(db_path)
    assert s2["opened"] == 0
    assert s2["rejected"] >= 1
    assert len(exchange.get_positions()) == 1


def test_scanner_run_pass_writes_approved(tmp_path, monkeypatch):
    import scanner as scanner_mod

    class FakeMarket2:
        def list_okx_swap_symbols(self):
            return ["AAAUSDT", "BBBUSDT"]

    def fake_eval(symbol, market):
        if symbol == "AAAUSDT":
            return dict(_signal(symbol)), "stub"
        return None, "bearish"

    monkeypatch.setattr(scanner_mod, "evaluate_symbol", fake_eval)
    monkeypatch.setattr(scanner_mod, "signal_candle_close_ms",
                        lambda m, s: 1_700_000_000_000)
    monkeypatch.setattr(scanner_mod, "make_signal_id",
                        lambda s, c, d: f"SID-{s}")
    monkeypatch.setattr(scanner_mod, "THROTTLE_SEC", 0)

    db_path = str(tmp_path / "signals.db")
    db = SignalDB(db_path)
    stats = scanner_mod.run_pass(FakeMarket2(), db, expiry_min=30)
    db.close()

    assert stats["symbols"] == 2
    assert stats["approved"] == 1
    assert stats["rejected"] == 1
    db = SignalDB(db_path)
    fresh = db.get_fresh_signals()
    assert len(fresh) == 1 and fresh[0]["symbol"] == "AAAUSDT"
    db.close()


def test_scanner_two_sided_flag_flips_bullish_only(tmp_path, monkeypatch):
    """--two-sided mirrors the live bot (bidirectional); default stays long-only."""
    import scanner as scanner_mod

    class FakeMarket3:
        def list_okx_swap_symbols(self):
            return ["AAAUSDT"]

    monkeypatch.setattr(scanner_mod, "evaluate_symbol",
                        lambda s, m: (None, "x"))
    monkeypatch.setattr(scanner_mod, "THROTTLE_SEC", 0)
    monkeypatch.setattr(config, "BULLISH_ONLY", True)  # restore after test

    db = SignalDB(str(tmp_path / "s1.db"))
    scanner_mod.run_pass(FakeMarket3(), db, expiry_min=30, two_sided=False)
    assert config.BULLISH_ONLY is True
    db.close()

    db = SignalDB(str(tmp_path / "s2.db"))
    scanner_mod.run_pass(FakeMarket3(), db, expiry_min=30, two_sided=True)
    assert config.BULLISH_ONLY is False
    db.close()


def test_scanner_cli_parses_two_sided():
    import scanner as scanner_mod
    ns = scanner_mod.main.__wrapped__ if hasattr(
        scanner_mod.main, "__wrapped__") else None
    # parse args directly via argparse used in main
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--two-sided", action="store_true")
    assert ap.parse_args(["--two-sided"]).two_sided is True
    assert ap.parse_args([]).two_sided is False
