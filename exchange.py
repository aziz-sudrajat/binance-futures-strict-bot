# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Binance USD-M Futures REST client (testnet-first).

Raw HTTPS + HMAC-SHA256 signing via `requests` — no exchange SDK, keeping the
project dependency-light (same philosophy as Phase 1). Keys are passed in at
construction time (from env vars); this module NEVER logs them.

Endpoints used:
  GET  /fapi/v1/exchangeInfo   (public)  - symbol status + filters
  GET  /fapi/v1/account        (signed)  - balances + positions
  POST /fapi/v1/leverage       (signed)  - set leverage
  POST /fapi/v1/marginType      (signed)  - set ISOLATED / CROSSED
  POST /fapi/v1/order          (signed)  - place order
  GET  /fapi/v1/order          (signed)  - query order
  DELETE /fapi/v1/order        (signed)  - cancel order
  GET  /fapi/v1/openOrders     (signed)  - open orders

Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import requests

import config
import rate_limiter

log = logging.getLogger("executor.exchange")


class ExchangeError(Exception):
    """Any exchange-side failure (network, auth, API error)."""


def _redact(params: dict) -> dict:
    """Copy of params safe for logging (signature only, never the secret)."""
    return {k: ("<sig>" if k == "signature" else v) for k, v in params.items()}


class ExchangeClient:
    """Thin signed wrapper around Binance USD-M Futures REST."""

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = config.BINANCE_FUTURES_BASE_URL,
                 recv_window_ms: int = config.EXEC_RECV_WINDOW_MS,
                 session: requests.Session | None = None):
        if not api_key or not api_secret:
            raise ValueError("api_key and api_secret are required")
        self._key = api_key
        # NOTE: the secret is used only for HMAC signing, never stored in logs.
        self._secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window_ms
        self.session = session or requests.Session()
        self._exchange_info: dict | None = None
        self._exchange_info_ts: float = 0.0

    # ------------------------------------------------------------ plumbing
    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        query = urlencode(params)
        sig = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = True, key_only: bool = False) -> dict:
        """key_only: send X-MBX-APIKEY header WITHOUT an HMAC signature.

        Binance listenKey endpoints require the API key but reject signed
        payloads -- they must not use signed=False (which omits the key
        entirely) nor signed=True (which attaches a signature).
        """
        params = dict(params or {})
        if signed:
            params = self._sign(params)
            headers = {"X-MBX-APIKEY": self._key}
        elif key_only:
            headers = {"X-MBX-APIKEY": self._key}
        else:
            headers = {}
        url = self.base_url + path
        try:
            rate_limiter.wait("binance")
            r = self.session.request(method, url, params=params,
                                     headers=headers,
                                     timeout=config.HTTP_TIMEOUT_SEC)
        except requests.RequestException as e:
            raise ExchangeError(f"{method} {path} network error: {e}")
        if r.status_code >= 400:
            body = r.text[:300]
            log.warning("%s %s -> HTTP %s %s params=%s",
                        method, path, r.status_code, body, _redact(params))
            raise ExchangeError(f"{method} {path} HTTP {r.status_code}: {body}")
        try:
            return r.json()
        except ValueError:
            raise ExchangeError(f"{method} {path}: non-JSON response")

    # ----------------------------------------------------- public metadata
    def get_exchange_info(self) -> dict:
        """Cached exchangeInfo (refreshed at most every 10 minutes)."""
        now = time.time()
        if self._exchange_info is None or now - self._exchange_info_ts > 600:
            self._exchange_info = self._request(
                "GET", "/fapi/v1/exchangeInfo", signed=False)
            self._exchange_info_ts = now
        return self._exchange_info

    def tradable_symbols(self) -> set[str] | None:
        """Set of TRADING PERPETUAL symbols, or None if unavailable.

        Used as a pre-filter so the scanner doesn't waste engine
        evaluations on symbols the exchange can't execute (e.g. PEPE,
        TRUTH, SOXS on testnet). Returns None on any failure — the
        caller must fail open and rely on the per-symbol check.
        """
        try:
            info = self.get_exchange_info()
        except Exception:  # noqa: BLE001 - fail open, never break a scan
            return None
        try:
            return {s.get("symbol") for s in info.get("symbols", [])
                    if s.get("symbol")
                    and s.get("status") == "TRADING"
                    and s.get("contractType") == "PERPETUAL"}
        except Exception:  # noqa: BLE001 - malformed payload
            return None

    def get_symbol_filters(self, symbol: str) -> dict | None:
        """Tradability + LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL for a symbol.

        Returns None when the symbol is not a tradable PERPETUAL contract.
        """
        info = self.get_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            if s.get("status") != "TRADING":
                return None
            if s.get("contractType") != "PERPETUAL":
                return None
            out = {"status": s["status"]}
            for f in s.get("filters", []):
                ft = f.get("filterType")
                if ft == "PRICE_FILTER":
                    out["tickSize"] = float(f["tickSize"])
                elif ft == "LOT_SIZE":
                    out["stepSize"] = float(f["stepSize"])
                    out["minQty"] = float(f["minQty"])
                    out["maxQty"] = float(f["maxQty"])
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                    # MIN_NOTIONAL (older) / NOTIONAL (newer) filter names.
                    out["minNotional"] = float(
                        f.get("minNotional", f.get("notional", 0)))
            if "stepSize" not in out or "tickSize" not in out:
                return None
            return out
        return None

    def get_top_volume_symbols(self, n: int = 30) -> list[str]:
        """Top-N USDT-margined perpetual symbols by 24h quote volume.

        Public endpoint (no auth). Leveraged tokens (UP/DOWN/BULL/BEAR) are
        excluded. Used for the dashboard "top_volume" universe mode.
        """
        data = self._request("GET", "/fapi/v1/ticker/24hr", signed=False)
        rows: list[tuple[float, str]] = []
        for t in data:
            sym = str(t.get("symbol", ""))
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            try:
                qv = float(t.get("quoteVolume", 0))
            except (TypeError, ValueError):
                continue
            if qv > 0:
                rows.append((qv, sym))
        rows.sort(key=lambda r: r[0], reverse=True)
        n = max(1, min(int(n), config.UNIVERSE_TOP_N_MAX))
        return [s for _, s in rows[:n]]

    # ------------------------------------------------------------- account
    def get_balance(self, asset: str = "USDT") -> float | None:
        """Available futures wallet balance for the asset, or None on failure."""
        try:
            acct = self._request("GET", "/fapi/v2/account")
        except ExchangeError:
            return None
        for a in acct.get("assets", []):
            if a.get("asset") == asset:
                try:
                    return float(a.get("availableBalance", 0))
                except (TypeError, ValueError):
                    return None
        return None

    def get_wallet_balance(self, asset: str = "USDT") -> float | None:
        """Realized wallet balance for the asset (excludes locked margin).

        Unlike get_balance() (availableBalance), the wallet balance only
        moves on realized PnL (fills, funding, fees) — it is immune to
        initial-margin locks when positions open. Use this for loss
        metrics (daily-loss kill switch), never availableBalance (H1).
        """
        try:
            acct = self._request("GET", "/fapi/v2/account")
        except ExchangeError:
            return None
        for a in acct.get("assets", []):
            if a.get("asset") == asset:
                try:
                    return float(a.get("walletBalance", 0))
                except (TypeError, ValueError):
                    return None
        try:
            return float(acct.get("totalWalletBalance", 0))
        except (TypeError, ValueError):
            return None

    def get_margin_balance(self) -> float | None:
        """Account equity (wallet + unrealized PnL), for drawdown guards."""
        try:
            acct = self._request("GET", "/fapi/v2/account")
        except ExchangeError:
            return None
        try:
            return float(acct.get("totalMarginBalance", 0))
        except (TypeError, ValueError):
            return None

    def get_positions(self) -> list[dict]:
        """Open positions (positionAmt != 0), normalized to a small dict.

        FAIL-CLOSED: raises ExchangeError when the account snapshot cannot
        be fetched. Callers must NOT treat a failure as "no positions" —
        doing so makes the bot misclassify live positions as closed
        (false "position gone from exchange", state corruption). Seen live
        2026-10-15: transient post-sleep network failure returned a phantom
        empty list and orphaned real positions.
        """
        acct = self._request("GET", "/fapi/v2/account")
        out = []
        for p in acct.get("positions", []):
            try:
                amt = float(p.get("positionAmt", 0))
            except (TypeError, ValueError):
                continue
            if amt == 0:
                continue
            out.append({
                "symbol": p.get("symbol"),
                "side": "LONG" if amt > 0 else "SHORT",
                "qty": abs(amt),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "unrealized_pnl": float(p.get("unRealizedProfit", 0) or 0),
                "leverage": int(p.get("leverage", 0) or 0),
            })
        return out

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self._request("POST", "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": int(leverage)})
            return True
        except ExchangeError as e:
            log.warning("set_leverage %s -> %s", symbol, e)
            return False

    def set_margin_type(self, symbol: str,
                        margin_type: str = "ISOLATED") -> bool:
        """Idempotent-ish: 'already ISOLATED' (-4046) counts as success."""
        try:
            self._request("POST", "/fapi/v1/marginType",
                          {"symbol": symbol, "marginType": margin_type})
            return True
        except ExchangeError as e:
            if "-4046" in str(e):  # "No need to change margin type."
                return True
            log.warning("set_margin_type %s -> %s", symbol, e)
            return False

    # --------------------------------------------------------------- orders
    def place_order(self, symbol: str, side: str, type: str,
                    quantity: float | None = None,
                    price: float | None = None,
                    stop_price: float | None = None,
                    time_in_force: str = "GTC",
                    reduce_only: bool = False,
                    working_type: str = "MARK_PRICE",
                    client_order_id: str | None = None) -> dict | None:
        """Place one futures order. Returns the order dict, or None on failure."""
        params: dict = {"symbol": symbol, "side": side, "type": type}
        if quantity is not None:
            params["quantity"] = quantity
        if price is not None:
            params["price"] = price
        if stop_price is not None:
            params["stopPrice"] = stop_price
        if type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            params["workingType"] = working_type
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        try:
            return self._request("POST", "/fapi/v1/order", params)
        except ExchangeError as e:
            log.warning("place_order %s %s %s -> %s",
                        symbol, side, type, e)
            return None

    def get_order(self, symbol: str, order_id: int | None = None,
                  orig_client_order_id: str | None = None) -> dict | None:
        params: dict = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        else:
            raise ValueError("order_id or orig_client_order_id required")
        try:
            return self._request("GET", "/fapi/v1/order", params)
        except ExchangeError as e:
            log.warning("get_order %s -> %s", symbol, e)
            return None

    def get_user_trades(self, symbol: str, limit: int = 20,
                      start_time: int | None = None) -> list:
        """Signed GET /fapi/v1/userTrades — ground-truth fills.

        Fail-closed: raises ExchangeError on failure (never fabricate).
        Each trade: time, side (BUY/SELL), qty, price, realizedPnl,
        commission. For a SHORT, the closing side is BUY (buyer=True).
        start_time (ms): restrict to trades at/after this timestamp —
        pass the position's opened_at so a long-downtime close can't be
        pushed out of the `limit` window by later trades on the symbol.
        """
        params = {"symbol": symbol.upper(),
                  "limit": max(1, min(1000, int(limit or 20)))}
        if start_time:
            params["startTime"] = int(start_time)
        return self._request("GET", "/fapi/v1/userTrades", params)

    def cancel_order(self, symbol: str, order_id: int | None = None,
                     orig_client_order_id: str | None = None) -> bool:
        params: dict = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        elif orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        else:
            raise ValueError("order_id or orig_client_order_id required")
        try:
            self._request("DELETE", "/fapi/v1/order", params)
            return True
        except ExchangeError as e:
            # -2011 (unknown order) means it is already gone — fine.
            if "-2011" in str(e):
                return True
            log.warning("cancel_order %s -> %s", symbol, e)
            return False

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        try:
            out = self._request("GET", "/fapi/v1/openOrders", params)
            return out if isinstance(out, list) else []
        except ExchangeError as e:
            log.warning("get_open_orders -> %s", e)
            return []

    # ------------------------------------------------- algo (conditional) orders
    # Binance migrated STOP_MARKET / TAKE_PROFIT_MARKET / STOP / TAKE_PROFIT /
    # TRAILING_STOP_MARKET to the Algo Order API (effective 2025-12-09):
    # /fapi/v1/order now rejects them with -4120. SL/TP brackets MUST go here.
    def place_algo_order(self, symbol: str, side: str, type: str,
                         quantity: float | None = None,
                         trigger_price: float | None = None,
                         price: float | None = None,
                         reduce_only: bool = False,
                         working_type: str = "MARK_PRICE",
                         time_in_force: str = "GTE_GTC",
                         client_algo_id: str | None = None) -> dict | None:
        """Place a conditional algo order. Returns the algo order dict
        (with algoId), or None on failure."""
        params: dict = {"algoType": "CONDITIONAL", "symbol": symbol,
                        "side": side, "type": type}
        if quantity is not None:
            params["quantity"] = quantity
        if trigger_price is not None:
            params["triggerPrice"] = trigger_price
        if price is not None:
            params["price"] = price
        if reduce_only:
            params["reduceOnly"] = "true"
        params["workingType"] = working_type
        params["timeInForce"] = time_in_force
        if client_algo_id:
            params["newClientAlgoId"] = client_algo_id
        try:
            return self._request("POST", "/fapi/v1/algoOrder", params)
        except ExchangeError as e:
            log.warning("place_algo_order %s %s %s -> %s",
                        symbol, side, type, e)
            return None

    def get_algo_order(self, symbol: str, algo_id: int) -> dict | None:
        try:
            return self._request("GET", "/fapi/v1/algoOrder",
                                 {"symbol": symbol, "algoId": algo_id})
        except ExchangeError as e:
            log.warning("get_algo_order %s -> %s", symbol, e)
            return None

    def cancel_algo_order(self, symbol: str, algo_id: int) -> bool:
        try:
            self._request("DELETE", "/fapi/v1/algoOrder",
                          {"symbol": symbol, "algoId": algo_id})
            return True
        except ExchangeError as e:
            # -2011 (unknown order) means it is already gone — fine.
            if "-2011" in str(e):
                return True
            log.warning("cancel_algo_order %s -> %s", symbol, e)
            return False

    def get_open_algo_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"symbol": symbol} if symbol else {}
        try:
            out = self._request("GET", "/fapi/v1/openAlgoOrders", params)
            return out if isinstance(out, list) else []
        except ExchangeError as e:
            log.warning("get_open_algo_orders -> %s", e)
            return []

    # ------------------------------------------------- user data stream
    def create_listen_key(self) -> str | None:
        """Create a user data stream listenKey (valid for 60m)."""
        try:
            res = self._request("POST", "/fapi/v1/listenKey", signed=False, key_only=True)
            return res.get("listenKey") if isinstance(res, dict) else None
        except ExchangeError as e:
            log.warning("create_listen_key failed: %s", e)
            return None

    def keepalive_listen_key(self) -> bool:
        """Ping user data stream listenKey to prevent expiry."""
        try:
            self._request("PUT", "/fapi/v1/listenKey", signed=False, key_only=True)
            return True
        except ExchangeError as e:
            log.warning("keepalive_listen_key failed: %s", e)
            return False

    def close_listen_key(self) -> bool:
        """Close user data stream listenKey."""
        try:
            self._request("DELETE", "/fapi/v1/listenKey", signed=False, key_only=True)
            return True
        except ExchangeError as e:
            log.warning("close_listen_key failed: %s", e)
            return False

