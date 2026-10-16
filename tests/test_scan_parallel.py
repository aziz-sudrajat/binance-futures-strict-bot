"""Unit tests for parallel scanning (item 2.1): ThreadPoolExecutor scan.

Covers: result determinism (input order), per-symbol fail-closed in the
parallel path, max_workers=1 identical to the legacy sequential path, the
shared throttled session + per-thread MarketData architecture, and DB
writes from the parallel pass. All I/O is faked — no network.
"""
import os
import random
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_executor import _signal  # noqa: E402

import config  # noqa: E402
import scanner as scanner_mod  # noqa: E402
from signals_db import SignalDB  # noqa: E402


class FakeMarket:
    """Duck-typed MarketData. No .session -> worker falls back to a fresh
    ThrottledSession (fine, since the fake evaluate ignores the market)."""

    def __init__(self, symbols):
        self._symbols = list(symbols)

    def list_okx_swap_symbols(self):
        return list(self._symbols)


def _rig(monkeypatch, behaviors):
    """behaviors: symbol -> 'approve' | 'reject' | 'raise'.

    The fake evaluate sleeps a random jitter so thread completion order is
    (almost surely) NOT the input order — the results must still come back
    in input order.
    """
    rng = random.Random(1234)

    def fake_eval(symbol, market):
        time.sleep(rng.random() * 0.02)
        how = behaviors.get(symbol, "reject")
        if how == "raise":
            raise RuntimeError(f"boom {symbol}")
        if how == "approve":
            s = _signal(symbol)
            s["quality"] = 80
            return s, "stub-approve"
        return None, "stub-reject"

    monkeypatch.setattr(scanner_mod, "evaluate_symbol", fake_eval)
    monkeypatch.setattr(scanner_mod, "signal_candle_close_ms",
                        lambda m, s: 1_700_000_000_000)
    monkeypatch.setattr(scanner_mod, "make_signal_id",
                        lambda s, c, d: f"SID-{s}")
    monkeypatch.setattr(scanner_mod, "THROTTLE_SEC", 0)


def _db(tmp_path):
    return SignalDB(str(tmp_path / "signals.db"))


# ---------------------------------------------------------------- knob

def test_config_scan_max_workers_default():
    assert config.SCAN_MAX_WORKERS == 4


# ------------------------------------------------- determinism + fail-closed

def test_parallel_results_in_input_order(tmp_path, monkeypatch):
    symbols = [f"C{i:02d}USDT" for i in range(16)]
    behaviors = {s: ("approve" if i % 3 == 0 else "reject")
                 for i, s in enumerate(symbols)}
    _rig(monkeypatch, behaviors)
    monkeypatch.chdir(tmp_path)

    results = list(scanner_mod.evaluate_symbols_ordered(
        symbols, FakeMarket(symbols), max_workers=4))
    assert [r["symbol"] for r in results] == symbols  # input order kept
    assert len(results) == len(symbols)
    for r, s in zip(results, symbols):
        if behaviors[s] == "approve":
            assert r["signal"] is not None and r["signal_id"] == f"SID-{s}"
            assert r["error"] is None
        else:
            assert r["signal"] is None and r["error"] is None


def test_parallel_one_error_does_not_kill_others(tmp_path, monkeypatch):
    symbols = ["AAAUSDT", "BOOMUSDT", "CCCUSDT", "DDDUSDT"]
    _rig(monkeypatch, {"AAAUSDT": "approve", "BOOMUSDT": "raise",
                       "CCCUSDT": "approve", "DDDUSDT": "reject"})
    monkeypatch.chdir(tmp_path)

    db = _db(tmp_path)
    stats = scanner_mod.run_pass(FakeMarket(symbols), db, expiry_min=30,
                                 max_workers=4, two_stage=False)
    db.close()

    assert stats["symbols"] == 4
    assert stats["approved"] == 2
    assert stats["rejected"] == 1
    assert stats["errors"] == 1
    db = SignalDB(str(tmp_path / "signals.db"))
    fresh = {row["symbol"] for row in db.get_fresh_signals()}
    assert fresh == {"AAAUSDT", "CCCUSDT"}
    db.close()


# --------------------------------------- max_workers=1 == legacy sequential

def test_run_pass_max_workers_1_matches_parallel(tmp_path, monkeypatch):
    symbols = [f"S{i:02d}USDT" for i in range(10)]
    behaviors = {s: ("approve" if i % 2 == 0 else "reject")
                 for i, s in enumerate(symbols)}
    behaviors[symbols[3]] = "raise"
    _rig(monkeypatch, behaviors)

    def one_pass(workers, name):
        monkeypatch.chdir(tmp_path)
        db = SignalDB(str(tmp_path / f"{name}.db"))
        stats = scanner_mod.run_pass(FakeMarket(symbols), db, expiry_min=30,
                                     max_workers=workers, two_stage=False)
        rows = sorted(r["symbol"] for r in db.get_fresh_signals())
        db.close()
        return stats, rows

    stats_seq, rows_seq = one_pass(1, "seq")
    stats_par, rows_par = one_pass(4, "par")

    for key in ("symbols", "approved", "rejected", "errors"):
        assert stats_seq[key] == stats_par[key], key
    assert rows_seq == rows_par
    assert stats_seq["approved"] == 5  # even indices approved (incl. idx 0)
    assert stats_seq["errors"] == 1
    assert stats_seq["rejected"] == 4


# --------------------------------------------- session / MarketData sharing

def test_parallel_shares_session_but_not_marketdata(tmp_path, monkeypatch):
    symbols = [f"X{i:02d}USDT" for i in range(12)]
    _rig(monkeypatch, {s: "approve" for s in symbols})
    monkeypatch.chdir(tmp_path)

    created = []
    created_lock = threading.Lock()

    class RecordingMarketData:
        def __init__(self, session=None):
            self.session = session
            with created_lock:
                created.append((self, session))

    monkeypatch.setattr(scanner_mod, "MarketData", RecordingMarketData)

    results = list(scanner_mod.evaluate_symbols_ordered(
        symbols, FakeMarket(symbols), max_workers=4))
    assert len(results) == 12
    instances = [c[0] for c in created]
    assert 1 <= len(instances) <= 4  # one MarketData per worker THREAD...
    assert len(instances) < len(symbols)  # ...reused across symbols
    assert len({id(i) for i in instances}) == len(instances)  # never shared
    sessions = [c[1] for c in created]
    assert len({id(s) for s in sessions}) == 1  # one shared session


def test_parallel_reuses_market_session_when_present(tmp_path, monkeypatch):
    symbols = ["Y1USDT", "Y2USDT"]
    _rig(monkeypatch, {s: "approve" for s in symbols})
    monkeypatch.chdir(tmp_path)

    seen = []

    class FakeMD:
        def __init__(self, session=None):
            self.session = session
            seen.append(session)

    class MarketWithSession(FakeMarket):
        session = object()

    monkeypatch.setattr(scanner_mod, "MarketData", FakeMD)
    list(scanner_mod.evaluate_symbols_ordered(
        symbols, MarketWithSession(symbols), max_workers=2))
    assert seen and all(s is MarketWithSession.session for s in seen), \
        "workers must reuse the caller's throttled session"


# ------------------------------------------------------- scan.py --workers

def test_scan_py_parallel_and_sequential_agree(tmp_path, monkeypatch,
                                               capsys):
    import scan as scan_mod

    symbols = ["P1USDT", "P2USDT", "P3USDT", "BOOMUSDT", "P5USDT"]
    behaviors = {s: "approve" for s in symbols}
    behaviors["BOOMUSDT"] = "raise"

    rng = random.Random(99)

    def fake_eval(symbol, market):
        time.sleep(rng.random() * 0.01)
        if behaviors[symbol] == "raise":
            raise RuntimeError("boom")
        s = _signal(symbol)
        for k in ("entry_low", "entry_high", "stop_loss", "tp1", "tp2",
                  "tp3", "rsi", "regime", "deriv_score", "deriv_provider"):
            s.setdefault(k, 1)
        return s, "stub"

    monkeypatch.setattr(scan_mod, "evaluate_symbol", fake_eval)
    # the parallel path delegates to scanner.evaluate_symbols_ordered,
    # which uses scanner's evaluate_symbol binding (same real function
    # in production) — patch it too.
    monkeypatch.setattr(scanner_mod, "evaluate_symbol", fake_eval)
    monkeypatch.setattr(scanner_mod, "signal_candle_close_ms",
                        lambda m, s: 1_700_000_000_000)
    monkeypatch.setattr(scanner_mod, "make_signal_id",
                        lambda s, c, d: f"SID-{s}")

    market = FakeMarket(symbols)
    log1 = str(tmp_path / "paper1.jsonl")
    log2 = str(tmp_path / "paper2.jsonl")
    passed_seq = scan_mod.scan(symbols, log1, market, max_workers=1)
    capsys.readouterr()
    passed_par = scan_mod.scan(symbols, log2, market, max_workers=4)
    out = capsys.readouterr().out

    assert sorted(p["symbol"] for p in passed_seq) == \
        sorted(p["symbol"] for p in passed_par) == \
        ["P1USDT", "P2USDT", "P3USDT", "P5USDT"]
    assert "ERROR" in out  # BOOMUSDT fail-closed, printed not raised
    # log files got the approved signals
    for path in (log1, log2):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 4
