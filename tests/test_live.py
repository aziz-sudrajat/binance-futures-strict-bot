# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Live-mode safety tests for run_executor --live.

Live = real money. These tests pin the hard safety properties:
- --live refuses without BINANCE_LIVE_KEY/BINANCE_LIVE_SECRET (never falls back)
- --live refuses every TEST ONLY flag (--fixed-margin-usdt, --leverage,
  --leverage-tiers, --leverage-default, --manual)
- --live is mutually exclusive with --paper/--testnet
- --live builds against the mainnet base URL with production risk defaults
  (1%/trade, max 3x) and separate state files
"""
import argparse

import pytest

import config
from run_executor import build, require_live_keys


def _args(**over):
    base = dict(paper=False, testnet=False, live=True, once=True, loop=False,
                interval=5, fixed_margin_usdt=None, leverage=None,
                leverage_tiers=None, leverage_default=None, manual=None)
    base.update(over)
    return argparse.Namespace(**base)


LIVE_ENV = {"BINANCE_LIVE_KEY": "k", "BINANCE_LIVE_SECRET": "s"}


# ---------------------------------------------------------- key requirement
def test_require_live_keys_refuses_empty():
    with pytest.raises(SystemExit):
        require_live_keys({})


def test_require_live_keys_refuses_partial():
    with pytest.raises(SystemExit):
        require_live_keys({"BINANCE_LIVE_KEY": "k"})


def test_require_live_keys_accepts_full():
    assert require_live_keys(LIVE_ENV) == ("k", "s")


def test_build_live_refuses_without_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE",
                        str(tmp_path / "state-live.json"))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "lifecycle-live.jsonl"))
    with pytest.raises(SystemExit):
        build(_args(), env={})


# ------------------------------------------------- TEST ONLY flag refusals
@pytest.mark.parametrize("flag", ["fixed_margin_usdt", "leverage",
                                   "leverage_default", "manual"])
def test_build_live_refuses_test_only_flags(flag, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE",
                        str(tmp_path / "state-live.json"))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "lifecycle-live.jsonl"))
    val = "BTCUSDT=20" if flag == "leverage_tiers" else (
        "BTCUSDT" if flag == "manual" else 10)
    with pytest.raises(SystemExit) as ei:
        build(_args(**{flag: val}), env=dict(LIVE_ENV))
    assert "TEST ONLY" in str(ei.value)


def test_build_live_refuses_leverage_tiers(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE",
                        str(tmp_path / "state-live.json"))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "lifecycle-live.jsonl"))
    with pytest.raises(SystemExit) as ei:
        build(_args(leverage_tiers="BTCUSDT=20"), env=dict(LIVE_ENV))
    assert "TEST ONLY" in str(ei.value)


# ------------------------------------------------------- mode exclusivity
@pytest.mark.parametrize("extra", [dict(paper=True), dict(testnet=True)])
def test_build_live_mutually_exclusive(extra, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE",
                        str(tmp_path / "state-live.json"))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "lifecycle-live.jsonl"))
    with pytest.raises(SystemExit):
        build(_args(**extra), env=dict(LIVE_ENV))


def test_build_no_mode_refuses(tmp_path, monkeypatch):
    a = _args(live=False)  # all three modes off
    with pytest.raises(SystemExit):
        build(a, env=dict(LIVE_ENV))


# ------------------------------------------------- live build correctness
def test_build_live_uses_mainnet_and_safe_defaults(tmp_path, monkeypatch):
    state_p = tmp_path / "state-live.json"
    life_p = tmp_path / "lifecycle-live.jsonl"
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE", str(state_p))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE", str(life_p))
    executor, mode = build(_args(), env=dict(LIVE_ENV))
    assert mode == f"LIVE ({config.BINANCE_FUTURES_MAINNET_URL})"
    assert executor.exchange.base_url == config.BINANCE_FUTURES_MAINNET_URL
    # production risk defaults, no test overrides
    assert executor.fixed_margin_usdt is None
    assert executor.leverage_override is None
    assert executor.leverage_tiers is None
    assert executor.leverage_default is None
    assert config.RISK_PCT_PER_TRADE == 1.0
    assert config.MAX_LEVERAGE == 3
    # separate state files, never the shared testnet/paper ones
    assert executor.state.path == str(state_p)
    assert executor.lifecycle.path == str(life_p)
    assert "live" in executor.state.path


def test_build_live_honours_base_url_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE",
                        str(tmp_path / "state-live.json"))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "lifecycle-live.jsonl"))
    env = dict(LIVE_ENV, BINANCE_FUTURES_BASE_URL="https://fapi.example")
    executor, mode = build(_args(), env=env)
    assert mode == "LIVE (https://fapi.example)"
    assert executor.exchange.base_url == "https://fapi.example"


def test_live_never_uses_testnet_key_env(tmp_path, monkeypatch):
    """Testnet keys must not satisfy --live (no cross-mode fallback)."""
    monkeypatch.setattr(config, "EXEC_LIVE_STATE_FILE",
                        str(tmp_path / "state-live.json"))
    monkeypatch.setattr(config, "EXEC_LIVE_LIFECYCLE_FILE",
                        str(tmp_path / "lifecycle-live.jsonl"))
    with pytest.raises(SystemExit):
        build(_args(), env={"BINANCE_TESTNET_KEY": "k",
                            "BINANCE_TESTNET_SECRET": "s"})


# ------------------------------------------------------------------ --balance
class _FakeBalanceExchange:
    def __init__(self, bal):
        self._bal = bal

    def get_balance(self, asset="USDT"):
        return self._bal


class _FakeBalanceExecutor:
    def __init__(self, bal):
        self.exchange = _FakeBalanceExchange(bal)


def test_balance_live_refuses_without_keys():
    from run_executor import main
    with pytest.raises(SystemExit):
        main(["--live", "--balance"])


def test_balance_prints_live_balance(monkeypatch, capsys):
    import run_executor
    from run_executor import main
    monkeypatch.setattr(run_executor, "build",
                        lambda args, env=None: (_FakeBalanceExecutor(1234.5),
                                                "LIVE (https://fapi.binance.com)"))
    rc = main(["--live", "--balance"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1234.50" in out and "USDT" in out


def test_balance_reports_exchange_error(monkeypatch, capsys):
    import run_executor
    from run_executor import main
    monkeypatch.setattr(run_executor, "build",
                        lambda args, env=None: (_FakeBalanceExecutor(None),
                                                "LIVE (https://fapi.binance.com)"))
    rc = main(["--live", "--balance"])
    assert rc == 1
    assert "unavailable" in capsys.readouterr().out


# -------------------------------- listenKey auth (cyb fix 2026-10-17)
class _CapturingSession:
    """requests.Session stand-in capturing method/url/params/headers."""

    def __init__(self, body=None):
        self.calls = []
        self._body = body

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "params": dict(params or {}),
                           "headers": dict(headers or {})})
        body = self._body

        class _R:
            status_code = 200
            text = "{}"

            def json(self):
                return body

        return _R()


def _listenkey_client(monkeypatch, body=None):
    import rate_limiter
    monkeypatch.setattr(rate_limiter, "wait", lambda *a, **k: True)
    from exchange import ExchangeClient
    sess = _CapturingSession(body=body or {})
    return ExchangeClient("KEY123", "SECRET123", session=sess), sess


def test_listenkey_create_sends_api_key_without_signature(monkeypatch):
    client, sess = _listenkey_client(monkeypatch, {"listenKey": "k" * 60})
    assert client.create_listen_key() == "k" * 60
    call = sess.calls[0]
    assert call["method"] == "POST"
    assert call["headers"].get("X-MBX-APIKEY") == "KEY123"
    assert "signature" not in call["params"]
    assert "timestamp" not in call["params"]


def test_listenkey_keepalive_sends_api_key_without_signature(monkeypatch):
    client, sess = _listenkey_client(monkeypatch, {"listenKey": "k" * 60})
    assert client.create_listen_key() == "k" * 60
    assert client.keepalive_listen_key() is True
    call = sess.calls[1]
    assert call["method"] == "PUT"
    assert call["headers"].get("X-MBX-APIKEY") == "KEY123"
    assert "signature" not in call["params"]
    assert "timestamp" not in call["params"]


def test_listenkey_keepalive_sends_mandatory_listenkey_param(monkeypatch):
    """Binance rejects PUT /fapi/v1/listenKey without the listenKey param."""
    client, sess = _listenkey_client(monkeypatch, {"listenKey": "k" * 60})
    assert client.create_listen_key() == "k" * 60
    assert client.keepalive_listen_key() is True
    call = sess.calls[1]
    assert call["params"].get("listenKey") == "k" * 60


def test_listenkey_keepalive_without_acquired_key_returns_false(monkeypatch):
    """No HTTP call may be made when no listenKey was acquired."""
    client, sess = _listenkey_client(monkeypatch)
    assert client.keepalive_listen_key() is False
    assert sess.calls == []


def test_listenkey_close_sends_api_key_without_signature(monkeypatch):
    client, sess = _listenkey_client(monkeypatch, {"listenKey": "k" * 60})
    assert client.create_listen_key() == "k" * 60
    assert client.close_listen_key() is True
    call = sess.calls[1]
    assert call["method"] == "DELETE"
    assert call["headers"].get("X-MBX-APIKEY") == "KEY123"
    assert "signature" not in call["params"]
    assert "timestamp" not in call["params"]


def test_listenkey_close_sends_mandatory_listenkey_param(monkeypatch):
    """Binance rejects DELETE /fapi/v1/listenKey without the listenKey param."""
    client, sess = _listenkey_client(monkeypatch, {"listenKey": "k" * 60})
    assert client.create_listen_key() == "k" * 60
    assert client.close_listen_key() is True
    call = sess.calls[1]
    assert call["params"].get("listenKey") == "k" * 60


def test_listenkey_close_clears_stored_key(monkeypatch):
    """After close, keepalive must refuse instead of pinging a dead key."""
    client, sess = _listenkey_client(monkeypatch, {"listenKey": "k" * 60})
    assert client.create_listen_key() == "k" * 60
    assert client.close_listen_key() is True
    n_calls = len(sess.calls)
    assert client.keepalive_listen_key() is False
    assert len(sess.calls) == n_calls


def test_listenkey_close_without_acquired_key_returns_false(monkeypatch):
    client, sess = _listenkey_client(monkeypatch)
    assert client.close_listen_key() is False
    assert sess.calls == []


def test_recent_notable_events_captures_all_exits_and_realized_pnl(tmp_path):
    from lifecycle import LifecycleLogger
    from run_executor import _recent_notable_events
    p = str(tmp_path / "lifecycle.jsonl")
    life = LifecycleLogger(path=p)
    life.log("s1", "BTCUSDT", "profit_take", realized=12.5, price=102.0)
    life.log("s2", "ETHUSDT", "trail_stop", realized=-4.2, close_price=98.0)
    life.log("s3", "SOLUSDT", "time_stop", realized=0.5, price=150.0)
    life.log("s4", "ADAUSDT", "manual_close", realized=1.1, close_price=0.55)

    events = _recent_notable_events(life, cycle_start_ms=0)
    by_sym = {e["symbol"]: e for e in events}
    assert set(by_sym.keys()) == {"BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"}
    assert by_sym["BTCUSDT"]["state"] == "profit_take"
    assert by_sym["BTCUSDT"]["pnl"] == 12.5
    assert by_sym["ETHUSDT"]["state"] == "trail_stop"
    assert by_sym["ETHUSDT"]["pnl"] == -4.2
    assert by_sym["ETHUSDT"]["price"] == 98.0
    assert by_sym["SOLUSDT"]["state"] == "time_stop"
    assert by_sym["SOLUSDT"]["pnl"] == 0.5
    assert by_sym["ADAUSDT"]["state"] == "manual_close"
    assert by_sym["ADAUSDT"]["pnl"] == 1.1
    assert by_sym["ADAUSDT"]["price"] == 0.55
