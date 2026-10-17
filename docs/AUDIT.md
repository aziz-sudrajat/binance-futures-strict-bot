# Technical Audit — GAS Reference Bot & Python STRICT Port

**Date:** 2026-10-14
**Type:** AUDIT ONLY — no code modified.
**Method:** `~/workspace/skills/quant-trading/SKILL.md` (audit procedure + layered validation).
**Reference:** `~/workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs` (8,659 lines, Google Apps Script)
**Target:** `~/workspace/binance-futures-bot/` Phase 1 (Python STRICT signal engine, paper mode)

> Priority order applied throughout: **DATA VALID > CAPITAL SAFETY > SIGNAL QUALITY > SYSTEM RELIABILITY > SIGNAL FREQUENCY.**

---

## 1. Actual architecture map of the GAS script

### 1.1 Subsystems

| Subsystem | Responsible functions (GAS line) | Data sources | Config params affecting decisions |
|---|---|---|---|
| Market-data ingestion | `getKlinesWithProvider_` (515), `getOkxClosedKlines_` (568), `parseOkxRows_` (644), `getBinanceKlines_` (543), `fetchOkxKlinesBatch_` (588), `tickersCached_` (463), `fetchAllSpotTickers_` (1605) | OKX `/api/v5/market/candles` (primary), Binance `/api/v3/klines` (fallback), OKX 24h ticker | `KLINES_LIMIT` (220), `klinesTtlSec_` per interval (418), `MIN_24H_VOLUME_USD` (2M), `DERIVATIVES_CACHE_SECONDS` |
| Indicators | `emaSeries_` (1648), `rsiWilder_` (1659), `macd_` (1701), `atrWilder_` (1712), `adx_` (1747), `bollinger_` (1786), `stochasticRsi_` (1798), `supertrend_` (1818), `obvSlope_` (1845), `moneyFlowIndex_` (1859) | Closed klines only (forming candle dropped: Binance `slice(0,-1)` L557; OKX `confirmed==='1'` filter L644) | `ATR_PERIOD` (14) |
| Setup analysis (15m) | `analyzeCandles_` (1904), `analyzeSymbol_` (1976), `buildTradePlan_` (1984) | Klines pack | `SIGNAL_MIN_SCORE` (5), `ENTRY_MAX_PULLBACK_ATR` (0.5), `ENTRY_ZONE_ATR` (0.1), `STOP_ATR_MIN/MAX` (1.5/3.0), `TP1/2/3_RR` (1.5/2.25/3.0), `SWING_LOOKBACK` (12) |
| Scoring | `smartCryptoScore_` (2026), `signalNowCryptoQuality_` (2031), `regimeBonus_` (1878), `classifyMarketRegime_` (1889) | Indicator outputs + trade plan | none (hardcoded weights) |
| MTF confirmation | `mtfConfirmPass_` (2091), `mtfConfirm4hPass_` (2074), `mtfTrendDirection_` (3144), `mtfCascadeCheck_` (3156) | 30m / 4h / 1d klines | `MTF_CONFIRM_MODE` ('LEAN' default), `MTF_CONFIRM_REQUIRED`, `MTF_CONFIRM_4H_BOOST/PENALTY` (6/10) |
| Chart patterns | `analyzeChartPatterns_` (3000), `detectDoubleTopBottom_` (2566), `detectTriangles_` (2624), `detectWedges_` (2685), `detectHeadShoulders_` (2735/2788), `detectRectangles_` (2833), `detectFlags_` (2883), `detectCandlestickPatterns_` (2953) | Fresh 15m klines | `PATTERN_MIN_CONFIDENCE` (55), `PATTERN_VETO_ENABLED`, `PATTERN_VETO_CONFIDENCE` (85) |
| Derivatives | `getDerivativesSnapshot_` (802), `calculateDerivativesScore_` (879), `getAdvancedDerivativesSnapshot_` (843), `derivGateAdvanced_` (994), `derivLiquidityWalls_` (1361), `getEstimatedLiquidationZones_` (1374) | Gate.io + Bitget + OKX futures (per-component failover via `derivSafe_` 722) | `DERIVATIVES_ENABLED`, `DERIV_LEVERAGE_RISK_VETO` (90), `DERIV_FUNDING_EXTREME` (0.001), `DERIV_SQUEEZE_RISK_VETO` (85), `DERIV_TOP_TRADER_VETO_LONG` (35), `DERIV_TOP_TRADER_EXTREME_LONG` (25), `LIQ_WALL_VETO_DISTANCE_PCT` (0.5), `LIQ_WALL_MIN_NOTIONAL_USD` (250k) |
| News sentiment | `fetchCryptoCoinNews_` (3454), `classifyCoinNewsSentiment_` (3483), `deepCheckNews_` (3437) | Google News RSS → Gemini 2.5-flash | `NEWS_SENTIMENT_VETO_ENABLED`, `NEWS_LOOKBACK_HOURS` (6), quota gate ≤17000 |
| Deep-check gate | `prePublishDeepCheck_` (3172), `deepCheckQualityScore_` (3532) | Re-fetched klines, derivatives, order book, news | `DEEPCHECK_ENABLED`, **`DEEPCHECK_FAIL_OPEN: true`**, `MIN_QUALITY_TO_POST` (55) |
| Publish gates | `publishSignal_` (5095), `freshnessCheck_` (5075), `canPostSignal_` (4614), `directionGate_` (2473), `canAutoPostNow_` (4641) | Script Properties (cooldown state), live spot tickers | `AUTO_POST_MINUTES` (45), `COIN_COOLDOWN_MINUTES` (120), `FRESHNESS_MAX_DRIFT_PCT` (0.4), `BULLISH_ONLY: true` |
| Publishers | `postToThreads_*` (4110+), `postToBinanceSquare_*` (4235+), `xCreatePost_` (4026), `sendTelegramChannelPost_` (4415) | — | per-platform enable flags |
| Lifecycle tracking | `recordSignalLifecycle_` (5758), `updateSignalLifecycle_` (5840), `notifyLifecycleChange_` (6024) | 5m lifecycle bars (`fetchCryptoLifecycleBars_` 1518) | `CRYPTO_SIGNAL_EXPIRY_HOURS` (24), `SIGNAL_LIFECYCLE_MAX_ACTIVE` (30) |
| Backtest | `maybeRunBacktestStep_` (6565), `backtestCryptoSymbol_` (6584), `formatBacktestSummary_` (6645) | Historical klines (same providers) | `BACKTEST_STEP_HOURS` (6) |
| Win-rate reporting | `getDailyWinrateReport_` (6238), `observedSignalR_` (6212) | Signals sheet | `DAILY_WINRATE_NEAR_TP_PCT` (80) |
| Quota | `quotaGuard_` (362), `quotaBump_` (318), `quotaFlush_` (343) | UrlFetch counter | `URLFETCH_HARD_STOP` (18500), warn at 16000 |
| Auto cycle | `runCryptoAutoCycleUnlocked_` (7761), `autoMarketCycle_` (7688), `scanActiveMarket_` (2112), `selectActiveSymbols_` (2154) | — | `AUTO_TIMEFRAME` (15m), `AUTO_MIN_STRENGTH` (3), `ACTIVE_SCAN_LIMIT` (100), `SIGNAL_FILTER_MODE` ('NORMAL') |

### 1.2 Exact signal check order (AUTO path, `runCryptoAutoCycleUnlocked_` → `publishSignal_`)

1. `scanActiveMarket_('15m')` — batch klines for universe (favorites + active-signal symbols + top movers + tracked, ≤100); `analyzeCandles_` per symbol; sort by `compareSignals_`.
2. Filter: `signal === 'BULLISH WATCH'` (BULLISH_ONLY) AND `strength >= 3` (`AUTO_MIN_STRENGTH`); sort by `smartCryptoScore_`.
3. Mode filter (`SIGNAL_FILTER_MODE`, default **NORMAL**):
   - **STRICT** → `mtfCascadeCheck_`: 4h trend BULLISH (via `mtfTrendDirection_`) → 1d trend BULLISH → 30m via `mtfConfirmPass_` (**which reads `MTF_CONFIRM_MODE='LEAN'` by default — see §3.2**).
   - **NORMAL** → 30m `analyzeSymbol_` + `mtfConfirmPass_` (LEAN) + 4h soft (`mtfConfirm4hPass_`, never vetoes).
   - **BASIC** → no MTF check at all.
4. `canAutoPostNow_` — global cadence (≥45 min since last auto post).
5. Per candidate: `canPostSignal_` (coin cooldown 120 min; near-identical setup guard) → `publishSignal_(a, false, 'AUTO')`.
6. Inside `publishSignal_`:
   a. `publicId` assigned; BULLISH_ONLY re-check.
   b. MTF re-verification per filter mode (L5110–5130; re-runs cascade in STRICT if not attached).
   c. NEUTRAL / missing-tradePlan rejection.
   d. `canPostSignal_` again; `directionGate_` (8 h repost cooldown, no-direction-change dedup) unless forced.
   e. `prePublishDeepCheck_` (if `DEEPCHECK_ENABLED`) → vetoes + quality gate (`quality < minQ`).
   f. `freshnessCheck_` — live ticker vs entry zone.
   g. Build caption + chart + hook image (quota permitting).
   h. Publish to Threads / Square / X / Telegram channel (any single success suffices; all-fail → throw).
   i. `savePostedSignal_` + `dirStateMarkPosted_` + admin summary message.

---

## 2. Veto & fail-open analysis

### 2.1 Complete veto catalog (conditions that REJECT a signal)

| # | Veto | Location | Notes |
|---|---|---|---|
| V1 | <80 candles / bad candle data | `analyzeCandles_` L1905 (throws) | Fail-closed. Good. |
| V2 | Not BULLISH WATCH / strength <3 | auto cycle L7768 | — |
| V3 | 30m BEARISH WATCH (all modes) | `mtfConfirmPass_` L2091 | In LEAN, only hard-bearish 30m fails |
| V4 | STRICT cascade: 4h or 1d not BULLISH | `mtfCascadeCheck_` L3156 | Real hard gate (trend legs) |
| V5 | Coin cooldown / near-identical setup / direction repost | `canPostSignal_` L4614, `directionGate_` L2473 | Dedup layer |
| V6 | Deep-check: NEUTRAL / no trade plan | `prePublishDeepCheck_` L3177 | — |
| V7 | Pattern veto (confidence ≥85, bearish-against) | `deepCheckPatterns_` via `PATTERN_VETO_ENABLED` | AI-free, deterministic |
| V8 | 30m confirm fails on re-check | `deepCheckMtf_` L3286 | — |
| V9 | Leverage-risk index ≥90 | `deepCheckDerivatives_` L3336 | Crowded-trade protection |
| V10 | Funding extreme against direction | L3344 | |
| V11 | Squeeze risk ≥85 against direction | L3355 | |
| V12 | Whale/top-trader extreme divergence | L3366 | |
| V13 | Liquidity wall ≥$250k within 0.5% blocking direction | `deepCheckLiquidity_` L3403 | Order-book based |
| V14 | Opposing news sentiment, impact ≥70 (Gemini) | `deepCheckNews_` L3437 | AI-driven veto |
| V15 | BTC dump >3%/1h | `deepCheckBtcBreadth_` L3634 | **Market-wide circuit breaker** |
| V16 | Quality < threshold (70/55/40) | `prePublishDeepCheck_` L3253 | Holistic bar |
| V17 | Price >0.4% above entry zone | `freshnessCheck_` L5075 | Stale-entry guard (longs only) |

### 2.2 Fail-open paths (signal passes DESPITE a failed check) — the critical section

**F1. Deep-check triple fail-open (CRITICAL).** `prePublishDeepCheck_` (L3172):
- Level 1: each of the 8 sub-checks is wrapped in its own try/catch → on exception, logged as `"... skipped"` and the signal **continues** (L3187, L3195, L3203, L3212, L3221, L3230, L3237, L3245).
- Level 2: the whole body is wrapped in an outer try/catch → on exception returns `out` whose `pass` defaults to **`true`** (L3258–3261).
- Level 3: the caller `publishSignal_` catches any throw from the gate and, because `DEEPCHECK_FAIL_OPEN: true` (CFG L71), **continues to publish** (L5159–5166).
- Net effect: **no failure mode of the validation gate can block publication.** A broken data feed, a throwing indicator, or a dead Gemini key all resolve to "pass".

**F2. Quality threshold lives inside the failing component (CRITICAL).** The `quality < minQ` gate (L3253) executes *inside* `prePublishDeepCheck_`. If the gate errors (F1 levels 2–3), the 70/55/40 quality bar is bypassed entirely — a quality-30 signal can publish.

**F3. Freshness guard fail-open on price-feed failure (CRITICAL).** `freshnessCheck_` L5075:
```js
} catch (e) { return { ok: true, skipped: 'price_unavailable' }; }
if (!last) return { ok: true, skipped: 'price_unavailable' };
```
If the ticker fetch throws or returns no price, the guard reports **ok:true** and the signal publishes with an unverified entry. (Direction of the check is also long-only by design: only drift *above* entryHigh vetoes.)

**F4. 4h "confirmation" never vetoes (HIGH, by design but misleading).** `mtfConfirm4hPass_` L2074: missing 4h data → `{pass:true}`; bearish 4h → `{pass:true, penalty:-10}`. Comment admits "SOFT: tidak pernah veto". Any reader trusting the name "confirmation" is misled; it is score-only.

**F5. News check silently disabled under load (HIGH).** `deepCheckNews_` runs only if `quotaTotal_() <= 17000` (L3216). When the bot is busiest (high quota burn), the news veto disappears — strictness is *inversely* correlated with load.

**F6. Volume gate placement (MEDIUM).** `MIN_24H_VOLUME_USD` (2M) filters the scan *universe* (`getTopMovers_` L1628), not each signal. A coin already in favorites/tracked bypasses the volume check at signal time.

**F7. `getMinQuality_` fallback chain (LOW).** `getSignalFilterMode_` returns NORMAL for any unrecognized value (fail toward the middle, not toward STRICT). Acceptable, but the default mode is NORMAL, not STRICT — the "STRICT is default" principle is not met in the reference.

---

## 3. Config audit

### 3.1 Filter-mode thresholds
- `SIGNAL_FILTER_MODE`: default `'NORMAL'` (CFG L88; `getSignalFilterMode_` L3131).
- `getMinQuality_` (L3137): **STRICT → 70**, **BASIC → 40**, otherwise `MIN_QUALITY_TO_POST` → **55**.
- Quality formula: `deepCheckQualityScore_ = 40 + strength×10 + scoreDelta(±40)`, clamped [5, 98] (L3532).
- Consequence: in NORMAL mode a strength-3 signal needs only scoreDelta ≥ −15 to publish (40+30−15=55, `qNow < minQ` is false at exactly 55). The bar is low.

### 3.2 The two "STRICT" knobs (naming collision — HIGH)
- `SIGNAL_FILTER_MODE='STRICT'` (L88): score threshold 70 + `mtfCascadeCheck_` path.
- `MTF_CONFIRM_MODE='STRICT'` (L86): 30m must be a full BULLISH WATCH. **Default is `'LEAN'`** = 30m not-bearish + bullish lean.
- `mtfCascadeCheck_` (L3156) calls `mtfConfirmPass_(c30)`, which reads `MTF_CONFIRM_MODE`. The code comment claims *"STRICT cascade: 4h → 1d → 30m harus bullish semua"* — but with the default LEAN setting, a **NEUTRAL 30m with bullScore ≥ bearScore passes** the cascade's 30m leg (boost +3). The STRICT cascade is therefore **not** all-bullish unless the operator also flips `MTF_CONFIRM_MODE`. Two configs, one misleading comment, divergent actual behavior.

### 3.3 Duplicate/conflicting configs
- `MIN_QUALITY_TO_POST` (55) vs `getMinQuality_` STRICT/BASIC overrides — coherent (tiered), but the override is invisible in CFG (hardcoded in function).
- `DERIV_TOP_TRADER_VETO_LONG` (35, penalty) vs `DERIV_TOP_TRADER_EXTREME_LONG` (25, veto) — tiered, coherent.
- `LIQ_WALL_VETO_DISTANCE_PCT` (0.5, veto) vs `LIQ_WALL_WARN_DISTANCE_PCT` (1.0, −8 penalty) — coherent in intent, but the warn penalty is **silently dropped** (see M1).
- `SIGNAL_MIN_SCORE` (5) + `bull - bear >= 2` margin — coherent.
- `AUTO_MIN_STRENGTH` (3) vs backtest `strength >= 4` (L6592) — **inconsistent**: the backtest evaluates a stricter subset than production publishes (§6.4).

---

## 4. Scoring audit

### 4.1 What the scores actually are
- `smartCryptoScore_` (L2026) = `strength×20` (≤100) + `min(15, volumeRatio×6)` + `regimeBonus` (−4…+12) + `min(10, rr×4)` − `warning?5:0`. Typical max ≈ 137. **Used only for ranking candidates**, not gating.
- `signalNowCryptoQuality_` (L2031) = `round(smartScore / 1.37)`, clamped [0,100]. The divisor 1.37 is arbitrary ("normally tops out around the 130s" — code comment).
- `deepCheckQualityScore_` (L3532) = `40 + strength×10 + scoreDelta(±40)`, clamped [5,98]. **This is the gating score.**
- `calculateDerivativesScore_` (L879) = base 50 ± fixed rule deltas (crowding ±10/±3, funding ±10/±2, OI ±8/±4, taker ±6), clamped [0,100].

### 4.2 What the score is NOT
It is **not a calibrated win probability**. Nothing in the pipeline estimates P(win): no historical hit-rate per setup, no logistic calibration, no out-of-sample validation of the score→outcome mapping. It is a **heuristic rank** measuring: indicator agreement count, volume participation, regime alignment, trade-plan RR, and (via deltas) derivatives/pattern/news modifiers. Correlated inputs are double-counted as independent evidence (EMA9>EMA21, EMA21>EMA50, price>EMA21, EMA21-rising are four points for one trend fact; MACD-histogram>0 is largely the same fact again). The 0–100 presentation invites misreading as a probability — it must never be labeled or used as one.

---

## 5. Call-graph verification (5 mechanisms)

| # | Mechanism | Called? | Affects publish decision? | Verdict |
|---|---|---|---|---|
| 1 | **Liquidity walls** `derivLiquidityWalls_` (1361) ← `deepCheckLiquidity_` (3403) ← `prePublishDeepCheck_` | Yes | **Partially.** The <0.5% veto path works. But the 0.5–1.0% warn branch does `r.delta -= 8` on an object with **no `delta` field** (→ `NaN`), and `prePublishDeepCheck_` never adds `lc.delta` to `scoreDelta` anyway — the penalty is dead code twice over. | Veto real; warn penalty silently dropped (bug M1) |
| 2 | **Liquidation zones** `getEstimatedLiquidationZones_` (1374) | Yes — once, at L851 inside `getAdvancedDerivativesSnapshot_` | **No.** The result is stored as `liquidationMap` and **never read anywhere** in the codebase. The "model" is synthetic anyway: `intensity = (longPct/50)×(1+funding×800)×volFactor` at fixed distances — heuristic, not exchange liquidation data. | Dead computation; burns quota (2 klines fetches + snapshot per deep check) |
| 3 | **News sentiment veto** `classifyCoinNewsSentiment_` (3483) ← `deepCheckNews_` (3437) | Yes, iff `NEWS_SENTIMENT_VETO_ENABLED` **and** `quotaTotal_() ≤ 17000` | Yes — vetoes on opposing sentiment with AI `impact ≥ 70`. Caveats: impact is an uncalibrated LLM number; the prompt **reveals the signal direction** to the classifier (bias risk); AI failure → NEUTRAL fallback (no veto); skipped under high quota (F5). | Real but fragile; AI-driven hard veto |
| 4 | **Signal lifecycle** `updateSignalLifecycle_` (5840) ← `runLifecycleCheck` (6796, time trigger) | Yes | No — post-hoc tracking only, correctly separated from the publish decision. Well-designed: ignores pre-signal candles, AMBIGUOUS on same-candle TP/SL, never marks EXPIRED on provider failure, cursor-based resume. | Working as intended |
| 5 | **Quota guard** `quotaGuard_` (362) | Yes — inside `httpFetchCounted_` (373), batch fetch (383, 602), lifecycle bars (1538), tickers (1608) | Yes — throws at 18,500/20,000 daily UrlFetch; callers degrade (scan aborts gracefully; deep-check sub-checks catch → "skipped" → fail-open per F1). | Real enforcement; interacts with fail-opens |

---

## 6. Backtest audit (`backtestCryptoSymbol_` L6584)

**What's right:** point-in-time signal formation (`analyzeCandles_` on `c.slice(0,i+1)`, evaluated forward from `i+1`) — no look-ahead in the setup detection; `blockUntil` prevents overlapping paper positions; entry requires zone touch (unfilled ≠ win).

**What's wrong:**
1. **Win-rate denominator excludes non-decisive outcomes.** `winRate = wins/(wins+losses)`; AMBIGUOUS, timeouts (no TP/SL within 12 candles), and unfilled signals are dropped. Timeouts are the most flattering exclusion — a real trade held 12 candles without resolution would be closed at market, usually a scratch or loss.
2. **No costs.** No fees, no slippage, no funding. Wins credited at full 1.5R.
3. **Production mismatch.** Backtest filters `strength >= 4` (L6592) while production auto-posts at `strength >= 3`; backtest applies **no** MTF confirm, no deep check, no freshness guard, no quality gate — and evaluates BEARISH signals while production is bullish-only. It does not test what production does.
4. **Tiny in-sample, no OOS.** 220 candles, step 3, one symbol per 6 h run; parameters were tuned on the same regime. No walk-forward, no out-of-sample, no cross-regime reporting.
5. **Same-candle rule is exclude-not-conservative.** AMBIGUOUS trades are dropped rather than resolved conservatively (e.g., assume SL first).
6. **Daily win-rate report** (`getDailyWinrateReport_` L6238): `winRate = tpHit/(tpHit+stoppedBeforeTp)` — EXPIRED signals are excluded from the decisive denominator while still dragging `avgR`. Published win rates are flattered by construction.
7. **Lifecycle R accounting** (`observedSignalR_` L6212): credits full 1.5/2.25/3.0R on TP touch, assuming the whole position rode to target — overstates realized R under any scaled-exit scheme.

**Bottom line:** no win-rate or profit figure derived from this backtester may be cited as evidence. It is a smoke test, not a validation.

---

## 7. Python port fidelity audit

### 7.1 Function-by-function comparison

| Python | GAS original | Fidelity |
|---|---|---|
| `indicators.ema_series` | `emaSeries_` L1648 | **Exact** (values[0] seed, α=2/(p+1)) |
| `indicators.rsi_wilder` | `rsiWilder_` L1659 | **Exact** (Wilder smoothing, 50/100 guards) |
| `indicators.macd` | `macd_` L1701 | **Exact** |
| `indicators.atr_wilder` | `atrWilder_` L1712 | **Exact** |
| `indicators.adx` | `adx_` L1747 | **Exact** (loop/smoothing structure matches) |
| `analyze_candles` | `analyzeCandles_` L1904 | **Exact** (point counting, strength map, warning bands, volume-ratio window) |
| `build_trade_plan` | `buildTradePlan_` L1984 | **Exact** (entry/stop/TP math, ATR guard) |
| `regime_bonus` / `classify_market_regime` | L1878 / L1889 | **Exact** |
| `smart_crypto_score` / `signal_quality_rank` | L2026 / L2031 | **Exact** (rank unused in pipeline — display only) |
| `derivatives_score` | `calculateDerivativesScore_` L879 | **Exact** for the base formula |
| `mtf_trend_direction` | `mtfTrendDirection_` L3144 | **Exact** (incl. 55-close minimum → NEUTRAL) |
| `mtf_confirm_strict` | `mtfConfirmPass_` STRICT branch L2097–2101 | **Exact** for that branch |
| `mtf_cascade_check` | `mtfCascadeCheck_` L3156 | **Exact** structurally |
| `quality_score` | `deepCheckQualityScore_` L3532 + clamp L3250 | Formula exact; **delta source simplified** (see D2) |
| `data.get_klines` / `_okx_klines` / `_binance_klines` | L515 / L568 / L543 | **Exact** (OKX `confirmed` filter, Binance drop-last-candle, TTLs, limit math) |
| `data.get_derivatives_snapshot` | `getDerivativesSnapshot_` L802 | **Diverged**: per-*provider* failover (Binance-all → Gate-all) vs GAS per-*component* failover across Gate+Bitget+OKX (see D4) |
| `data.normalize_symbol` | `normalizeSymbol_` L793 | **Exact** |

### 7.2 Divergences and porting issues

**D1. Freshness uses stale analysis price, not a live price (CRITICAL).**
GAS `freshnessCheck_` fetches a *fresh* spot ticker and vetoes if price ran >0.4% above the entry zone. Python `evaluate_symbol` step 6 compares `a15["price"]` — the close of the last *closed* 15m candle, up to ~15 min old plus up to 8 min cache TTL. A +2% rip in the last 10 minutes passes the Python freshness gate. The existing test (`test_evaluate_symbol_stale_entry_rejected`) mocks `analyze_candles` to inject the stale price — it validates the diverged behavior, not the GAS behavior. **Fix:** add a live last-price fetch to `data.py` (the OKX ticker call already exists for volume) and fail-closed when unavailable.

**D2. Quality gate is nearly non-binding (HIGH).**
Python `delta = +8 (30m) + 8 (cascade) + round((deriv−50)×0.4)`. Because reaching the quality step *requires* the cascade to pass, the +16 is a constant for every evaluated signal — not a differentiator. Quality becomes `56 + strength×10 ± 20`: strength-3 → 86±20, strength-4 → 96±20. The 70 threshold rejects almost nothing. GAS, by contrast, accumulated *real negative* deltas (leverage −10, top-traders −12, liquidation-event −15, squeeze −8, H1 penalty −10, BTC penalty −15, pattern penalties) that could drag a passing setup below 70. **Fix:** port the negative-delta vetoes/penalties (D3) or re-derive the threshold from the new delta distribution; document that 70 is a rank cutoff, not a probability.

**D3. Deep-check hard vetoes not ported (HIGH).**
Missing from the Python pipeline: leverage-risk ≥90 veto, funding-extreme-against veto, squeeze-risk ≥85 veto, whale-divergence veto, liquidity-wall veto (V9–V13), BTC-dump circuit breaker (V15), pattern veto (V7). The only surviving derivatives signal is the soft `calculateDerivativesScore_` formula. For a *trading* system these are precisely the capital-protection vetoes. **Fix:** port V9–V13 and V15 as hard gates (deterministic, no AI); keep pattern/news vetoes out or score-only until calibrated.

**D4. Derivatives failover granularity (MEDIUM).**
GAS averages per-component successes across providers (e.g., funding from OKX + OI from Gate). Python takes all-or-nothing per provider, and partial Binance data is used *without* Gate supplementation. Worse: total derivatives failure → all aggregates `None` → score 50 (neutral) → `deriv_delta = 0` → signal can pass with **zero derivatives visibility** — the same fail-open exists in GAS (`calculateDerivativesScore_` treats missing as neutral), but for STRICT trading, missing positioning data should *hold* the signal, not neutralize it. **Fix:** require at least funding + long/short ratio present, else `(None, "derivatives unavailable")`.

**D5. MTF_CONFIRM_MODE hardcoded STRICT — stricter than GAS default (MEDIUM, direction: safer).**
`config.py` sets `MTF_CONFIRM_MODE = "STRICT"` while GAS defaults to `'LEAN'` (CFG L86) — and per §3.2, the GAS STRICT cascade's 30m leg actually runs in LEAN unless the operator changed it. The Python engine therefore rejects neutral-lean 30m setups that the GAS bot would publish. This is the *right* choice for the trading mandate, but `config.py`'s comment cites "GAS L86" (the LEAN line) for the STRICT behavior — a misleading citation. **Fix:** correct the citation; state the divergence explicitly in README (it currently implies parity: "the same strictness as the GAS bot").

**D6. Volume gate fail-open (MEDIUM).**
`if vol is not None and vol < MIN: reject` — unavailable volume data passes. GAS applies the floor at universe-selection time. For trading, an illiquid coin with a failed ticker call is exactly what should not be traded. **Fix:** fail-closed (see test T12).

**D7. Fixed 8-symbol universe vs GAS dynamic universe (MEDIUM).**
GAS scans up to 100 symbols (favorites + active-signal symbols + top movers + tracked). Python scans 8 majors. Fewer, more-liquid symbols is defensible for trading, but the opportunity set — and therefore any signal-frequency comparison with GAS — differs. Note: GAS's top-mover inclusion is momentum-chasing selection; a fixed liquid universe avoids that bias.

**D8. scan.py emits all passing signals per scan; GAS posts max 1 per cycle (LOW).**
Executor-side `MAX_CONCURRENT_POSITIONS` must absorb bursts. Already in config; must be enforced in Phase 2.

**D9. Geo-blocked primary endpoints (LOW, operational).**
`api.binance.com` and `fapi.binance.com` return HTTP 451 from this VM, so every klines/derivatives call pays a failed Binance attempt before fallback (OKX / Gate.io). Correctness is preserved via fallback; latency is not. Consider making the provider order environment-aware.

**D10. `signal_quality_rank` (÷1.37) is dead code in the pipeline (INFO).**
Kept for display; harmless, but the arbitrary divisor should carry a "not a probability" comment wherever surfaced.

### 7.3 Assessment of the documented simplifications (README)
The README's simplification list is accurate as far as it goes, but it **understates** D1 (freshness), D2 (non-binding gate), and D5 (stricter-than-GAS MTF). The claim "scans the market with the same strictness as the GAS bot" is not supported: the Python engine is *stricter* on MTF, *looser* on quality/derivatives/news/liquidity, and *diverged* on freshness. The honest statement: "same indicator math and cascade structure; different (and currently weaker) risk-veto coverage."

---

## 8. Prioritized bug & risk list

### CRITICAL
- **C1 — Deep-check triple fail-open (GAS L3172, L5159; CFG L71).** No failure of the validation gate can block publication. *Trading impact:* the entire pre-trade validation layer is optional at runtime. *Fix:* invert to fail-closed; distinguish mandatory-check failure (reject) from optional-service failure (degrade with annotation). The Python engine must be built fail-closed from the start (it currently is, except D1/D4/D6).
- **C2 — Freshness fail-open on price-feed failure (GAS L5075).** `price_unavailable` → `ok:true`. *Fix:* fail-closed.
- **C3 — Python freshness uses stale candle close (signal_engine.py `evaluate_symbol` step 6).** Diverges from GAS's live-ticker design. *Fix:* live price fetch + fail-closed (test T1).
- **C4 — Quality threshold bypassed when the gate errors (GAS L3253 inside try).** The 70/55/40 bar is inside the component that can fail open. *Fix:* evaluate the quality gate outside the fallible component.
- **C5 — No market-wide circuit breaker in Python port.** GAS has the BTC-dump veto (V15); the port has nothing. *Fix:* port `deepCheckBtcBreadth_` logic (BTC 1h change ≤ −3% → veto; ≤ −2% → penalty/hold).

### HIGH
- **H1 — Python quality gate non-binding (D2).** 70 threshold rarely rejects. *Fix:* port negative deltas or recalibrate.
- **H2 — MTF "STRICT" naming collision (GAS L86 vs L88; §3.2).** The STRICT cascade's 30m leg runs LEAN by default. *Fix:* in Python, keep hardcoded STRICT and document the deliberate divergence (D5).
- **H3 — Derivatives/liquidity vetoes not ported (D3).** Crowded-trade protections missing. *Fix:* port V9–V13 as hard gates.
- **H4 — Backtest cannot validate anything (§6).** Denominator exclusions, no costs, production mismatch, tiny in-sample, no OOS. *Fix:* rebuild the backtester to production parity before any performance claim (roadmap step 5).
- **H5 — Published win-rate flattered by construction (GAS L6238).** EXPIRED excluded from decisive denominator. *Fix:* report win-rate over all resolved signals; disclose each bucket.
- **H6 — Derivatives total failure → neutral → pass (GAS + Python, D4).** *Fix:* require minimum derivatives coverage.
- **H7 — Backtest/production strength mismatch (GAS L6592 vs L98).** Backtest uses ≥4, production ≥3.

### MEDIUM
- **M1 — `deepCheckLiquidity_` NaN/dead −8 penalty (GAS L3403).** `r.delta` never initialized; never added to `scoreDelta`.
- **M2 — `liquidationMap` computed but never read (GAS L851).** Dead quota burn; the "model" is synthetic anyway.
- **M3 — 4h confirm pass-on-missing-data (GAS L2074).** Documented soft; ensure no one mistakes it for confirmation.
- **M4 — AI news veto: uncalibrated impact, direction-revealing prompt, quota-gated (GAS L3216, L3483).** Strictness weakens under load.
- **M5 — Volume gate fail-open in Python (D6).**
- **M6 — Fixed vs dynamic universe (D7).** Document the opportunity-set difference.
- **M7 — Lifecycle R assumes full-position TP (GAS L6212).** Overstates realized R under scaled exits.
- **M8 — API keys-in-source guidance in GAS comments (multi-AI router header).** Key-hygiene risk.
- **M9 — Misleading `config.py` citation ("GAS L86" for STRICT behavior).**
- **M10 — 0–100 scores presented without "not a probability" guardrails.**

---

## 9. Target architecture recommendation

Keep the Python engine's fail-closed skeleton (`evaluate_symbol` returns `(None, reason)` on any gate/data failure — this is already better than the GAS reference). Add the missing capital-protection layer rather than the publishing machinery:

```
Data (OKX→Binance klines; Binance→Gate derivatives; live ticker)
  → Layer A data-integrity (closed candles, ≥80, no NaN, cross-TF consistency)
  → Layer B liquidity (24h volume ≥ $2M, fail-closed; futures instrument listed)
  → Layer C regime (keep classify_market_regime; reject on HIGH_VOLATILITY for entries)
  → Layer D MTF cascade (4h+1d trend, 30m BULLISH WATCH — hardcoded STRICT, documented)
  → Layer E confluence (analyze_candles as-is; indicator math verified exact)
  → Layer F derivatives HARD vetoes (port V9–V13: leverage≥90, funding-extreme,
       squeeze≥85, whale-divergence, liq-wall <0.5%) + soft score
  → Layer G event risk (port BTC-dump breaker V15; news-AI stays OUT until calibrated)
  → Layer H entry quality (LIVE price vs zone, ≤0.4% above; fail-closed)
  → Layer I decision (quality gate with real negative deltas; full audit record
       per signal: all gate inputs/outputs/reasons → JSONL)
```

Explicitly **do not port**: autopost publishers, chart/hook image generation, Telegram admin, X/Square publishers, hour-boost (self-referential), AI news veto (until calibrated), synthetic liquidation-zone model (dead output).

### Prioritized roadmap
1. **Fail-closed hardening** — live-price freshness (C3), derivatives-coverage requirement (H6), volume fail-closed (M5). (C1/C2/C4 are GAS-only; Python must not regress.)
2. **Port deterministic vetoes** — V9–V13, V15 (H3, C5).
3. **Recalibrate quality gate** — real negative deltas; re-derive threshold (H1); correct M5/D5 docs (M9).
   ✅ DONE 2026-10-14: `quality_deltas.py` ports every deep-check scoreDelta
   contributor as an explicit, named, tested component (mtf 30m/4h, h1, BTC
   warn, leverage/funding/squeeze/whale/smart-money/liq-event/liq-wall warns;
   GAS L3172-3262). The old synthetic blend is gone; the GAS liq-warn NaN bug
   (M1) is deliberately not reproduced. Threshold study
   (`tools/threshold_study.py`, 14d × 5 majors, n=45): keep 70 (see README
   "Threshold decision"). 83 tests green incl. T7 boundaries.
4. **Honest backtester** — production-parity gates, costs/slippage/funding, conservative same-candle rule, timeouts resolved at market, walk-forward + OOS (§6 → roadmap step 5).
5. **Cross-validation** — Python signals vs GAS signals, 2–4 weeks paper (no live capital).
6. **Phase 2 executor** — sizing, kill switch, idempotency, reconciliation (already scoped).
7. **Lifecycle accounting** — partial-exit-aware R (M7); daily reports with full buckets (H5).

### Concrete test-case list
- **T1.** Freshness with live price: ticker +1%/−1% vs entryHigh → reject/accept; ticker failure → **reject** (fail-closed).
- **T2.** Derivatives snapshot total failure → signal held with reason (not passed-neutral).
- **T3.** BTC 1h −3% → all symbols vetoed; −2% → penalty/hold.
- **T4.** Funding extreme against direction → veto; leverage-risk ≥90 → veto; squeeze ≥85 → veto; whale divergence → veto; $250k wall <0.5% → veto.
- **T5.** 30m NEUTRAL (bull-lean, bullScore≥bearScore) → cascade **rejects** (locks the STRICT divergence vs GAS LEAN).
- **T6.** 4h/1d non-bullish → reject at the correct stage with the correct reason string.
- **T7.** Quality boundary: deriv −20 → quality 66/76/86 for strength 3/4/5 → gate behaves per recalibrated spec.
- **T8.** Backtest honesty on fixtures: ambiguous candle → conservative resolution; timeout → market close; 0.1% cost applied → metrics reproducible.
- **T9.** Duplicate scan of the same setup → single paper-log entry / single position intent.
- **T10.** Indicator parity: Python vs GAS formulas on randomized candle fixtures (tolerance 1e-9).
- **T11.** Cache staleness: expired TTL → refetch; forming candle never in analysis set.
- **T12.** Volume: below $2M → reject; volume unavailable → reject (fail-closed).
- **T13.** MTF data exception (e.g., 1d fetch fails) → reject, never pass.
- **T14.** LONG signal math invariants: SL < entry < TP1 < TP2 < TP3; risk > 0; no NaN/Inf in plan.

---

## Principles (unchanged)

**NO VALID SETUP, NO SIGNAL. NO VALID DATA, NO DECISION. NO VERIFIED RESULTS, NO PERFORMANCE CLAIMS. NO RISK CONTROL, NO LIVE TRADING.**
