# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Persistent executor state (state.json) with atomic writes.

Schema:
{
  "signal_ids": { "<signal_id>": {"ts": ms, "symbol": str, "note": str} },
  "positions":  { "<pos_id>": {
      "symbol": str, "signal_id": str, "direction": "LONG",
      "qty": float, "entry_price": float, "stop_loss": float,
      "tp": {"tp1": {"price": float, "qty": float, "order_id": int|None,
                     "filled": bool}, ...},
      "sl_order_id": int|None, "breakeven_moved": bool,
      "opened_at": ms, "unmanaged": bool, "entry_order_id": int|None } },
  "daily":  {"date": "YYYY-MM-DD", "start_balance": float, "realized_pnl": float},
  "kill_switch": {"date": "YYYY-MM-DD", "active": bool, "reason": str}
}

restart recovery: reconcile() compares state positions against the exchange's
actual open positions on startup.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

import config

log = logging.getLogger("executor.state")


def _default_state() -> dict:
    return {
        "signal_ids": {},
        "positions": {},
        "daily": {"date": "", "start_balance": 0.0, "realized_pnl": 0.0},
        "kill_switch": {"date": "", "active": False, "reason": ""},
        "last_cycle_ts": 0,
    }


class StateStore:
    def __init__(self, path: str = config.EXEC_STATE_FILE):
        self.path = path
        self._data = _default_state()
        self.load()

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            base = _default_state()
            base.update(loaded or {})
            self._data = base
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._data = _default_state()

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)  # atomic on POSIX
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    # ------------------------------------------------------------- signal ids
    def seen_signal(self, signal_id: str) -> bool:
        return signal_id in self._data["signal_ids"]

    def mark_signal(self, signal_id: str, symbol: str, note: str = "") -> None:
        self._data["signal_ids"][signal_id] = {
            "ts": int(time.time() * 1000), "symbol": symbol, "note": note,
        }
        self.prune_signal_ids()

    def prune_signal_ids(self) -> None:
        cutoff = int(time.time() * 1000) - int(
            config.EXEC_SIGNAL_ID_TTL_HOURS * 3600 * 1000)
        ids = self._data["signal_ids"]
        for sid in [s for s, v in ids.items() if v.get("ts", 0) < cutoff]:
            del ids[sid]

    # --------------------------------------------------------------- reconcile
    def reconcile(self, exchange_positions: list[dict]) -> dict:
        """Match state positions against real exchange positions at startup.

        Returns {"adopted": [...], "closed": [...]}:
          adopted - on the exchange but unknown to state: marked unmanaged
                    (no SL/TP plan known; NEVER auto-trade them, flag loudly).
          closed  - in state but gone from the exchange AND not already
                    marked closed: newly discovered closes only, so the
                    caller can attribute each exactly once.
        """
        adopted, closed = [], []
        by_symbol = {p["symbol"]: p for p in exchange_positions}
        for pos_id, pos in list(self._data["positions"].items()):
            sym = pos.get("symbol")
            # Only report newly-closed positions: without the guard every
            # restart would re-list (and re-attribute) already-closed ones.
            if sym not in by_symbol and not pos.get("closed"):
                pos["closed"] = True
                pos["closed_reason"] = "gone from exchange at startup"
                closed.append(pos_id)
                log.warning("position %s (%s) gone from exchange at startup",
                            pos_id, sym)
        state_symbols = {p.get("symbol") for p in
                         self._data["positions"].values()
                         if not p.get("closed")}
        for sym, xp in by_symbol.items():
            if sym not in state_symbols:
                pos_id = f"adopted_{sym}_{int(time.time() * 1000)}"
                self._data["positions"][pos_id] = {
                    "symbol": sym, "signal_id": "", "direction": xp["side"],
                    "qty": xp["qty"], "entry_price": xp["entry_price"],
                    "stop_loss": 0.0,
                    "tp": {}, "sl_order_id": None, "breakeven_moved": False,
                    "opened_at": int(time.time() * 1000),
                    "unmanaged": True,
                }
                adopted.append(pos_id)
                log.warning("ADOPTED unmanaged exchange position %s (%s %s @ %s) "
                            "- manual review required, no auto SL/TP will be placed",
                            pos_id, sym, xp["side"], xp["entry_price"])
        self.save()
        return {"adopted": adopted, "closed": closed}
