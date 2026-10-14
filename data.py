"""Market data layer — public endpoints only, no API key.

Klines provider order mirrors GAS getKlinesWithProvider_ (L515):
  1. OKX spot klines   <- getOkxClosedKlines_ (L568) + parseOkxRows_ (L644)
  2. Binance spot klines (fallback) <- getBinanceKlines_ (L543)
In-memory TTL cache mirrors klinesTtlSec_ (L418-428).

Derivatives snapshot mirrors getDerivativesSnapshot_ (L802) + derivSafe_ (L722):
  1. Binance Futures public endpoints (funding, OI history, global long/short
     account ratio, taker long/short ratio)
  2. Gate.io public futures endpoints (fallback) <- derivGateFunding_/Oi_/
     LongShort_/Taker_ (L934-976)
Each component is fetched defensively; the snapshot aggregates whatever
succeeded, exactly like the GAS derivSafe_ pattern.
"""

from __future__ import annotations

import math
import re
import time

import requests

from config import (
    ADV_DERIV_CACHE_SEC,
    ADV_DERIV_STATS_LIMIT,
    DERIVATIVES_CACHE_SEC,
    DERIVATIVES_PERIOD,
    HTTP_TIMEOUT_SEC,
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
_BINANCE_INTERVALS = set(_INTERVAL_MS)  # GAS: getBinanceKlines_ interval map (L545)

_FAPI_BASE = "https://fapi.binance.com"
_GATE_BASE = "https://api.gateio.ws/api/v4"


def normalize_symbol(s: str) -> str:
    """GAS: normalizeSymbol_ (L793). 'btc' -> 'BTCUSDT'."""
    x = re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    if not x:
        return ""
    if not re.search(r"(USDT|USDC)$", x):
        x += "USDT"
    return x


def ratio_to_long_pct(ratio: float) -> float:
    """GAS: ratioToLongPct_ (L1426)."""
    r = float(ratio or 0)
    return r / (1 + r) * 100 if r > 0 else 50.0


class MarketData:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "binance-futures-bot/phase1"})
        self._klines_cache: dict[str, tuple[float, list]] = {}
        self._deriv_cache: dict[str, tuple[float, dict]] = {}
        self._quanto_cache: dict[str, float] = {}  # Gate contract multipliers

    # ------------------------------------------------------------- klines
    def _klines_ttl(self, interval: str) -> int:
        return KLINES_TTL_SEC.get(interval, 600)

    def get_klines(self, symbol: str, interval: str, limit: int = KLINES_LIMIT) -> list[dict]:
        """Closed candles, oldest-first. Raises if neither provider delivers."""
        symbol = normalize_symbol(symbol)
        limit = max(1, min(1000, int(limit or KLINES_LIMIT)))
        key = f"{symbol}_{interval}_{limit}"
        hit = self._klines_cache.get(key)
        if hit and time.time() - hit[0] < self._klines_ttl(interval):
            return hit[1]
        errors = []
        for fetcher in (self._okx_klines, self._binance_klines):
            try:
                candles = fetcher(symbol, interval, limit)
                if len(candles) >= min(MIN_CANDLES, limit):
                    self._klines_cache[key] = (time.time(), candles)
                    return candles
                errors.append(f"{fetcher.__name__}: only {len(candles)} candles")
            except Exception as e:  # noqa: BLE001 - provider fallback by design
                errors.append(f"{fetcher.__name__}: {e}")
        raise RuntimeError(f"klines unavailable for {symbol} {interval}: {'; '.join(errors)}")

    def _okx_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        bar = _OKX_INTERVAL.get(interval)
        m = re.fullmatch(r"([A-Z0-9]+)(USDT|USDC)", symbol)
        if not bar or not m:
            raise ValueError(f"OKX mapping unavailable for {symbol} {interval}")
        inst_id = f"{m.group(1)}-{m.group(2)}"
        req_limit = min(max(limit + 2, 80), 300)  # GAS: getOkxClosedKlines_ (L573)
        url = "https://www.okx.com/api/v5/market/candles"
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

    def _binance_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        if interval not in _BINANCE_INTERVALS:
            raise ValueError(f"interval not supported by Binance: {interval}")
        wanted = min(1000, max(1, limit))
        lim = min(1000, max(80, wanted + 1))  # GAS: getBinanceKlines_ (L550-552)
        url = "https://api.binance.com/api/v3/klines"
        r = self.session.get(url, params={"symbol": symbol, "interval": interval, "limit": lim},
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or not data:
            raise ValueError("Binance data empty")
        dur = _INTERVAL_MS[interval]
        out = []
        for k in data[:-1]:  # GAS: drop the still-open last candle (L557)
            t = int(k[0])
            out.append({
                "open_time": t, "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                "close_time": t + dur - 1,
            })
        return out[-wanted:]

    # ------------------------------------------------------- 24h statistics
    def get_24h_quote_volume_usd(self, symbol: str) -> float | None:
        """Quote-volume (USDT) over the last 24h; None if unavailable."""
        symbol = normalize_symbol(symbol)
        m = re.fullmatch(r"([A-Z0-9]+)(USDT|USDC)", symbol)
        try:
            if m:  # OKX 24h ticker
                r = self.session.get(
                    "https://www.okx.com/api/v5/market/ticker",
                    params={"instId": f"{m.group(1)}-{m.group(2)}"},
                    timeout=HTTP_TIMEOUT_SEC)
                r.raise_for_status()
                data = r.json().get("data") or []
                if data:
                    return float(data[0].get("volCcy24h") or 0)
        except Exception:  # noqa: BLE001 - best effort, like GAS
            pass
        return None

    # ---------------------------------------------------------- live price
    def get_live_price(self, symbol: str) -> float | None:
        """Live last price for the freshness gate (GAS: freshnessCheck_ L5075
        fetches a fresh ticker — the Python port previously used the last
        closed 15m candle, up to ~23 min stale; audit D1/C3).

        Binance Futures ticker/price first, OKX spot ticker as fallback.
        Returns None on ANY failure — the caller must fail closed, never
        treat a missing price as "fine" (audit C2: GAS returned ok:true on
        price_unavailable — a fail-open we do not reproduce).
        """
        symbol = normalize_symbol(symbol)
        try:
            r = self.session.get(f"{_FAPI_BASE}/fapi/v1/ticker/price",
                                 params={"symbol": symbol},
                                 timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            price = float((r.json() or {}).get("price") or 0)
            if price > 0:
                return price
        except Exception:  # noqa: BLE001 - fall through to OKX
            pass
        m = re.fullmatch(r"([A-Z0-9]+)(USDT|USDC)", symbol)
        if m:
            try:
                r = self.session.get(
                    "https://www.okx.com/api/v5/market/ticker",
                    params={"instId": f"{m.group(1)}-{m.group(2)}"},
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
        """Aggregate derivatives positioning. Never raises for partial data."""
        symbol = normalize_symbol(symbol)
        key = f"{symbol}_{period}"
        hit = self._deriv_cache.get(key)
        if hit and time.time() - hit[0] < DERIVATIVES_CACHE_SEC:
            return hit[1]

        long_pcts, fundings, oi_changes, taker_ratio = [], [], [], None

        def safe(fn, *a):
            try:
                return fn(*a)
            except Exception:  # noqa: BLE001 - GAS derivSafe_ (L722)
                return None

        # --- Binance Futures (primary) ---
        b = safe(self._binance_derivatives, symbol, period)
        # --- Gate.io (fallback, GAS derivGate* L934-976) ---
        g = safe(self._gate_derivatives, symbol, period) if b is None else None
        src = b or g or {}

        if src.get("long_pct") is not None:
            long_pcts.append(src["long_pct"])
        if src.get("funding") is not None:
            fundings.append(src["funding"])
        if src.get("oi_change_pct") is not None:
            oi_changes.append(src["oi_change_pct"])
        taker_ratio = src.get("taker_ratio")

        snap = {
            "symbol": symbol,
            "period": period,
            "at": int(time.time() * 1000),
            "provider": src.get("provider", "none"),
            "aggregate_long": sum(long_pcts) / len(long_pcts) if long_pcts else None,
            "aggregate_funding": sum(fundings) / len(fundings) if fundings else None,
            "aggregate_oi_change": sum(oi_changes) / len(oi_changes) if oi_changes else None,
            "taker_buy_sell_ratio": taker_ratio,
        }
        self._deriv_cache[key] = (time.time(), snap)
        return snap

    def _binance_derivatives(self, symbol: str, period: str) -> dict:
        out: dict = {"provider": "binance_futures"}
        # Funding rate (last)
        r = self.session.get(f"{_FAPI_BASE}/fapi/v1/fundingRate",
                             params={"symbol": symbol, "limit": 1}, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
        if rows:
            out["funding"] = float(rows[-1]["fundingRate"])
        # Open interest history -> change %
        r = self.session.get(f"{_FAPI_BASE}/futures/data/openInterestHist",
                             params={"symbol": symbol, "period": period, "limit": 2},
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
        if len(rows) >= 2:
            now_v = float(rows[-1]["sumOpenInterestValue"])
            prev_v = float(rows[-2]["sumOpenInterestValue"])
            out["oi_change_pct"] = (now_v - prev_v) / prev_v * 100 if prev_v else 0.0
        elif rows:
            out["oi_change_pct"] = 0.0
        # Global long/short account ratio -> long %
        r = self.session.get(f"{_FAPI_BASE}/futures/data/globalLongShortAccountRatio",
                             params={"symbol": symbol, "period": period, "limit": 1},
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
        if rows:
            out["long_pct"] = ratio_to_long_pct(float(rows[-1]["longShortRatio"]))
        # Taker buy/sell ratio
        r = self.session.get(f"{_FAPI_BASE}/futures/data/takerlongshortRatio",
                             params={"symbol": symbol, "period": period, "limit": 1},
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
        if rows:
            out["taker_ratio"] = float(rows[-1]["buySellRatio"])
        if len(out) <= 1:
            raise ValueError("Binance derivatives empty")
        return out

    def _gate_derivatives(self, symbol: str, period: str) -> dict:
        """Port of GAS derivGateFunding_/Oi_/LongShort_/Taker_ (L934-976)."""
        contract = re.sub(r"USDT$", "_USDT", symbol)  # BTCUSDT -> BTC_USDT
        out: dict = {"provider": "gateio"}
        interval = {"4h": "4h", "1d": "1d"}.get(period, "4h")  # GAS derivGateInterval_
        # Funding from ticker
        r = self.session.get("https://api.gateio.ws/api/v4/futures/usdt/tickers",
                             params={"contract": contract}, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json() if isinstance(r.json(), list) else []
        tick = next((x for x in rows if x.get("contract") == contract), rows[0] if rows else None)
        if tick is not None:
            out["funding"] = float(tick.get("funding_rate") or 0)
        # Stats: OI change, long/short ratio, taker ratio
        r = self.session.get("https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                             params={"contract": contract, "interval": interval, "limit": 2},
                             timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError("Gate stats empty")
        rows.sort(key=lambda x: float(x.get("time") or 0))
        last, prev = rows[-1], rows[-2] if len(rows) > 1 else rows[-1]
        now_oi = float(last.get("open_interest_usd") or 0)
        prev_oi = float(prev.get("open_interest_usd") or now_oi)
        out["oi_change_pct"] = (now_oi - prev_oi) / prev_oi * 100 if prev_oi else 0.0
        lsr = float(last.get("lsr_account") or 0)
        if lsr > 0:
            out["long_pct"] = ratio_to_long_pct(lsr)
        taker = float(last.get("lsr_taker") or 0)
        if taker > 0:
            out["taker_ratio"] = taker
        if len(out) <= 1:
            raise ValueError("Gate derivatives empty")
        return out

    # ---------------------------------------------- advanced derivatives
    def get_advanced_derivatives(self, symbol: str,
                                 period: str = DERIVATIVES_PERIOD) -> dict:
        """Port of GAS derivGateAdvanced_ (L994): positioning, leverage-risk
        inputs, squeeze inputs, and the liquidation window — all from Gate.io
        contract_stats, exactly like the GAS reference (which uses Gate for
        the advanced snapshot even when other providers feed the basic one).

        Returns a dict whose individual fields may be None; raises
        RuntimeError when Gate delivers nothing usable at all. Callers must
        fail closed (hold the signal), never treat missing fields as neutral.
        """
        symbol = normalize_symbol(symbol)
        key = f"adv_{symbol}_{period}"
        hit = self._deriv_cache.get(key)
        if hit and time.time() - hit[0] < ADV_DERIV_CACHE_SEC:
            return hit[1]

        contract = re.sub(r"USDT$", "_USDT", symbol)
        r = self.session.get(
            f"{_GATE_BASE}/futures/usdt/contract_stats",
            params={"contract": contract, "interval": "15m",
                    "limit": ADV_DERIV_STATS_LIMIT},
            timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"Gate contract_stats empty for {contract}")
        rows.sort(key=lambda x: float(x.get("time") or 0))
        last = rows[-1]
        prev = rows[-2] if len(rows) > 1 else last

        def num(v):
            try:
                f = float(v)
                return f if math.isfinite(f) else None
            except (TypeError, ValueError):
                return None

        def liq_usd(row, side):
            # GAS: derivGateLiqUsdFromRow_ (L1243)
            for k in (f"{side}_liq_usd_new", f"{side}_liq_usd"):
                v = num(row.get(k))
                if v is not None and v > 0:
                    return v
            amt, mark = num(row.get(f"{side}_liq_amount")), num(row.get("mark_price"))
            if amt is not None and amt > 0 and mark is not None and mark > 0:
                return amt * mark
            return 0.0

        # Liquidation window: 16 latest 15m buckets vs prior 16-bucket windows
        # (GAS: derivGateLiquidationWindow_ L1154, spec 4h -> 15m x 16).
        cur = rows[-16:]
        long_usd = sum(liq_usd(x, "long") for x in cur)
        short_usd = sum(liq_usd(x, "short") for x in cur)
        total_usd = long_usd + short_usd
        prior = rows[:-16]
        totals = []
        for end in range(len(prior), 15, -16):
            totals.append(sum(liq_usd(x, "long") + liq_usd(x, "short")
                              for x in prior[end - 16:end]))
        avg_hist = sum(totals) / len(totals) if totals else 0.0
        spike = (total_usd / avg_hist if avg_hist > 0
                 else (1.0 if total_usd > 0 else 0.0))

        mark = num(last.get("mark_price"))
        oi_usd = num(last.get("open_interest_usd"))
        if oi_usd is None:
            oi_c = num(last.get("open_interest"))
            oi_usd = oi_c * mark if oi_c is not None and mark else None
        prev_oi = num(prev.get("open_interest_usd"))
        if prev_oi is None:
            prev_c = num(prev.get("open_interest"))
            prev_oi = prev_c * mark if prev_c is not None and mark else None
        oi_change = ((oi_usd - prev_oi) / prev_oi * 100
                     if oi_usd is not None and prev_oi else None)

        # Funding + 24h quote volume from the Gate futures ticker.
        funding, volume_24h, price_change_24h = None, None, None
        try:
            tr = self.session.get(
                f"{_GATE_BASE}/futures/usdt/tickers",
                params={"contract": contract}, timeout=HTTP_TIMEOUT_SEC)
            tr.raise_for_status()
            trows = tr.json() if isinstance(tr.json(), list) else []
            tick = next((x for x in trows if x.get("contract") == contract),
                        trows[0] if trows else None)
            if tick is not None:
                funding = num(tick.get("funding_rate"))
                volume_24h = (num(tick.get("volume_24h_usd"))
                              or num(tick.get("volume_24h_quote")))
                price_change_24h = num(tick.get("change_percentage"))
        except Exception:  # noqa: BLE001 - ticker best-effort; fields stay None
            pass

        def long_pct(v):
            return ratio_to_long_pct(v) if v is not None and v > 0 else None

        retail_long = long_pct(num(last.get("lsr_account")))
        top_long = long_pct(num(last.get("top_lsr_account")))
        adv = {
            "symbol": symbol, "period": period,
            "at": int(time.time() * 1000), "provider": "gateio",
            "retail_long": retail_long,
            "top_long": top_long,
            "top_position_long": long_pct(num(last.get("top_lsr_size"))),
            "taker_long": long_pct(num(last.get("lsr_taker"))),
            "divergence": (top_long - retail_long
                           if top_long is not None and retail_long is not None
                           else None),
            "funding": funding,
            "oi_usd": oi_usd,
            "oi_change": oi_change,
            "volume_24h": volume_24h,
            "price_change_24h": price_change_24h,
            "long_liq_usd": long_usd,
            "short_liq_usd": short_usd,
            "spike": spike,
        }
        self._deriv_cache[key] = (time.time(), adv)
        return adv

    def _gate_quanto(self, contract: str) -> float:
        """Per-contract size multiplier for Gate.io futures books.

        Gate's order_book `s` is in contracts; true base-asset size is
        s * quanto_multiplier (verified live: BTC_USDT 0.0001, ETH_USDT 0.01,
        SOL_USDT 1, DOGE_USDT 10). Cached — contract specs are static.
        Raises instead of guessing: a wrong multiplier is a false veto.
        """
        if contract not in self._quanto_cache:
            r = self.session.get(
                f"{_GATE_BASE}/futures/usdt/contracts/{contract}",
                timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            q = float((r.json() or {}).get("quanto_multiplier") or 0)
            if not q > 0:
                raise ValueError(f"Gate quanto_multiplier missing for {contract}")
            self._quanto_cache[contract] = q
        return self._quanto_cache[contract]

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
        """Order book for the liquidity-wall veto (GAS: derivLiquidityWalls_
        L1361, which reads the OKX spot book). Provider order: Binance
        futures depth (most relevant for futures entries) -> Gate.io futures
        book -> OKX spot book. Raises if every provider fails — the caller
        must fail closed, never assume 'no wall'.
        """
        symbol = normalize_symbol(symbol)
        errors = []
        try:  # Binance futures depth (public)
            r = self.session.get(
                f"{_FAPI_BASE}/fapi/v1/depth",
                params={"symbol": symbol,
                        "limit": min(max(int(limit), 5), 1000)},
                timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            b = r.json()
            return self._pack_book(b.get("bids", []), b.get("asks", []),
                                   "binance_futures")
        except Exception as e:  # noqa: BLE001 - provider fallback by design
            errors.append(f"binance_futures: {e}")
        try:  # Gate.io futures order book (public)
            # Gate quotes size in *contracts*, not base asset: notional needs
            # the per-contract quanto_multiplier (BTC_USDT 0.0001, ETH_USDT
            # 0.01, ...). Without it a best ask reads as a multi-billion
            # "wall" — a 10,000x false veto. Sizes are converted to base-asset
            # units here so downstream notional math stays venue-agnostic.
            contract = re.sub(r"USDT$", "_USDT", symbol)
            quanto = self._gate_quanto(contract)
            r = self.session.get(
                f"{_GATE_BASE}/futures/usdt/order_book",
                params={"contract": contract, "limit": int(limit)},
                timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            b = r.json()
            bids = [(x["p"], float(x["s"]) * quanto) for x in b.get("bids", [])]
            asks = [(x["p"], float(x["s"]) * quanto) for x in b.get("asks", [])]
            return self._pack_book(bids, asks, "gateio")
        except Exception as e:  # noqa: BLE001
            errors.append(f"gateio: {e}")
        try:  # OKX spot book (public; the GAS reference source)
            m = re.fullmatch(r"([A-Z0-9]+)(USDT|USDC)", symbol)
            if not m:
                raise ValueError("no OKX mapping")
            r = self.session.get(
                "https://www.okx.com/api/v5/market/books",
                params={"instId": f"{m.group(1)}-{m.group(2)}",
                        "sz": int(limit)},
                timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            data = (r.json().get("data") or [{}])[0]
            return self._pack_book(data.get("bids", []), data.get("asks", []),
                                   "okx_spot")
        except Exception as e:  # noqa: BLE001
            errors.append(f"okx_spot: {e}")
        raise RuntimeError(f"order book unavailable for {symbol}: "
                           f"{'; '.join(errors)}")
