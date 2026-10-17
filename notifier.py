# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Lightweight, non-blocking Telegram notifier for live trade lifecycle events.

Reads credentials from environment variables:
  TELEGRAM_BOT_TOKEN: Telegram bot token from @BotFather
  TELEGRAM_CHAT_ID: Target chat or channel ID

If either variable is missing, notifications are quietly skipped (no-op).
All network calls run in background daemon threads so message delivery
never delays critical trading order paths or exchange polling.
"""

from __future__ import annotations

import logging
import os
import threading
import requests

log = logging.getLogger("notifier")

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def is_configured() -> bool:
    """True when both token and chat_id are present in the environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return bool(token and chat_id)


def _get_allowed_user_ids() -> set[str]:
    """User ids permitted to send control commands.

    Empty set == 'private-chat mode': only the chat id configured in
    TELEGRAM_CHAT_ID may send commands, and only from a private chat.
    Fill this to allow specific users from group/channel chats.
    """
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def _sender_is_allowed(msg: dict, target_chat: str) -> tuple[bool, str]:
    """Validate the Telegram sender. Returns (allowed, reason).

    Private chat with the configured chat id is always allowed. Group and
    channel chats require an explicit per-user allowlist, because a group
    message reaches the bot with the *group* id, not the sender's user id.
    """
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    chat_type = str(chat.get("type", ""))
    if chat_type == "private" and chat_id == target_chat:
        return True, "private-chat"
    from_user = msg.get("from") or {}
    user_id = str(from_user.get("id", ""))
    user_name = (
        f'{from_user.get("first_name") or ""}'
        f'{(from_user.get("username") or "").lstrip("@")}'
    ).strip()
    allowed_ids = _get_allowed_user_ids()
    if user_id in allowed_ids:
        return True, f"allowlisted user {user_id} ({user_name}) in {chat_type}"
    return False, f"{chat_type} chat {chat_id} from unlisted user {user_id} ({user_name})"


def send_message(text: str, parse_mode: str = "HTML") -> None:
    """Send a Telegram notification in a background thread (non-blocking)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    def _worker():
        try:
            url = _API_URL.format(token=token)
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            resp = requests.post(url, json=payload, timeout=5.0)
            if resp.status_code >= 400:
                log.warning("Telegram send failed (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as e:  # noqa: BLE001 - never break the trading loop
            log.debug("Telegram notification error: %s", e)

    t = threading.Thread(target=_worker, daemon=True, name="TelegramNotifier")
    t.start()


def notify_position_opened(symbol: str, direction: str, entry_px: float,
                           qty: float, margin_usdt: float, leverage: int,
                           sl: float, tp1: float, tp2: float, tp3: float) -> None:
    """Format and send position opened notification."""
    side_emoji = "🟢 LONG" if direction.upper() == "LONG" else "🔴 SHORT"
    msg = (
        f"<b>🚀 POSISI DIBUKA — {symbol}</b>\n\n"
        f"<b>Arah:</b> {side_emoji}\n"
        f"<b>Entry:</b> <code>{entry_px:,.6g}</code>\n"
        f"<b>Qty:</b> <code>{qty}</code> ({margin_usdt:.2f} USDT @ {leverage}x)\n\n"
        f"<b>🛡️ Stop Loss:</b> <code>{sl:,.6g}</code>\n"
        f"<b>🎯 TP 1:</b> <code>{tp1:,.6g}</code>\n"
        f"<b>🎯 TP 2:</b> <code>{tp2:,.6g}</code>\n"
        f"<b>🎯 TP 3:</b> <code>{tp3:,.6g}</code>"
    )
    send_message(msg)


def notify_tp_hit(symbol: str, leg: str, price: float, net_pnl: float) -> None:
    """Format and send take-profit hit notification."""
    pnl_sign = "+" if net_pnl >= 0 else ""
    msg = (
        f"<b>🎯 TAKE PROFIT {leg.upper()} HIT — {symbol}</b>\n\n"
        f"<b>Exit Price:</b> <code>{price:,.6g}</code>\n"
        f"<b>Realized Net PnL:</b> <b>{pnl_sign}{net_pnl:.4f} USDT</b> 💰"
    )
    send_message(msg)


def notify_sl_hit(symbol: str, price: float, net_pnl: float) -> None:
    """Format and send stop-loss hit notification."""
    pnl_sign = "+" if net_pnl >= 0 else ""
    msg = (
        f"<b>🛑 STOP LOSS HIT — {symbol}</b>\n\n"
        f"<b>Exit Price:</b> <code>{price:,.6g}</code>\n"
        f"<b>Realized Net PnL:</b> <b>{pnl_sign}{net_pnl:.4f} USDT</b>"
    )
    send_message(msg)


def notify_profit_take(symbol: str, price: float, net_pnl: float) -> None:
    """Format and send auto profit-take (+20%) notification."""
    pnl_sign = "+" if net_pnl >= 0 else ""
    msg = (
        f"<b>💎 AUTO PROFIT-TAKE HIT — {symbol}</b>\n\n"
        f"<b>Market Exit Price:</b> <code>{price:,.6g}</code>\n"
        f"<b>Realized Net PnL:</b> <b>{pnl_sign}{net_pnl:.4f} USDT</b> 🚀"
    )
    send_message(msg)


def notify_trail_stop(symbol: str, price: float, net_pnl: float) -> None:
    """Format and send trailing stop exit notification."""
    pnl_sign = "+" if net_pnl >= 0 else ""
    msg = (
        f"<b>🎯 TRAILING STOP EXIT — {symbol}</b>\n\n"
        f"<b>Trailing Exit Price:</b> <code>{price:,.6g}</code>\n"
        f"<b>Realized Net PnL:</b> <b>{pnl_sign}{net_pnl:.4f} USDT</b> 📈"
    )
    send_message(msg)


def notify_time_stop(symbol: str, price: float, net_pnl: float) -> None:
    """Format and send time stop exit notification."""
    pnl_sign = "+" if net_pnl >= 0 else ""
    msg = (
        f"<b>⏰ TIME STOP EXIT — {symbol}</b>\n\n"
        f"<b>Market Exit Price:</b> <code>{price:,.6g}</code>\n"
        f"<b>Realized Net PnL:</b> <b>{pnl_sign}{net_pnl:.4f} USDT</b>"
    )
    send_message(msg)


def notify_profit_locked(symbol: str, price: float) -> None:
    """Format and send profit locked notification (SL moved to TP1)."""
    msg = (
        f"<b>🔒 PROFIT LOCKED — {symbol}</b>\n\n"
        f"<b>Info:</b> TP2 terpenuhi. Stop Loss berhasil dinaikkan ke level TP1.\n"
        f"<b>New Guaranteed SL Price:</b> <code>{price:,.6g}</code>\n\n"
        f"✅ <i>Sisa posisi dijamin keluar dalam kondisi profit terlindungi.</i>"
    )
    send_message(msg)


def notify_kill_switch(reason: str, balance: float | None = None) -> None:
    """Format and send emergency kill switch notification."""
    bal_str = f" (${balance:,.2f} USDT)" if balance is not None else ""
    msg = (
        f"<b>🚨 KILL SWITCH AKTIF!</b>\n\n"
        f"<b>Penyebab:</b> <i>{reason}</i>\n"
        f"<b>Saldo Sekarang:</b>{bal_str}\n\n"
        f"⚠️ <i>Semua entri baru otomatis dihentikan demi pengamanan modal.</i>"
    )
    send_message(msg)


# ------------------------------------------------ 2-Way Interactive Bot
def handle_command(cmd: str, state=None, risk=None, exchange=None) -> str:
    """Process incoming slash commands and return formatted HTML reply."""
    c = cmd.strip().split()[0].lower() if cmd.strip() else ""
    if c in ("/start", "/help"):
        return (
            "<b>🤖 Binance Futures Strict Bot Controller</b>\n\n"
            "• <code>/status</code> — Status bot, saldo, drawdown & kill switch\n"
            "• <code>/positions</code> — Daftar posisi aktif & bracket SL/TP\n"
            "• <code>/kill</code> — Aktifkan kill switch darurat (stop entri)\n"
            "• <code>/resume</code> — Reset kill switch hari ini (lanjut trading)\n"
            "• <code>/help</code> — Tampilkan panduan ini"
        )
    if c == "/status":
        bal_str = "N/A"
        if exchange and hasattr(exchange, "get_balance"):
            try:
                b = exchange.get_balance()
                if b is not None:
                    bal_str = f"${b:,.2f} USDT"
            except Exception:  # noqa: BLE001
                pass
        ks_active = False
        ks_reason = "-"
        if risk and hasattr(risk, "kill_switch_active"):
            ks_active = risk.kill_switch_active()
            ks = risk.state.get("kill_switch", {}) if hasattr(risk, "state") else {}
            ks_reason = ks.get("reason", "-")
        pos_count = 0
        if state and hasattr(state, "get"):
            pos_count = len([p for p in state.get("positions", {}).values() if not p.get("closed")])
        ks_icon = "🛑 AKTIF" if ks_active else "🟢 NORMAL"
        return (
            f"<b>📊 STATUS SISTEM</b>\n\n"
            f"<b>Saldo:</b> <code>{bal_str}</code>\n"
            f"<b>Posisi Aktif:</b> <code>{pos_count}</code>\n"
            f"<b>Kill Switch:</b> {ks_icon}\n"
            f"<b>Alasan KS:</b> <i>{ks_reason}</i>"
        )
    if c == "/positions":
        if not state or not hasattr(state, "get"):
            return "<i>State tidak tersedia.</i>"
        positions = [p for p in state.get("positions", {}).values() if not p.get("closed")]
        if not positions:
            return "<i>Tidak ada posisi trading yang sedang aktif.</i>"
        lines = ["<b>📑 POSISI AKTIF:</b>\n"]
        for p in positions:
            sym = p.get("symbol", "?")
            side = p.get("direction", "LONG")
            entry = float(p.get("entry_price") or 0.0)
            sl = float(p.get("stop_loss") or 0.0)
            lines.append(f"• <b>{sym}</b> ({side}) @ <code>{entry:,.2f}</code> | SL: <code>{sl:,.2f}</code>")
        return "\n".join(lines)
    if c == "/kill":
        if risk and hasattr(risk, "engage_kill_switch"):
            risk.engage_kill_switch("Manually triggered via Telegram command")
            return "<b>🚨 KILL SWITCH DIAKTIFKAN MANUAL!</b>\nSemua entri baru telah dihentikan."
        return "<i>Risk manager tidak terhubung.</i>"
    if c == "/resume":
        if state and hasattr(state, "set"):
            from risk import utc_today
            state.set("kill_switch", {"date": utc_today(), "active": False, "reason": ""})
            if hasattr(state, "save"):
                state.save()
            return "<b>✅ KILL SWITCH DI-RESET!</b>\nBot siap menerima sinyal baru kembali."
        return "<i>State manager tidak terhubung.</i>"
    return "<i>Perintah tidak dikenali. Ketik /help untuk daftar perintah.</i>"


_listener_running = False
_listener_stop = threading.Event()


def start_command_listener(state=None, risk=None, exchange=None) -> threading.Thread | None:
    """Launch background polling loop for interactive user commands.

    Command sender is validated by ``_sender_is_allowed`` before any
    handler runs (private chat with the configured chat id, or an
    allowlisted user id). Unlisted group/channel participants are
    refused. See ``_sender_is_allowed`` for the security model.
    """
    global _listener_running
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not target_chat:
        log.debug("Telegram credentials absent; command listener not started")
        return None

    if _listener_running:
        return None

    _listener_running = True
    _listener_stop.clear()

    def _loop():
        offset = 0
        get_url = f"https://api.telegram.org/bot{token}/getUpdates"
        while _listener_running and not _listener_stop.is_set():
            try:
                params = {"offset": offset, "timeout": 2}
                r = requests.get(get_url, params=params, timeout=5.0)
                if r.status_code == 200:
                    data = r.json()
                    for item in data.get("result", []):
                        offset = item["update_id"] + 1
                        msg = item.get("message", {})
                        text = (msg.get("text") or "").strip()
                        if not text.startswith("/"):
                            continue
                        allowed, reason = _sender_is_allowed(msg, target_chat)
                        if not allowed:
                            log.warning("BLOCKED Telegram command: %s", reason)
                            continue
                        reply = handle_command(text, state=state, risk=risk, exchange=exchange)
                        send_message(reply)
            except Exception as e:  # noqa: BLE001
                log.debug("Telegram listener polling error: %s", e)
            _listener_stop.wait(1.5)

    t = threading.Thread(target=_loop, name="TelegramCommandListener", daemon=True)
    t.start()
    log.info("Telegram interactive 2-way command listener active")
    return t


def stop_command_listener():
    """Halt background command polling."""
    global _listener_running
    _listener_running = False
    _listener_stop.set()

