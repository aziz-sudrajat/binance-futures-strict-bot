"""Unit tests for the multi-exchange klines fallback (item 3.1).

No network: a fake session returns scripted OKX / Gate.io payloads.
Verifies: default fail-closed (no fallback), opt-in fallback path,
Gate.io parsing/normalization, both-down -> RuntimeError, and the
contract-id / interval mapping helpers.
"""
import time

import pytest

import config
import data
from data import MarketData, gateio_contract_id


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _okx_rows(n=100, interval_ms=900_000):
    now = int(time.time() * 1000)
    rows = []
    for i in range(n):
        t = now - (n - i) * interval_ms
        rows.append([t, "1", "2", "0.5", "1.5", "100", "0", "0", "1"])
    return rows


def _gate_rows(n=100, interval_s=900):
    now_s = int(time.time())
    return [{"t": now_s - (n - i) * interval_s,
             "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "100"}
            for i in range(n)]


class _FakeSession:
    """Scripted: okx_ok / gate_ok booleans control which venue answers."""

    def __init__(self, okx_ok=True, gate_ok=True):
        self.okx_ok = okx_ok
        self.gate_ok = gate_ok
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if "okx.com" in url:
            if not self.okx_ok:
                raise ConnectionError("okx down")
            return _Resp({"code": "0", "data": _okx_rows()})
        if "gateio" in url:
            if not self.gate_ok:
                raise ConnectionError("gate down")
            return _Resp(_gate_rows())
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture(autouse=True)
def _reset_knob(monkeypatch):
    # NB: data.py uses `from config import ...`, so the knob must be
    # patched on the data module (bound at import), not on config.
    monkeypatch.setattr(data, "KLINES_FALLBACK_ENABLED", False)
    yield


def _enable_fallback(monkeypatch):
    monkeypatch.setattr(data, "KLINES_FALLBACK_ENABLED", True)


def test_default_fail_closed_no_fallback():
    md = MarketData(session=_FakeSession(okx_ok=False))
    with pytest.raises((RuntimeError, ConnectionError)):
        md.get_klines("BTCUSDT", "15m", limit=100)


def test_opt_in_fallback_to_gateio(monkeypatch):
    _enable_fallback(monkeypatch)
    sess = _FakeSession(okx_ok=False, gate_ok=True)
    md = MarketData(session=sess)
    candles = md.get_klines("BTCUSDT", "15m", limit=100)
    assert len(candles) == 100
    c = candles[0]
    assert c["source"] == "gateio"
    assert c["open"] == 1.0 and c["close"] == 1.5
    assert c["close_time"] == c["open_time"] + 900_000 - 1
    assert any("gateio" in u for u in sess.calls)


def test_okx_preferred_when_up(monkeypatch):
    _enable_fallback(monkeypatch)
    sess = _FakeSession(okx_ok=True, gate_ok=True)
    md = MarketData(session=sess)
    candles = md.get_klines("BTCUSDT", "15m", limit=100)
    assert "source" not in candles[0]  # OKX candles carry no source tag
    assert not any("gateio" in u for u in sess.calls)


def test_both_down_raises(monkeypatch):
    _enable_fallback(monkeypatch)
    md = MarketData(session=_FakeSession(okx_ok=False, gate_ok=False))
    with pytest.raises(RuntimeError):
        md.get_klines("BTCUSDT", "15m", limit=100)


def test_unsupported_interval_fails_closed(monkeypatch):
    # Gate.io has no 3m bars -> fallback unavailable -> RuntimeError
    _enable_fallback(monkeypatch)
    md = MarketData(session=_FakeSession(okx_ok=False, gate_ok=True))
    with pytest.raises(RuntimeError):
        md.get_klines("BTCUSDT", "3m", limit=100)


def test_gateio_contract_id():
    assert gateio_contract_id("BTCUSDT") == "BTC_USDT"
    assert gateio_contract_id("ethusdt") == "ETH_USDT"
    with pytest.raises(ValueError):
        gateio_contract_id("")


def test_gateio_invalid_response(monkeypatch):
    _enable_fallback(monkeypatch)

    class _Bad(_FakeSession):
        def get(self, url, params=None, timeout=None):
            if "gateio" in url:
                return _Resp({"not": "a list"})
            return super().get(url, params=params, timeout=timeout)

    md = MarketData(session=_Bad(okx_ok=False))
    with pytest.raises(RuntimeError):
        md.get_klines("BTCUSDT", "15m", limit=100)
