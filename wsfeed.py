# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""OKX public WebSocket price feed (item 2.5).

A minimal, dependency-free WebSocket client (RFC 6455 over stdlib
``socket`` + ``ssl``) that keeps a thread-safe cache of the latest
OKX perpetual ticker prices. It exists to serve the executor's
*freshness re-check* with a live price WITHOUT one REST round-trip
per signal — pure price-source layer, no trading/signal/risk logic.

Design decisions (documented per task 2.5):
  * stdlib-only, NO new dependency: the WS handshake, masked client
    frames, unmasked server frames, ping/pong and close handling are
    all small and fully specified by RFC 6455. Adding ``websocket-client``
    would add a pip step to every deployment (incl. the Cue host) for
    no functional gain. ``requirements.txt`` is unchanged.
  * OKX (not Binance) public WS: the whole market-data layer reads OKX
    (Binance mainnet is geo-blocked from some hosts; OKX is the signal
    venue — see data.MarketData.get_live_price).
  * Fail-safe by construction: ``get_live_price(symbol, rest_fn)`` tries
    the WS cache first (fresh only) and falls back to the existing REST
    path on ANY failure — disabled knob, no connection yet, stale
    price, exception. Existing behavior is never broken.

Usage (single wired path — executor._try_open freshness re-check):
    from wsfeed import get_live_price
    live = get_live_price(symbol, self.market.get_live_price)
"""

import base64
import hashlib
import json
import logging
import os
import random
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlparse

import config

log = logging.getLogger("wsfeed")

OKX_WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
TICKERS_CHANNEL = "tickers"

CONNECT_TIMEOUT_SEC = 10.0      # TCP+TLS+handshake budget per attempt
PING_INTERVAL_SEC = 20.0        # OKX expects a client "ping" regularly
PONG_GRACE_SEC = 15.0           # extra slack after a ping before reconnect
BACKOFF_BASE_SEC = 1.0          # reconnect backoff start
BACKOFF_MAX_SEC = 60.0          # reconnect backoff cap
MAX_FRAME_BYTES = 1 << 20       # sanity cap on one WS frame payload
MAX_SUBSCRIBE_PER_MSG = 50      # OKX limit: batch subscriptions


# ---------------------------------------------------------------------------
# Minimal RFC 6455 client (stdlib only)
# ---------------------------------------------------------------------------

class _WsError(Exception):
    """Any WebSocket protocol/transport failure (triggers reconnect)."""


class _WsClosed(_WsError):
    """Peer sent a close frame."""


_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def _encode_frame(opcode: int, payload: bytes, mask: bool) -> bytes:
    """Encode one WS frame. Client->server frames MUST be masked."""
    first = 0x80 | (opcode & 0x0F)          # FIN=1
    n = len(payload)
    mask_bit = 0x80 if mask else 0
    if n < 126:
        header = struct.pack("!BB", first, mask_bit | n)
    elif n < (1 << 16):
        header = struct.pack("!BBH", first, mask_bit | 126, n)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, n)
    if not mask:
        return header + payload
    mask_key = os.urandom(4)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return header + mask_key + masked


class _RawWs:
    """One blocking WebSocket connection. Not thread-safe for concurrent
    send/recv — callers serialize sends with a lock."""

    def __init__(self, url: str):
        self.url = url
        self.sock: socket.socket | None = None
        self._buf = bytearray()

    # -- connect / handshake -------------------------------------------
    def connect(self) -> None:
        parts = urlparse(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise _WsError(f"unsupported scheme: {parts.scheme}")
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        raw = socket.create_connection((host, port),
                                       timeout=CONNECT_TIMEOUT_SEC)
        if parts.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self.sock.settimeout(CONNECT_TIMEOUT_SEC)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise _WsError("handshake: connection closed")
            head += chunk
            if len(head) > 16384:
                raise _WsError("handshake: header too large")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise _WsError(f"handshake rejected: {status!r}")

        accept = hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
        expected = base64.b64encode(accept)
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-accept:"):
                if line.split(b":", 1)[1].strip() != expected:
                    raise _WsError("handshake: bad Sec-WebSocket-Accept")
                break
        else:
            raise _WsError("handshake: missing Sec-WebSocket-Accept")
        self._buf = bytearray()

    # -- IO --------------------------------------------------------------
    def send_text(self, text: str) -> None:
        if self.sock is None:
            raise _WsError("not connected")
        self.sock.sendall(_encode_frame(_OP_TEXT, text.encode("utf-8"),
                                        mask=True))

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise _WsError("not connected")
        self.sock.sendall(_encode_frame(opcode, payload, mask=True))

    def _read_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            assert self.sock is not None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise _WsError("connection closed by peer")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_text(self, timeout: float) -> str | None:
        """Next text message, or None on idle timeout. Handles ping/pong
        and close frames internally. Raises _WsError on protocol errors."""
        assert self.sock is not None
        fragments: list[bytes] = []
        self.sock.settimeout(timeout)
        while True:
            try:
                hdr = self._read_exactly(2)
            except socket.timeout:
                # Idle (or stalled mid-message: drop the incomplete
                # fragments — the stream stays aligned on frame
                # boundaries). Buffered bytes are kept as-is.
                return None
            b1, b2 = hdr[0], hdr[1]
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exactly(8))[0]
            if length > MAX_FRAME_BYTES:
                raise _WsError(f"frame too large: {length}")
            if b2 & 0x80:  # server frames MUST NOT be masked
                mask_key = self._read_exactly(4)
                raw = self._read_exactly(length)
                payload = bytes(b ^ mask_key[i % 4]
                                for i, b in enumerate(raw))
            else:
                payload = self._read_exactly(length)

            if opcode == _OP_TEXT or opcode == 0x0:  # text / continuation
                fragments.append(payload)
                if b1 & 0x80:  # FIN
                    return b"".join(fragments).decode("utf-8",
                                                      errors="replace")
            elif opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
            elif opcode == _OP_PONG:
                continue
            elif opcode == _OP_CLOSE:
                self._send_frame(_OP_CLOSE, payload)
                raise _WsClosed("peer closed")
            else:
                raise _WsError(f"unsupported opcode: {opcode:#x}")

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                self._send_frame(_OP_CLOSE)
            except Exception:  # noqa: BLE001 - best effort
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001 - best effort
                pass


# ---------------------------------------------------------------------------
# OKX ticker feed
# ---------------------------------------------------------------------------

class WsPriceFeed:
    """Background OKX public-WS ticker cache.

    Thread-safe: one daemon reader thread, prices guarded by a lock.
    Auto-reconnects with capped exponential backoff + jitter and
    re-subscribes every symbol after each reconnect.
    """

    def __init__(self, url: str = OKX_WS_PUBLIC_URL):
        self.url = url
        self._lock = threading.Lock()
        self._prices: dict[str, tuple[float, float]] = {}  # inst -> (px, ts)
        self._subscribed: set[str] = set()   # confirmed by server
        self._wanted: set[str] = set()       # requested via ensure_symbols
        self._ws: _RawWs | None = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_pong = 0.0
        self._connects = 0  # successful connects (test introspection)

    # -- public API ------------------------------------------------------
    def start(self) -> "WsPriceFeed":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="wsfeed",
                                            daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)

    def ensure_symbols(self, inst_ids: list[str]) -> None:
        """Request ticker subscriptions (idempotent). Non-blocking; if a
        connection is live the subscribe is sent immediately, otherwise
        it goes out on the next (re)connect."""
        if not inst_ids:
            return
        with self._lock:
            new = [i for i in inst_ids
                   if i and i not in self._wanted]
            for i in new:
                self._wanted.add(i)
        if new:
            self._send_subscribe(new)

    def get_price(self, inst_id: str) -> tuple[float, float] | None:
        """(price, receipt_time) or None if never seen."""
        with self._lock:
            return self._prices.get(inst_id)

    def fresh_price(self, inst_id: str, max_age_sec: float) -> float | None:
        """Price only if the cached tick is younger than max_age_sec."""
        hit = self.get_price(inst_id)
        if hit is None:
            return None
        price, ts = hit
        if price <= 0 or (time.time() - ts) > max_age_sec:
            return None
        return price

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._ws is not None

    # -- internals ---------------------------------------------------------
    def _send_subscribe(self, inst_ids: list[str]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            with self._send_lock:
                for i in range(0, len(inst_ids), MAX_SUBSCRIBE_PER_MSG):
                    batch = inst_ids[i:i + MAX_SUBSCRIBE_PER_MSG]
                    ws.send_text(json.dumps({
                        "op": "subscribe",
                        "args": [{"channel": TICKERS_CHANNEL, "instId": s}
                                 for s in batch],
                    }))
        except Exception as exc:  # noqa: BLE001 - reader loop reconnects
            log.debug("ws subscribe failed: %r", exc)

    def _on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        if "event" in msg:
            # subscribe/error acks — log errors, ignore the rest
            if msg.get("event") == "error":
                log.warning("ws server error: %s %s",
                            msg.get("code"), msg.get("msg"))
            elif msg.get("event") == "subscribe":
                arg = msg.get("arg") or {}
                inst = arg.get("instId")
                if inst:
                    with self._lock:
                        self._subscribed.add(inst)
            return
        arg = msg.get("arg") or {}
        if arg.get("channel") != TICKERS_CHANNEL:
            return
        inst = arg.get("instId")
        data = msg.get("data") or []
        if not inst or not data:
            return
        try:
            price = float(data[0].get("last") or 0)
        except (TypeError, ValueError):
            return
        if price > 0:
            with self._lock:
                self._prices[inst] = (price, time.time())

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._connect_and_serve()
                attempt = 0  # clean exit only via stop()
            except _WsError as exc:
                log.debug("ws feed error: %r", exc)
            except Exception as exc:  # noqa: BLE001 - never kill the thread
                log.warning("ws feed unexpected error: %r", exc)
            if self._stop.is_set():
                break
            delay = min(BACKOFF_BASE_SEC * (2 ** attempt),
                        BACKOFF_MAX_SEC)
            delay = delay * (0.5 + random.random())  # jitter
            attempt += 1
            log.info("ws feed reconnecting in %.1fs (attempt %d)",
                     delay, attempt)
            self._stop.wait(delay)

    def _connect_and_serve(self) -> None:
        ws = _RawWs(self.url)
        ws.connect()
        with self._lock:
            self._ws = ws
            # fresh connection = server forgot every subscription
            self._subscribed.clear()
            pending = sorted(self._wanted)
            self._connects += 1
        self._last_pong = time.time()
        if pending:
            self._send_subscribe(pending)
        try:
            while not self._stop.is_set():
                text = ws.recv_text(PING_INTERVAL_SEC)
                now = time.time()
                if text is None:
                    # idle: keep-alive; bail out if the server went quiet
                    if now - self._last_pong > PING_INTERVAL_SEC + PONG_GRACE_SEC:
                        raise _WsError("pong timeout — reconnecting")
                    try:
                        with self._send_lock:
                            ws.send_text("ping")
                    except _WsError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        raise _WsError(f"ping failed: {exc!r}")
                    continue
                if text == "pong":
                    self._last_pong = now
                    continue
                if text == "ping":  # server keep-alive ping
                    with self._send_lock:
                        ws.send_text("pong")
                    self._last_pong = now
                    continue
                self._last_pong = now  # any traffic proves liveness
                self._on_message(text)
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws = None
            ws.close()


# ---------------------------------------------------------------------------
# Fail-safe accessor used by the executor
# ---------------------------------------------------------------------------

_feed: WsPriceFeed | None = None
_feed_lock = threading.Lock()


def _shared_feed() -> WsPriceFeed:
    """Process-wide singleton, started lazily and non-blocking. Never
    raises — a feed that cannot start is the same as a dead feed:
    get_live_price() falls back to REST."""
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = WsPriceFeed(url=OKX_WS_PUBLIC_URL)
            try:
                _feed.start()
            except Exception:  # noqa: BLE001 - background thread start
                log.warning("ws feed failed to start; using REST only")
        return _feed


def _reset_shared_feed() -> None:
    """Test hook: drop the singleton so tests start isolated."""
    global _feed
    with _feed_lock:
        old, _feed = _feed, None
    if old is not None:
        try:
            old.stop()
        except Exception:  # noqa: BLE001 - best effort
            pass


def get_live_price(symbol: str, rest_fn) -> float | None:
    """Live price for the freshness gate: OKX WS cache first (fresh only),
    REST ``rest_fn(symbol)`` as automatic fallback.

    Never raises for WS-side failures (they degrade to REST). ``rest_fn``
    keeps its original semantics — exceptions from it propagate exactly
    as before, so existing fail-closed call sites are unchanged.
    """
    if not config.WS_PRICE_FEED_ENABLED:
        return rest_fn(symbol)
    try:
        from data import normalize_symbol, okx_swap_id
        sym = normalize_symbol(symbol)
        if not sym:
            return rest_fn(symbol)
        inst_id = okx_swap_id(sym)
        feed = _shared_feed()
        feed.ensure_symbols([inst_id])
        price = feed.fresh_price(inst_id, config.WS_STALE_SEC)
        if price is not None:
            return price
    except Exception as exc:  # noqa: BLE001 - fail-safe to REST
        log.debug("ws price lookup failed (%r); falling back to REST", exc)
    return rest_fn(symbol)
