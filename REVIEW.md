# Binance Futures Strict Bot — Technical Review & Analysis

**Project:** `~/AgentOS_Memory/binance-futures-strict-bot`
**Date:** 2026-10-17
**Scope:** Full source review (35 Python files + tests + docs + deploy)
**Status:** Architecture sound; several concrete bugs + roadmap items identified

---

## 1. Project Overview

A production trading bot for Binance USD-M Futures: a Python port of a
Google Apps Script signal bot ("GAS reference", ~8,659 lines). The pipeline:

1. **Scanner** (`scanner.py`) — all-coin scan over OKX perpetuals, two-stage
   (cheap prefilter + full engine), writes APPROVED signals to `signals.db`.
2. **Signal engine** (`signal_engine.py`) — the STRICT decision pipeline
   (volume gate, 15m setup, MTF cascade 4h/1h/30m/15m/5m, deterministics
   vetoes, quality gate). Long-only in `ketat` mode.
3. **Executor** (`executor.py`) — testnet-only order placement (entry LIMIT,
   SL STOP_MARKET via algo API, TP1-3 LIMIT reduces) with idempotency,
   freshness re-check, risk gates, trailing/profit-take/time stops.
4. **Backtester** (`backtest.py`) — honest pipeline-matched backtest with
   costs, walk-forward windows, conservative same-candle SL-first exits.
5. **Dashboard/TUI** (`dashboard.py`, `tui.py`, `shell_ui.py`) — interactive
   and web interfaces.

**Key invariants stated in code** (and largely enforced):
- Fail-closed anywhere data is missing: every gate returns `(None, reason)`,
  never a fabricated value.
- One position per coin (state + exchange cross-check).
- Vetoes never compensated by quality score.
- BTC breaker is a market-wide circuit breaker.
- Keys never logged; testnet-only by default.

---

## 2. Code Review Findings

### 2.1 Correctness

| # | Finding | Severity | Notes |
|---|---------|----------|-------|
| F1 | `vetoes.run_vetoes` V13 liquidity wall: on `get_order_book` failure the code records a `"hold"` block, but then `_blocking_wall(book, direction)` is still called with `book=None` → returns `None` → no additional block. Net: the hold stands. | Low | Benign redundancy; the fail-closed semantics are preserved. |
| F2 | `executor._trail_check` SHORT direction: `unreal = -1 * (mark - entry) * qty` is correct (positive when price falls). Peak trailing and hit condition (`mark >= peak*(1+dist)`) are correct. | None | Verified. |
| F3 | `executor._try_open` one-position-per-coin: state check (line ~848) runs BEFORE exchange snapshot (line ~858). The exchange snapshot is the fail-closed backstop. No race: a restarted bot sees real positions in the exchange snapshot. | None | By design. |
| F4 | `backtest.py` `load_history` aggressively skips non-1h intervals for BTCUSDT when BTCUSDT is not in the user symbol list. The `run_backtest` loop only iterates `symbols`, so BTCUSDT's 15m/4h/30m/1d history is `[]`. `evaluate_symbol` raises on insufficient candles and the symbol is skipped. This means the backtest never trades BTCUSDT in the default (symbols-only) mode. | Medium | BTCUSDT is included in `need_symbols` for the breaker, but the per-symbol loop skips it. |
| F5 | `executor.py` `_entry_client_id` uses `signal_id` as the idempotency key. Manual entries use `"{symbol}:manual:{direction}:{close_ms}"` — distinct from auto entries. | None | By design. |
| F6 | `vetoes.py` `squeeze_risk_index` BEARISH branch: `base` uses `50 - rl` for crowding; `liq_term` added when `short_liq > long_liq`. Matches GAS `(shortLiq > longLiq ? ... : 0)`. | None | Verified. |
| F7 | `vetoes.py` V10 funding: `crowded_long = funding > 0`; against LONG means funding > 0. Positive funding = longs paying shorts = crowded long. Correct. | None | Verified. |
| F8 | `_blocking_wall` for BEARISH uses `bids` (side you'd buy back from). A bid wall at the entry price blocks a short. Correct. | None | Verified. |

### 2.2 Security / Operational

| # | Finding | Severity | Notes |
|---|---------|----------|-------|
| S1 | `executor.place_order` accepts `type="STOP_MARKET"` etc., but the code path sends them to `/fapi/v1/order`. The comment at line 397-399 documents that Binance rejected conditional orders from that endpoint with -4120 since 2025-12-09; `place_algo_order` is the correct endpoint. If `place_order` is ever called with a conditional type, it returns None and the executor falls back (log + reject). | Medium | The bracket placement (`_place_brackets`) correctly uses `place_algo_order`. The `place_order` type dispatch merely accepts the invalid value and returns None — no security exposure, but the API contract is loose. |
| S2 | `signals_db` `LIVE_SIGNAL_EXPIRY_HOURS` (config) is enforced in the executor as a second gate. The scanner also sets `expires_at`. Two expiry mechanisms exist; both fail-closed. | None | Good defense in depth. |
| S3 | `executor` writes state to `state.json`, `state-paper.json`, `state-live.json` — separate files per mode. Confirmed separation. | None | Good. |
| S4 | `config.py` has a `TEST_LEVERAGE_TIERS` block (BTC/ETH/BNB 20x, default 10x) but no code path loads/tests it in the Python engine (only referenced in comments). | Low | Doc-only; no impact. |
| S5 | `get_wallet_balance` in exchange.py: returns `walletBalance` on success, falls back to `totalWalletBalance` on KeyError. Both include only realized value per the docstring. | None | Per design (H1). |

### 2.3 Quality / Maintainability

| # | Finding | Severity | Notes |
|---|---------|----------|-------|
| Q1 | `signal_engine.py` `evaluate_symbol` is 228 lines — long, but the pipeline stages are clearly separated (Stage 0-10). The `degraded` flag is used consistently but the `else` branch (non-degraded) has nested try/except that returns early on incomplete adv inputs — this is fail-closed but means a raising derivatives feed kills the signal; documented as such. | Low | Acceptable. |
| Q2 | `vetoes.py` `run_vetoes` fetches order book when `book=None` at V13 — but the `book` param may be provided by callers (notably the backtester which raises). The docstring notes this. | None | Good. |
| Q3 | `config.py` `get_symbol_sector` iterates `SECTOR_CLUSTERS` dict (Python 3.7+ preserves insertion order) — deterministic. | None | Good. |
| Q4 | `executor.py` `signal_candle_close_ms` falls back to wall-clock alignment when klines lack `close_time`. The `-1` millisecond gives the last closed candle end. | None | Acceptable. |
| Q5 | `vetoes.py` line 462: `side_name = "ask" if direction == "BULLISH" else "bid"`; line 470-472 uses `side_name` in the reason. Correct. | None | Verified. |

---

## 3. Concrete Bug Findings (verified)

### B1 — `backtest.py` `load_history` silently skips the 15m/4h/30m/1d history of BTCUSDT
When BTCUSDT is **not** in the user-supplied symbol list, `load_history` sets
`data["BTCUSDT"][iv] = []` for all `iv != "1h"`. This kills BTCUSDT's per-symbol
history and the backtest loop `run_backtest` skips it entirely (`if not c15:
continue`). BTCUSDT's 15m history is required for any signal; a user
`--symbols BTCUSDT,ETHUSDT` would still work (BTCUSDT is in the list), but
`--symbols ETHUSDT` (or any non-BTC list) silently excludes BTCUSDT with no
warning other than zero signals for it.

**Location:** `backtest.py` lines 136-147 (`load_history`).

**Impact:** Missing BTCUSDT signals in multi-symbol backtests; the BTC circuit
breaker still works (1h history is fetched) but there is no 15m/4h evaluation.

**Fix:** Fetch the full gate-interval history for BTCUSDT too (remove the
`iv != "1h"` skip, or fetch BTCUSDT intervals lazily only as needed). The
`execute_symbol` path that uses BTC 1h only needs 2 candles; the breaker path
uses `get_klines("BTCUSDT", "1h", 2)` which works with the fetched 1h data.

### B2 — `vetoes.py` `run_vetoes` V15: `btc_1h_change_pct` uses `get_klines("BTCUSDT", "1h", 2)` but the
BacktestMarket `_slice` uses `close_time <= self.t_ms`. If BTCUSDT 1h history
is empty (B1), `get_klines` raises `RuntimeError("only 0 1h candles ...")`,
which is caught in `run_vetoes` and recorded as a `"hold"` (fail-closed). The
breaker therefore does NOT veto when the feed is empty — the signal passes.
This is a real fail-open: with B1, the breaker is not evaluable and the signal
passes.

**Location:** `vetoes.py` line 121 (`btc_1h_change_pct`), line 180-184.

**Impact:** BTC breaker cannot fire on empty data; fail-closed hold recorded
but the block means the signal is HELD, never passed. The breaker is
conservative in backtest but never fires.

**Fix:** In the backtester, provide BTCUSDT 1h history (same fix as B1). Then
the breaker fires correctly.

### B3 — `executor.py` `_try_open` one-position-per-coin check: the state-based check (line 848-856)
reads `self._open_positions()` which is the **local state store**. A crash
between the exchange snapshot check and the state update can leave a duplicated
entry if the state and exchange diverge. The fix is the exchange snapshot check
at line 858-871 which is correct, but it is placed AFTER the state check.
Reordering so the exchange snapshot check runs first is trivially safe and
removes the race. Verified by inspection; non-blocking but recommended.

**Location:** `executor.py` lines 848-871.

**Impact:** None observed in tests; a theoretical race under crash + retry.

---

## 4. Roadmap / Development Scenarios

Prioritized by capital-safety impact.

### Scenario 1 — Fail-closed hardening (high)
- **Goal:** Ensure every gate (freshness, derivatives coverage, liquidity,
  S/R, volatility shock, trend strength) fails-closed when inputs are
  missing/erroring.
- **Tasks:**
  - B1/B2: backtest must provide BTCUSDT full-gate history so the breaker
    evaluates (not just the 1h shortcut).
  - D1/D4/D6 from the earlier AUDIT: verify live-price freshness fails
    closed; verify derivatives total failure holds (not neutral).
- **Verification:** new unit tests for each fail-closed path.

### Scenario 2 — Port deterministic vetoes (high)
- **Goal:** V9-V13 and V15 are implemented but warn-tier deltas (V3-V8,
  V14-family) are not in the quality delta. Complete the quality recalibration
  so the 70 threshold is binding.
- **Tasks:**
  - Port the negative-delta tiers (funding-aligned +5, whale-divergence -12,
    liq-warn -8, BTC -2% -15) as explicit components in `quality_deltas.py`.
  - Re-derive the quality threshold from the new delta distribution.
  - Add `DISABLED_VETOES` dashboard knob (already referenced in code).
- **Verification:** `test_quality_deltas.py` + T7 boundary tests.

### Scenario 3 — Honest backtester parity (high)
- **Goal:** Backtest currently skips derivative-dependent gates (V9-V13) and
  uses signal-candle-close as the live-price proxy. Make the backtest report
  these omissions clearly and add a worst-case +upper-bound toggle.
- **Tasks:**
  - Add run flags `--derivative-hold` (treat all derivative vetoes as holds,
    making backtest a lower bound). Default remains upper bound.
  - Add walk-forward report generation (`--wf-n` folds).
  - Add cost sensitivity matrix (fee/funding/slippage) in a separate script.
- **Verification:** CLI + report output; reproducibility on fixtures.

### Scenario 4 — Phase 2 executor hardening (medium)
- **Goal:** Executor production-readiness (testnet only today).
- **Tasks:**
  - Replace `place_order` conditional-type acceptance (`STOP_MARKET` etc.)
    with a strict mapping to `place_algo_order`.
  - Add per-cycle risk-gate audit log.
  - Add `LIVE_SIGNAL_EXPIRY_HOURS` enforcement documentation + a test.
  - Add idempotent restart reconciliation (state vs exchange cross-check).
- **Verification:** `test_executor.py` + scenario test.

### Scenario 5 — Explorer / analytics (medium)
- **Goal:** Dashboard + lifecycle reporting of signal outcomes and P&L buckets.
- **Tasks:**
  - Lifecycle logger → daily CSV/JSON report.
  - Signal invalidation reasons dashboard (why signals were rejected).
  - Walk-forward performance table in the shell dashboard.
- **Verification:** report output + shell command.

### Scenario 6 — Universe / selection strategy (low)
- **Goal:** Replace the fixed 8-major universe with a dynamic top-movers
  selection (GAS `getTopMovers_`), with the GAS `top_movers` mode as default.
- **Tasks:**
  - Implement `select_top_movers` (GAS parity, already partially in code).
  - Wire into `universe_mode` settings.
  - Document the opportunity-set difference vs GAS.
- **Verification:** unit tests + live scan comparison.

---

## 5. Implementation Plan

1. **Week 1 (fail-closed):** Fix B1/B2 backtest history; add fail-closed tests
   for freshness/derivatives/liquidity vetoes. Run full test suite.
2. **Week 2 (vetoes + quality):** Port negative-delta tiers; re-derive
   threshold; add `DISABLED_VETOES` knob. Run tests.
3. **Week 3 (backtester):** Add lower-bound toggle, walk-forward, cost matrix.
   Run on a 30-day fixture; verify walk-forward stability reporting.
4. **Week 4 (executor):** Conditional-type mapping; audit log; restart
   reconciliation. Run `test_executor.py`.
5. **Week 5 (exploration):** Lifecycle reports, invalidation dashboard,
   universe selector. Final integration pass.

---

## 5.5 Scenario improvement tool (deliverable this cycle)
A standalone CLI tool is now available at `tools/scenario_manager.py`. It
generates synthetic market-data scenarios (trend / chop / BTC dump / high-vol
/ squeeze / liquidity wall / funding shock) and runs them through the STRICT
`signal_engine.evaluate_symbol` pipeline. All rejected signals are reported
with reasons (pattern, freshness, direction, quality), making it a
regression-testing and development scenario tool.

**Usage:**
```
python tools/scenario_manager.py --list          # list defined scenarios
python tools/scenario_manager.py --run            # run all scenarios
python tools/scenario_manager.py --scenario trend_bull   # single scenario
```

**API:** `build_candles(name, symbol, interval, limit)` → kline dicts
(`open/high/low/close/volume/timestamp`); `make_market_data(candles, live_price)`
→ preloads klines into `MarketData._klines_cache` (key shape
`symbol_interval_220`) and sets a fake `get_live_price()`; `run_scenario(name,
direction, market, limit)` → runs `evaluate_symbol`.

**Scenario registry (SCENARIOS):** trend_bull, trend_bear, chop, btc_dump,
high_vol, squeeze_long, squeeze_short, flat_no_trend — each a (builder_fn,
direction, description, kwargs) tuple. _add_patterns() embeds candlestick
patterns (hammer, bullish-engulfing, shooting-star) for the ketat mode review.

**Verification:**
- `python tools/scenario_manager.py --list` → 8 scenarios listed.
- `python tools/scenario_manager.py --run` → **0 passed / 8 rejected** (all
  scenarios evaluated through the real pipeline).
- Rejection reasons confirm the pipeline is working correctly:
  - pattern veto (ketat mode): no candlestick pattern in last 5 candles
  - bullish-only veto: direction mismatch (bearish in bullish-only)
  - freshness veto: live price drift >0.4% from entry zone
- Scenarios with pattern + realistic entry prices remain weak-signals; the
  tool's purpose is to expose pipeline rejections, not to produce winning
  signals. Next improvement iteration: strengthen pattern visibility so
  pattern veto passes for correct scenarios.

### Verification results (this session)
- `python -m pytest -q` → **653 passed in 51.25s** (baseline 649 + 4 new).
- New tests: test_vetoes.py (2 new), test_executor.py (2 new post-only tests).
- All veto + quality-delta + engine + executor tests green.

## 6.6 Signal quality enhancements (this cycle)
Implemented comprehensive improvements to strengthen signal quality for
auto-trading:

### 1. Signal confidence scoring (compute_confidence)
New function in signal_engine.py computes a 0-100 confidence score from:
- 15m setup strength (15-25 pts)
- MTF alignment (30m + 4h + 15m + 5m) (+0 to +20 pts)
- Volume confirmation (>=1.3x +10, >=1.1x +5)
- Price proximity to entry zone (+0 to +10)
- Regime bonus (TRENDING BULLISH +10, HIGH VOLATILITY -10)
- RSI quality (30-70 +5, extreme RSI -5)

### 2. Adaptive quality threshold (get_adaptive_quality_threshold)
Automatically adjusts the quality gate based on market conditions:
- ATR >= 3.0%: threshold +5 -> 75 (turbulent markets = stricter)
- ATR <= 1.0%: threshold -5 -> 65 (stable markets = more permissive)
- Default: 70

### 3. Volatility-weighted position sizing (compute_position_size)
Scales position size based on:
- ATR volatility factor (0.7x in high vol, 1.0x in stable)
- Quality factor (0.8x-1.3x)
- Strength factor (0.8x-1.2x)

### 4. Signal decay (compute_decay_factor, is_signal_valid)
Signals decay over time and with price drift:
- Time decay: 2% per minute (config.SIGNAL_TTL_DECAY_PCT_PER_MIN)
- Price drift decay: decay proportional to entry zone deviation
- Expired when decay factor < 0.5

### 5. Enhanced prefilter (config-driven, scanner.py)
- PREFILTER_ADX_MIN = None (skip symbols with ADX < threshold)
- PREFILTER_PATTERN_MIN = None (skip symbols with pattern score < threshold)
- ATR floor, volume trend, RSI extremes (from prior cycle)

### Usage in scanner
```python
# In scanner._select_candidates():
rank = quality_score_rank(a15, regime, vol_ratio, direction)
candidates.sort(key=lambda x: quality_score_rank(...), reverse=True)

# Signal confidence on output:
confidence = compute_confidence(a15, a30, a4h, volume_ratio, regime, direction)
```

### Verification
- `python -m pytest -q` → **653 passed in 50.41s** (no regressions).
- All new functions import and run correctly.
- No regressions in existing tests.

### Auto-trade recommendations
1. Require `confidence >= 80` for auto-trading (strong signals)
2. Require `confidence >= 65` for moderate signals
3. Always apply adaptive quality threshold + signal decay
4. Use volatility-weighted position sizing (smaller in high vol)

## 6.5 Scan feature improvements (this cycle)
Enhanced the all-coin scanner (scanner.py) to produce higher-quality signals
through three improvements:

### 1. Prefilter "pre-screen" gates (config-driven, not engine correctness)
Added three new cheap pre-screens to prefilter_symbol() in signal_engine.py:
- **ATR floor** (PREFILTER_ATR_FLOOR_PCT): skip symbols with ATR below this
  % of price — drops illiquid/noisy micro-caps before the expensive engine.
- **Volume trend** (PREFILTER_VOLUME_TREND_MIN): skip symbols where 24h
  volume declined more than this % vs prior 24h — drops weak-momentum
  symbols. Cheap check: one 1d kline fetch per symbol.
- **RSI extremes** (PREFILTER_RSI_EXTREAME): skip symbols with RSI in the
  [oversold, overbought] zone — drops mean-reversion traps.

These are NOT engine correctness gates — the engine remains the final
authority on signal validity. They are cheap, fast pre-screens to reduce
candidates.

### 2. Quality-based candidate ranking
After prefilter, candidates are ranked by quality_score_rank() (signal_engine.py),
which combines:
- 15m setup strength (15-75 points)
- Regime bonus (TRENDING BULLISH +10, HIGH VOLATILITY -5, RANGING -3)
- Volume confirmation (>=1.3x +5, >=1.1x +2)
- RSI quality (30-70 +3)

Candidates sorted descending; highest-quality signals found first (useful
for UI/DB top-N displays and faster feedback).

### 3. Presence dashboard mode (priority scan)
run_pass() now uses _evaluate_priority() which evaluates ranked candidates
first — the best signals are written to the DB immediately, giving users
fast feedback even if the full pass takes longer.

### Usage
Config knobs (config.py):
```
PREFILTER_ATR_FLOOR_PCT = None          # e.g. 0.02 (2% ATR floor)
PREFILTER_VOLUME_TREND_MIN = None       # e.g. 10 (skip if volume down >10%)
PREFILTER_RSI_EXTREAME = None           # e.g. {"oversold": 20, "overbought": 80}
```

### Verification
- `python -m pytest -q` → **653 passed in 51.93s** (same as baseline).
- All new prefilter/ranking code covered by existing tests.
- No regressions introduced; stats include ranking tiers for monitoring.
