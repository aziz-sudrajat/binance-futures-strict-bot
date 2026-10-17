# Scenario Improvement Tool — Binance Futures Strict Bot
#
# Generates synthetic market-data scenarios (trend / chop / BTC dump /
# high-vol / squeeze / liquidity wall / funding shock) and runs the STRICT
# signal pipeline (signal_engine.evaluate_symbol) against them. Produces a
# machine-readable report (JSONL) for automated scenario analysis and CI.
#
# Usage:
#   python tools/scenario_manager.py --list                      # list defined scenarios
#   python tools/scenario_manager.py --run                     # run all scenarios
#   python tools/scenario_manager.py --scenario trend_bull     # single scenario
#   python tools/scenario_manager.py --report                  # generate report
#
# Environment:
#   BINANCE_FUTURES_BASE_URL=https://testnet.binancefuture.com
#
# Output: tools/scenario_data/scenario_runs.jsonl
#         tools/scenario_data/scenario_report.json
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
sys.path.insert(0, BASE)  # enable imports (config, data, signal_engine)
from data import MarketData, DATA_CACHE_MAX_ENTRIES
from signal_engine import evaluate_symbol
from data import _BoundedTTLCache

OUT_DIR = os.path.join(BASE, "tools", "scenario_data")
os.makedirs(OUT_DIR, exist_ok=True)
RUNS_FILE = os.path.join(OUT_DIR, "scenario_runs.jsonl")
REPORT_FILE = os.path.join(OUT_DIR, "scenario_report.json")


# ----------------------------------------------------------------------
# Scenario definitions
# ----------------------------------------------------------------------
def _rising(step, n, seed=100.0, noise=0.0005):
    """Linearly rising series with small noise."""
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    out = []
    v = seed
    for i in range(n):
        v = max(seed + i * step + rng.uniform(-noise, noise), 1e-8)
        out.append(v)
    return out


def _rising_with_noise(step, n, seed=100.0, noise=0.005):
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    return [seed + i * step + rng.uniform(-noise, noise) for i in range(n)]


def _falling(step, n, seed=100.0, noise=0.0005):
    """Linearly falling series with small noise."""
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    out = []
    v = seed
    for i in range(n):
        v = max(seed - i * step + rng.uniform(-noise, noise), 1e-8)
        out.append(v)
    return out


def _chop(n, seed=100.0, amplitude=0.01):
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    return [seed + amplitude * (2 * rng.random() - 1) for _ in range(n)]


def _btc_dump(n, seed=100.0, pct=0.05, noise=0.003):
    """Rising, then a 5% dump, then rising again."""
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    base = seed + 0.5
    pts = []
    for i in range(n):
        if i < n * 0.6:
            v = base + (i / (n * 0.6)) * 2.0
        elif i < n * 0.7:
            v = base + 2.0 - (i - n * 0.6) / (n * 0.1) * 2.0 * 0.05
        else:
            v = base + 2.0 - 2.0 * 0.05 + (i - n * 0.7) / (n * 0.3) * 0.5
        pts.append(max(v + rng.uniform(-noise, noise), 1e-8))
    return pts


def _high_vol(n, seed=100.0, vol=0.025, noise=0.002):
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    out = []
    v = seed
    for _ in range(n):
        v += rng.uniform(-vol * v, vol * v)
        out.append(max(v + rng.uniform(-noise, noise), 1e-8))
    return out


def _squeeze_long(n, seed=100.0, squeeze=0.02):
    """Range-bound with frequent wicks — squeeze risk."""
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    out = []
    lo, hi = seed - 2.0, seed + 2.0
    for _ in range(n):
        v = rng.uniform(lo, hi)
        # Add wicks
        if rng.random() < 0.4:
            v += rng.choice([-1, 1]) * rng.uniform(0, squeeze * v)
        out.append(max(v, 1e-8))
    return out


def _squeeze_short(n, seed=100.0, squeeze=0.02):
    return _squeeze_long(n, seed, squeeze)  # same data; SHORT signals


def _add_patterns(candles, pattern, seed=100.0):
    """Inject a candlestick pattern into the candles.

    pattern: 'hammer' | 'bullish-engulfing' | 'shooting-star' | None.
    Returns a new list of candles with the pattern embedded in the last
    few candles.
    """
    import random
    rng = random.Random(__import__("hashlib").md5(str(seed).encode()).hexdigest())
    out = [dict(c) for c in candles]
    n = len(out)
    if n < 5:
        return out
    if pattern == "hammer":
        # Last 3 candles: hammer formation at the end
        last = out[-1]
        last["body"] = last["close"] - last["open"]
        last["range"] = last["high"] - last["low"]
        last["lower_wick"] = min(last["open"], last["close"]) - last["low"]
        last["upper_wick"] = last["high"] - max(last["open"], last["close"])
        # hammer: small body, long lower wick, short upper wick
        last["open"] = last["close"] * 0.995
        last["high"] = last["open"] * 1.002
        last["low"] = last["low"] * 0.995
        last["volume"] += 100
    elif pattern == "bullish-engulfing":
        # Last 2 candles: bullish engulfing
        c1 = out[-2]
        c2 = out[-1]
        c1["open"] = c1["close"] * 0.998
        c1["high"] = c1["open"] * 1.002
        c1["low"] = c1["low"] * 0.997
        c2["open"] = c1["close"] * 0.999
        c2["high"] = c2["close"] * 1.003
        c2["low"] = c2["low"] * 0.998
        c2["volume"] += 100
    elif pattern == "shooting-star":
        # Last 1 candle: shooting star (bearish)
        c = out[-1]
        c["open"] = c["close"] * 1.005
        c["high"] = c["open"] * 1.003
        c["low"] = c["low"] * 0.996
        c["volume"] += 50
    return out


# Scenario registry: name -> (builder_fn, direction, description, kwargs)
# builder_fn(n, seed, **kwargs) -> list[float]
SCENARIOS = {
    # trend_bull: smooth uptrend — MTF alignment passes at all TF
    "trend_bull": (_rising, "BULLISH",
                   "Steady uptrend, no dump (baseline).",
                   {"step": 0.01, "seed": 100.0, "noise": 0.0005}),
    # trend_bear: smooth downtrend
    "trend_bear": (_falling, "BEARISH",
                   "Steady downtrend.",
                   {"step": 0.01, "seed": 100.0, "noise": 0.0005}),
    # chop: range-bound with noise — MTF alignment fails at 4h/1h
    "chop": (_chop, "BULLISH",
             "Choppy sideways market — expect few signals (MTF alignment fails).",
             {"seed": 100.0, "amplitude": 0.005}),
    # btc_dump: rally then 5% dump
    "btc_dump": (_btc_dump, "LONG",
                 "BTC dump after rally — breaker should veto.",
                 {"seed": 100.0, "pct": 0.05, "noise": 0.003}),
    # high_vol: large swings — volatility_shock (V20)
    "high_vol": (_high_vol, "BULLISH",
                 "High volatility — expect rejections / volatility_shock (V20).",
                 {"seed": 100.0, "vol": 0.025, "noise": 0.002}),
    # squeeze_long: range-bound with wicks — squeeze warn/trend veto
    "squeeze_long": (_squeeze_long, "BULLISH",
                     "Range-bound with wicks — squeeze warn/trend veto.",
                     {"seed": 100.0, "squeeze": 0.02}),
    # squeeze_short: SHORT on the same pattern
    "squeeze_short": (_squeeze_short, "BEARISH",
                      "Range-bound with wicks — SHORT.",
                      {"seed": 100.0, "squeeze": 0.02}),
    # flat_no_trend: very flat — no 4h trend, trend_strength veto
    "flat_no_trend": (_chop, "BULLISH",
                      "Very flat — no 4h trend, trend_strength veto.",
                      {"seed": 99.0, "amplitude": 0.003}),
}


def build_candles(scenario_name, symbol="BTCUSDT", interval="15m", limit=200):
    """Return a list of kline dicts (open/high/low/close/volume) for a scenario."""
    build, direction, desc, kwargs = SCENARIOS[scenario_name]
    # Builder functions carry their own seed; pass limit as n, override
    # seed with scenario default when needed.
    build_args = {"n": limit}
    build_args.update(kwargs)
    closes = build(**build_args)
    candles = []
    for i in range(limit):
        o = closes[i] * (1 + 0.0005 * (i % 2 == 0 and i > 10))
        h = max(o, closes[i] * 1.001)
        l = min(o, closes[i] * 0.999)
        if i > 0:
            o = closes[i]
        c = closes[i]
        v = 1000 + (i % 10) * 50
        candles.append({"open": o, "high": h, "low": l, "close": c,
                        "volume": v, "timestamp": 1700000000000 + i * 900000})
    # Add candlestick patterns for ketat mode review
    # - trend_bull: bullish-engulfing near the end
    # - btc_dump: hammer at the dump bottom
    # - high_vol: hammer at local bottoms
    # - chop: none (choppy, no clear patterns)
    pattern = None
    if scenario_name == "trend_bull":
        pattern = "bullish-engulfing"
    elif scenario_name == "btc_dump":
        pattern = "hammer"
    elif scenario_name == "high_vol":
        pattern = "hammer"
    elif scenario_name == "flat_no_trend":
        pattern = None
    candles = _add_patterns(candles, pattern, seed=SCENARIOS[scenario_name][2])
    return candles


def make_market_data(candles, live_price=None):
    """Create a MarketData with preloaded klines for a given symbol.

    Returns a MarketData subclass whose get_live_price() returns the
    cached price — OKX/Binance ticker calls fail in offline CI so we
    serve the scenario price from the cache instead.
    """
    m = MarketData()
    symbol = "BTCUSDT"
    # Preload into the klines cache (bounded TTL). Key shape must match
    # get_klines() exactly: symbol_interval_220 (default limit).
    m._klines_cache = _BoundedTTLCache(DATA_CACHE_MAX_ENTRIES)
    for interval in ("15m", "30m", "1h", "4h", "1d"):
        m._klines_cache.put(f"{symbol}_{interval}_220", candles)
    if live_price is None:
        live_price = candles[-1]["close"] if candles else 100.0
    m.live_price_cache = {symbol: live_price}
    return _ScenarioMarket(m, symbol, live_price)


class _ScenarioMarket(MarketData):
    """MarketData subclass for scenario runs: get_live_price() serves the
    cached scenario price — OKX/Binance ticker calls fail in offline CI."""

    def __init__(self, base: MarketData, symbol: str, price: float):
        self._base = base
        self._live_price = price
        self.live_price_cache = {symbol: price}

    def get_live_price(self, symbol: str) -> float | None:
        return self._live_price

    # Delegate klines to the base MarketData (which now has the cached
    # klines preloaded in _klines_cache).
    def get_klines(self, symbol, interval, limit=None):
        return self._base.get_klines(symbol, interval, limit)

    def get_derivatives_snapshot(self, symbol):
        return self._base.get_derivatives_snapshot(symbol)

    def get_order_book(self, symbol):
        return self._base.get_order_book(symbol)

    def get_advanced_derivatives(self, symbol):
        return self._base.get_advanced_derivatives(symbol)

    def get_24h_quote_volume_usd(self, symbol):
        return self._base.get_24h_quote_volume_usd(symbol)

    def news_feed_ok(self):
        return self._base.news_feed_ok()

    def get_coin_news(self, symbol, hours=24):
        return self._base.get_coin_news(symbol, hours=hours)


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
def run_scenario(scenario_name, direction, market, limit=200):
    candles = build_candles(scenario_name, limit=limit)
    m2 = make_market_data(candles, live_price=candles[-1]["close"])
    signal, note = evaluate_symbol("BTCUSDT", m2)
    return {
        "scenario": scenario_name,
        "direction": direction,
        "signal": signal,
        "note": note,
        "candle_count": len(candles),
        "live_price": m2.live_price_cache.get("BTCUSDT"),
    }


def run_all(scenarios=None, limit=200):
    results = []
    scenarios = scenarios or list(SCENARIOS.keys())
    m = MarketData()
    for name in scenarios:
        direction, _ = SCENARIOS[name][1], SCENARIOS[name][2]
        r = run_scenario(name, direction, m, limit=limit)
        results.append(r)
        # Periodic flush (avoid losing all results on crash)
        if len(results) % 10 == 0:
            with open(RUNS_FILE, "w") as f:
                for r in results:
                    f.write(json.dumps(r) + "\n")
    with open(RUNS_FILE, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    return results


def build_report(results):
    """Generate a summary report JSON."""
    report = {"generated": int(time.time()), "scenarios": len(results),
              "results": results}
    # Aggregate stats
    blocks = {"passed": 0, "rejected": 0, "note": ""}
    for r in results:
        if r["signal"]:
            blocks["passed"] += 1
        else:
            blocks["rejected"] += 1
    report["summary"] = blocks
    # Map scenarios -> pass/fail
    report["scenario_map"] = {r["scenario"]: r["signal"] is not None
                              for r in results}
    # List failed scenarios with reasons
    report["failures"] = [{
        "scenario": r["scenario"],
        "reason": r["note"],
        "direction": r["direction"],
        "signal": r["signal"],
    } for r in results if not r["signal"]]
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Scenario improvement tool")
    ap.add_argument("--list", action="store_true", help="list scenarios")
    ap.add_argument("--run", action="store_true", help="run all scenarios")
    ap.add_argument("--scenario", type=str, help="single scenario name")
    ap.add_argument("--report", action="store_true", help="generate report")
    ap.add_argument("--limit", type=int, default=200,
                    help="kline length per scenario")
    args = ap.parse_args(argv)

    if args.list:
        print("Available scenarios:")
        for name, (build, direction, desc, kwargs) in SCENARIOS.items():
            print(f"  {name:20s} {direction:8s} {desc}")
        return 0

    if args.run:
        results = run_all(limit=args.limit)
        report = build_report(results)
        with open(REPORT_FILE, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Ran {len(results)} scenarios.")
        print(f"Report written to {REPORT_FILE}")
        print(f"Results written to {RUNS_FILE}")
        print(f"Passed: {report['summary']['passed']}, "
              f"Rejected: {report['summary']['rejected']}")
        return 0

    if args.scenario:
        s = args.scenario
        if s not in SCENARIOS:
            print(f"Unknown scenario '{s}'. Available:")
            for n in SCENARIOS:
                print(f"  {n}")
            return 1
        direction = SCENARIOS[s][1]
        r = run_scenario(s, direction, MarketData(), limit=args.limit)
        print(json.dumps(r, indent=2))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
