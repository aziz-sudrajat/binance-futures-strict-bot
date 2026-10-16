"""Signal lifecycle accounting (lifecycle.jsonl).

Every signal the executor evaluates gets an append-only event trail:
  generated -> approved | rejected(reason)
  approved  -> entry_triggered -> position_opened
            -> tp1_hit | tp2_hit | tp3_hit | sl_hit | expired | cancelled
A position fully closed is a terminal event; partial TPs are recorded per
the documented TP_SPLIT allocation (never multi-counted as multiple wins).

report.py consumes this file for the daily report.
"""

from __future__ import annotations

import json
import time

import config

# Canonical states (ROADMAP ITEM 7, light fold-in).
GENERATED = "generated"
APPROVED = "approved"
REJECTED = "rejected"
ENTRY_TRIGGERED = "entry_triggered"
POSITION_OPENED = "position_opened"
TP1_HIT = "tp1_hit"
TP2_HIT = "tp2_hit"
TP3_HIT = "tp3_hit"
SL_HIT = "sl_hit"
MANUAL_CLOSE = "manual_close"
EXPIRED = "expired"
CANCELLED = "cancelled"
PROFIT_TAKE = "profit_take"   # auto TP: unrealized hit +PROFIT_TAKE_PCT% of margin


class LifecycleLogger:
    def __init__(self, path: str = config.EXEC_LIFECYCLE_FILE):
        self.path = path

    def log(self, signal_id: str, symbol: str, state: str, **fields) -> None:
        event = {
            "ts": int(time.time() * 1000),
            "signal_id": signal_id,
            "symbol": symbol,
            "state": state,
        }
        event.update(fields)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def read_all(self) -> list[dict]:
        events = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        return events

    def has_terminal_event(self, signal_id: str) -> bool:
        """True when a terminal outcome event was already logged for this
        signal. Dedupe guard for startup-reconcile retries: a crash between
        lifecycle.log and state.save must not attribute the same close
        twice (the PnL was already recorded with the first log).
        """
        if not signal_id:
            return False
        for e in self.read_all():
            if e.get("signal_id") == signal_id and e.get("terminal"):
                return True
        return False

    def last_event_ts(self) -> int | None:
        """Newest event timestamp (ms), or None when the log is empty/missing.

        Reads only the tail of the file so workers can compare against a
        watermark without parsing the whole log.
        """
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # Walk back over at most the last 4KB to find the last line.
                step = min(size, 4096)
                f.seek(size - step)
                tail = f.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                ts = json.loads(line).get("ts")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(ts, (int, float)):
                return int(ts)
        return None
