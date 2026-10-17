# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Unit tests for stream.py — User Data Stream Manager & WebSocket dispatcher."""

import json
import pytest
from stream import BinanceUserStream, parse_user_stream_event


class MockExchange:
    def __init__(self):
        self.key_counter = 0
        self.closed = False
        self.keepalive_count = 0

    def create_listen_key(self):
        self.key_counter += 1
        return f"test_listen_key_{self.key_counter}"

    def keepalive_listen_key(self):
        self.keepalive_count += 1
        return True

    def close_listen_key(self):
        self.closed = True
        return True


def test_parse_order_trade_update_json():
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1568879465651,
        "T": 1568879465650,
        "o": {
            "s": "BTCUSDT",
            "c": "my_client_id_123",
            "S": "BUY",
            "o": "LIMIT",
            "f": "GTC",
            "q": "0.500",
            "p": "10000.00",
            "ap": "9999.50",
            "sp": "0",
            "x": "TRADE",
            "X": "FILLED",
            "i": 888666,
            "l": "0.500",
            "z": "0.500",
            "L": "10000.00",
            "n": "0.002",
            "N": "USDT",
            "T": 1568879465650,
            "t": 1,
            "m": False,
            "R": False,
            "wt": "CONTRACT_PRICE",
            "rp": "15.50000000",
        }
    }
    raw_str = json.dumps(payload)
    parsed = parse_user_stream_event(raw_str)
    assert parsed is not None
    assert parsed["event_type"] == "ORDER_TRADE_UPDATE"
    assert parsed["symbol"] == "BTCUSDT"
    assert parsed["client_order_id"] == "my_client_id_123"
    assert parsed["side"] == "BUY"
    assert parsed["order_type"] == "LIMIT"
    assert parsed["orig_qty"] == 0.5
    assert parsed["orig_price"] == 10000.0
    assert parsed["last_filled_qty"] == 0.5
    assert parsed["last_filled_price"] == 10000.0
    assert parsed["order_status"] == "FILLED"
    assert parsed["order_id"] == 888666
    assert parsed["realized_profit"] == 15.5
    assert parsed["commission"] == 0.002
    assert parsed["is_maker"] is False


def test_parse_account_update_dict():
    payload = {
        "e": "ACCOUNT_UPDATE",
        "E": 1564745798939,
        "T": 1564745798938,
        "a": {
            "m": "ORDER",
            "B": [
                {"a": "USDT", "wb": "122624.12345678", "cw": "100.12345678", "bc": "50.5"},
                {"a": "BNB", "wb": "1.00000000", "cw": "0.0", "bc": "0.0"}
            ],
            "P": [
                {
                    "s": "BTCUSDT",
                    "pa": "0.5",
                    "ep": "9910.0",
                    "up": "45.0",
                    "mt": "isolated",
                    "iw": "500.0",
                    "ps": "BOTH"
                }
            ]
        }
    }
    parsed = parse_user_stream_event(payload)
    assert parsed is not None
    assert parsed["event_type"] == "ACCOUNT_UPDATE"
    assert parsed["reason"] == "ORDER"
    assert len(parsed["balances"]) == 2
    assert parsed["balances"][0]["asset"] == "USDT"
    assert parsed["balances"][0]["wallet_balance"] == pytest.approx(122624.12345678)
    assert parsed["balances"][0]["balance_change"] == 50.5
    assert len(parsed["positions"]) == 1
    assert parsed["positions"][0]["symbol"] == "BTCUSDT"
    assert parsed["positions"][0]["position_amount"] == 0.5
    assert parsed["positions"][0]["entry_price"] == 9910.0
    assert parsed["positions"][0]["unrealized_pnl"] == 45.0


def test_parse_listen_key_expired():
    payload = {"e": "listenKeyExpired", "E": 1699999999999}
    parsed = parse_user_stream_event(payload)
    assert parsed is not None
    assert parsed["event_type"] == "listenKeyExpired"
    assert parsed["event_time"] == 1699999999999


def test_parse_invalid_payloads():
    assert parse_user_stream_event("invalid-json{") is None
    assert parse_user_stream_event(12345) is None
    assert parse_user_stream_event({}) is None
    assert parse_user_stream_event({"e": "unknown_event"}) is None


def test_stream_dispatch_callbacks():
    order_events = []
    account_events = []

    mock_ex = MockExchange()
    stream = BinanceUserStream(
        exchange=mock_ex,
        on_order_update=lambda evt: order_events.append(evt),
        on_account_update=lambda evt: account_events.append(evt),
    )

    # 1. Order update dispatch
    order_msg = {
        "e": "ORDER_TRADE_UPDATE",
        "o": {"s": "ETHUSDT", "S": "SELL", "X": "FILLED", "q": "1.0", "p": "3000.0"}
    }
    res = stream.handle_message(order_msg)
    assert res is not None
    assert len(order_events) == 1
    assert order_events[0]["symbol"] == "ETHUSDT"
    assert len(account_events) == 0

    # 2. Account update dispatch
    account_msg = {
        "e": "ACCOUNT_UPDATE",
        "a": {"m": "DEPOSIT", "B": [{"a": "USDT", "wb": "5000.0"}], "P": []}
    }
    res2 = stream.handle_message(account_msg)
    assert res2 is not None
    assert len(account_events) == 1
    assert account_events[0]["reason"] == "DEPOSIT"


def test_stream_listen_key_expired_refresh():
    mock_ex = MockExchange()
    stream = BinanceUserStream(exchange=mock_ex)
    initial_key = stream.start()
    assert initial_key == "test_listen_key_1"

    # Incoming listenKeyExpired event triggers auto-refresh
    stream.handle_message({"e": "listenKeyExpired", "E": 1700000000000})
    assert stream.listen_key == "test_listen_key_2"

    stream.stop()
    assert not stream.is_running()
    assert mock_ex.closed is True
