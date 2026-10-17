# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Signal cache — SQLite database shared by the scanner and the executor.

Architecture:
  scanner.py  --(writes)--> signals.db --(reads)--> run_executor.py --from-db

The scanner evaluates ALL OKX perpetuals at its own pace and stores APPROVED
signals here. The Binance executor no longer scans the market itself; each
cycle it takes the freshest unconsumed signals from this DB and executes
them (freshness re-check + risk gates still apply at execution time).

A signal is "fresh" when status='new' and expires_at > now. The executor
marks a signal 'consumed' once acted on (opened or rejected at execution);
the scanner marks rows 'expired' when past expires_at.
"""

from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol       TEXT NOT NULL,
  signal_id    TEXT NOT NULL,
  direction    TEXT NOT NULL,
  quality      REAL NOT NULL,
  confidence   REAL DEFAULT 0,
  entry        REAL,
  entry_low    REAL,
  entry_high   REAL,
  stop_loss    REAL,
  tp1          REAL,
  tp2          REAL,
  tp3          REAL,
  reasons      TEXT,          -- JSON list
  regime       TEXT,
  source       TEXT DEFAULT 'okx',
  created_at   INTEGER NOT NULL,   -- ms
  expires_at   INTEGER NOT NULL,   -- ms
  status       TEXT DEFAULT 'new', -- new | consumed | expired
  consumed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_lookup
  ON signals(status, expires_at, quality DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol
  ON signals(symbol, status);
"""

_COLUMNS = ("id", "symbol", "signal_id", "direction", "quality", "confidence",
            "strength", "entry",
            "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3",
            "atr",
            "reasons", "regime", "source", "created_at", "expires_at",
            "status", "consumed_at")


class SignalDB:
    def __init__(self, path: str = "signals.db"):
        self.path = path
        self._conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Lightweight migrations for existing DBs (additive only)."""
        cols = {r["name"] for r in
                self._conn.execute("PRAGMA table_info(signals)")}
        if "strength" not in cols:
            self._conn.execute("ALTER TABLE signals ADD COLUMN strength REAL")
        if "atr" not in cols:
            self._conn.execute("ALTER TABLE signals ADD COLUMN atr REAL")
        if "confidence" not in cols:
            self._conn.execute("ALTER TABLE signals ADD COLUMN confidence REAL DEFAULT 0")

    def close(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- writes
    def upsert_signal(self, signal: dict, signal_id: str,
                      expiry_min: int = 30,
                      source: str = "okx") -> int:
        """Store an APPROVED signal.

        One active ('new') signal per symbol: an existing active row for the
        symbol is replaced only when the new signal is strictly better
        (higher quality/confidence) or the old one already expired.
        Returns the row id.
        """
        now_ms = int(time.time() * 1000)
        expires_at = now_ms + int(expiry_min) * 60_000
        cur = self._conn.execute(
            "SELECT id, quality, confidence, strength, expires_at FROM signals "
            "WHERE symbol = ? AND status = 'new' "
            "ORDER BY quality DESC, COALESCE(confidence, 0) DESC, COALESCE(strength, 0) DESC LIMIT 1",
            (signal["symbol"],))
        row = cur.fetchone()
        sig_qual = float(signal.get("quality") or 0)
        sig_conf = float(signal.get("confidence") or 0)
        if row and row["expires_at"] > now_ms:
            row_qual = float(row["quality"] or 0)
            row_conf = float(row["confidence"] or 0)
            if (row_qual, row_conf) >= (sig_qual, sig_conf):
                return row["id"]  # keep the better existing signal
        if row:
            self._conn.execute("UPDATE signals SET status = 'expired' "
                               "WHERE id = ?", (row["id"],))
        cur = self._conn.execute(
            """INSERT INTO signals
               (symbol, signal_id, direction, quality, confidence, strength, entry,
                entry_low, entry_high, stop_loss, tp1, tp2, tp3, atr, reasons,
                regime, source, created_at, expires_at, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal["symbol"], signal_id, signal.get("direction", "LONG"),
             sig_qual, sig_conf,
             signal.get("strength"),
             signal.get("entry"), signal.get("entry_low"),
             signal.get("entry_high"), signal.get("stop_loss"),
             signal.get("tp1"), signal.get("tp2"), signal.get("tp3"),
             signal.get("atr"),
             json.dumps(signal.get("reasons") or []),
             signal.get("regime"), source, now_ms, expires_at, "new"))

        self._conn.commit()
        return cur.lastrowid

    def mark_consumed(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE signals SET status = 'consumed', consumed_at = ? "
            "WHERE id = ?", (int(time.time() * 1000), row_id))
        self._conn.commit()

    def expire_old(self) -> int:
        """Mark past-expiry 'new' rows as expired. Returns count."""
        cur = self._conn.execute(
            "UPDATE signals SET status = 'expired' "
            "WHERE status = 'new' AND expires_at <= ?",
            (int(time.time() * 1000),))
        self._conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------- reads
    def get_fresh_signals(self, limit: int = 20) -> list[dict]:
        """Fresh signals, best quality first."""
        now_ms = int(time.time() * 1000)
        cur = self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM signals "
            "WHERE status = 'new' AND expires_at > ? "
            "ORDER BY quality DESC, COALESCE(confidence, 0) DESC, COALESCE(strength, 0) DESC, id ASC LIMIT ?",
            (now_ms, limit))
        return [self._row_to_dict(r) for r in cur.fetchall()]

    def stats(self) -> dict:
        now_ms = int(time.time() * 1000)
        out = {}
        for st in ("new", "consumed", "expired"):
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status = ?", (st,))
            out[st] = cur.fetchone()[0]
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM signals "
            "WHERE status = 'new' AND expires_at > ?", (now_ms,))
        out["fresh"] = cur.fetchone()[0]
        return out

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        d = {c: r[c] for c in _COLUMNS}
        try:
            d["reasons"] = json.loads(d.get("reasons") or "[]")
        except (TypeError, ValueError):
            d["reasons"] = []
        return d

    def to_signal(self, row: dict) -> dict:
        """Rebuild an engine-style signal dict from a DB row."""
        return {
            "symbol": row["symbol"],
            "direction": row["direction"],
            "quality": row["quality"],
            "confidence": row.get("confidence"),
            "strength": row.get("strength"),
            "entry": row["entry"],
            "entry_low": row["entry_low"],
            "entry_high": row["entry_high"],
            "stop_loss": row["stop_loss"],
            "tp1": row["tp1"], "tp2": row["tp2"], "tp3": row["tp3"],
            "atr": row.get("atr"),
            "reasons": row["reasons"],
            "regime": row["regime"],
        }

