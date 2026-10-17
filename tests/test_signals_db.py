# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Unit tests for SignalDB (signal cache)."""
from signals_db import SignalDB


def _sig(sym="BTCUSDT", q=80.0):
    return {"symbol": sym, "direction": "LONG", "quality": q,
            "entry": 100.0, "entry_low": 99.0, "entry_high": 100.5,
            "stop_loss": 97.0, "tp1": 102.0, "tp2": 104.0, "tp3": 106.0,
            "reasons": ["a", "b"], "regime": "TREND"}


def test_upsert_and_get_fresh(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    db.upsert_signal(_sig(), "SID1", expiry_min=30)
    fresh = db.get_fresh_signals()
    assert len(fresh) == 1
    assert fresh[0]["signal_id"] == "SID1"
    assert fresh[0]["reasons"] == ["a", "b"]
    db.close()


def test_upsert_keeps_better_signal(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    id1 = db.upsert_signal(_sig(q=90.0), "SID-HI", expiry_min=30)
    id2 = db.upsert_signal(_sig(q=70.0), "SID-LO", expiry_min=30)
    assert id2 == id1  # worse signal does not replace
    fresh = db.get_fresh_signals()
    assert len(fresh) == 1 and fresh[0]["quality"] == 90.0
    db.close()


def test_upsert_replaces_with_better(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    db.upsert_signal(_sig(q=70.0), "SID-LO", expiry_min=30)
    db.upsert_signal(_sig(q=90.0), "SID-HI", expiry_min=30)
    fresh = db.get_fresh_signals()
    assert len(fresh) == 1 and fresh[0]["signal_id"] == "SID-HI"
    assert db.stats()["expired"] == 1
    db.close()


def test_consume_and_expire(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    rid = db.upsert_signal(_sig(), "SID1", expiry_min=30)
    db.mark_consumed(rid)
    assert db.get_fresh_signals() == []
    assert db.stats()["consumed"] == 1

    db.upsert_signal(_sig(), "SID2", expiry_min=30)
    db._conn.execute("UPDATE signals SET expires_at = 1")
    db._conn.commit()
    assert db.expire_old() >= 1
    assert db.get_fresh_signals() == []
    assert db.stats()["fresh"] == 0
    db.close()


def test_to_signal_roundtrip(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    s = _sig()
    s["strength"] = 4
    s["atr"] = 1.25
    db.upsert_signal(s, "SID1", expiry_min=30)
    row = db.get_fresh_signals()[0]
    sig = db.to_signal(row)
    assert sig["symbol"] == "BTCUSDT"
    assert sig["tp3"] == 106.0
    assert sig["strength"] == 4
    assert sig["atr"] == 1.25
    db.close()



def _sig_strength(sym="BTCUSDT", q=98.0, strength=5):
    s = _sig(sym, q)
    s["strength"] = strength
    return s


def test_strength_migration_and_ordering(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    # same quality -> stronger strength wins, then earliest id
    db.upsert_signal(_sig_strength("AAAUSDT", 98.0, 4), "S1", expiry_min=30)
    db.upsert_signal(_sig_strength("BBBUSDT", 98.0, 5), "S2", expiry_min=30)
    db.upsert_signal(_sig_strength("CCCUSDT", 98.0, 5), "S3", expiry_min=30)
    fresh = db.get_fresh_signals()
    assert [f["symbol"] for f in fresh] == ["BBBUSDT", "CCCUSDT", "AAAUSDT"]
    assert fresh[0]["strength"] == 5
    db.close()


def test_migration_on_existing_db_without_strength(tmp_path):
    import sqlite3
    import signals_db as sdb
    p = str(tmp_path / "old.db")
    # realistic "old" DB: previous code version's schema (no strength column —
    # the CREATE TABLE never had it; only _migrate adds it)
    conn = sqlite3.connect(p)
    conn.executescript(sdb.SCHEMA)
    conn.execute("INSERT INTO signals (symbol, signal_id, direction, quality,"
                 " created_at, expires_at, status)"
                 " VALUES ('BTCUSDT','OLD','LONG',80,1,9999999999999,'new')")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(p)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)")]
    assert "strength" not in cols  # old code never created it
    assert "atr" not in cols
    conn.close()
    db = SignalDB(p)  # must not crash; columns added by _migrate
    cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(signals)")}
    assert "strength" in cols
    assert "atr" in cols
    fresh = db.get_fresh_signals()
    assert len(fresh) == 1 and fresh[0]["symbol"] == "BTCUSDT"
    db.close()


def test_null_strength_ranks_below_numeric_strength(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    sig_null = _sig("AAAUSDT", q=85.0)
    sig_num = _sig_strength("BBBUSDT", q=85.0, strength=10)
    db.upsert_signal(sig_null, "S_NULL", expiry_min=30)
    db.upsert_signal(sig_num, "S_NUM", expiry_min=30)
    fresh = db.get_fresh_signals()
    assert [f["symbol"] for f in fresh] == ["BBBUSDT", "AAAUSDT"]
    db.close()


def test_atr_storage_and_recovery(tmp_path):
    db = SignalDB(str(tmp_path / "s.db"))
    s = _sig()
    s["atr"] = 3.1415
    db.upsert_signal(s, "SATR", expiry_min=30)
    fresh = db.get_fresh_signals()
    assert fresh[0]["atr"] == 3.1415
    sig = db.to_signal(fresh[0])
    assert sig["atr"] == 3.1415
    db.close()


def test_confidence_storage_and_ordering(tmp_path):
    p = str(tmp_path / "test_conf.db")
    db = SignalDB(p)
    s1 = _sig("COIN1", q=80)
    s1["confidence"] = 70
    s2 = _sig("COIN2", q=80)
    s2["confidence"] = 95
    db.upsert_signal(s1, "S1")
    db.upsert_signal(s2, "S2")

    fresh = db.get_fresh_signals()
    assert len(fresh) == 2
    # COIN2 should come first due to higher confidence (95 > 70) with same quality (80)
    assert fresh[0]["symbol"] == "COIN2"
    assert fresh[0]["confidence"] == 95
    sig = db.to_signal(fresh[0])
    assert sig["confidence"] == 95
    db.close()

