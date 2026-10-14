# Binance Futures Bot — STRICT Signal Engine + Testnet Executor

> Phase 1: STRICT signal engine (paper mode). Phase 2: testnet executor
> (`executor.py`, `risk.py`, `state.py`, `exchange.py`, `run_executor.py`).
> Phase 3: live mainnet mode (`--live`) — real orders on Binance Futures
> mainnet, requires `BINANCE_LIVE_KEY`/`BINANCE_LIVE_SECRET`, and refuses all
> TEST ONLY flags. Run it only from a non-geo-blocked host.

Port of the **decision pipeline** from the GAS signal bot
(`workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs`, 8.659 lines)
to Python. Only the STRICT signal logic is ported — the autopost/publishing
machinery (Threads captions, charts, Telegram, Binance Square, X) is
intentionally out of scope.

**Phase 1 places no orders and needs no API key.** It scans the market with the
GAS bot's indicator math and cascade structure, and records passing signals
to `paper_trades.jsonl`.

**On "same strictness":** the port is *not* a strictness clone of the GAS bot.
It is deliberately *stricter* in three places — the 30m cascade leg defaults to
`STRICT` (full BULLISH WATCH) while the GAS default is `LEAN` (see
`MTF_30M_MODE` in `config.py`), every data gap fails closed (GAS has
several fail-open paths, e.g. `price_unavailable → ok:true`), and the GAS
deep-check hard vetoes (leverage-risk, funding-extreme, squeeze-risk,
whale-divergence, liquidity-wall, BTC circuit breaker) are ported as
non-compensable hard gates (roadmap item 2). AI news-sentiment and
chart-pattern vetoes remain out (audit: keep out until calibrated).

## Layout

| File | Purpose |
|---|---|
| `config.py` | STRICT constants (MIN_QUALITY_SCORE=70, MTF timeframes, GAS indicator params, risk defaults for Phase 2) |
| `indicators.py` | Exact ports: EMA, Wilder RSI, MACD, Wilder ATR, ADX |
| `data.py` | Public market data: klines (OKX primary → Binance spot fallback), 24h volume, futures derivatives snapshot (Binance Futures → Gate.io fallback), **advanced derivatives** (Gate.io contract_stats: top-trader positioning, OI, liquidation window), **order book** (Binance futures → Gate.io → OKX spot) |
| `signal_engine.py` | STRICT pipeline: 15m setup → MTF cascade → derivatives coverage → **deterministic vetoes** → quality gate → live-price freshness guard |
| `vetoes.py` | Deterministic hard vetoes ported from the GAS deep check (roadmap item 2): leverage-risk, funding-extreme, squeeze-risk, whale-divergence, liquidity-wall, BTC circuit breaker |
| `scan.py` | CLI scanner; appends passing signals to `paper_trades.jsonl` |
| `tests/` | Unit tests (synthetic candles) + integration test (real public endpoints) |

## The STRICT pipeline

```
15m analyze  → BULLISH WATCH? + trade plan? + strength ≥ 3?
      │  (BULLISH_ONLY: bearish never passes; STRICT is LONG-only)
      ▼
Liquidity    → 24h volume ≥ $2M? (fail-closed: unavailable volume REJECTS)
      ▼
MTF cascade  → 4h trend BULLISH? → 1d trend BULLISH? → 30m BULLISH WATCH?
      │  (mtfTrendDirection_: price > EMA21 > EMA50, EMA21 rising;
      │   30m leg per MTF_30M_MODE, default STRICT — stricter than GAS LEAN;
      │   any MTF fetch exception REJECTS)
      ▼
Derivatives  → funding + open-interest REQUIRED (missing → HOLD, never
               neutral); long/short + taker optional but flagged →
               score 0-100 (neutral 50)
      ▼
Vetoes       → HARD gates, never compensated by score (any fired veto or
               unevaluable required input → REJECT):
               leverage-risk ≥ 90 · funding |f| ≥ 0.1% against direction ·
               squeeze-risk ≥ 85 · top-trader extreme divergence ·
               $250k wall within 0.5% blocking the move ·
               BTC −3%/1h circuit breaker (all symbols)
      ▼
Quality      → 40 + strength×10 + delta, delta ∈ [-40, +40]
      │        delta = Σ explicit named components (quality_deltas.py),
      │        each ported from the GAS deep check (L3172-3262):
      │        mtf_30m +8 · mtf_4h +6/-10/+3/-5 · h1 +8/-10/0 ·
      │        btc_warn -15 · lev_warn -10 · funding +5 ·
      │        squeeze_warn -8 · whale_warn -12 · smart_money ±6 ·
      │        liq_event -15 · liq_wall_warn -8
      ▼
Gate         → quality ≥ 70 ?
      ▼
Freshness    → LIVE ticker price ≤ 0.4% above entry zone?
               (price unavailable → REJECT; one-sided like GAS)
      ▼
SIGNAL {symbol, LONG, entry, entry_low/high, stop_loss, tp1/2/3, quality, …}
```

Any gate failure returns `(None, reason)` — the bot never forces an entry.

## Deterministic vetoes (hard gates)

Ported from the GAS pre-publish deep check (`deepCheckDerivatives_` L3336-3393,
`deepCheckLiquidity_` L3395-3425, `deepCheckBtcBreadth_` L3634-3662).
A veto **rejects** the signal; a veto whose required inputs are unavailable
**holds** it (fail-closed). No score can compensate.

| Veto | GAS ref | Rule (LONG) | Data source |
|---|---|---|---|
| Leverage risk | L3338, index L1016-1022 | index ≥ 90 → veto | Gate.io contract_stats: retail long% (lsr_account), funding, OI USD, 24h quote volume, liquidation spike |
| Funding extreme | L3346-3354, CFG L72 | \|funding\| ≥ 0.001 **and** positive (crowded long) → veto | basic derivatives snapshot (required component) |
| Squeeze risk | L3360, index L1024-1034 | long-squeeze ≥ 85 → veto | retail long%, funding, OI change, liq-window side dominance |
| Whale divergence | L3366-3379, CFG L90 | top-trader long% ≤ 25 **and** whale-crowd divergence ≤ −15pp → veto | Gate.io `top_lsr_account` |
| Liquidity wall | L3395-3425, CFG L75-76 | ask wall ≥ $250k within 0.5% of mid → veto | order book: Binance futures depth → Gate.io → OKX spot |
| BTC circuit breaker | L3634-3662, CFG L106 | BTC −3%/1h (last two closed 1h candles) → veto **all** symbols | BTCUSDT 1h klines, 5-min cache |

Notes:
- The GAS *warn/penalty* tiers are ported as real score deltas (roadmap
  item 3, `quality_deltas.py`): funding +5, leverage −10, squeeze −8,
  whale-divergence −12, smart-money ±6, liquidation-event −15, liq-wall
  warn −8, BTC −2% −15, 1h-trend +8/−10, 4h-confirm +6/−10/+3/−5. The GAS
  liq-warn −8 was dead code twice over (audit M1: `r.delta` never
  initialized → NaN, and never summed into scoreDelta); here it is a real,
  tested component — the bug is deliberately **not** reproduced.
- AI news-sentiment and chart-pattern vetoes/deltas stay out until
  calibrated (audit).
- Scores are **not** win probabilities and are never inflated to force a
  pass; missing component data contributes 0 (never a bonus) and is noted
  on the signal's `delta_notes`.
- **Gate.io book units:** Gate quotes book size in *contracts*; notional needs
  the per-contract `quanto_multiplier` (BTC_USDT 0.0001, ETH_USDT 0.01, …).
  Without it a best ask reads as a multi-billion "wall" — a 10,000x false veto.
  `data.py` converts to base-asset units via the contracts endpoint (cached).
- The advanced derivatives snapshot follows GAS in using **Gate.io**
  (`contract_stats`, 48 × 15m buckets) even when Binance feeds the basic
  snapshot — there is no Binance-primary advanced path yet.

## Deliberate simplifications vs the GAS bot

1. **Deep-check vetoes: ported as hard gates; patterns/news stay out.** GAS runs
   a pre-publish deep check (chart patterns, liquidity walls, Gemini news
   sentiment, advanced derivatives gate). Roadmap item 2 ports the
   *deterministic* capital-protection vetoes — leverage-risk ≥90,
   funding-extreme, squeeze-risk ≥85, whale-divergence, $250k liquidity wall,
   BTC-dump circuit breaker — as non-compensable hard gates (`vetoes.py`).
   Chart-pattern and AI news vetoes stay out until calibrated (audit).
   Warn-tier score penalties (funding +5, whale −12, liq-warn −8, BTC −2% −15)
   are not ported; they belong to roadmap item 3 (quality recalibration).
2. **Quality gate (roadmap item 3).** GAS: `quality = 40 + strength×10 +
   deepCheck.scoreDelta` (delta clamped ±40, quality clamped 5–98, STRICT bar
   70). Ours keeps that exact formula; the delta is the sum of explicit,
   named, tested components in `quality_deltas.py`, each ported from the GAS
   deep check (prePublishDeepCheck_, L3172-3262). The old synthetic blend
   (`+8 +8 + round((deriv-50)×0.4)`) is gone: the second +8 was a Python
   invention (in GAS the cascade boost reaches scoreDelta only through the
   30m leg in `deepCheckMtf_`), and the deriv blend is replaced by the real
   warn-tier components. Every signal carries `delta_breakdown` (name →
   delta) and `delta_notes` for the audit trail.
3. **Klines provider order.** GAS: OKX primary → Binance spot fallback. Same here.
   (Binance spot klines are dropped-last-candle, exactly like GAS.)
4. **Derivatives provider order.** GAS aggregates Gate.io + Bitget + OKX.
   Phase 1 tries **Binance Futures** endpoints first
   (`fundingRate`, `openInterestHist`, `globalLongShortAccountRatio`,
   `takerlongshortRatio`), then falls back to **Gate.io**
   (`tickers` funding_rate + `contract_stats` OI/LSR/taker) — the GAS primary
   derivatives source. Per-component failures are swallowed (`derivSafe_`
   pattern); the snapshot aggregates whatever succeeded.
5. **No anti-duplicate / cooldown / lifecycle tracking.** GAS dedups posts per
   coin+direction and tracks signal lifecycle. Phase 2's executor owns
   idempotency (one position per signal ID).
6. **Freshness uses a live ticker price** (Binance Futures `ticker/price` →
   OKX ticker fallback), one-sided exactly like GAS `freshnessCheck_`
   (reject only if price ran >0.4% *above* the entry zone). The port previously
   used the last closed 15m candle (up to ~23 min stale). Unavailable price
   **rejects** the signal — GAS returned `ok:true` on `price_unavailable`,
   a fail-open we deliberately do not reproduce.
7. **Derivatives coverage is fail-closed.** Funding + open-interest are
   required; a snapshot with neither **holds** the signal instead of
   neutralizing to score 50 (GAS and the old port both passed on zero
   visibility). Long/short ratio + taker volume are optional but recorded
   on the signal dict (`deriv_missing`) when absent.
8. **Volume gate is fail-closed.** 24h quote volume must be ≥ $2M *and*
   available; GAS applied the floor only at universe-selection time.
9. **30m cascade leg is STRICT by default** (`MTF_30M_MODE='STRICT'`), i.e.
   30m must be a full BULLISH WATCH. The GAS default is LEAN, which passes
   neutral-with-bullish-lean 30m setups. This is a deliberate, documented
   divergence (safer direction); set `MTF_30M_MODE='LEAN'` for GAS parity.

## Setup & usage

```bash
cd ~/workspace/binance-futures-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# run tests (unit + live-public-endpoint integration)
.venv/bin/python -m pytest tests/ -q

# scan the default universe (paper mode)
.venv/bin/python scan.py
.venv/bin/python scan.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --log paper_trades.jsonl

# honest backtest: production pipeline on historical klines (paper mode)
.venv/bin/python backtest.py --symbols BTCUSDT --days 30
.venv/bin/python backtest.py --symbols BTCUSDT,ETHUSDT \
    --in-sample 2025-01-01 2025-06-30 --out-of-sample 2025-07-01 2025-09-30
```

Dependencies: `requests` only (`pytest` for tests). `python-binance` is the
planned Phase 2 dependency for signed order placement — deliberately not
included yet so Phase 1 *cannot* place orders.

## Test report (2026-10-14)

- `tests/test_indicators.py` — 6 tests: hand-computed EMA values, RSI extremes,
  MACD histogram sign on accelerating/decelerating series, Wilder ATR seeding +
  smoothing, ADX on trend vs flat, short-input guards. **Pass.**
- `tests/test_engine.py` — 27 tests: bullish/bearish/neutral detection on
  synthetic candles, exact trade-plan math (entry/SL/TP1-3/RR), ATR guard,
  hand-computed `smartCryptoScore_` (110 → quality rank 80), derivatives-score
  components, quality gate with explicit named components (replaces the old
  synthetic deriv blend), MTF trend
  direction, STRICT 30m confirm, LEAN 30m confirm (GAS-parity branch),
  cascade pass/fail stages incl. neutral-lean 30m rejection (T5), full
  `evaluate_symbol` pass + rejection paths (bearish, low volume, volume
  unavailable (T12), weak strength, stale live-price entry (T1), ticker
  failure (T1), derivatives total failure (T2), optional derivatives gap
  flagged, MTF 1d exception (T13)). **Pass.**
- `tests/test_vetoes.py` — 27 tests (roadmap item 2): hand-computed
  leverage-risk / squeeze-risk formulas (GAS L1016-1034), BTC 1h change math,
  each veto firing on synthetic data (T4), BTC −3%/1h vetoing all symbols /
  −2% not vetoing / data failure holding (T3), fail-closed holds for
  derivatives/book/BTC outages, veto + hold end-to-end rejections, and the
  regression that a pre-veto passing signal still passes. **Pass.**
- `tests/test_quality_deltas.py` — 19 tests (roadmap item 3): every
  warn-tier component hand-computed against the GAS source (mtf 30m/4h tiers,
  1h trend +8/−10, BTC −2% −15, leverage −10, funding +5, squeeze −8,
  whale −12, smart-money score 73 hand-computed + ±6 tiers, liquidation-event
  −15, liq-wall warn −8 incl. the NaN-bug regression guard), delta clamp
  ±40, and T7 quality boundaries 66/69/70/76/86 → reject/reject/accept/
  accept/accept, plus the proof that real negative deltas can now pull a
  strength-4 signal below 70. **Pass.**
- `tests/test_integration.py` — 4 tests against real public endpoints
  (BTCUSDT): klines structure/order/closed-candle, cache hit, 24h volume,
  derivatives snapshot shape. **Pass** (derivatives served via Gate.io fallback;
  Binance mainnet returns HTTP 451 geo-block from this network).

**83 passed, 0 failed.**

- `tests/test_backtest.py` — 10 tests (roadmap item 4): T8 hand-computed
  trade simulation (entry → TP1 → TP2 → breakeven stop, exact pnl/fees/
  funding/R), T12 same-candle SL+TP → conservative SL-first, NO_ENTRY on
  untouched/expired signals, MAX_HOLD market close, BacktestMarket
  no-look-ahead + insufficient-data fail-closed, degraded-mode pipeline
  wiring, hand-checked metrics math (win rate, expectancy, PF, max DD,
  consecutive wins/losses). **Pass.**

**93 passed, 0 failed.**

## Threshold decision (roadmap item 3, 2026-10-14)

**Decision: keep `MIN_QUALITY_SCORE = 70`** (the GAS STRICT bar, `getMinQuality_` L3137).

Evidence — `tools/threshold_study.py`, offline run over 14 days × 5 majors
(BTC/ETH/SOL/DOGE/AVAX), 285 evaluation points, historical OKX klines only,
no orders:

- Funnel: 131 passed the 15m setup → 48 passed the 4h/1d cascade → 45 reached
  the quality gate. The cascade (not the quality bar) does the heavy filtering.
- Quality distribution of the 45 gate-reaching signals: min 81, p25 94,
  median 98, max 98. Pass rates: 100% at 60, 65, 70 **and** 75.
- The study could not score historical derivatives warns (no historical
  feed); they were treated as 0, so these pass rates are **upper bounds** —
  real warns (lev −10, whale −12, liq-event −15, btc −15, squeeze −8) can only
  push scores down.

Why 70 stays: (1) it is the GAS STRICT bar this system is calibrated against;
(2) in this 14-day bullish window even 75 would not discriminate, so the data
does not argue for raising; (3) lowering to 65/60 has no supporting evidence;
(4) the audit's actual complaint (H1 — gate non-binding) is fixed by the
ported negative deltas, not by moving the bar: a strength-4 signal at
80 + 14 − 47 = 47 is now rejected where the old constant-+16 formula could
never reject it. Revisit the threshold only with out-of-sample evidence from
live paper trading (roadmap step 5: cross-validation vs GAS signals).

## Backtest (roadmap item 4, 2026-10-14)

`backtest.py` runs the **production** `evaluate_symbol()` pipeline — same hard
gates, MTF cascade, vetoes, quality gate, freshness — against historical OKX
klines via a time-sliced `BacktestMarket`, then simulates entries/exits.

Gate parity and documented approximations:

- 15m setup, MTF cascade (4h/1d/30m STRICT), quality ≥ 70, freshness: run
  identically. Freshness uses the **signal-candle close** as the live-price
  proxy (documented approximation; the gate only rejects drift above the zone).
- **Derivatives are degraded**: no historical derivatives/order-book feed
  exists, so V9/V10/V11/V12/V13 vetoes and derivatives warn deltas are
  SKIPPED (never fabricated) and flagged on every signal. The V15 BTC circuit
  breaker runs on real historical BTC 1h klines. Backtest pass rates are
  therefore **upper bounds** on production selectivity.
- Costs: taker fee 0.05% + slippage 0.02% per side on every fill; funding
  estimated at 0.01%/8h on open notional, assumed paid by the long
  (conservative; historical funding not used).
- Same-candle rule: the stop is checked **before** take-profits on every
  candle (conservative for longs) — including the entry candle.
- Entry: first post-signal 15m candle whose low touches ≤ entry, filled at the
  entry price. Never touched within 24h → NO_ENTRY (excluded from win rate).
  Open past 48h → closed at market and counted.
- No look-ahead: signals use only candles with `close_time <= t`; entries
  evaluated from t+1. No pyramiding: one open simulated position per symbol.

Walk-forward: `--in-sample` / `--out-of-sample` date ranges (or an automatic
70/30 split with `--days`). Both windows are reported separately; the report
states SAMPLE TOO SMALL below 30 trades and flags IS/OOS sign flips as
UNSTABLE.

## Ready for Phase 2 (executor)

- `signal_engine.evaluate_symbol()` returns everything an executor needs:
  direction, entry zone, stop-loss, TP1-3, quality, strength, timestamp.
- Risk defaults are in `config.py` (`RISK_PCT_PER_TRADE=1.0`,
  `MAX_LEVERAGE=3`, `TP_SPLIT`, `MAX_CONCURRENT_POSITIONS`,
  `DAILY_LOSS_CAP_PCT=3.0`) — user-correctable before live trading.
- Recommended Phase 2 order: paper validation of Python signals vs GAS signals
  → executor on **Binance Futures testnet** (`testnet.binancefuture.com`
  is reachable from here) with `python-binance` → live only after validation
  + explicit user approval.

## Phase 2: testnet executor (2026-10-14)

New modules (signal logic untouched):

| File | Responsibility |
|---|---|
| `exchange.py` | Binance USD-M Futures REST client (HMAC-SHA256, `requests` only — no SDK). Signed: account, leverage, margin type, place/query/cancel order. |
| `executor.py` | Poll loop over `config.UNIVERSE` calling the same `evaluate_symbol()`. Per APPROVED signal: idempotency (symbol + signal-candle close + direction), live-price freshness re-check, tradability/leverage/ISOLATED checks, risk-based sizing, entry (MARKET in-zone else LIMIT) + STOP_MARKET SL + 3 TP LIMITs, TP1→breakeven SL move, fill monitoring, lifecycle logging. |
| `risk.py` | `RiskManager`: daily-loss kill switch (3% of start-of-day balance, UTC day), max 3 concurrent positions, per-symbol exposure cap (20%). `PaperBroker`: honest fill simulation for `--paper` (fills only on price touch, 0.05% fee/fill). |
| `state.py` | `state.json` (atomic writes): signal IDs (72h TTL), positions, daily PnL, kill-switch flag. Startup `reconcile()`: adopts unknown exchange positions as UNMANAGED (manual review, no auto orders), marks vanished ones closed. |
| `lifecycle.py` | Append-only `lifecycle.jsonl`: generated → approved/rejected(reason) → entry_triggered → position_opened → tp1/2/3_hit / sl_hit / expired / cancelled. |
| `report.py` | `--daily` honest report: signals, rejections, fills, PnL; win rate shown only at n ≥ 30 ("SAMPLE TOO SMALL" otherwise). |
| `run_executor.py` | CLI: `--paper` (default) \| `--testnet` (needs env keys, refuses without them — no mainnet fallback) × `--once` \| `--loop`. |

### Running it

```bash
# paper mode, one scan (no keys, no orders — simulated fills only)
.venv/bin/python run_executor.py --paper --once

# paper mode, continuous (5-min scans by default)
.venv/bin/python run_executor.py --paper --loop

# testnet (real test orders) — create the key first, see below
export BINANCE_TESTNET_KEY=... BINANCE_TESTNET_SECRET=...
.venv/bin/python run_executor.py --testnet --once

# daily report (UTC)
.venv/bin/python report.py --daily
```

### Creating a Binance Futures testnet key (user does this manually)

1. Go to https://testnet.binancefuture.com and log in with GitHub/Google.
2. The testnet funds a demo wallet automatically; if empty, use the faucet.
3. API Management → **Generate HMAC_SHA256 Key** → label it (e.g. `muse-executor`).
4. Leave IP access **unrestricted** only for testing, or restrict to your host IP.
5. `export BINANCE_TESTNET_KEY=<key> BINANCE_TESTNET_SECRET=<secret>` —
   never paste them into chat, never commit them (see `.gitignore`).

### Live deployment (mainnet) — enabled via `--live`

- Binance **mainnet is geo-blocked (HTTP 451) from some hosting locations**
  (verified 2026-10-14 from this VM). Run the live executor from a
  non-blocked host (e.g. the user's own machine in Indonesia, or a VPS in an
  allowed region). The data layer already falls back to OKX/Gate.io.
- Setup on the live host:
  1. `git clone https://github.com/aziz-sudrajat/binance-futures-strict-bot`
  2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
  3. Create a mainnet key at https://www.binance.com (API Management:
     enable **Futures**, DISABLE **withdrawals**, restrict IP if possible).
  4. `export BINANCE_LIVE_KEY=<key> BINANCE_LIVE_SECRET=<secret>` —
     never paste them into chat, never commit them (see `.gitignore`).
  5. Dry-check without trading: `.venv/bin/python run_executor.py --live --once`
     (a full scan cycle that opens real positions only on APPROVED signals).
- Run continuously: `.venv/bin/python run_executor.py --live --loop`
  (default interval 5 min), or via cron/systemd with a lock file.
- Hard safety properties (enforced in code, tested):
  - `--live` refuses without `BINANCE_LIVE_KEY`/`BINANCE_LIVE_SECRET`.
  - `--live` REFUSES all TEST ONLY flags (`--fixed-margin-usdt`,
    `--leverage`, `--leverage-tiers`, `--leverage-default`, `--manual`).
  - Live always uses production risk defaults: 1% risk/trade, max 3x
    leverage, TP1/TP2/TP3 + SL + breakeven, daily-loss kill switch.
  - Live keeps separate state files (`state-live.json`,
    `lifecycle-live.jsonl`) so testnet/paper history can never mix in.
- Do NOT run live until: paper+testnet validation is complete, the
  cross-validation vs GAS signals (roadmap item 5) is done, risk limits are
  reviewed, and the user gives explicit approval.

### Pre-deploy checklist (from the quant-trading skill)

- [ ] Full test suite green (`pytest tests/ -q`) — currently 112 passed.
- [ ] Paper-mode smoke run completed with no errors.
- [ ] Testnet key created by the user; `--testnet --once` places/cancels cleanly.
- [ ] Kill switch verified on testnet (force a >3% day or lower the cap temporarily).
- [ ] Restart recovery verified (kill mid-position, restart, reconcile adopts).
- [ ] `state.json` / `lifecycle.jsonl` excluded from git (`.gitignore` present).
- [ ] No secrets in code, logs, or tests (`grep -ri secret` clean).
- [ ] Roadmap item 5 (2–4 week Python-vs-GAS cross-validation) scheduled.
- [ ] Explicit user approval recorded before any mainnet consideration.

## Safety

- No API key / secret is requested, stored, or logged anywhere. Keys live
  only in environment variables at runtime.
- `--testnet`/`--live` without their keys refuse to run; live never falls
  back to testnet or paper, and refuses every TEST ONLY flag.
- No profit promises; signals are technical setups, not financial advice.
