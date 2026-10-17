# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Graceful shutdown management for trading loops and critical sections.

Ensures that SIGINT / SIGTERM signals are intercepted cleanly, allowing
in-flight orders and state saves to complete before process exit.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
from typing import Generator

log = logging.getLogger("shutdown")

_shutdown_event = threading.Event()
_installed = False
_lock = threading.Lock()
_in_critical_section = False


def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name
    log.warning("Received signal %s — initiating graceful shutdown...", sig_name)
    _shutdown_event.set()


def register_shutdown() -> None:
    """Install signal handlers for SIGINT and SIGTERM."""
    global _installed
    with _lock:
        if _installed:
            return
        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
            _installed = True
            log.debug("Graceful shutdown handlers installed.")
        except (ValueError, AttributeError) as e:
            # Can fail if not in main thread (e.g. during certain test runners)
            log.debug("Could not register signal handlers (not main thread?): %s", e)


def shutdown_requested() -> bool:
    """True if a termination signal has been received."""
    return _shutdown_event.is_set()


def trigger_shutdown() -> None:
    """Programmatically trigger shutdown."""
    _shutdown_event.set()


def reset_shutdown() -> None:
    """Reset shutdown state (used by unit tests)."""
    _shutdown_event.clear()


@contextlib.contextmanager
def graceful_section(name: str = "critical_section") -> Generator[None, None, None]:
    """Context manager protecting a critical section (e.g. order placement).

    Logs warning if shutdown was requested while the critical section was executing.
    """
    global _in_critical_section
    with _lock:
        _in_critical_section = True
    try:
        yield
    finally:
        with _lock:
            _in_critical_section = False
        if shutdown_requested():
            log.warning("Critical section '%s' finished while shutdown was requested.", name)
