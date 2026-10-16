"""Unit tests for the Telegram/Discord notifier (item 3.2). No network."""
import notify
from notify import (
    discord_configured,
    format_trade_event,
    notify_all,
    send_discord,
    send_telegram,
    telegram_configured,
)


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


def _patch(monkeypatch, post_fn, env):
    monkeypatch.setattr(notify.requests, "post", post_fn)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "DISCORD_WEBHOOK_URL"):
        if k not in env:
            monkeypatch.delenv(k, raising=False)


def test_telegram_disabled_without_env(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: _Resp(), {})
    assert not telegram_configured()
    assert send_telegram("x") is False


def test_telegram_success(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _Resp(200, {"ok": True})

    _patch(monkeypatch, fake_post,
           {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    assert telegram_configured()
    assert send_telegram("hello") is True
    url, payload = calls[0]
    # Telegram Bot API puts the token in the URL path (API design);
    # the message body must carry only chat_id + text.
    assert url == "https://api.telegram.org/bottok/sendMessage"
    assert payload == {"chat_id": "123", "text": "hello"}


def test_telegram_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")

    _patch(monkeypatch, boom,
           {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    assert send_telegram("hello") is False


def test_telegram_api_error(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: _Resp(200, {"ok": False}),
           {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"})
    assert send_telegram("hello") is False


def test_discord_success(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _Resp(204)

    _patch(monkeypatch, fake_post,
           {"DISCORD_WEBHOOK_URL": "https://discord/webhook"})
    assert discord_configured()
    assert send_discord("hi") is True
    assert calls[0][0] == "https://discord/webhook"
    assert calls[0][1]["content"] == "hi"


def test_discord_disabled_without_env(monkeypatch):
    _patch(monkeypatch, lambda *a, **k: _Resp(), {})
    assert not discord_configured()
    assert send_discord("x") is False


def test_discord_never_raises(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("down")

    _patch(monkeypatch, boom,
           {"DISCORD_WEBHOOK_URL": "https://discord/webhook"})
    assert send_discord("hi") is False


def test_notify_all_fires_both(monkeypatch):
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append(url)
        return _Resp(200, {"ok": True})

    _patch(monkeypatch, fake_post,
           {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c",
            "DISCORD_WEBHOOK_URL": "https://discord/w"})
    res = notify_all("ping")
    assert res == {"telegram": True, "discord": True}
    assert len(seen) == 2


def test_format_trade_event():
    e = {"state": "CLOSED_TP", "symbol": "BTCUSDT", "reason": "tp1",
         "realized": 0.42, "terminal": True}
    s = format_trade_event(e)
    assert "BTCUSDT" in s and "0.42" in s and "tp1" in s
    assert format_trade_event({"state": "X"}) == "[X] ?"
