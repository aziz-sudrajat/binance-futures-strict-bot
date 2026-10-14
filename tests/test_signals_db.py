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
    db.upsert_signal(_sig(), "SID1", expiry_min=30)
    row = db.get_fresh_signals()[0]
    sig = db.to_signal(row)
    assert sig["symbol"] == "BTCUSDT"
    assert sig["tp3"] == 106.0
    db.close()
