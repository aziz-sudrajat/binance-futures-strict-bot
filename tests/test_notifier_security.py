# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# Telegram notifier sender validation + run_executor tail optimization tests.
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifier  # noqa: E402
from run_executor import _recent_notable_events  # noqa: E402


# ------------------------------------------------------------------ notifier
def make_msg(chat_id, chat_type="private", user_id=123, username="alice",
             first_name="Alice"):
    return {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "username": username, "first_name": first_name},
        "text": "/help",
    }


def test_get_allowed_user_ids_empty():
    os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
    assert notifier._get_allowed_user_ids() == set()


def test_get_allowed_user_ids_parsed():
    os.environ["TELEGRAM_ALLOWED_USER_IDS"] = " 1, 2 ,3 "
    assert notifier._get_allowed_user_ids() == {"1", "2", "3"}


@pytest.mark.parametrize(
    "chat_type, user_id, allowed, expect_allowed",
    [
        ("private", 123, None, True),          # private + matching chat id
        ("private", 999, None, True),          # private chat: anyone there
                                                # is the owner (no user gate)
        ("group", 123, None, False),           # group without allowlist
        ("group", 123, [123], True),           # group + allowlisted user
        ("channel", 123, [999], False),        # channel + unlisted user
    ],
)
def test_sender_is_allowed(chat_type, user_id, allowed, expect_allowed):
    os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
    msg = make_msg(chat_id="12345", chat_type=chat_type, user_id=user_id)
    if allowed is not None:
        os.environ["TELEGRAM_ALLOWED_USER_IDS"] = ",".join(
            str(u) for u in allowed)
    allowed_flag, reason = notifier._sender_is_allowed(msg, target_chat="12345")
    assert allowed_flag is expect_allowed


def test_sender_is_allowed_private_is_owner_only():
    # In private-chat mode, the chat id IS the owner's id; a different user
    # cannot message the bot's private chat, so user_id is not the gate here.
    os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
    msg = make_msg(chat_id="99999", chat_type="private", user_id=123)
    ok, reason = notifier._sender_is_allowed(msg, target_chat="99999")
    assert ok is True
    assert "private" in reason


def test_sender_group_without_allowlist_is_rejected():
    os.environ.pop("TELEGRAM_ALLOWED_USER_IDS", None)
    msg = make_msg(chat_id="222", chat_type="group", user_id=777)
    ok, _ = notifier._sender_is_allowed(msg, target_chat="222")
    assert ok is False


def test_get_allowed_user_ids_parsed_from_comma_separated():
    os.environ["TELEGRAM_ALLOWED_USER_IDS"] = " 1, 2 ,3 , 4 "
    assert notifier._get_allowed_user_ids() == {"1", "2", "3", "4"}


def test_sender_is_allowed_returns_reason():
    os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "123"
    msg = make_msg(chat_id="123", chat_type="private", user_id=123,
                   first_name="Alice")
    ok, reason = notifier._sender_is_allowed(msg, target_chat="123")
    assert ok is True
    assert "private" in reason


# ---------------------------------------------------------------- run_executor
def _write_lifecycle(path, events):
    """events: list of dicts with 'ts', 'symbol', 'state'."""
    import json
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_recent_notable_events_tail_read():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        # 10000 old events, then 3 notable new ones
        old = [{"ts": i, "symbol": "BTCUSDT", "state": "position_opened"}
               for i in range(0, 10000)]
        new = [
            {"ts": 20000, "symbol": "ETHUSDT", "state": "tp1_hit",
             "qty": 1.0, "entry": 100.0, "price": 110.0, "pnl": 5.0},
            {"ts": 20001, "symbol": "SOLUSDT", "state": "sl_hit",
             "qty": 1.0, "entry": 50.0, "price": 40.0, "pnl": -3.0},
        ]
        _write_lifecycle(path, old + new)
        # dummy lifecycle object (path only)
        class _L:
            def __init__(self, p):
                self.path = p
        notable = _recent_notable_events(_L(path), cycle_start_ms=19990)
        # only the two events AFTER the cutoff, despite 10k older events
        assert len(notable) == 2
        assert notable[0]["symbol"] == "ETHUSDT"
        assert notable[1]["symbol"] == "SOLUSDT"
    finally:
        os.unlink(path)


def test_recent_notable_events_empty_file():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        class _L:
            def __init__(self, p):
                self.path = p
        assert _recent_notable_events(_L(path), cycle_start_ms=0) == []
    finally:
        os.unlink(path)


def test_recent_notable_events_no_cutoff():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        _write_lifecycle(path, [
            {"ts": 5, "symbol": "X", "state": "position_opened"},
            {"ts": 6, "symbol": "Y", "state": "tp1_hit"},
        ])
        class _L:
            def __init__(self, p):
                self.path = p
        notable = _recent_notable_events(_L(path), cycle_start_ms=0)
        assert len(notable) == 2
    finally:
        os.unlink(path)


def test_notify_trail_and_time_stop(monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "send_message", lambda text: sent.append(text))
    notifier.notify_trail_stop("BTCUSDT", price=65000.0, net_pnl=12.5)
    notifier.notify_time_stop("ETHUSDT", price=3500.0, net_pnl=-4.2)
    assert len(sent) == 2
    assert "TRAILING STOP" in sent[0] and "+12.5000 USDT" in sent[0]
    assert "TIME STOP" in sent[1] and "-4.2000 USDT" in sent[1]


def test_lifecycle_realized_fallback(monkeypatch, tmp_path):
    from lifecycle import LifecycleLogger, SL_HIT, PROFIT_TAKE
    dispatched = []
    monkeypatch.setattr(notifier, "is_configured", lambda: True)
    monkeypatch.setattr(notifier, "notify_sl_hit",
                        lambda symbol, price, net_pnl: dispatched.append(("sl", symbol, price, net_pnl)))
    monkeypatch.setattr(notifier, "notify_profit_take",
                        lambda symbol, price, net_pnl: dispatched.append(("pt", symbol, price, net_pnl)))

    ll = LifecycleLogger(str(tmp_path / "test_life.jsonl"))
    # When executor passes realized= instead of pnl=
    ll.log("S1", "BTCUSDT", SL_HIT, close_price=98.0, realized=-2.5, terminal=True)
    ll.log("S2", "ETHUSDT", PROFIT_TAKE, price=110.0, pnl=5.0)

    assert len(dispatched) == 2
    assert dispatched[0] == ("sl", "BTCUSDT", 98.0, -2.5)
    assert dispatched[1] == ("pt", "ETHUSDT", 110.0, 5.0)

