# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# run_executor.py --health flag + _health_report tests.
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_executor import _health_report  # noqa: E402


# -------------------------------------------------------------------- helpers
class _State:
    def __init__(self, closed_map):
        self._closed = closed_map

    def get(self, k, default=None):
        if k == "positions":
            return self._closed
        return default


class _Risk:
    def __init__(self, active=False):
        self.active = active

    def kill_switch_active(self):
        return self.active


class _Exchange:
    def __init__(self, balance=100.0):
        self.balance = balance

    def get_balance(self):
        return self.balance


class _Executor:
    def __init__(self, positions_closed=None, ks_active=False, balance=None,
                 mode="paper"):
        self.state = _State(positions_closed or {})
        self.risk = _Risk(active=ks_active)
        self.exchange = _Exchange(balance=balance)
        self.mode = mode

    def kill_switch_active(self):
        return self._kill


# ------------------------------------------------------------- health report
def test_health_report_counts_open_positions():
    ex = _Executor(positions_closed={"p1": {"closed": False},
                                    "p2": {"closed": True}})
    r = _health_report(ex)
    assert r["positions_active"] == 1


def test_health_report_kill_switch_and_balance():
    ex = _Executor(ks_active=False, balance=250.5)
    r = _health_report(ex)
    assert r["kill_switch_active"] is False
    assert r["balance_usdt"] == 250.5


def test_health_report_status_ok():
    ex = _Executor()
    r = _health_report(ex)
    assert r["status"] == "ok"
    assert r["mode"] == "paper"


def test_health_report_returns_zeros_on_missing_proc():
    # On non-Linux, /proc/self/stat fails; uptime should default to 0.
    orig_open = open
    original_stat = None

    class _FakeStat:
        def __enter__(self):
            raise FileNotFoundError("/proc/self/stat")
        def __exit__(self, *a):
            return False

    import builtins
    orig_open = builtins.open
    def fake_open(path, *a, **k):
        if path == "/proc/self/stat":
            raise FileNotFoundError(path)
        return orig_open(path, *a, **k)

    try:
        builtins.open = fake_open
        ex = _Executor()
        r = _health_report(ex)
        assert r["uptime_s"] == 0
    finally:
        builtins.open = orig_open
