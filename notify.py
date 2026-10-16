# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Telegram + Discord notifiers (item 3.2).

The WhatsApp notifier already exists as an external cron — this module
adds the two channels that were missing, with the SAME external pattern:
a poller script (tools/notify_lifecycle.py) tails the lifecycle log and
calls notify_all(). The trading executor is NOT touched.

Configuration is env-only (never hardcode tokens):
    TELEGRAM_BOT_TOKEN   + TELEGRAM_CHAT_ID   -> Telegram Bot API
    DISCORD_WEBHOOK_URL                       -> Discord webhook

A channel with missing env is silently disabled. Sends never raise —
failures are logged and the other channel still fires. Tokens are never
written to logs.
"""
from __future__ import annotations

import html
import logging
import os

import requests

log = logging.getLogger("notify")

_TIMEOUT_SEC = 10


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def telegram_configured() -> bool:
    return bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))


def discord_configured() -> bool:
    return bool(_env("DISCORD_WEBHOOK_URL"))


def send_telegram(text: str) -> bool:
    """Send via Telegram Bot API. Returns True on success. Never raises."""
    token, chat_id = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=_TIMEOUT_SEC,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        if not ok:
            log.warning("telegram send failed: HTTP %s", r.status_code)
        return bool(ok)
    except Exception as exc:  # noqa: BLE001 - notifier must never crash
        log.warning("telegram send error: %r", exc)
        return False


def send_discord(text: str) -> bool:
    """Send via Discord webhook. Returns True on success. Never raises."""
    url = _env("DISCORD_WEBHOOK_URL")
    if not url:
        return False
    try:
        r = requests.post(
            url,
            json={"content": text[:2000]},
            timeout=_TIMEOUT_SEC,
        )
        ok = r.status_code in (200, 204)
        if not ok:
            log.warning("discord send failed: HTTP %s", r.status_code)
        return ok
    except Exception as exc:  # noqa: BLE001 - notifier must never crash
        log.warning("discord send error: %r", exc)
        return False


def format_trade_event(event: dict) -> str:
    """One-line human summary of a lifecycle event (no secrets)."""
    state = event.get("state", "?")
    sym = event.get("symbol", "?")
    reason = event.get("reason", "")
    line = f"[{state}] {sym}"
    if reason:
        line += f" — {reason}"
    if event.get("terminal") and event.get("realized") is not None:
        line += f" (PnL {event['realized']})"
    return line


def notify_all(text: str) -> dict:
    """Fire every configured channel. Returns {channel: ok}."""
    return {"telegram": send_telegram(text), "discord": send_discord(text)}


def escape_html(s: str) -> str:
    return html.escape(s or "")
