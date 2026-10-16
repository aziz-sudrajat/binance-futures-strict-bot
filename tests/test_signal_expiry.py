"""Tests for live signal expiry (1.4): signals.db rows older than
LIVE_SIGNAL_EXPIRY_HOURS are rejected fail-closed, independent of the
scanner-set expires_at and of the live-price freshness re-check."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_from_db import _db_signal, _rig  # noqa: E402

from signals_db import SignalDB  # noqa: E402
import config  # noqa: E402


@pytest.fixture(autouse=True)
def _trading_hours_open(monkeypatch):
    # These tests exercise the expiry gate, not the hours gate
    # (covered in test_trading_hours.py): force entries allowed.
    monkeypatch.setattr(config, "within_trading_hours", lambda now=None: True)


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def _upsert_aged(workdir, age_hours, knob_hours=24, expiry_min=60 * 26):
    """Insert a signal row aged `age_hours` but still within expires_at.

    The long expires_at keeps the row "fresh" per the DB layer, so the test
    isolates the new created_at age gate (executor crash/restart scenario).
    """
    executor, exchange, state = _rig(workdir)
    db_path = os.path.join(workdir, "signals.db")
    db = SignalDB(db_path)
    db.upsert_signal(_db_signal(), "SID-AGE", expiry_min=expiry_min)
    now_ms = int(time.time() * 1000)
    created_ms = int(now_ms - age_hours * 3_600_000)
    db._conn.execute("UPDATE signals SET created_at = ? "
                     "WHERE signal_id = 'SID-AGE'", (created_ms,))
    db._conn.commit()
    db.close()
    return executor, exchange, db_path


def _status(db_path, signal_id="SID-AGE"):
    db = SignalDB(db_path)
    try:
        cur = db._conn.execute("SELECT status FROM signals "
                               "WHERE signal_id = ?", (signal_id,))
        row = cur.fetchone()
        return row["status"] if row else None
    finally:
        db.close()


def test_stale_signal_rejected_fail_closed(workdir, monkeypatch):
    monkeypatch.setattr(config, "LIVE_SIGNAL_EXPIRY_HOURS", 24)
    executor, exchange, db_path = _upsert_aged(workdir, age_hours=25)

    summary = executor.scan_from_db(db_path)

    assert summary["opened"] == 0, "stale signal must never open a position"
    assert summary["approved"] == 0, "stale signal must not count as approved"
    assert summary["rejected"] >= 1
    assert not exchange.get_positions()
    assert _status(db_path) == "consumed", \
        "stale row must be marked consumed (never retried)"


def test_fresh_signal_accepted(workdir, monkeypatch):
    monkeypatch.setattr(config, "LIVE_SIGNAL_EXPIRY_HOURS", 24)
    executor, exchange, db_path = _upsert_aged(workdir, age_hours=1)

    summary = executor.scan_from_db(db_path)

    assert summary["opened"] == 1, "fresh signal must still be executed"
    assert summary["approved"] == 1
    assert exchange.get_positions()


def _knob_via_dashboard(monkeypatch, executor, hours):
    """Set the expiry knob through the dashboard settings path (production).

    scan_from_db applies dashboard overrides at cycle start, so patching
    the config attribute directly is clobbered — the knob must go through
    _dash_settings like the real dashboard does.
    """
    monkeypatch.setattr(executor, "_dash_settings",
                        lambda: {"live_signal_expiry_hours": hours})


def test_boundary_age_just_under_knob_accepted(workdir, monkeypatch):
    executor, exchange, db_path = _upsert_aged(workdir, age_hours=1.9)
    _knob_via_dashboard(monkeypatch, executor, 2)

    summary = executor.scan_from_db(db_path)
    assert summary["opened"] == 1, "age below the knob must be accepted"


def test_boundary_age_just_over_knob_rejected(workdir, monkeypatch):
    executor, exchange, db_path = _upsert_aged(workdir, age_hours=2.1)
    _knob_via_dashboard(monkeypatch, executor, 2)

    summary = executor.scan_from_db(db_path)
    assert summary["opened"] == 0, "age above the knob must be rejected"
    assert summary["rejected"] >= 1
    assert _status(db_path) == "consumed"


def test_missing_created_at_rejected_fail_closed(workdir, monkeypatch):
    """created_at missing/zero is unverifiable data -> no decision, no entry."""
    monkeypatch.setattr(config, "LIVE_SIGNAL_EXPIRY_HOURS", 24)
    executor, exchange, state = _rig(workdir)
    db_path = os.path.join(workdir, "signals.db")
    db = SignalDB(db_path)
    db.upsert_signal(_db_signal(), "SID-NOAGE", expiry_min=60 * 26)
    db._conn.execute("UPDATE signals SET created_at = 0 "
                     "WHERE signal_id = 'SID-NOAGE'")
    db._conn.commit()
    db.close()

    summary = executor.scan_from_db(db_path)

    assert summary["opened"] == 0
    assert summary["rejected"] >= 1
    assert not exchange.get_positions()
    assert _status(db_path, "SID-NOAGE") == "consumed"
