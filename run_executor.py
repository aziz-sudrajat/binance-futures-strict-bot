"""CLI entry point for the Phase 2 executor.

Modes:
  --paper    paper-trade with simulated fills (DEFAULT; no keys, no orders)
  --testnet  real orders on the Binance Futures TESTNET (needs env keys)
  --live     REAL orders on Binance Futures MAINNET (needs env keys; REAL MONEY)

  --once     run a single scan cycle then exit
  --loop     run forever (default when neither --once nor --loop is given,
             behaves like --loop)

Testnet keys come ONLY from the environment:
  BINANCE_TESTNET_KEY, BINANCE_TESTNET_SECRET
Live (mainnet) keys come ONLY from the environment:
  BINANCE_LIVE_KEY, BINANCE_LIVE_SECRET
Base URL override (for a non-geo-blocked host):
  BINANCE_FUTURES_BASE_URL (default: testnet URL for --testnet,
  mainnet URL for --live)

--testnet/--live without keys REFUSE to run. --live REFUSES all TEST ONLY
flags (--fixed-margin-usdt, --leverage, --leverage-tiers, --leverage-default,
--manual): live always uses the production risk defaults
(RISK_PCT_PER_TRADE, MAX_LEVERAGE) and real STRICT signals only.
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


def require_live_keys(env: dict | None = None) -> tuple[str, str]:
    """Returns (key, secret) for MAINNET or raises SystemExit.

    Factored out for testability. NEVER falls back to testnet/paper keys.
    """
    env = os.environ if env is None else env
    key = (env.get("BINANCE_LIVE_KEY") or "").strip()
    secret = (env.get("BINANCE_LIVE_SECRET") or "").strip()
    if not key or not secret:
        raise SystemExit(
            "ERROR: --live needs BINANCE_LIVE_KEY and "
            "BINANCE_LIVE_SECRET in the environment.\n"
            "Create a mainnet key at https://www.binance.com (API Management: "
            "enable Futures, DISABLE withdrawals, restrict IP if possible), "
            "then e.g.:\n"
            "  export BINANCE_LIVE_KEY=... BINANCE_LIVE_SECRET=...\n"
            "Refusing to run: live mode never falls back to testnet or paper.")
    return key, secret


def _refuse_live_test_params(args) -> None:
    """Hard refusal: TEST ONLY knobs must never reach real-money trading."""
    offenders = []
    if args.fixed_margin_usdt is not None:
        offenders.append("--fixed-margin-usdt")
    if args.leverage is not None:
        offenders.append("--leverage")
    if args.leverage_tiers:
        offenders.append("--leverage-tiers")
    if args.leverage_default is not None:
        offenders.append("--leverage-default")
    if args.manual:
        offenders.append("--manual")
    if offenders:
        raise SystemExit(
            "ERROR: --live refuses TEST ONLY flags: "
            + ", ".join(offenders) + ".\n"
            "Live trading always uses the production risk defaults "
            "(RISK_PCT_PER_TRADE, MAX_LEVERAGE) and real STRICT signals only. "
            "For full order control (fixed leverage/entry, margin mode) use "
            "the dashboard Order settings instead.")


def build(args, env: dict | None = None):
    """Assemble market/exchange/risk/state/executor. Returns (executor, mode)."""
    env = os.environ if env is None else env
    market = MarketData()

    modes = [m for m, on in (("paper", args.paper), ("testnet", args.testnet),
                             ("live", args.live)) if on]
    if len(modes) != 1:
        raise SystemExit(
            "ERROR: exactly one of --paper / --testnet / --live is required "
            f"(got: {modes or ['none']}).")
    is_live = args.live
    if is_live:
        _refuse_live_test_params(args)

    # Live mode keeps its own state files so testnet/paper records can never
    # contaminate (or be mistaken for) real-money position history.
    state = StateStore(config.EXEC_LIVE_STATE_FILE if is_live
                       else config.EXEC_STATE_FILE)
    lifecycle = LifecycleLogger(config.EXEC_LIVE_LIFECYCLE_FILE if is_live
                                else config.EXEC_LIFECYCLE_FILE)
    default_base = (config.BINANCE_FUTURES_MAINNET_URL if is_live
                    else config.BINANCE_FUTURES_BASE_URL)
    base_url = env.get("BINANCE_FUTURES_BASE_URL") or default_base

    if args.testnet:
        key, secret = require_testnet_keys(env)
        exchange = ExchangeClient(key, secret, base_url=base_url)
        mode = f"TESTNET ({base_url})"
    elif is_live:
        key, secret = require_live_keys(env)
        exchange = ExchangeClient(key, secret, base_url=base_url)
        mode = f"LIVE ({base_url})"
        log.warning("LIVE MODE: real orders on Binance Futures mainnet. "
                    "Risk defaults: %.1f%% per trade, max %dx leverage.",
                    config.RISK_PCT_PER_TRADE, config.MAX_LEVERAGE)
    else:
        broker = PaperBroker(config.EXEC_PAPER_START_BALANCE,
                             price_fn=market.get_live_price)
        exchange = broker
        mode = "PAPER"
    risk = RiskManager(state, exchange.get_balance)
    tiers = _parse_leverage_tiers(args.leverage_tiers)
    settings_mode = "live" if is_live else ("testnet" if args.testnet
                                            else "paper")
    executor = Executor(market, exchange, risk, state, lifecycle,
                        poll_interval_min=args.interval,
                        fixed_margin_usdt=args.fixed_margin_usdt,
                        leverage_override=args.leverage,
                        leverage_tiers=tiers,
                        leverage_default=args.leverage_default,
                        settings_mode=settings_mode)
    return executor, mode


def _parse_leverage_tiers(spec: str | None) -> dict | None:
    """Parse 'BTCUSDT=20,ETHUSDT=20' into a dict. None -> config default."""
    if not spec:
        return None
    tiers = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            sym, lev = part.split("=")
            lev_i = int(lev)
        except ValueError:
            raise SystemExit(f"ERROR: bad --leverage-tiers entry {part!r} "
                             "(expected SYMBOL=LEV, e.g. BTCUSDT=20).")
        if not 1 <= lev_i <= 125:
            raise SystemExit(f"ERROR: leverage for {sym} must be 1-125.")
        tiers[sym.strip().upper()] = lev_i
    return tiers or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Binance Futures executor")
    ap.add_argument("--paper", action="store_true", default=True,
                    help="paper mode (default)")
    ap.add_argument("--testnet", action="store_true",
                    help="trade the Binance Futures testnet (needs env keys)")
    ap.add_argument("--live", action="store_true",
                    help="trade REAL Binance Futures mainnet (needs env keys; "
                         "REAL MONEY; refuses all TEST ONLY flags)")
    ap.add_argument("--once", action="store_true",
                    help="single scan cycle then exit")
    ap.add_argument("--loop", action="store_true",
                    help="run scan cycles forever")
    ap.add_argument("--interval", type=int,
                    default=config.EXEC_POLL_INTERVAL_MIN,
                    help="minutes between scans")
    ap.add_argument("--fixed-margin-usdt", type=float, default=None,
                    help="TEST ONLY: fixed margin USDT per position instead of "
                         "risk-based sizing, e.g. 10. Overrides RISK_PCT_PER_TRADE. "
                         "Risk gates (kill switch, caps) still apply.")
    ap.add_argument("--leverage", type=int, default=None,
                    help="TEST ONLY: explicit leverage for ALL symbols, e.g. 10. "
                         "Overrides MAX_LEVERAGE and the per-symbol tiers.")
    ap.add_argument("--leverage-tiers", type=str, default=None,
                    help="TEST ONLY: per-symbol leverage tiers, e.g. "
                         "'BTCUSDT=20,ETHUSDT=20'. Applies in fixed-margin test "
                         "mode; default comes from config.TEST_LEVERAGE_TIERS.")
    ap.add_argument("--leverage-default", type=int, default=None,
                    help="TEST ONLY: leverage for symbols not in --leverage-tiers "
                         "(default: config.TEST_LEVERAGE_DEFAULT).")
    ap.add_argument("--manual", type=str, default=None, metavar="SYMBOL",
                    help="TEST ONLY: manual market LONG entry on SYMBOL at the live "
                         "price (bypasses ALL STRICT signal gates; ATR-based SL + "
                         "RR-based TP brackets still apply). E.g. --manual GRASSUSDT.")
    ap.add_argument("--balance", action="store_true",
                    help="print the futures USDT balance and exit "
                         "(needs --live or --testnet keys)")
    args = ap.parse_args(argv)
    # BOT_MODE env (set by `source switch.sh live|demo`) selects the mode
    # when no explicit flag is given.
    if not args.live and not args.testnet:
        m = (os.environ.get("BOT_MODE") or "").strip().lower()
        if m == "live":
            args.live = True
        elif m in ("demo", "testnet"):
            args.testnet = True
    if args.testnet or args.live:
        args.paper = False
    if args.leverage is not None and not 1 <= args.leverage <= 125:
        raise SystemExit("ERROR: --leverage must be between 1 and 125.")
    if args.leverage_default is not None and not 1 <= args.leverage_default <= 125:
        raise SystemExit("ERROR: --leverage-default must be between 1 and 125.")
    if args.fixed_margin_usdt is not None and args.fixed_margin_usdt <= 0:
        raise SystemExit("ERROR: --fixed-margin-usdt must be > 0.")
    if args.fixed_margin_usdt is not None or args.leverage is not None:
        log.warning("TEST PARAMS: fixed_margin_usdt=%s leverage=%s - "
                    "deviates from the 1%%/3x defaults; testnet only",
                    args.fixed_margin_usdt, args.leverage)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    executor, mode = build(args)
    log.warning("starting executor in %s mode", mode)
    if args.balance:
        bal = executor.exchange.get_balance()
        if bal is None:
            print("balance: unavailable (exchange error)", flush=True)
            return 1
        print(f"futures balance: {bal:.2f} USDT ({mode})", flush=True)
        return 0
    if args.manual:
        if not args.testnet:
            raise SystemExit("ERROR: --manual needs --testnet (TEST ONLY; "
                             "refused in paper and live modes).")
        ok, note = executor.manual_entry(args.manual)
        log.warning("manual entry %s: %s", "OK" if ok else "REJECTED", note)
        print(note, flush=True)
        return 0 if ok else 1
    if args.once:
        summary = executor.scan_once()
        log.info("once summary: %s", summary)
    else:
        executor.run_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
