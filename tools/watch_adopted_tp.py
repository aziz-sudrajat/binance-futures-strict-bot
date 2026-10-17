#!/usr/bin/env python3
"""Watcher: TP adopted (unmanaged) live positions at +20% of margin.

The live executor skips unmanaged positions entirely ("manual review only"),
so the built-in +20% profit-take never fires for them. This watcher polls
those positions and market-closes the whole position once unrealized PnL
reaches +20% of the isolated margin (user 2026-10-16: "TP kalo sudah
profit 20%").

- Read-only until the trigger fires. NEVER touches bot-managed positions.
- On trigger: cancel all working orders for the symbol (TP limits + algo SL),
  market-close the full qty reduce-only, verify flat on exchange, mark the
  state entry closed, log a terminal profit_take lifecycle event.
- If the exchange shows flat (SL/TP filled first): mark the stale state
  entry closed so the per-coin cap doesn't block future entries.
- Prints one line per position: WATCHING ... / TP_EXECUTED ... / FLAT ...
- Exit 0 always. Never prints secrets.
"""
import json
import os
import sys
import time

BASE = os.environ.get("BOT_BASE_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(BASE, "state-live.json")
LIFECYCLE_FILE = os.environ.get("LIFECYCLE_FILE") or os.path.join(BASE, "lifecycle-live.jsonl")
ENV_FILE = os.environ.get("ENV_FILE") or os.path.join(os.path.expanduser("~"), ".binance-live.env")
WATCH_LOG = os.environ.get("WATCH_LOG") or os.path.join(BASE, "adopted-tp-watch.log")
PROFIT_TAKE_PCT = float(os.environ.get("PROFIT_TAKE_PCT", 20.0))  # mirrors live profit_take_pct

sys.path.insert(0, BASE)
from exchange import ExchangeClient  # noqa: E402
from lifecycle import LifecycleLogger, PROFIT_TAKE  # noqa: E402


def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return env


def wlog(obj):
    obj["ts"] = int(time.time() * 1000)
    try:
        with open(WATCH_LOG, "a") as f:
            f.write(json.dumps(obj) + "\n")
    except OSError:
        pass


def main():
    env = load_env(ENV_FILE)
    key, secret = env.get("BINANCE_LIVE_KEY", ""), env.get("BINANCE_LIVE_SECRET", "")
    if not key or not secret:
        print("WATCHER_ERROR missing creds")
        return 0
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        print("WATCHER_ERROR cannot read state-live.json")
        return 0
    unmanaged = {pid: p for pid, p in state.get("positions", {}).items()
                 if p.get("unmanaged") and not p.get("closed")}
    if not unmanaged:
        print("WATCHING none (no unmanaged positions)")
        return 0

    client = ExchangeClient(key, secret, base_url="https://fapi.binance.com")
    try:
        risk = client._request("GET", "/fapi/v2/positionRisk", {})
    except Exception as e:  # noqa: BLE001 - fail closed, never act blind
        print(f"WATCHER_ERROR positionRisk failed: {type(e).__name__}")
        return 0
    live = {p.get("symbol"): p for p in risk}

    lc = LifecycleLogger(LIFECYCLE_FILE)
    dirty = False
    for pid, pos in unmanaged.items():
        symbol = pos.get("symbol")
        lp = live.get(symbol, {})
        try:
            amt = float(lp.get("positionAmt") or 0)
        except (TypeError, ValueError):
            print(f"WATCHER_ERROR {symbol}: bad positionAmt")
            continue
        if amt == 0:
            # Gone from exchange (SL/TP filled or manual close): release the
            # stale state entry so the per-coin cap doesn't block re-entry.
            pos["closed"] = True
            pos["closed_reason"] = "exchange_flat"
            pos["closed_at"] = int(time.time() * 1000)
            dirty = True
            wlog({"event": "FLAT", "pos_id": pid, "symbol": symbol})
            print(f"FLAT {symbol}: already closed on exchange, state released")
            continue
        try:
            unreal = float(lp.get("unRealizedProfit") or 0)
            margin = float(lp.get("isolatedMargin") or 0) or 1.0
        except (TypeError, ValueError):
            print(f"WATCHER_ERROR {symbol}: bad numbers")
            continue
        target = margin * PROFIT_TAKE_PCT / 100.0
        if unreal < target:
            print(f"WATCHING {symbol}: unreal={unreal:+.4f} target=+{target:.4f} "
                  f"(margin {margin})")
            continue
        # ---- TRIGGER: TP at +20% ----
        direction = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)
        entry = pos.get("entry_price") or pos.get("entryPrice")
        # 1. Cancel working orders (TP limits).
        for o in client.get_open_orders(symbol):
            try:
                client.cancel_order(symbol, order_id=o.get("orderId"))
            except Exception:  # noqa: BLE001
                pass
        # 2. Cancel algo orders (SL).
        for o in client.get_open_algo_orders(symbol):
            try:
                client.cancel_algo_order(symbol, int(o.get("algoId")))
            except Exception:  # noqa: BLE001
                pass
        # 3. Market-close full qty, reduce-only (cannot open a new position).
        side = "SELL" if direction == "LONG" else "BUY"
        fill = client.place_order(symbol, side, "MARKET", quantity=qty,
                                 reduce_only=True)
        if not fill:
            print(f"WATCHER_ERROR {symbol}: market close failed, retry next run")
            wlog({"event": "TP_CLOSE_FAILED", "pos_id": pid, "symbol": symbol})
            continue
        # 4. Verify flat.
        try:
            risk2 = client._request("GET", "/fapi/v2/positionRisk", {})
            amt2 = float(next((p.get("positionAmt") or 0)
                              for p in risk2 if p.get("symbol") == symbol))
        except Exception:  # noqa: BLE001
            amt2 = None
        fill_px = fill.get("avgPrice") or fill.get("price")
        pos["closed"] = True
        pos["closed_reason"] = "profit_take_20pct"
        pos["closed_at"] = int(time.time() * 1000)
        pos["close_price"] = float(fill_px or 0)
        dirty = True
        lc.log(pos.get("signal_id", pid), symbol, PROFIT_TAKE,
               reason=f"watcher profit_take +{PROFIT_TAKE_PCT:g}% of margin",
               price=float(fill_px or 0), qty=qty,
               pnl=round(unreal, 4), terminal=True)
        wlog({"event": "TP_EXECUTED", "pos_id": pid, "symbol": symbol,
              "direction": direction, "qty": qty, "fill_price": fill_px,
              "unreal_at_trigger": round(unreal, 4),
              "verified_flat": (amt2 == 0)})
        print(f"TP_EXECUTED {symbol} {direction} qty={qty} fill={fill_px} "
              f"unreal_was={unreal:+.4f} flat={amt2 == 0}")
    if dirty:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except OSError:
            print("WATCHER_ERROR cannot write state-live.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
