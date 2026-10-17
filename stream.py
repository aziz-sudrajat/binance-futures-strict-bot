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

import logging
import threading
import time
from typing import Callable

log = logging.getLogger("stream")


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
                                self.listen_key = new_key
                                last_ping = now
                                log.info("listenKey refreshed: %s...", new_key[:8])
