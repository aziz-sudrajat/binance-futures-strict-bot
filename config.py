# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""STRICT-mode configuration for the Binance Futures signal engine.

Constants mirror the GAS bot (workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs,
CFG block lines 13-160) so the Python port makes identical decisions.
Risk defaults below are user-correctable and feed Phase 2 (executor).
"""

import os

# ------------------------------------------------------------------ logging
# LOG_JSON=1 / true / yes -> log one JSON object per line (logutil.py)
# instead of the human-readable default. Readable stays the default.
LOG_JSON = os.environ.get("BOT_LOG_JSON", "").strip().lower() in (
    "1", "true", "yes")

# ---------------------------------------------------------------- STRICT gate
MIN_QUALITY_SCORE = 70          # GAS: getMinQuality_() -> 70 when SIGNAL_FILTER_MODE='STRICT' (L3137-3141)
MTF_TIMEFRAMES = ("15m", "30m", "4h", "1d")
# STRICT cascade (GAS L3156-3170): 4h bullish AND 1d bullish AND 30m BULLISH WATCH.
MTF_CONFIRM_INTERVAL = "30m"
MTF_30M_MODE = "STRICT"        # 30m leg mode (see note below).
# NOTE (audit D5): STRICT requires a full 30m BULLISH WATCH (GAS mtfConfirmPass_
# STRICT branch, L2097-2101). This is DELIBERATELY stricter than the GAS default:
# GAS CFG sets MTF_CONFIRM_MODE='LEAN' (L86), so the GAS cascade's 30m leg passes
# neutral-with-bullish-lean setups. We keep STRICT for the trading mandate;
# set MTF_30M_MODE='LEAN' to reproduce the GAS default behavior instead.
MTF_CASCADE_BOOST = 8           # GAS: mtfCascadeCheck_ returns boost: 8 (L3156)
MTF_CONFIRM_BOOST = 8           # GAS: mtfConfirmPass_ STRICT returns boost: 8 (L2097)
MTF_LEAN_BOOST = 3             # GAS: mtfConfirmPass_ LEAN returns boost: 3 on lean (L2107)
MTF_REQUIRE_1D = True          # STRICT cascade: 1d trend alignment is a hard gate.
                               # Relaxed modes (biasa) turn the 1d leg into a soft
                               # quality delta (bonus when aligned, penalty when
                               # opposed) instead of a hard reject.
MTF_CONFIRM_1D_BOOST = 6       # soft 1d delta: major trend aligned (relaxed cascade)
MTF_CONFIRM_1D_PENALTY = 10    # soft 1d delta: major trend opposed (relaxed cascade)
MTF_REQUIRE_1H = True         # sedang/ketat: 1h trend must align with signal
                              # direction (user spec 2026-10-15).
BTC_ALIGN_REQUIRED = True     # sedang/ketat: BTC 15m direction must match the
                              # signal direction (user spec 2026-10-15).
PATTERN_REQUIRED = True       # ketat: candlestick pattern must support the
                              # direction (user spec 2026-10-15).
NEWS_CHECK = True             # ketat: coin news sentiment review.
LIQ_REVIEW = True             # ketat: liquidation-spike review.
WHALE_REVIEW = True           # ketat: whale/top-trader positioning review.
SIGNAL_MODE = "ketat"         # ketat | sedang | biasa — set from dashboard
                              # settings via apply_dashboard_overrides.

# ------------------------------------------------- indicator / setup constants
KLINES_LIMIT = 220              # GAS: CFG.KLINES_LIMIT (L97)
MIN_CANDLES = 80                # GAS: analyzeCandles_ requires >= 80 (L1905)
# Item 3.1: multi-exchange klines fallback. Default OFF = historical
# fail-closed behavior (OKX down -> RuntimeError, never another venue's
# tape). Opt-in only: when True and OKX fails, get_klines falls back to
# Gate.io USDT futures klines (candles tagged "source": "gateio").
KLINES_FALLBACK_ENABLED = False
SIGNAL_MIN_SCORE = 5            # GAS: CFG.SIGNAL_MIN_SCORE (L98)
MIN_STRENGTH = 3                # GAS: AUTO_MIN_STRENGTH, minS = max(3, ...) (L98, L7535)
BULLISH_ONLY = True             # GAS: CFG.BULLISH_ONLY (L84) -> STRICT is effectively LONG-only
ATR_PERIOD = 14                 # GAS: CFG.ATR_PERIOD (L120)
SWING_LOOKBACK = 12             # GAS: CFG.SWING_LOOKBACK (L121)
ENTRY_MAX_PULLBACK_ATR = 0.50   # GAS: CFG.ENTRY_MAX_PULLBACK_ATR (L122)
ENTRY_ZONE_ATR = 0.10           # GAS: CFG.ENTRY_ZONE_ATR (L123)
STOP_ATR_MIN = 1.25             # GAS: CFG.STOP_ATR_MIN (L124)
STOP_ATR_MAX = 2.00             # GAS: CFG.STOP_ATR_MAX (L125)
TP1_RR = 1.50                   # GAS: CFG.TP1_RR (L126)
TP2_RR = 2.25                   # GAS: CFG.TP2_RR (L127)
TP3_RR = 3.00                   # GAS: CFG.TP3_RR (L128)
FRESHNESS_MAX_DRIFT_PCT = 0.4   # GAS: CFG.FRESHNESS_MAX_DRIFT_PCT (L103)
MIN_24H_VOLUME_USD = 2_000_000  # GAS: CFG.MIN_24H_VOLUME_USD (L100)

# Quality-score blend (ROADMAP ITEM 3): every scoreDelta contributor is an
# explicit, named component in quality_deltas.py, ported from the GAS deep
# check (prePublishDeepCheck_, L3172-3262). No synthetic blends.
SCORE_DELTA_CLAMP = 40          # GAS clamps deep-check scoreDelta to [-40, +40] (L3250)
QUALITY_MIN = 5                 # GAS: deepCheckQualityScore_ clamps to [5, 98] (L3532-3535)
QUALITY_MAX = 98

# ------------------------------------------------- warn-tier delta constants
# Each mirrors a GAS deep-check delta (veto tiers stay in vetoes.py).
H1_TREND_BONUS = 8              # GAS CFG L101: deepCheckH1Trend_ (L3605) bullish-aligned 1h
H1_TREND_PENALTY = 10           # GAS CFG L102: bearish-opposed 1h
BTC_DUMP_WARN_PCT = 2.0         # GAS CFG L105: BTC <= -2%/1h -> quality penalty (LONG)
BTC_DUMP_PENALTY = 15           # GAS CFG L107
LIQ_WALL_WARN_DISTANCE_PCT = 1.0  # GAS CFG L91: wall penalty zone (veto only < 0.5%)
MTF_CONFIRM_4H_BOOST = 6        # GAS CFG L112: mtfConfirm4hPass_ (L2074) 4h bullish
MTF_CONFIRM_4H_PENALTY = 10     # GAS CFG L113: 4h bearish
DERIV_LIQ_SPIKE_THRESHOLD = 3   # GAS default (L1067): liq spike >= 3x -> LIQUIDATION EVENT

# --------------------------------------- test-mode leverage tiers
# User-authorized 2026-10-14 (TESTNET only): leverage per coin tier when
# fixed-margin test mode is active. Trending majors get 20x, everything
# else 10x. A single --leverage CLI value overrides all tiers.
TEST_LEVERAGE_TIERS = {
    "BTCUSDT": 20,
    "ETHUSDT": 20,
    "BNBUSDT": 20,
}
TEST_LEVERAGE_DEFAULT = 10

# --------------------------------------- derivatives coverage (fail-closed)
# Audit D4/H6: a snapshot with zero positioning visibility must HOLD the
# signal, never auto-neutralize. Required components block the signal when
# missing; optional ones are recorded on the signal dict but do not block.
DERIV_COMPONENT_KEYS = {       # logical name -> get_derivatives_snapshot() key
    "funding": "aggregate_funding",
    "open_interest": "aggregate_oi_change",
    "long_short": "aggregate_long",
    "taker_volume": "taker_buy_sell_ratio",
}
DERIV_REQUIRED_COMPONENTS = ("funding", "open_interest")
DERIV_OPTIONAL_COMPONENTS = ("long_short", "taker_volume")

# -------------------------------- deterministic vetoes (ROADMAP ITEM 2)
# Ports of the GAS deep-check hard vetoes (deepCheckDerivatives_ L3336-3393,
# deepCheckLiquidity_ L3395-3425, deepCheckBtcBreadth_ L3634-3662).
# Any fired veto — or any veto whose required inputs are unavailable —
# REJECTS the signal; vetoes are never compensated by the quality score.
DERIV_LEVERAGE_RISK_VETO = 90       # GAS CFG L71: leverageRisk >= 90 -> veto
DERIV_FUNDING_EXTREME = 0.001       # GAS CFG L72: |funding| >= 0.1% against direction -> veto
DERIV_SQUEEZE_RISK_VETO = 85        # GAS CFG L73: squeeze risk >= 85 -> veto
DERIV_TOP_TRADER_VETO_LONG = 35     # GAS CFG L74: whale-divergence *penalty* tier (not ported as veto;
                                    # penalties belong to roadmap item 3, quality recalibration)
DERIV_TOP_TRADER_EXTREME_LONG = 25  # GAS CFG L90: top-trader hard-veto tier
LIQ_WALL_VETO_DISTANCE_PCT = 0.5    # GAS CFG L75: wall within 0.5% of mid blocks direction
LIQ_WALL_MIN_NOTIONAL_USD = 250_000 # GAS CFG L76: wall notional threshold
BTC_DUMP_VETO_PCT = 3.0             # GAS CFG L106: BTC -3%/1h -> veto ALL new signals (LONG-only system)
BTC_BREAKER_TTL_SEC = 300           # own cache for the market-wide breaker check
# S/R location veto (user 2026-10-15): never SHORT into 4h support, never
# LONG into 4h resistance. Support/resistance = 4h swing extremes over the
# lookback (~2-3 months of 4h candles). The "zone" is SR_ZONE_ATR_MULT x
# ATR(4h) inside the level — volatility-adaptive per coin. Entries sitting
# inside the zone edge are vetoed; a breakdown already below support (or
# breakout above resistance) is allowed.
SR_ZONE_LOOKBACK_4H = 500
SR_ZONE_ATR_MULT = 2.5
ADV_DERIV_CACHE_SEC = 120           # Gate contract-stats cache (GAS DERIVATIVES_CACHE_SECONDS L50)
ADV_DERIV_STATS_LIMIT = 48          # 48 x 15m buckets ~ 12h: 16-bucket window + history (GAS L1002)
ORDER_BOOK_DEPTH_LIMIT = 100        # depth levels fetched for the wall veto

# ------------------------------------------------------------- risk defaults
# User-correctable. Consumed by the Phase 2 executor; Phase 1 is paper-only.
RISK_PCT_PER_TRADE = 1.0       # % of futures balance risked per trade
MAX_LEVERAGE = 3               # max leverage multiplier
TP_SPLIT = (0.4, 0.3, 0.3)     # scale-out fractions at TP1 / TP2 / TP3
MAX_CONCURRENT_POSITIONS = 3
DAILY_LOSS_CAP_PCT = 3.0       # kill switch: stop trading if daily loss exceeds this
PROFIT_TAKE_PCT = 0.0          # auto TP: market-close the whole position once
                               # unrealized PnL reaches this % of margin.
                               # 0 = off. User 2026-10-16: "+20% langsung TP"
                               # (live). Dashboard knob: profit_take_pct.
TRAIL_TRIGGER_PCT = 0.0        # trailing stop: activate once unrealized PnL
                               # reaches this % of margin. 0 = off.
                               # Dashboard knob: trail_trigger_pct.
TRAIL_DISTANCE_PCT = 0.0       # trailing stop: market-close when price pulls
                               # back this % from the trailed peak. 0 = off.
                               # Dashboard knob: trail_distance_pct.
TIME_STOP_HOURS = 0.0          # time stop: market-close a position open longer
                               # than this many hours. 0 = off.
                               # Dashboard knob: time_stop_hours.
MAX_DRAWDOWN_CAP_PCT = 10.0    # kill switch: stop trading if account equity
                               # drops this % from its peak (high-watermark).
                               # 0 = off. User feature 2026-10-16.
MAX_CONSECUTIVE_LOSSES = 3     # entry cooldown: pause new entries after this
                               # many losing trades in a row. 0 = off.
                               # User feature 2026-10-16.
CONSECUTIVE_LOSS_COOLDOWN_HOURS = 4.0  # cooldown duration (hours) after the
                               # consecutive-loss trigger above. 0 = off.
                               # User feature 2026-10-16.
MARKET_DATA_FALLBACK_BINANCE = True  # market data: when OKX klines/price are
                               # unavailable, fall back to Binance Futures
                               # public API (klines + ticker price).
                               # False = fail-closed on OKX outage.
                               # User feature 2026-10-16.
EXEC_POST_ONLY_ENTRY = False   # entries: use post-only (GTX) limit orders so
                               # entries never cross the spread (maker only).
                               # Falls back to regular limit if unsupported.
                               # User feature 2026-10-16.
CANCEL_ORPHAN_ORDERS_ON_STARTUP = True  # on startup, cancel NEW exchange
                               # orders that have no matching tracked
                               # position (orphans from a crashed run).
                               # User feature 2026-10-16.
ADAPTIVE_VOLATILITY_TARGETS = False  # signal engine: scale TP/SL distances
                               # by recent ATR/volatility regime instead of
                               # fixed multiples. Experimental; default off.
                               # User feature 2026-10-16.

# ------------------------------------------------------- backtest defaults
# ROADMAP ITEM 4 (honest backtester). Costs are applied per side; every
# assumption is documented in backtest.py and the README — nothing is hidden.
BACKTEST_TAKER_FEE_PCT = 0.05      # taker fee per side (conservative: entries
                                   # are limits but we charge taker)
BACKTEST_SLIPPAGE_PCT = 0.02       # slippage estimate per side
BACKTEST_FUNDING_PER_8H_PCT = 0.01 # funding estimate per 8h while in position
                                   # (assumed PAID by the long — conservative;
                                   # historical funding is NOT used: degraded)
BACKTEST_MAX_HOLD_HOURS = 48       # positions older than this close at market
BACKTEST_SIGNAL_EXPIRY_HOURS = 24  # GAS: CRYPTO_SIGNAL_EXPIRY_HOURS — a signal
                                   # never entered within this window = NO_TRADE
BACKTEST_START_EQUITY = 10_000.0   # hypothetical USD equity for sizing
BACKTEST_MIN_TRADES = 30           # below this the report says SAMPLE TOO SMALL
BACKTEST_BREAKEVEN_AFTER_TP1 = True  # move SL to entry once TP1 fills
BACKTEST_DEFAULT_DAYS = 60         # default lookback for the CLI smoke run
BACKTEST_DEFAULT_STEP_H = 6        # signal evaluation step (hours)

# ------------------------------------------------------------- executor
# Phase 2 executor. Modes: paper (default), testnet (--testnet), live (--live).
# Keys come from env vars and are never hardcoded, never logged:
#   testnet: BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET
#   live:    BINANCE_LIVE_KEY / BINANCE_LIVE_SECRET  (mainnet, real money)
# BASE_URL defaults per mode (testnet vs mainnet) and can be overridden via
# BINANCE_FUTURES_BASE_URL (needed because Binance mainnet is geo-blocked
# from some hosts — see README "Live deployment" note).
BINANCE_FUTURES_BASE_URL = "https://testnet.binancefuture.com"
BINANCE_FUTURES_MAINNET_URL = "https://fapi.binance.com"
EXEC_POLL_INTERVAL_MIN = 5
EXEC_MAX_EXPOSURE_PCT_PER_SYMBOL = 20.0  # max position notional as % of balance
EXEC_BREAKEVEN_AFTER_TP1 = True           # move SL to entry once TP1 fills
EXEC_SIGNAL_ID_TTL_HOURS = 72             # processed signal IDs are kept this long
LIVE_SIGNAL_EXPIRY_HOURS = 24            # absolute max age (hours) of a
                                         # signals.db row before the executor
                                         # rejects it fail-closed. Defense in
                                         # depth on top of the scanner-set
                                         # expires_at: after an executor crash
                                         # / restart a row can linger; older
                                         # than this = no entry, no exceptions.
EXEC_RECV_WINDOW_MS = 10000
EXEC_STATE_FILE = "state.json"
EXEC_LIFECYCLE_FILE = "lifecycle.jsonl"
# Paper mode uses SEPARATE state files so paper trades can never
# contaminate (or be mistaken for) testnet position history.
EXEC_PAPER_STATE_FILE = "state-paper.json"
EXEC_PAPER_LIFECYCLE_FILE = "lifecycle-paper.jsonl"
# Dashboard settings (written by dashboard.py, honored by the executor).
# Missing file -> built-in defaults (current behavior, auto-trade ON).
DASHBOARD_SETTINGS_FILE = "dashboard_settings.json"
# Live mode uses SEPARATE state files so testnet/paper records can never
# contaminate (or be mistaken for) real-money position history.
EXEC_LIVE_STATE_FILE = "state-live.json"
EXEC_LIVE_LIFECYCLE_FILE = "lifecycle-live.jsonl"
EXEC_PAPER_START_BALANCE = 10_000.0       # hypothetical USDT for paper mode
# Pre-filter the scan universe to exchange-tradable symbols (from cached
# exchangeInfo). Prevents wasting engine evaluations on symbols that can
# never be executed (e.g. PEPE/TRUTH/SOXS on testnet). Fail-open: if the
# filter can't be built, the universe is unfiltered and the per-symbol
# execution-time check remains the backstop.
TRADABILITY_PREFILTER = True

# ------------------------------------------------- trading hours (WIB)
# New entries are only opened inside this daily window, evaluated in the
# Asia/Jakarta timezone. Position MANAGEMENT (TP/SL monitoring, breakeven
# moves, expiry) keeps running around the clock, and the exchange-side
# SL/TP algo orders protect open positions regardless of this gate.
# Applies to LIVE entries only; paper/testnet are unaffected (testing).
# User instruction 2026-10-15: "auto trading jalan 05:00-18:00 WIB aja",
# confirmed with signal mode ketat.
TRADING_HOURS_ENABLED = True
TRADING_HOURS_START = "05:00"   # inclusive, "HH:MM"
TRADING_HOURS_END = "18:00"     # exclusive, "HH:MM"
TRADING_HOURS_TZ = "Asia/Jakarta"


def within_trading_hours(now=None):
    """True when `now` falls inside the configured trading window.

    `now`: optional datetime (naive = assumed in TRADING_HOURS_TZ, aware =
    converted). Returns True unconditionally when TRADING_HOURS_ENABLED
    is False (dashboard override can disable the gate without restart).
    """
    if not TRADING_HOURS_ENABLED:
        return True
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TRADING_HOURS_TZ)
    now = now or datetime.now(tz)
    now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    cur = now.strftime("%H:%M")
    if TRADING_HOURS_START <= TRADING_HOURS_END:
        return TRADING_HOURS_START <= cur < TRADING_HOURS_END
    return cur >= TRADING_HOURS_START or cur < TRADING_HOURS_END

# ------------------------------------------------------------------ universe
# Curated universe of liquid USDT-M perpetuals. The dashboard can switch to
# "top_volume" mode, which picks the top-N symbols by 24h quote volume from
# the exchange instead (see executor.resolve_universe). One bad symbol never
# kills a scan — untradable/delist symbols are skipped gracefully.
UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT",
    "TRXUSDT", "DOTUSDT", "POLUSDT", "LTCUSDT", "BCHUSDT",
    "NEARUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "TIAUSDT", "JUPUSDT", "WLDUSDT", "TAOUSDT",
    "PEPEUSDT", "SHIBUSDT", "BONKUSDT", "WIFUSDT", "FLOKIUSDT",
    "FETUSDT", "RENDERUSDT", "GALAUSDT", "SANDUSDT", "MANAUSDT",
    "AAVEUSDT", "MKRUSDT", "LDOUSDT", "ARUSDT", "STXUSDT",
    "IMXUSDT", "HBARUSDT", "VETUSDT", "GRTUSDT", "ONDOUSDT",
    "JASMYUSDT", "NOTUSDT", "AXSUSDT", "SNXUSDT", "CRVUSDT",
]
UNIVERSE_TOP_N_DEFAULT = 30
UNIVERSE_TOP_N_MAX = 100
# GAS CFG L114: stablecoins never become signal candidates.
STABLECOINS = ["USDT", "USDC", "DAI", "USDG", "FDUSD", "TUSD", "USDP",
               "PYUSD", "USDE", "USTC", "LUSD", "FRAX", "GUSD", "BUSD",
               "EURI", "XAUT"]
# Universe modes (executor.resolve_universe):
#   curated    — the built-in liquid list above (config.UNIVERSE)
#   top_volume — top-N by 24h quote volume
#   top_movers — GAS parity (selectActiveSymbols_/getTopMovers_): top-N by
#                |24h % change| * log10(24h USD volume), stables excluded
#   all        — every USDT-margined OKX perpetual (~hundreds of symbols)
UNIVERSE_MODES = ("curated", "top_volume", "top_movers", "all")
UNIVERSE_TOP_MOVERS_DEFAULT = 60  # GAS: getTopMovers_(n), n default 60

# ------------------------------------------------------- signal cache (DB)
# Two-stage pipeline: scanner.py scans ALL OKX coins -> signals.db;
# run_executor.py --from-db executes the freshest signals from the DB.
DB_SCAN_LIMIT = 20  # max fresh signals considered per executor cycle

# ------------------------------------------------------------ data / caching
# GAS: klinesTtlSec_ per interval (L418-428), DERIVATIVES_CACHE_SECONDS=120 (L50)
KLINES_TTL_SEC = {"5m": 300, "15m": 480, "30m": 720, "1h": 1200, "4h": 2400, "1d": 7200}
DERIVATIVES_CACHE_SEC = 120
# Max entries per MarketData in-memory cache (klines / derivatives / ctVal).
# Working-set math: klines keys are {symbol}_{interval}_{limit} and the
# engine touches 5 intervals (15m/30m/1h/4h/1d), so universe 'all'
# (~485 symbols) needs ~2425 klines keys alone. The bound must stay above
# that or LRU thrash collapses the hit rate on 'scan all'. 4000 leaves
# headroom for derivatives/ctVal keys too; TTL semantics unchanged.
DATA_CACHE_MAX_ENTRIES = 4000
DERIVATIVES_PERIOD = "4h"       # GAS: CFG.DERIVATIVES_DEFAULT_PERIOD (L51)
HTTP_TIMEOUT_SEC = 15

# ------------------------------------------------- HTTP throttle + retry
# Shared by ExchangeClient (exchange.py) and MarketData (data.py) through
# httputil.ThrottledSession: minimum gap between requests + bounded retries
# with exponential backoff + jitter for transient failures (HTTP 429/5xx,
# timeouts, connection errors). Non-429 4xx responses are never retried.
HTTP_MIN_INTERVAL_SEC = 0.1  # max ~10 req/s per session; softens scan bursts
HTTP_MAX_RETRIES = 3         # retries per request (total attempts = 4)
HTTP_BACKOFF_BASE_SEC = 1.0  # first backoff window; doubles per retry

# ------------------------------------------------- WebSocket price feed
# Item 2.5: OKX public-WS ticker cache (wsfeed.py) as the first price
# source for the executor's freshness re-check. REST polling stays as
# the automatic fail-safe fallback — this changes the price SOURCE only,
# never trading/signal/risk logic.
WS_PRICE_FEED_ENABLED = True  # master switch; False = pure REST, as before
WS_STALE_SEC = 30.0           # WS price older than this counts as stale
                              # and falls back to REST

# ------------------------------------------------- scanner parallelism
# ThreadPoolExecutor workers for the all-coin scanner pass (scanner.py) and
# the phase-1 paper scanner (scan.py). The scan is I/O-bound (OKX REST), so
# threads are safe and need no new dependency (no aiohttp migration).
# 1 = legacy sequential behavior (kept as the exact fallback path).
SCAN_MAX_WORKERS = 4  # OKX rate limits still enforced via the shared session

# ------------------------------------------------- dashboard studio overrides
# Trading-studio settings (dashboard.py writes dashboard_settings.json; the
# executor applies them at the start of every scan cycle via
# apply_dashboard_overrides(), so changes take effect WITHOUT a restart).
#
# Numeric engine knobs: setting name -> (config attribute, min, max).
# STRICT defaults are the code defaults; the dashboard may move them within
# the ranges below. Nothing here can disable a hard gate except the explicit
# veto toggles (which the user opts into deliberately).
DASH_NUMERIC_OVERRIDES = {
    # signal quality gate
    "min_quality_score": ("MIN_QUALITY_SCORE", 50, 95),
    "min_24h_volume_usd": ("MIN_24H_VOLUME_USD", 500_000, 10_000_000),
    "freshness_max_drift_pct": ("FRESHNESS_MAX_DRIFT_PCT", 0.1, 2.0),
    # TP / SL geometry (RR multiples of the ATR stop distance)
    "tp1_rr": ("TP1_RR", 0.5, 5.0),
    "tp2_rr": ("TP2_RR", 1.0, 8.0),
    "tp3_rr": ("TP3_RR", 1.5, 12.0),
    "stop_atr_min": ("STOP_ATR_MIN", 0.5, 3.0),
    "stop_atr_max": ("STOP_ATR_MAX", 1.0, 5.0),
    # risk: max position notional as % of balance (operator-widened only
    # on explicit instruction; default 20 keeps concentration in check)
    "max_exposure_pct_per_symbol": ("EXEC_MAX_EXPOSURE_PCT_PER_SYMBOL", 5, 500),
    # kill switch: daily-loss cap as % of day-start balance. 0 = trip on any
    # daily loss; 100 = effectively off. Widened only on explicit operator
    # instruction (user 2026-10-15: "habiskan saja saldonya").
    "daily_loss_cap_pct": ("DAILY_LOSS_CAP_PCT", 0, 100),
    # profit take: market-close the whole position once unrealized PnL
    # reaches this % of the position margin. 0 = off (default).
    # User 2026-10-16: "+20% langsung TP" (live).
    "profit_take_pct": ("PROFIT_TAKE_PCT", 0, 100),
    # trailing stop: activate at +% of margin, exit on -% pullback from
    # peak. Both 0 = off (default). User 2026-10-16.
    "trail_trigger_pct": ("TRAIL_TRIGGER_PCT", 0, 100),
    "trail_distance_pct": ("TRAIL_DISTANCE_PCT", 0, 20),
    # time stop: close positions open longer than N hours. 0 = off.
    # User 2026-10-16.
    "time_stop_hours": ("TIME_STOP_HOURS", 0, 168),
    # WS price feed (item 2.5): how old a cached WS tick may be before
    # the freshness re-check falls back to REST polling.
    "ws_stale_sec": ("WS_STALE_SEC", 5, 300),
    # live signal expiry (item 1.4): max age (hours) of a signals.db row
    # before the executor rejects it fail-closed. 1 = strict, 72 = lenient.
    "live_signal_expiry_hours": ("LIVE_SIGNAL_EXPIRY_HOURS", 1, 72),
}
# Boolean engine knobs: setting name -> config attribute.
DASH_BOOL_OVERRIDES = {
    "bullish_only": "BULLISH_ONLY",
    "mtf_require_1d": "MTF_REQUIRE_1D",
    "mtf_require_1h": "MTF_REQUIRE_1H",
    "btc_align_required": "BTC_ALIGN_REQUIRED",
    "pattern_required": "PATTERN_REQUIRED",
    "news_check": "NEWS_CHECK",
    "liq_review": "LIQ_REVIEW",
    "whale_review": "WHALE_REVIEW",
    "breakeven_after_tp1": "EXEC_BREAKEVEN_AFTER_TP1",
    "trading_hours_enabled": "TRADING_HOURS_ENABLED",
    # WS price feed master switch (item 2.5): False = pure REST polling,
    # exactly the pre-2.5 behavior.
    "ws_price_feed_enabled": "WS_PRICE_FEED_ENABLED",
}
# MTF 30m leg mode: STRICT (default) or LEAN (GAS default behavior).
DASH_MTF_MODES = ("STRICT", "LEAN")
# Deterministic veto codes the dashboard can toggle (default: all enabled).
# Disabling a veto is an explicit operator choice and is logged as such.
DASH_VETO_CODES = ("leverage_risk", "funding", "squeeze", "whale",
                   "liq_wall", "btc_breaker", "sr_zone")
# Signal strictness presets: ketat | sedang | biasa.
# Choosing a mode applies its bundle to the individual dashboard settings
# (quality gate, MTF 30m leg, 1d cascade leg, bullish-only, volume gate,
# freshness drift, veto toggles). ketat/sedang are LONG-only (GAS parity)
# with the full 4h+1d cascade as a hard gate. biasa is bidirectional and
# relaxes the 1d cascade leg to a soft quality delta (bonus/penalty) instead
# of a hard reject — the major-trend information still moves the score.
# The engine runs a fully mirrored SHORT pipeline for biasa
# (BEARISH WATCH -> SHORT with bearish cascade, bearish vetoes/deltas,
# mirrored freshness, short-capable executor).
# The user can still fine-tune knobs afterwards — the mode is a preset,
# not a lock.
SIGNAL_MODES = ("ketat", "sedang", "biasa")
# Signal mode semantics (user spec 2026-10-15; aturan MTF 2026-10-15:
# SEMUA mode cek MTF 4h/1h/30m/15m/5m (cascade 2026-10-16):
#   biasa : 15m indicators (MACD, MA/EMA, RSI) + MTF 4h/1h/30m/15m/5m.
#           No BTC, no vetoes, no derivatives gate.
#   sedang: biasa + MTF alignment (4h, 1h, 30m, 15m all same direction)
#           + BTC direction must match.
#   ketat : sedang + pattern review + news review + liquidation review
#           + whale review + BTC direction + full veto/derivatives/quality.
SIGNAL_MODE_PRESETS = {
    "ketat": {
        "min_quality_score": 70,
        "mtf_30m_mode": "STRICT",
        "mtf_require_1d": True,
        "mtf_require_1h": True,
        "btc_align_required": True,
        "pattern_required": True,
        "news_check": True,
        "liq_review": True,
        "whale_review": True,
        "bullish_only": True,
        "min_24h_volume_usd": 2_000_000.0,
        "freshness_max_drift_pct": 0.4,
        "veto_leverage_risk": True,
        "veto_funding": True,
        "veto_squeeze": True,
        "veto_whale": True,
        "veto_liq_wall": True,
        "veto_btc_breaker": True,
        "veto_sr_zone": True,
    },
    "sedang": {
        "min_quality_score": 60,
        "mtf_30m_mode": "STRICT",
        "mtf_require_1d": False,
        "mtf_require_1h": True,
        "btc_align_required": True,
        "pattern_required": False,
        "news_check": False,
        "liq_review": False,
        "whale_review": False,
        "bullish_only": True,
        "min_24h_volume_usd": 1_000_000.0,
        "freshness_max_drift_pct": 0.6,
        "veto_leverage_risk": True,
        "veto_funding": True,
        "veto_squeeze": True,
        "veto_whale": True,
        "veto_liq_wall": False,
        "veto_btc_breaker": True,
        "veto_sr_zone": True,
    },
    "biasa": {
        "min_quality_score": 50,
        "mtf_30m_mode": "LEAN",
        "mtf_require_1d": False,
        "mtf_require_1h": True,   # aturan user 2026-10-15, cascade 2026-10-16: semua mode cek MTF 4h/1h/30m/15m/5m
        "btc_align_required": False,
        "pattern_required": False,
        "news_check": False,
        "liq_review": False,
        "whale_review": False,
        "bullish_only": False,
        "min_24h_volume_usd": 500_000.0,
        "freshness_max_drift_pct": 1.0,
        "veto_leverage_risk": False,
        "veto_funding": False,
        "veto_squeeze": False,
        "veto_whale": False,
        "veto_liq_wall": False,
        "veto_btc_breaker": False,
        "veto_sr_zone": False,
    },
}
# Vetoes currently disabled by the dashboard (populated by
# apply_dashboard_overrides; read by vetoes.run_vetoes).
DISABLED_VETOES = frozenset()

# Canonical dashboard defaults — single source of truth for dashboard.py,
# executor.py and validation. Risk knobs (auto_trade, risk_pct, leverage,
# interval) live here too.
DASHBOARD_DEFAULTS = {
    # operation
    "auto_trade": True,
    "risk_pct_per_trade": 1.0,
    "max_leverage": 3,
    "poll_interval_min": 5,
    # kill switch: stop new entries if daily loss exceeds this % of the
    # day-start balance. 100 = effectively off (explicit operator choice).
    "daily_loss_cap_pct": 3.0,
    # trading-hours gate (LIVE entries only 05:00-18:00 WIB). False = off.
    # NOTE: must stay in DASHBOARD_DEFAULTS — load_settings() drops any
    # settings key without a default, which silently deadens the knob.
    "trading_hours_enabled": True,
    # signal
    "signal_mode": "ketat",  # ketat | sedang | biasa (preset bundle)
    "min_quality_score": 70,
    "min_24h_volume_usd": 2_000_000.0,
    "freshness_max_drift_pct": 0.4,
    # WS price feed (item 2.5): prefer OKX WS ticker cache for the
    # freshness re-check; REST stays as automatic fallback.
    "ws_price_feed_enabled": True,
    "ws_stale_sec": 30.0,
    "live_signal_expiry_hours": 24.0,
    "bullish_only": True,
    "mtf_30m_mode": "STRICT",
    "mtf_require_1d": True,     # 1d trend alignment: hard gate (ketat)
    "mtf_require_1h": True,     # 1h trend alignment: hard gate (semua mode)
    "btc_align_required": True,  # BTC 15m direction must match (sedang/ketat)
    "pattern_required": True,   # candlestick pattern review (ketat)
    "news_check": True,         # coin news sentiment review (ketat)
    "liq_review": True,         # liquidation-spike review (ketat)
    "whale_review": True,       # whale/top-trader review (ketat)
    # TP / SL
    "tp1_rr": 1.5,
    "tp2_rr": 2.25,
    "tp3_rr": 3.0,
    "stop_atr_min": 1.25,
    "stop_atr_max": 2.0,
    "tp_split_1": 0.4,
    "tp_split_2": 0.3,
    "tp_split_3": 0.3,
    "breakeven_after_tp1": True,
    # vetoes (True = enabled)
    "veto_leverage_risk": True,
    "veto_funding": True,
    "veto_squeeze": True,
    "veto_whale": True,
    "veto_liq_wall": True,
    "veto_btc_breaker": True,
    "veto_sr_zone": True,       # S/R location: no short into 4h support / long into 4h resistance
    # universe
    "universe_mode": "top_movers",  # curated | top_volume | top_movers | all (GAS parity: top_movers)
    "universe_top_n": 30,        # 10-100, used when top_volume/top_movers
    # order execution (full operator control; defaults are the safe ones)
    "margin_mode": "ISOLATED",   # ISOLATED | CROSS
    "leverage_mode": "auto",     # auto (risk default) | fixed
    "leverage": 3,               # 1-125, used when leverage_mode=fixed
    "entry_mode": "risk",        # risk (RISK_PCT_PER_TRADE sizing) | fixed_usdt
    "fixed_usdt": 10.0,          # USDT margin per position when fixed_usdt
    # risk: max position notional as % of balance (default 20 = safe)
    "max_exposure_pct_per_symbol": 20.0,
    # profit take: close whole position at +% of margin (0 = off)
    "profit_take_pct": 0.0,
    # trailing stop: trigger +% of margin, distance % from peak (0 = off)
    "trail_trigger_pct": 0.0,
    "trail_distance_pct": 0.0,
    # time stop: close positions open longer than N hours (0 = off)
    "time_stop_hours": 0.0,
}

_ORIGINALS: dict = {}


def _snapshot_originals() -> None:
    if _ORIGINALS:
        return
    for _name, (attr, _lo, _hi) in DASH_NUMERIC_OVERRIDES.items():
        _ORIGINALS[attr] = globals()[attr]
    for attr in DASH_BOOL_OVERRIDES.values():
        _ORIGINALS[attr] = globals()[attr]
    _ORIGINALS["TP_SPLIT"] = TP_SPLIT
    _ORIGINALS["MTF_30M_MODE"] = MTF_30M_MODE
    _ORIGINALS["SIGNAL_MODE"] = SIGNAL_MODE
    _ORIGINALS["DISABLED_VETOES"] = frozenset()


def apply_dashboard_overrides(settings: dict) -> None:
    """Patch engine config from dashboard settings (clamped, idempotent).

    Called by the executor at the start of every scan cycle. Missing keys
    keep current values; out-of-range values are clamped. Never raises —
    the scan loop must survive a corrupt settings file.
    """
    try:
        _snapshot_originals()
        for name, (attr, lo, hi) in DASH_NUMERIC_OVERRIDES.items():
            if name not in settings:
                continue
            try:
                v = float(settings[name])
            except (TypeError, ValueError):
                continue
            v = max(lo, min(hi, v))
            if attr == "MIN_QUALITY_SCORE":
                v = int(round(v))
            globals()[attr] = v
        for name, attr in DASH_BOOL_OVERRIDES.items():
            if name in settings and isinstance(settings[name], bool):
                globals()[attr] = settings[name]
        if settings.get("mtf_30m_mode") in DASH_MTF_MODES:
            globals()["MTF_30M_MODE"] = settings["mtf_30m_mode"]
        if settings.get("signal_mode") in SIGNAL_MODES:
            globals()["SIGNAL_MODE"] = settings["signal_mode"]
        # TP split: three fractions, normalized to sum 1.
        try:
            parts = [float(settings[f"tp_split_{i}"]) for i in (1, 2, 3)]
            if all(p > 0 for p in parts):
                tot = sum(parts)
                globals()["TP_SPLIT"] = tuple(p / tot for p in parts)
        except (KeyError, TypeError, ValueError):
            pass
        # stop ATR sanity: min must not exceed max.
        if globals()["STOP_ATR_MIN"] > globals()["STOP_ATR_MAX"]:
            globals()["STOP_ATR_MIN"] = globals()["STOP_ATR_MAX"]
        disabled = frozenset(
            code for code in DASH_VETO_CODES
            if settings.get(f"veto_{code}", True) is False)
        globals()["DISABLED_VETOES"] = disabled
    except Exception:  # noqa: BLE001 - never break the scan loop
        pass


def reset_dashboard_overrides() -> None:
    """Restore the STRICT code defaults (used by tests)."""
    _snapshot_originals()
    for attr, v in _ORIGINALS.items():
        globals()[attr] = v
