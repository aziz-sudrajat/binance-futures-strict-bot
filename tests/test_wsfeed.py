"""Tests for the OKX WebSocket price feed (wsfeed, item 2.5).

No real network: a fake OKX WS server runs on 127.0.0.1 and speaks just
enough RFC 6455 + the OKX public-WS protocol (subscribe acks, tickers
pushes, text ping/pong) to exercise the client.
"""
import base64
import hashlib
import json
import socket
import struct
import threading
import time
from unittest.mock import Mock

import pytest

import config
import wsfeed
from wsfeed import WsPriceFeed, _RawWs, _WsError, _encode_frame

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ------------------------------------------------------------------ helpers

def _wait_for(cond, timeout=10.0, tick=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(tick)
    return False


def _srv_encode_text(payload: bytes) -> bytes:
    """Server -> client text frame (never masked, per RFC 6455)."""
    n = len(payload)
    if n < 126:
        return struct.pack("!BB", 0x81, n) + payload
    if n < (1 << 16):
        return struct.pack("!BBH", 0x81, 126, n) + payload
    return struct.pack("!BBQ", 0x81, 127, n) + payload


def _decode_client_frame(buf: bytearray):
    """Parse one client->server (masked) frame from buf. Returns
    (opcode, payload) or None if incomplete."""
    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    n = b2 & 0x7F
    idx = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = struct.unpack("!H", buf[2:4])[0]
        idx = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = struct.unpack("!Q", buf[2:10])[0]
        idx = 10
    if not (b2 & 0x80):
        raise AssertionError("client frame must be masked")
    if len(buf) < idx + 4 + n:
        return None
    mask = buf[idx:idx + 4]
    raw = buf[idx + 4:idx + 4 + n]
    del buf[:idx + 4 + n]
    return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(raw))


class FakeOkxServer:
    """Minimal OKX public-WS stand-in on localhost."""

    def __init__(self, reject_handshake=False):
        self.reject_handshake = reject_handshake
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", 0))
        self._lsock.listen(5)
        self._lsock.settimeout(0.5)
        self.port = self._lsock.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/ws/v5/public"
        self.subscribes: list[list[str]] = []  # instIds per subscribe msg
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        try:
            self._lsock.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=5.0)

    # -- test control ----------------------------------------------------
    def wait_subscribe(self, inst_ids, timeout=10.0):
        want = set(inst_ids)
        return _wait_for(
            lambda: any(want <= set(s) for s in self.subscribes),
            timeout=timeout)

    def drop_all(self):
        """Simulate a network drop: kill every client connection."""
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def push(self, inst_id, price):
        msg = json.dumps({
            "arg": {"channel": "tickers", "instId": inst_id},
            "data": [{"instId": inst_id, "last": str(price),
                      "ts": str(int(time.time() * 1000))}],
        }).encode()
        frame = _srv_encode_text(msg)
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.sendall(frame)
            except OSError:
                pass

    # -- internals --------------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._lsock.accept()
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(target=self._serve, args=(conn,),
                                 daemon=True)
            t.start()

    def _serve(self, conn):
        conn.settimeout(10.0)
        with self._lock:
            self._conns.append(conn)
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            if self.reject_handshake:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n"
                             b"Content-Length: 0\r\n\r\n")
                return
            key = ""
            for line in head.decode("latin1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            conn.sendall(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            buf = bytearray()
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buf += chunk
                while True:
                    fr = _decode_client_frame(buf)
                    if fr is None:
                        break
                    opcode, payload = fr
                    if opcode == 0x8:  # close
                        return
                    if opcode != 0x1:
                        continue
                    text = payload.decode("utf-8", errors="replace")
                    if text == "ping":
                        conn.sendall(_srv_encode_text(b"pong"))
                        continue
                    if text == "pong":
                        continue
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    if msg.get("op") == "subscribe":
                        insts = [a.get("instId")
                                 for a in msg.get("args", [])]
                        with self._lock:
                            self.subscribes.append(insts)
                        ack = json.dumps({
                            "event": "subscribe",
                            "arg": {"channel": "tickers",
                                    "instId": insts[0] if insts else ""},
                        }).encode()
                        conn.sendall(_srv_encode_text(ack))
        except OSError:
            pass
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def ws_server():
    srv = FakeOkxServer().start()
    yield srv
    srv.stop()


@pytest.fixture
def ws_enabled(monkeypatch):
    monkeypatch.setattr(config, "WS_PRICE_FEED_ENABLED", True)
    yield
    wsfeed._reset_shared_feed()


# ------------------------------------------------------- frame codec tests

def _client_decode(payload_frame: bytes) -> bytes:
    """Decode one masked client frame (test-side mirror of the encoder)."""
    buf = bytearray(payload_frame)
    fr = _decode_client_frame(buf)
    assert fr is not None
    opcode, payload = fr
    assert opcode == 0x1
    assert not buf
    return payload


@pytest.mark.parametrize("size", [0, 5, 125, 126, 1000, 70000])
def test_frame_encode_decode_roundtrip(size):
    payload = bytes((i * 7) % 256 for i in range(size))
    frame = _encode_frame(0x1, payload, mask=True)
    assert _client_decode(frame) == payload


def test_client_frames_are_masked():
    frame = _encode_frame(0x1, b"hello", mask=True)
    assert frame[1] & 0x80, "MASK bit must be set on client frames"


def test_handshake_rejected_raises():
    srv = FakeOkxServer(reject_handshake=True).start()
    try:
        with pytest.raises(_WsError):
            _RawWs(srv.url).connect()
    finally:
        srv.stop()


# ------------------------------------------------------- feed behavior tests

def test_price_updates_received(ws_server):
    feed = WsPriceFeed(url=ws_server.url).start()
    try:
        feed.ensure_symbols(["BTC-USDT-SWAP"])
        assert ws_server.wait_subscribe(["BTC-USDT-SWAP"])
        ws_server.push("BTC-USDT-SWAP", "61234.5")
        assert _wait_for(
            lambda: feed.fresh_price("BTC-USDT-SWAP", 30) is not None)
        assert feed.fresh_price("BTC-USDT-SWAP", 30) == 61234.5
        price, ts = feed.get_price("BTC-USDT-SWAP")
        assert price == 61234.5
        assert time.time() - ts < 30
        # unknown symbol: nothing cached
        assert feed.fresh_price("ETH-USDT-SWAP", 30) is None
    finally:
        feed.stop()


def test_staleness_detection(ws_server):
    feed = WsPriceFeed(url=ws_server.url).start()
    try:
        feed.ensure_symbols(["BTC-USDT-SWAP"])
        assert ws_server.wait_subscribe(["BTC-USDT-SWAP"])
        ws_server.push("BTC-USDT-SWAP", "60000")
        assert _wait_for(
            lambda: feed.fresh_price("BTC-USDT-SWAP", 30) is not None)
        time.sleep(0.4)
        # tick is older than the 0.2s budget -> stale
        assert feed.fresh_price("BTC-USDT-SWAP", 0.2) is None
        # but still retrievable with a generous budget
        assert feed.fresh_price("BTC-USDT-SWAP", 30) == 60000.0
    finally:
        feed.stop()


def test_reconnect_resubscribes_after_drop(ws_server):
    feed = WsPriceFeed(url=ws_server.url).start()
    try:
        feed.ensure_symbols(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        assert ws_server.wait_subscribe(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        ws_server.push("BTC-USDT-SWAP", "61000")
        assert _wait_for(
            lambda: feed.fresh_price("BTC-USDT-SWAP", 30) is not None)
        connects_before = feed._connects
        n_subs_before = len(ws_server.subscribes)

        ws_server.drop_all()  # simulate network drop

        assert _wait_for(lambda: feed._connects > connects_before,
                         timeout=15.0), "feed did not reconnect"
        # resubscribed on the new connection
        assert _wait_for(lambda: len(ws_server.subscribes) > n_subs_before,
                         timeout=10.0), "no resubscribe after reconnect"
        ws_server.push("ETH-USDT-SWAP", "3200.5")
        assert _wait_for(
            lambda: feed.fresh_price("ETH-USDT-SWAP", 30) == 3200.5,
            timeout=10.0)
    finally:
        feed.stop()


def test_ensure_symbols_idempotent(ws_server):
    feed = WsPriceFeed(url=ws_server.url).start()
    try:
        feed.ensure_symbols(["BTC-USDT-SWAP"])
        feed.ensure_symbols(["BTC-USDT-SWAP"])  # duplicate: no-op
        assert ws_server.wait_subscribe(["BTC-USDT-SWAP"])
        time.sleep(0.5)
        n = sum(1 for s in ws_server.subscribes
                if "BTC-USDT-SWAP" in s)
        assert n == 1, f"duplicate subscribe sent: {ws_server.subscribes}"
    finally:
        feed.stop()


def test_feed_survives_server_gone():
    # No server listening: start() must not raise, feed just stays empty.
    feed = WsPriceFeed(url="ws://127.0.0.1:1/none").start()
    try:
        feed.ensure_symbols(["BTC-USDT-SWAP"])
        time.sleep(0.5)
        assert feed.fresh_price("BTC-USDT-SWAP", 30) is None
        assert not feed.connected
    finally:
        feed.stop()


# --------------------------------------------- fail-safe get_live_price tests

def test_get_live_price_prefers_ws(ws_server, ws_enabled, monkeypatch):
    feed = WsPriceFeed(url=ws_server.url).start()
    monkeypatch.setattr(wsfeed, "_feed", feed)
    try:
        rest = Mock(return_value=999.0)
        wsfeed.get_live_price("BTCUSDT", rest)  # subscribes BTC-USDT-SWAP
        assert ws_server.wait_subscribe(["BTC-USDT-SWAP"])
        ws_server.push("BTC-USDT-SWAP", "61234.5")
        assert _wait_for(
            lambda: feed.fresh_price("BTC-USDT-SWAP", 30) == 61234.5,
            timeout=10.0)
        # now the cache is warm: a fresh Mock must never be touched
        rest2 = Mock(return_value=999.0)
        assert wsfeed.get_live_price("BTCUSDT", rest2) == 61234.5
        rest2.assert_not_called()
    finally:
        feed.stop()


def test_get_live_price_falls_back_when_stale(ws_server, ws_enabled,
                                              monkeypatch):
    monkeypatch.setattr(config, "WS_STALE_SEC", 0.2)
    feed = WsPriceFeed(url=ws_server.url).start()
    monkeypatch.setattr(wsfeed, "_feed", feed)
    try:
        feed.ensure_symbols(["BTC-USDT-SWAP"])
        assert ws_server.wait_subscribe(["BTC-USDT-SWAP"])
        ws_server.push("BTC-USDT-SWAP", "60000")
        assert _wait_for(
            lambda: feed.fresh_price("BTC-USDT-SWAP", 30) is not None)
        time.sleep(0.5)  # let the tick go stale
        rest = Mock(return_value=59999.0)
        assert wsfeed.get_live_price("BTCUSDT", rest) == 59999.0
        rest.assert_called_once_with("BTCUSDT")
    finally:
        feed.stop()


def test_get_live_price_rest_only_when_disabled(monkeypatch):
    # conftest already forces WS_PRICE_FEED_ENABLED=False
    monkeypatch.setattr(wsfeed, "_feed", None)
    rest = Mock(return_value=123.0)
    assert wsfeed.get_live_price("BTCUSDT", rest) == 123.0
    rest.assert_called_once_with("BTCUSDT")
    assert wsfeed._feed is None  # feed never started


def test_get_live_price_fallback_on_feed_error(ws_enabled, monkeypatch):
    monkeypatch.setattr(wsfeed, "_shared_feed",
                        Mock(side_effect=RuntimeError("boom")))
    rest = Mock(return_value=77.0)
    assert wsfeed.get_live_price("BTCUSDT", rest) == 77.0
    rest.assert_called_once_with("BTCUSDT")


def test_get_live_price_rest_exception_propagates(ws_enabled, monkeypatch):
    # WS-side failures degrade to REST; REST failures keep old semantics.
    monkeypatch.setattr(wsfeed, "_shared_feed",
                        Mock(side_effect=RuntimeError("boom")))

    def rest(symbol):
        raise ConnectionError("rest down")

    with pytest.raises(ConnectionError):
        wsfeed.get_live_price("BTCUSDT", rest)


# ------------------------------------------------- executor wiring test

from tests.test_executor import _executor_with, _signal  # reuse fakes


def test_try_open_freshness_routes_through_wsfeed(monkeypatch, tmp_path):
    """_try_open must call wsfeed.get_live_price(symbol, rest_fn):
    WS None -> same fail-closed reject as before."""
    ex = _executor_with(str(tmp_path))
    seen = {}

    def fake_ws_price(symbol, rest_fn):
        seen["symbol"] = symbol
        seen["rest_fn"] = rest_fn
        return None  # WS has no fresh price

    monkeypatch.setattr(wsfeed, "get_live_price", fake_ws_price)
    ok, reason = ex._try_open("BTCUSDT", _signal(), "sid1", balance=10_000.0)
    assert ok is False
    assert "live price unavailable" in reason
    assert seen["symbol"] == "BTCUSDT"
    # rest_fn must be the market's own live-price method (bound-method
    # objects compare by identity of their __self__/__func__)
    assert seen["rest_fn"].__self__ is ex.market
    assert seen["rest_fn"].__func__ is ex.market.get_live_price.__func__
    assert seen["rest_fn"]("BTCUSDT") == 100.0


def test_try_open_freshness_uses_ws_price(monkeypatch, tmp_path):
    """A fresh WS price inside the entry zone passes the freshness gate
    (proceeds to exchange filters instead of rejecting)."""
    ex = _executor_with(str(tmp_path))
    monkeypatch.setattr(wsfeed, "get_live_price",
                        lambda symbol, rest_fn: 100.2)
    ok, reason = ex._try_open("BTCUSDT", _signal(), "sid2", balance=10_000.0)
    # freshness passes; FakeExchange lacks set_leverage -> fails later,
    # which proves we got PAST the price gate.
    assert "freshness" not in reason.lower()
