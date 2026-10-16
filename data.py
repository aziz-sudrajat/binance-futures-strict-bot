# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Market data layer — public endpoints only, no API key.

ARCHITECTURE: signal data comes from OKX perpetuals ONLY; Binance is
execution-only (orders, balance, positions, leverage). There is no fallback
market-data provider — if OKX is unreachable the engine fails closed
(no signal), never trades on another venue's tape.

OKX public endpoints used:
  klines          /api/v5/market/candles (BTC-USDT-SWAP)
  ticker/price    /api/v5/market/ticker
  order book      /api/v5/market/books (+ ctVal from /api/v5/public/instruments)
  funding         /api/v5/public/funding-rate
  OI history      /api/v5/rubik/stat/contracts/open-interest-volume
  L/S account     /api/v5/rubik/stat/contracts/long-short-account-ratio
  top traders     /api/v5/rubik/stat/contracts/long-short-position-ratio
  taker volume    /api/v5/rubik/stat/taker-volume
  liquidations    /api/v5/public/liquidation-orders
In-memory TTL caches mirror the GAS klinesTtlSec_ / DERIVATIVES_CACHE_SECONDS.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import math
import re
import time

log = logging.getLogger("data")

import requests

import httputil
import sentiment
from config import (
    ADV_DERIV_CACHE_SEC,
    DATA_CACHE_MAX_ENTRIES,
    DERIVATIVES_CACHE_SEC,
    DERIVATIVES_PERIOD,
    HTTP_TIMEOUT_SEC,
    KLINES_FALLBACK_ENABLED,
    KLINES_LIMIT,
    KLINES_TTL_SEC,
    MIN_CANDLES,
    ORDER_BOOK_DEPTH_LIMIT,
)

_OKX_INTERVAL = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1Dutc",
}  # GAS: okxInterval_ (L667)
_INTERVAL_MS = {
    "1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000,
    "30m": 1800000, "1h": 3600000, "2h": 7200000, "4h": 14400000,
    "1d": 86400000,
}  # GAS: intervalToMs_ (L682)

_OKX_BASE = "https://www.okx.com"

# Gate.io futures klines (item 3.1: opt-in failover for _klines_with_fallback).
# Intervals Gate.io does NOT support (e.g. 3m) keep the fail-closed path.
_GATEIO_BASE = "https://api.gateio.ws"
_GATEIO_INTERVAL = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d",
}


def gateio_contract_id(symbol: str) -> str:
    """Binance-style 'BTCUSDT' -> Gate.io futures 'BTC_USDT'."""
    m = re.fullmatch(r"([A-Z0-9]+)(USDT|USDC)", normalize_symbol(symbol))
    if not m:
        raise ValueError(f"no Gate.io contract mapping for {symbol}")
    return f"{m.group(1)}_{m.group(2)}"

# News review (ketat mode): public CoinTelegraph RSS, no API key.
_NEWS_RSS_URL = "https://cointelegraph.com/rss"
NEWS_TTL_SEC = 900            # cache headlines 15 min
NEWS_LOOKBACK_H = 24          # review window for coin mentions

# base symbol -> headline search names (lowercase substrings)
_COIN_NAMES = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth"],
    "BNB": ["bnb", "binance coin"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "DOGE": ["dogecoin", "doge"],
    "ADA": ["cardano", "ada"],
    "AVAX": ["avalanche", "avax"],
    "LINK": ["chainlink", "link"],
    "DOT": ["polkadot", "dot"],
    "MATIC": ["polygon", "matic"],
    "ATOM": ["cosmos", "atom"],
    "NEAR": ["near protocol", "near"],
    "ARB": ["arbitrum", "arb"],
    "OP": ["optimism", "op"],
}

# Keyword lists moved to sentiment.KeywordSentimentProvider (item 3.4);
# data.py keeps only a thin backward-compatible wrapper below.


def _news_sentiment(title: str) -> int:
    """Backward-compatible wrapper: delegates to the active sentiment
    provider (default: keyword heuristic). Sign convention unchanged:
    positive = bullish, negative = bearish (signal_engine thresholds
    depend on it). Use sentiment.set_provider() to plug an AI model.
    """
    return sentiment.get_provider().score(title)


def _parse_rss_date(pub: str) -> float:
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def normalize_symbol(s: str) -> str:
    """GAS: normalizeSymbol_ (L793). 'btc' -> 'BTCUSDT'."""
    x = re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    if not x:
        return ""
    if not re.search(r"(USDT|USDC)$", x):
        x += "USDT"
    return x


def okx_swap_id(symbol: str) -> str:
    """Binance-style 'BTCUSDT' -> OKX perpetual 'BTC-USDT-SWAP'.

    All signal data (klines, ticker, derivatives) comes from OKX; Binance
    is used for execution only.
    """
    m = re.fullmatch(r"([A-Z0-9]+)(USDT|USDC)", normalize_symbol(symbol))
    if not m:
        raise ValueError(f"no OKX SWAP mapping for {symbol}")
    return f"{m.group(1)}-{m.group(2)}-SWAP"


def ratio_to_long_pct(ratio: float) -> float:
    """GAS: ratioToLongPct_ (L1426)."""
    r = float(ratio or 0)
    return r / (1 + r) * 100 if r > 0 else 50.0


class _BoundedTTLCache:
    """In-memory cache with a hard max size and LRU eviction.

    Stores (timestamp, value) tuples — the same shape the old plain-dict
    caches used — so the per-interval TTL checks at the call sites are
    unchanged. A read hit marks the key most-recently-used; on write the
    least-recently-used entries are evicted until len <= max_entries.
    """

    def __init__(self, max_entries: int):
        self._d: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self.max_entries = max(1, int(max_entries))

    def get(self, key: str) -> "tuple[float, object] | None":
        hit = self._d.get(key)
        if hit is not None:
            self._d.move_to_end(key)  # LRU: hit = most recently used
        return hit

    def put(self, key: str, value: object) -> None:
        self._d[key] = (time.time(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_entries:
            self._d.popitem(last=False)  # evict least recently used

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def clear(self) -> None:
        self._d.clear()


class MarketData:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or httputil.ThrottledSession()
        self.session.headers.update({"User-Agent": "binance-futures-bot/phase1"})
        self._klines_cache = _BoundedTTLCache(DATA_CACHE_MAX_ENTRIES)
        self._deriv_cache = _BoundedTTLCache(DATA_CACHE_MAX_ENTRIES)
        self._quanto_cache = _BoundedTTLCache(DATA_CACHE_MAX_ENTRIES)  # OKX ctVal per instId
        self._news_cache: tuple[float, list] | None = None  # (ts, headlines)
        self._news_ok: bool = False  # True if last RSS fetch succeeded

    # ------------------------------------------------------------- klines
    def _klines_ttl(self, interval: str) -> int:
        return KLINES_TTL_SEC.get(interval, 600)

    def get_klines(self, symbol: str, interval: str, limit: int = KLINES_LIMIT) -> list[dict]:
        """Closed candles, oldest-first — OKX perpetual primary.

        Signal data comes from OKX; Binance is execution-only. If OKX is
        unreachable the caller gets a RuntimeError and must fail closed
        (no signal) — UNLESS the opt-in KLINES_FALLBACK_ENABLED knob is
        set, in which case Gate.io USDT futures klines are used (candles
        tagged "source": "gateio"). Both down -> RuntimeError, always
        fail-closed, never a partial/empty tape.
        """
        symbol = normalize_symbol(symbol)
        limit = max(1, min(1000, int(limit or KLINES_LIMIT)))
        key = f"{symbol}_{interval}_{limit}"
        hit = self._klines_cache.get(key)
        if hit and time.time() - hit[0] < self._klines_ttl(interval):
            return hit[1]
        candles = self._klines_with_fallback(symbol, interval, limit)
        if len(candles) < min(MIN_CANDLES, limit):
            raise RuntimeError(
                f"klines: only {len(candles)} candles for "
                f"{symbol} {interval}")
        self._klines_cache.put(key, candles)
        return candles

    def _klines_with_fallback(self, symbol: str, interval: str,
                              limit: int) -> list[dict]:
        """OKX first; Gate.io futures only if the opt-in knob allows it.

        Default (KLINES_FALLBACK_ENABLED=False) = historical fail-closed
        behavior: OKX down -> RuntimeError, never another venue's tape.
        Opt-in fallback exists for data continuity; fallback candles are
        tagged with "source": "gateio" so callers can tell venues apart.
        Both down -> RuntimeError (still fail-closed).
        """
        try:
            return self._okx_klines(symbol, interval, limit)
        except Exception as exc:
            if not KLINES_FALLBACK_ENABLED:
                raise
            log.warning("OKX klines failed for %s %s (%r); "
                        "trying Gate.io fallback", symbol, interval, exc)
            try:
                return self._gateio_klines(symbol, interval, limit)
            except Exception as gexc:
                raise RuntimeError(
                    f"klines unavailable for {symbol} {interval}: "
                    f"OKX failed ({exc!r}), Gate.io failed ({gexc!r})"
                ) from gexc

    def _gateio_klines(self, symbol: str, interval: str,
                       limit: int) -> list[dict]:
        """Gate.io USDT-margined futures klines, normalized to the same
        candle dicts as _okx_klines, plus a "source" tag."""
        bar = _GATEIO_INTERVAL.get(interval)
        if not bar:
            raise ValueError(f"Gate.io mapping unavailable for {interval}")
        contract = gateio_contract_id(symbol)
        req_limit = min(max(limit + 2, 80), 300)
        url = f"{_GATEIO_BASE}/api/v4/futures/usdt/candlesticks"
        r = self.session.get(
            url, params={"contract": contract, "interval": bar,
                         "limit": req_limit},
            timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        body = r.json()
        if not isinstance(body, list):
            raise ValueError("Gate.io invalid response")
        dur = _INTERVAL_MS[interval]
        now_ms = int(time.time() * 1000)
        out = []
        for k in body:
            try:
                t = int(float(k["t"]) * 1000)  # Gate.io: seconds
                if t + dur > now_ms:
                    continue  # in-progress candle, skip (OKX: k[8]=='1')
                out.append({
                    "open_time": t, "open": float(k["o"]),
                    "high": float(k["h"]), "low": float(k["l"]),
                    "close": float(k["c"]), "volume": float(k["v"]),
                    "close_time": t + dur - 1, "source": "gateio",
                })
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c["open_time"])
        return out[-limit:]

    def _okx_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        bar = _OKX_INTERVAL.get(interval)
        if not bar:
            raise ValueError(f"OKX mapping unavailable for {interval}")
        inst_id = okx_swap_id(symbol)  # perpetual candles, not spot
        req_limit = min(max(limit + 2, 80), 300)  # GAS: getOkxClosedKlines_ (L573)
        url = f"{_OKX_BASE}/api/v5/market/candles"
        r = self.session.get(url, params={"instId": inst_id, "bar": bar, "limit": req_limit},
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        body = r.json()
        if not body or str(body.get("code")) != "0" or not isinstance(body.get("data"), list):
            raise ValueError("OKX invalid response")
        dur = _INTERVAL_MS[interval]
        out = []
        for k in body["data"]:
            # GAS: parseOkxRows_ (L644) — k[8] '1' means the candle is closed.
            if str(k[8]) != "1":
                continue
            t = int(k[0])
            out.append({
                "open_time": t, "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                "close_time": t + dur - 1,
            })
        out.sort(key=lambda c: c["open_time"])
        return out[-limit:]

    # ------------------------------------------------------- 24h statistics
    def get_24h_quote_volume_usd(self, symbol: str) -> float | None:
        """Quote-volume (USDT) over the last 24h from the OKX perpetual
        ticker; None if unavailable.

        NOTE: for SWAP tickers OKX returns volCcy24h in *base* currency,
        so it is multiplied by the last price to get USDT notional.
        """
        symbol = normalize_symbol(symbol)
        try:
            rows = self._okx_get("/api/v5/market/ticker",
                                 {"instId": okx_swap_id(symbol)})
            if rows:
                base_vol = self._num(rows[0].get("volCcy24h"))
                last = self._num(rows[0].get("last"))
                if base_vol is not None and last:
                    return base_vol * last
        except Exception:  # noqa: BLE001 - best effort, like GAS
            pass
        return None

    # ---------------------------------------------------------- live price
    def get_live_price(self, symbol: str) -> float | None:
        """Live last price for the freshness gate (GAS: freshnessCheck_ L5075)
        from the OKX perpetual ticker — OKX is the only signal venue.

        Returns None on ANY failure — the caller must fail closed, never
        treat a missing price as "fine" (audit C2: GAS returned ok:true on
        price_unavailable — a fail-open we do not reproduce).
        """
        symbol = normalize_symbol(symbol)
        try:
            r = self.session.get(
                f"{_OKX_BASE}/api/v5/market/ticker",
                params={"instId": okx_swap_id(symbol)},
                timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json().get("data") or []
            if data:
                price = float(data[0].get("last") or 0)
                if price > 0:
                    return price
        except Exception:  # noqa: BLE001 - fail closed, return None
            pass
        return None

    # ---------------------------------------------------------- derivatives
    def get_derivatives_snapshot(self, symbol: str,
                                 period: str = DERIVATIVES_PERIOD) -> dict:
        """Derivatives positioning from OKX (the only signal venue).
        Never raises for partial data."""
        symbol = normalize_symbol(symbol)
        key = f"{symbol}_{period}"
        hit = self._deriv_cache.get(key)
        if hit and time.time() - hit[0] < DERIVATIVES_CACHE_SEC:
            return hit[1]

        try:
            src = self._okx_derivatives(symbol, period)
        except Exception:  # noqa: BLE001 - GAS derivSafe_ (L722)
            src = {"provider": "okx"}

        snap = {
            "symbol": symbol,
            "period": period,
            "at": int(time.time() * 1000),
            "provider": src.get("provider", "okx"),
            "aggregate_long": src.get("long_pct"),
            "aggregate_funding": src.get("funding"),
            "aggregate_oi_change": src.get("oi_change_pct"),
            "taker_buy_sell_ratio": src.get("taker_ratio"),
        }
        self._deriv_cache.put(key, snap)
        return snap

    def _okx_get(self, path: str, params: dict) -> list:
        """GET a public OKX endpoint; returns the data list (may be empty)."""
        r = self.session.get(f"{_OKX_BASE}{path}", params=params,
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        body = r.json()
        if not body or str(body.get("code")) != "0":
            raise ValueError(f"OKX {path}: code={body.get('code') if body else None}")
        data = body.get("data")
        return data if isinstance(data, list) else []

    @staticmethod
    def _num(v):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    def _okx_derivatives(self, symbol: str, period: str) -> dict:
        """Basic derivatives snapshot from OKX public endpoints.

        Rubik contracts endpoints only support periods 5m/1H/1D, so the
        engine's 4h/1d periods map to 1H/1D. Rows come back as arrays
        [ts, value, ...], not dicts.
        """
        inst = okx_swap_id(symbol)
        base = inst.split("-")[0]
        okx_period = {"4h": "1H", "1d": "1D"}.get(period, "1H")
        out: dict = {"provider": "okx"}
        # Funding rate (current)
        rows = self._okx_get("/api/v5/public/funding-rate",
                             {"instId": inst})
        if rows and isinstance(rows[0], dict):
            v = self._num(rows[0].get("fundingRate"))
            if v is not None:
                out["funding"] = v
        # Open interest history -> change %
        rows = self._okx_get("/api/v5/rubik/stat/contracts/open-interest-volume",
                             {"instId": inst, "period": okx_period,
                              "ccy": base})
        if len(rows) >= 2:
            now_v = self._num(rows[-1][1])
            prev_v = self._num(rows[-2][1])
            if now_v is not None and prev_v:
                out["oi_change_pct"] = (now_v - prev_v) / prev_v * 100
        # Long/short account ratio -> long %
        rows = self._okx_get(
            "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
            {"instId": inst, "period": okx_period})
        if rows:
            v = self._num(rows[-1][1])
            if v is not None and v > 0:
                out["long_pct"] = ratio_to_long_pct(v)
        # Taker buy/sell volume -> ratio (per base ccy, all its contracts)
        rows = self._okx_get("/api/v5/rubik/stat/taker-volume",
                             {"ccy": base, "instType": "CONTRACTS",
                              "period": "5m"})
        buy = sum(self._num(r[2]) or 0 for r in rows if len(r) >= 3)
        sell = sum(self._num(r[1]) or 0 for r in rows if len(r) >= 3)
        if buy > 0 and sell > 0:
            out["taker_ratio"] = buy / sell
        if len(out) <= 1:
            raise ValueError("OKX derivatives empty")
        return out

    # ---------------------------------------------- advanced derivatives
    def get_advanced_derivatives(self, symbol: str,
                                 period: str = DERIVATIVES_PERIOD) -> dict:
        """Positioning, leverage-risk inputs, squeeze inputs, and the
        liquidation window — all from OKX public endpoints (the only signal
        venue; Binance is execution-only).

        Returns a dict whose individual fields may be None; raises
        RuntimeError when OKX delivers nothing usable at all. Callers must
        fail closed (hold the signal), never treat missing fields as neutral.
        """
        symbol = normalize_symbol(symbol)
        key = f"adv_{symbol}_{period}"
        hit = self._deriv_cache.get(key)
        if hit and time.time() - hit[0] < ADV_DERIV_CACHE_SEC:
            return hit[1]

        inst = okx_swap_id(symbol)
        base = inst.split("-")[0]
        # Rubik contracts endpoints support 5m/1H/1D only.
        okx_period = {"4h": "1H", "1d": "1D"}.get(period, "1H")
        num = self._num

        def long_pct(v):
            return ratio_to_long_pct(v) if v is not None and v > 0 else None

        # Retail positioning: long/short account ratio (array rows).
        retail_long = None
        try:
            rows = self._okx_get(
                "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
                {"instId": inst, "period": okx_period})
            if rows:
                retail_long = long_pct(num(rows[-1][1]))
        except Exception:  # noqa: BLE001 - field stays None
            pass

        # Whale positioning: top-trader long/short position ratio.
        top_long = None
        try:
            rows = self._okx_get(
                "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader",
                {"instId": inst, "period": okx_period})
            if rows:
                top_long = long_pct(num(rows[-1][1]))
        except Exception:  # noqa: BLE001
            pass

        # Taker flow: buy/sell volume -> long % (per base ccy).
        taker_long = None
        try:
            rows = self._okx_get("/api/v5/rubik/stat/taker-volume",
                                 {"ccy": base, "instType": "CONTRACTS",
                                  "period": "5m"})
            buy = sum(num(r[2]) or 0 for r in rows if len(r) >= 3)
            sell = sum(num(r[1]) or 0 for r in rows if len(r) >= 3)
            if (buy + sell) > 0:
                taker_long = buy / (buy + sell) * 100
        except Exception:  # noqa: BLE001
            pass

        # Funding + OI from the same endpoints as the basic snapshot.
        funding, oi_usd, oi_change = None, None, None
        try:
            rows = self._okx_get("/api/v5/public/funding-rate",
                                 {"instId": inst})
            if rows:
                funding = num(rows[0].get("fundingRate"))
        except Exception:  # noqa: BLE001
            pass
        try:
            rows = self._okx_get(
                "/api/v5/rubik/stat/contracts/open-interest-volume",
                {"instId": inst, "period": okx_period, "ccy": base})
            if len(rows) >= 2:
                # NOTE: this endpoint returns oi in USD notional already.
                cur = num(rows[-1][1])
                prv = num(rows[-2][1])
                oi_usd = cur
                if cur and prv:
                    oi_change = (cur - prv) / prv * 100
        except Exception:  # noqa: BLE001
            pass

        # Liquidation window from OKX public liquidation orders (recent 100):
        # aggregate long/short USD; spike = recent-half vs older-half totals.
        long_liq_usd, short_liq_usd, spike = 0.0, 0.0, 0.0
        try:
            uly = inst[:-5]  # BTC-USDT-SWAP -> BTC-USDT
            rows = self._okx_get("/api/v5/public/liquidation-orders",
                                 {"instType": "SWAP", "uly": uly,
                                  "state": "filled", "limit": "100"})
            ct_val = self._okx_ct_val(inst)
            details = []
            for r in rows:
                d = r.get("details") if isinstance(r, dict) else None
                if isinstance(d, list):
                    details.extend(d)
            vals = []
            for d in details:
                px = num(d.get("bkPx")) or num(d.get("px"))
                sz = num(d.get("sz"))
                ts = num(d.get("ts"))
                if px and sz and ts:
                    vals.append((ts, d.get("posSide"),
                                 sz * ct_val * px))
            vals.sort(key=lambda x: x[0])
            half = len(vals) // 2
            for _ts, side, usd in vals[half:]:
                if side == "long":
                    long_liq_usd += usd
                else:
                    short_liq_usd += usd
            older_total = sum(u for _t, _s, u in vals[:half])
            recent_total = long_liq_usd + short_liq_usd
            spike = (recent_total / older_total if older_total > 0
                     else (1.0 if recent_total > 0 else 0.0))
        except Exception:  # noqa: BLE001 - fields stay 0.0
            pass

        # 24h quote volume from the OKX perpetual ticker.
        volume_24h = self.get_24h_quote_volume_usd(symbol)

        adv = {
            "symbol": symbol, "period": period,
            "at": int(time.time() * 1000), "provider": "okx",
            "retail_long": retail_long,
            "top_long": top_long,
            "top_position_long": top_long,
            "taker_long": taker_long,
            "divergence": (top_long - retail_long
                           if top_long is not None and retail_long is not None
                           else None),
            "funding": funding,
            "oi_usd": oi_usd,
            "oi_change": oi_change,
            "volume_24h": volume_24h,
            "price_change_24h": None,
            "long_liq_usd": long_liq_usd,
            "short_liq_usd": short_liq_usd,
            "spike": spike,
        }
        if all(adv[k] is None for k in ("retail_long", "top_long", "funding",
                                        "oi_usd", "taker_long")) \
                and not (long_liq_usd or short_liq_usd):
            raise RuntimeError(f"OKX advanced derivatives empty for {symbol}")
        self._deriv_cache.put(key, adv)
        return adv

    def list_okx_swap_symbols(self) -> list[str]:
        """All USDT-margined OKX perpetuals as Binance-style symbols.

        Used by the all-coin scanner. Leveraged tokens are excluded.
        """
        rows = self._okx_get("/api/v5/market/tickers",
                             {"instType": "SWAP"})
        out = []
        for r in rows:
            inst = str(r.get("instId", ""))
            m = re.fullmatch(r"([A-Z0-9]+)-USDT-SWAP", inst)
            if not m:
                continue
            base = m.group(1)
            if base.endswith(("UP", "DOWN", "BULL", "BEAR", "3L", "3S")):
                continue
            out.append(f"{base}USDT")
        return sorted(set(out))

    def get_swap_tickers(self) -> list[dict]:
        """Bulk 24h stats for every USDT-margined OKX perpetual (one HTTP call).

        Returns Binance-style dicts: symbol, last, vol_usd_24h, chg_pct_24h.
        chg is derived from open24h because /market/tickers has no % field.
        Leveraged tokens and non-USDT-SWAP instruments are excluded.
        """
        rows = self._okx_get("/api/v5/market/tickers", {"instType": "SWAP"})
        out = []
        for r in rows:
            inst = str(r.get("instId", ""))
            m = re.fullmatch(r"([A-Z0-9]+)-USDT-SWAP", inst)
            if not m:
                continue
            base = m.group(1)
            if base.endswith(("UP", "DOWN", "BULL", "BEAR", "3L", "3S")):
                continue
            last = self._num(r.get("last"))
            open24 = self._num(r.get("open24h"))
            base_vol = self._num(r.get("volCcy24h"))  # SWAP: base currency
            if not last:
                continue
            vol_usd = (base_vol or 0.0) * last
            chg_pct = ((last - open24) / open24 * 100.0
                       if open24 else 0.0)
            out.append({"symbol": f"{base}USDT", "base": base, "last": last,
                        "vol_usd_24h": vol_usd, "chg_pct_24h": chg_pct})
        return out

    def _okx_ct_val(self, inst_id: str) -> float:
        """Contract value (base currency) for an OKX SWAP instrument.

        Cached — instrument specs are static. Raises instead of guessing:
        a wrong multiplier corrupts liquidation/book notional math.
        """
        hit = self._quanto_cache.get(inst_id)
        if hit is None:
            rows = self._okx_get("/api/v5/public/instruments",
                                 {"instType": "SWAP", "instId": inst_id})
            ct = self._num((rows[0] if rows else {}).get("ctVal"))
            if not ct or ct <= 0:
                raise ValueError(f"OKX ctVal missing for {inst_id}")
            self._quanto_cache.put(inst_id, ct)
            return ct
        return hit[1]

    # ---------------------------------------------------------- order book
    @staticmethod
    def _pack_book(bids: list, asks: list, provider: str) -> dict:
        if not bids or not asks:
            raise ValueError(f"{provider}: empty order book side")
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        if not (mid > 0):
            raise ValueError(f"{provider}: invalid book mid")
        return {"bids": [(float(p), float(s)) for p, s in bids],
                "asks": [(float(p), float(s)) for p, s in asks],
                "mid": mid, "provider": provider}

    def get_order_book(self, symbol: str,
                       limit: int = ORDER_BOOK_DEPTH_LIMIT) -> dict:
        """Order book for the liquidity-wall veto — OKX perpetual ONLY
        (the only signal venue; Binance is execution-only).

        Sizes come in contracts; converted to base-asset units via the
        instrument ctVal so downstream notional math stays venue-agnostic.
        Raises if OKX fails — the caller must fail closed, never assume
        'no wall'.
        """
        symbol = normalize_symbol(symbol)
        inst = okx_swap_id(symbol)
        try:
            rows = self._okx_get("/api/v5/market/books",
                                 {"instId": inst,
                                  "sz": str(min(max(int(limit), 5), 400))})
            if not rows:
                raise ValueError("OKX empty book")
            book = rows[0]
            ct_val = self._okx_ct_val(inst)
            bids = [(float(b[0]), float(b[1]) * ct_val)
                    for b in book.get("bids", []) if len(b) >= 2]
            asks = [(float(a[0]), float(a[1]) * ct_val)
                    for a in book.get("asks", []) if len(a) >= 2]
            return self._pack_book(bids, asks, "okx_swap")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"order book unavailable for {symbol}: {e}")

    # ---------------------------------------------------------------- news
    # CoinTelegraph RSS (public, no key). Cached NEWS_TTL_SEC seconds.
    # Used by the ketat-mode news review: headlines mentioning the coin in
    # the last NEWS_LOOKBACK_H hours are keyword-scored for sentiment.
    # A fetch failure returns [] — the ketat review then FAILS CLOSED
    # (no signal) rather than trading blind to news.
    def get_coin_news(self, symbol: str,
                      hours: int = 24) -> list[dict]:
        """Headlines mentioning the coin: [{title, ts, sentiment}]."""
        symbol = normalize_symbol(symbol)
        headlines = self._fetch_news_headlines()
        cutoff = time.time() - hours * 3600
        base = re.sub(r"(USDT|USDC)$", "", symbol)
        names = _COIN_NAMES.get(base, [base.lower()])
        out = []
        for h in headlines:
            if h["ts"] < cutoff:
                continue
            title_l = h["title"].lower()
            if not any(n in title_l for n in names):
                continue
            out.append({"title": h["title"], "ts": h["ts"],
                        "sentiment": _news_sentiment(h["title"])})
        return out

    def _fetch_news_headlines(self) -> list[dict]:
        now = time.time()
        if self._news_cache and now - self._news_cache[0] < NEWS_TTL_SEC:
            return self._news_cache[1]
        headlines: list[dict] = []
        try:
            r = self.session.get(_NEWS_RSS_URL, timeout=20)
            r.raise_for_status()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if not title:
                    continue
                ts = _parse_rss_date(pub)
                headlines.append({"title": title, "ts": ts})
            self._news_ok = True
        except Exception:  # noqa: BLE001 - empty list -> ketat fails closed
            headlines = []
            self._news_ok = False
        self._news_cache = (now, headlines)
        return headlines

    def news_feed_ok(self) -> bool:
        """True if the news RSS was fetched successfully (cached)."""
        self._fetch_news_headlines()
        return self._news_ok
