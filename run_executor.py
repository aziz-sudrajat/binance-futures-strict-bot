"""CLI entry point for the Phase 2 executor.

Modes:
  --paper    paper-trade with simulated fills (DEFAULT; no keys, no orders)
  --testnet  real orders on the Binance Futures TESTNET (needs env keys)

  --once     run a single scan cycle then exit
  --loop     run forever (default when neither --once nor --loop is given,
             behaves like --loop)

Testnet keys come ONLY from the environment:
  BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET
Base URL override (for a future non-geo-blocked live host):
  BINANCE_FUTURES_BASE_URL (default: the testnet URL in config)

--testnet without keys REFUSES to run. There is no mainnet fallback.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import config
from data import MarketData
from exchange import ExchangeClient
from executor import Executor
from lifecycle import LifecycleLogger
from risk import PaperBroker, RiskManager
from state import StateStore

log = logging.getLogger("executor")


def require_testnet_keys(env: dict | None = None) -> tuple[str, str]:
    """Returns (key, secret) or raises SystemExit with a clear message.

    Factored out for testability. NEVER falls back to mainnet.
    """
    env = os.environ if env is None else env
    key = (env.get("BINANCE_TESTNET_KEY") or "").strip()
    secret = (env.get("BINANCE_TESTNET_SECRET") or "").strip()
    if not key or not secret:
        raise SystemExit(
            "ERROR: --testnet needs BINANCE_TESTNET_KEY and "
            "BINANCE_TESTNET_SECRET in the environment.\n"
            "Create a key at https://testnet.binancefuture.com "
            "(Generate HMAC_SHA256 Key), then e.g.:\n"
            "  export BINANCE_TESTNET_KEY=... BINANCE_TESTNET_SECRET=...\n"
            "Refusing to run: there is no mainnet fallback.")
    return key, secret


def build(args, env: dict | None = None):
    """Assemble market/exchange/risk/state/executor. Returns (executor, mode)."""
    env = os.environ if env is None else env
    market = MarketData()
    state = StateStore(config.EXEC_STATE_FILE)
    lifecycle = LifecycleLogger(config.EXEC_LIFECYCLE_FILE)
    base_url = (env.get("BINANCE_FUTURES_BASE_URL")
                or config.BINANCE_FUTURES_BASE_URL)

    if args.testnet and args.paper:
        raise SystemExit("ERROR: --paper and --testnet are mutually exclusive.")
    if args.testnet:
        key, secret = require_testnet_keys(env)
        exchange = ExchangeClient(key, secret, base_url=base_url)
        mode = f"TESTNET ({base_url})"
    else:
        broker = PaperBroker(config.EXEC_PAPER_START_BALANCE,
                             price_fn=market.get_live_price)
        exchange = broker
        mode = "PAPER"
    risk = RiskManager(state, exchange.get_balance)
    executor = Executor(market, exchange, risk, state, lifecycle,
                        poll_interval_min=args.interval)
    return executor, mode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Binance Futures executor")
    ap.add_argument("--paper", action="store_true", default=True,
                    help="paper mode (default)")
    ap.add_argument("--testnet", action="store_true",
                    help="trade the Binance Futures testnet (needs env keys)")
    ap.add_argument("--once", action="store_true",
                    help="single scan cycle then exit")
    ap.add_argument("--loop", action="store_true",
                    help="run scan cycles forever")
    ap.add_argument("--interval", type=int,
                    default=config.EXEC_POLL_INTERVAL_MIN,
                    help="minutes between scans")
    args = ap.parse_args(argv)
    if args.testnet:
        args.paper = False

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    executor, mode = build(args)
    log.warning("starting executor in %s mode", mode)
    if args.once:
        summary = executor.scan_once()
        log.info("once summary: %s", summary)
    else:
        executor.run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
