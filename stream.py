# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""User data stream manager for Binance Futures (ListenKey & WebSockets).

Maintains listenKey lifecycle via periodic keep-alive pings (every 30m)
to prevent stream expiry. Dispatches order execution and account events
asynchronously without blocking the main trading loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger("stream")


def parse_user_stream_event(raw_data: str | dict) -> dict | None:
    """Parse and normalize Binance Futures User Data Stream event payloads.

    Supports ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, and listenKeyExpired events.
    Returns normalized dictionary with human-readable keys, or None on invalid input.
    """
    if isinstance(raw_data, str):
        try:
            msg = json.loads(raw_data)
        except Exception:  # noqa: BLE001 - malformed JSON -> ignore
            return None
    elif isinstance(raw_data, dict):
        msg = raw_data
    else:
        return None

    if not isinstance(msg, dict):
        return None

    evt_type = msg.get("e")
    if evt_type == "ORDER_TRADE_UPDATE":
        o = msg.get("o") or {}
        try:
            return {
                "event_type": "ORDER_TRADE_UPDATE",
                "event_time": msg.get("E"),
                "transaction_time": msg.get("T"),
                "symbol": o.get("s"),
                "client_order_id": o.get("c"),
                "side": o.get("S"),
                "order_type": o.get("o"),
                "time_in_force": o.get("f"),
                "orig_qty": float(o["q"]) if o.get("q") is not None else 0.0,
                "orig_price": float(o["p"]) if o.get("p") is not None else 0.0,
                "avg_price": float(o["ap"]) if o.get("ap") is not None else 0.0,
                "stop_price": float(o["sp"]) if o.get("sp") is not None else 0.0,
                "execution_type": o.get("x"),
                "order_status": o.get("X"),
                "order_id": o.get("i"),
                "last_filled_qty": float(o["l"]) if o.get("l") is not None else 0.0,
                "cum_filled_qty": float(o["z"]) if o.get("z") is not None else 0.0,
                "last_filled_price": float(o["L"]) if o.get("L") is not None else 0.0,
                "commission": float(o["n"]) if o.get("n") is not None else 0.0,
                "commission_asset": o.get("N"),
                "trade_time": o.get("T"),
                "trade_id": o.get("t"),
                "is_maker": bool(o.get("m")),
                "is_reduce_only": bool(o.get("R")),
                "working_type": o.get("wt"),
                "realized_profit": float(o["rp"]) if o.get("rp") is not None else 0.0,
            }
        except Exception as err:
            log.debug("error parsing ORDER_TRADE_UPDATE: %s", err)
            return None

    if evt_type == "ACCOUNT_UPDATE":
        a = msg.get("a") or {}
        try:
            balances = []
            for b in a.get("B", []):
                balances.append({
                    "asset": b.get("a"),
                    "wallet_balance": float(b["wb"]) if b.get("wb") is not None else 0.0,
                    "cross_wallet_balance": float(b["cw"]) if b.get("cw") is not None else 0.0,
                    "balance_change": float(b["bc"]) if b.get("bc") is not None else 0.0,
                })
            positions = []
            for p in a.get("P", []):
                positions.append({
                    "symbol": p.get("s"),
                    "position_amount": float(p["pa"]) if p.get("pa") is not None else 0.0,
                    "entry_price": float(p["ep"]) if p.get("ep") is not None else 0.0,
                    "unrealized_pnl": float(p["up"]) if p.get("up") is not None else 0.0,
                    "margin_type": p.get("mt"),
                    "isolated_wallet": float(p["iw"]) if p.get("iw") is not None else 0.0,
                    "position_side": p.get("ps"),
                })
            return {
                "event_type": "ACCOUNT_UPDATE",
                "event_time": msg.get("E"),
                "transaction_time": msg.get("T"),
                "reason": a.get("m"),
                "balances": balances,
                "positions": positions,
            }
        except Exception as err:
            log.debug("error parsing ACCOUNT_UPDATE: %s", err)
            return None

    if evt_type == "listenKeyExpired":
        return {
            "event_type": "listenKeyExpired",
            "event_time": msg.get("E"),
        }

    return None


class BinanceUserStream:
    """Manages the lifecycle of a Binance Futures user data stream."""

    def __init__(self, exchange,
                 on_order_update: Callable[[dict], None] | None = None,
                 on_account_update: Callable[[dict], None] | None = None,
                 keepalive_interval_sec: float = 1800.0):
        self.exchange = exchange
        self.on_order_update = on_order_update
        self.on_account_update = on_account_update
        self.keepalive_interval_sec = keepalive_interval_sec

        self.listen_key: str | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> str | None:
        """Acquire a listenKey and launch the keep-alive background daemon."""
        with self._lock:
            if self._running:
                return self.listen_key

            create_fn = getattr(self.exchange, "create_listen_key", None)
            if not callable(create_fn):
                log.debug("exchange client does not support create_listen_key")
                return None

            self.listen_key = create_fn()
            if not self.listen_key:
                log.warning("failed to acquire listenKey from exchange")
                return None

            self._running = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._keepalive_worker,
                                            name="BinanceStreamKeepalive",
                                            daemon=True)
            self._thread.start()
            log.info("user data stream active (listenKey=%s...)", self.listen_key[:8])
            return self.listen_key

    def stop(self) -> None:
        """Stop keep-alive worker and release listenKey."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_event.set()

        close_fn = getattr(self.exchange, "close_listen_key", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception as e:  # noqa: BLE001
                log.debug("error closing listenKey: %s", e)
        self.listen_key = None
        log.info("user data stream stopped")

    def is_running(self) -> bool:
        """True when keep-alive loop is active."""
        return self._running and self.listen_key is not None

    def handle_message(self, raw_data: str | dict) -> dict | None:
        """Parse incoming WebSocket message and dispatch to registered callbacks."""
        evt = parse_user_stream_event(raw_data)
        if not evt:
            return None

        etype = evt.get("event_type")
        if etype == "ORDER_TRADE_UPDATE":
            if callable(self.on_order_update):
                try:
                    self.on_order_update(evt)
                except Exception as err:  # noqa: BLE001
                    log.error("error in on_order_update callback: %s", err)
        elif etype == "ACCOUNT_UPDATE":
            if callable(self.on_account_update):
                try:
                    self.on_account_update(evt)
                except Exception as err:  # noqa: BLE001
                    log.error("error in on_account_update callback: %s", err)
        elif etype == "listenKeyExpired":
            log.warning("listenKeyExpired event received from stream; refreshing")
            create_fn = getattr(self.exchange, "create_listen_key", None)
            if callable(create_fn):
                new_key = create_fn()
                if new_key:
                    with self._lock:
                        self.listen_key = new_key
                    log.info("listenKey refreshed on expiry event: %s...", new_key[:8])

        return evt

    def _keepalive_worker(self) -> None:
        """Periodically ping the exchange to maintain listenKey validity."""
        last_ping = time.time()
        while self._running:
            step = max(0.01, min(1.0, self.keepalive_interval_sec / 2))
            if self._stop_event.wait(timeout=step):
                break
            now = time.time()
            if now - last_ping >= self.keepalive_interval_sec:
                ping_fn = getattr(self.exchange, "keepalive_listen_key", None)
                if callable(ping_fn):
                    ok = ping_fn()
                    if ok:
                        log.debug("listenKey keepalive successful")
                        last_ping = now
                    else:
                        log.warning("listenKey keepalive ping failed; attempting refresh")
                        create_fn = getattr(self.exchange, "create_listen_key", None)
                        if callable(create_fn):
                            new_key = create_fn()
                            if new_key:
                                with self._lock:
                                    self.listen_key = new_key
                                last_ping = now
                                log.info("listenKey refreshed: %s...", new_key[:8])

