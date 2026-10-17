# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Phase 2 testnet executor.

Polls the hardened STRICT signal engine (signal_engine.evaluate_symbol —
unchanged) and turns APPROVED signals into Binance USD-M Futures orders:

  entry:  LIMIT buy at the signal entry (MARKET if the live price is already
          inside the entry zone)
  SL:     STOP_MARKET (reduceOnly, MARK_PRICE) for the full quantity
  TP1-3:  LIMIT sells (reduceOnly) splitting qty per config.TP_SPLIT
  After TP1 fills and EXEC_BREAKEVEN_AFTER_TP1: SL moves to entry (breakeven).

Safety properties (see ~/workspace/skills/quant-trading/SKILL.md):
  - idempotent per signal (symbol + signal-candle close time + direction)
  - freshness re-checked with a LIVE price immediately before ordering
  - fail-closed on any missing data, failed exchange call, or risk breach
  - kill switch, max positions, per-symbol exposure cap (risk.py)
  - testnet ONLY: base URL defaults to the Binance Futures testnet

Module map (item 3.5 code-smell note — deliberately NOT split: this is
the live-money core and a mechanical split risks regressions for zero
behavioral gain; the sections below are the documented boundaries):
  - Executor.__init__ / _dash_settings / _effective_* / _resolve_leverage
      (~270-383): construction + per-cycle effective risk resolution
  - scan_once / scan_from_db / _apply_tradability_prefilter / _scan_symbol
      (~384-578): signal intake (DB + direct), prefilter, per-symbol eval
  - manual_entry / preview_manual_entry (~579-683): operator-initiated entry
  - _try_open / _reject (~684-827): the ONE order-placement path
      (freshness re-check, risk gates, idempotency key)
  - _manage_positions / _manage_one (~828-963): per-cycle position upkeep
  - _check_entry_fill / _expire_stale_entry (~964-1014): entry lifecycle
  - _place_brackets / _sl_cancel / _sl_status / _move_sl_to_breakeven
      (~1015-1120): bracket orders (SL + TP1-3)
  - _classify_vanished_close / _close_position / _mark_pending_close /
    _flat_position / _take_profit / _trail_check /
    _close_reconciled_positions (~1121-1481): exits, TP/SL/trailing,
      orphan reconciliation
  - _entry_filled / run_loop (~1482-end): main loop, graceful shutdown,
      heartbeat, state flush
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

import config
import wsfeed
from lifecycle import LifecycleLogger
from lifecycle import (
    GENERATED, APPROVED, REJECTED, ENTRY_TRIGGERED, POSITION_OPENED,
    TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT, MANUAL_CLOSE, EXPIRED, CANCELLED,
    PROFIT_TAKE, TRAIL_STOP, TIME_STOP,
)
from risk import floor_to_step, quantize_price
log = logging.getLogger("executor")

_FEE_PCT = 0.05  # fee estimate per fill for PnL accounting (matches backtest)
_ENTRY_TIMEOUT_MINUTES = 30  # cancel an unfilled entry limit order after
                            # this long (user 2026-10-16; was 6h)
_CLOSE_DUST_QTY = 1e-9  # residual below this after a market close counts as
                       # fully closed (float noise, not a real position)


def _signal_age_hours(row: dict) -> float:
    """Age of a signals.db row in hours, from its created_at (ms).

    Missing/unparseable created_at -> +inf (fail-closed: treated as stale,
    never executed). Per quant discipline: unverifiable data = no decision.
    """
    try:
        created_ms = float(row.get("created_at") or 0)
    except (TypeError, ValueError):
        created_ms = 0.0
    if created_ms <= 0:
        return float("inf")
    age_h = (time.time() * 1000.0 - created_ms) / 3_600_000.0
    return max(0.0, age_h)  # clock skew (created_at in the future) -> 0


def signal_candle_close_ms(market, symbol: str) -> int:
    """Close time (ms) of the last CLOSED 15m candle — the signal candle.

    Prefers the klines feed (same data evaluate_symbol used); falls back to
    wall-clock 15m alignment when the feed lacks close_time (e.g. fakes).
    """
    try:
        klines = market.get_klines(symbol, "15m")
        if klines:
            ct = klines[-1].get("close_time")
            if ct:
                return int(ct)
    except Exception:  # noqa: BLE001 - fallback below
        pass
    now_ms = int(time.time() * 1000)
    return (now_ms // 900_000) * 900_000 - 1


def make_signal_id(symbol: str, close_ms: int, direction: str) -> str:
    return f"{symbol}:{close_ms}:{direction}"


def compute_qty(balance: float, entry: float, stop: float,
                filters: dict, risk_pct: float | None = None,
                atr: float | None = None) -> tuple[float, str]:
    """Risk-based sizing. Returns (qty, note); qty == 0 means skip.

    qty = (balance * RISK_PCT_PER_TRADE/100) / |entry - stop|, floored to
    stepSize, honoring minQty / minNotional and the per-symbol exposure cap.
    risk_pct overrides the config default (dashboard may only tighten it).
    When VOLATILITY_SCALING_ENABLED is True and atr is provided, dynamically
    scales down risk under high or extreme ATR % regimes.
    """
    if balance is None or balance <= 0:
        return 0.0, "balance unavailable"
    rp = config.RISK_PCT_PER_TRADE if risk_pct is None else risk_pct
    scale_note = ""
    if getattr(config, "VOLATILITY_SCALING_ENABLED", True) and atr is not None and entry > 0:
        try:
            atr_f = float(atr)
            if atr_f > 0:
                atr_pct = (atr_f / entry) * 100.0
                extreme_th = getattr(config, "VOLATILITY_EXTREME_ATR_PCT", 5.0)
                high_th = getattr(config, "VOLATILITY_HIGH_ATR_PCT", 3.5)
                if atr_pct >= extreme_th:
                    f = getattr(config, "VOLATILITY_SCALE_EXTREME_FACTOR", 0.50)
                    rp *= f
                    scale_note = f" (volatility scaled x{f:.2f}: atr {atr_pct:.2f}% >= {extreme_th:.1f}%)"
                elif atr_pct >= high_th:
                    f = getattr(config, "VOLATILITY_SCALE_HIGH_FACTOR", 0.75)
                    rp *= f
                    scale_note = f" (volatility scaled x{f:.2f}: atr {atr_pct:.2f}% >= {high_th:.1f}%)"
        except (TypeError, ValueError):
            pass
    risk_usdt = balance * rp / 100
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        return 0.0, "zero stop distance"
    step = filters.get("stepSize", 0.0) or 0.0
    min_qty = filters.get("minQty", 0.0) or 0.0
    min_notional = filters.get("minNotional", 0.0) or 0.0
    qty = floor_to_step(risk_usdt / per_unit, step)
    # Per-symbol exposure cap.
    cap_notional = balance * config.EXEC_MAX_EXPOSURE_PCT_PER_SYMBOL / 100
    if qty * entry > cap_notional:
        qty = floor_to_step(cap_notional / entry, step)
    if qty < min_qty:
        return 0.0, (f"qty {qty} < minQty {min_qty} "
                     f"(risk ${risk_usdt:.2f}{scale_note}, stop dist {per_unit})")
    if qty * entry < min_notional:
        return 0.0, (f"notional ${qty * entry:.2f} < minNotional "
                     f"${min_notional:.2f}")
    return qty, (f"risk ${risk_usdt:.2f}{scale_note} / stop-dist {per_unit} -> qty {qty}")


def compute_test_qty(fixed_margin_usdt: float, leverage: int, entry: float,
                     filters: dict) -> tuple[float, str]:
    """Fixed-margin sizing. Returns (qty, note); qty == 0 means skip.

    notional = fixed_margin_usdt * leverage; qty = notional / entry, floored
    to stepSize, honoring minQty / minNotional. The per-symbol exposure cap
    and the other risk gates still apply downstream in risk.check_can_open.
    """
    if fixed_margin_usdt is None or fixed_margin_usdt <= 0:
        return 0.0, "fixed margin must be > 0"
    if leverage is None or leverage <= 0:
        return 0.0, "leverage must be > 0"
    if entry is None or entry <= 0:
        return 0.0, "entry must be > 0"
    step = filters.get("stepSize", 0.0) or 0.0
    min_qty = filters.get("minQty", 0.0) or 0.0
    min_notional = filters.get("minNotional", 0.0) or 0.0
    notional = fixed_margin_usdt * leverage
    qty = floor_to_step(notional / entry, step)
    if qty < min_qty:
        return 0.0, (f"qty {qty} < minQty {min_qty} "
                     f"(margin ${fixed_margin_usdt:.2f} x{leverage})")
    if qty * entry < min_notional:
        return 0.0, (f"notional ${qty * entry:.2f} < minNotional "
                     f"${min_notional:.2f}")
    return qty, (f"test margin ${fixed_margin_usdt:.2f} x{leverage} -> "
                 f"notional ${notional:.2f} -> qty {qty}")


def split_tp_qty(qty: float, step: float, min_qty: float,
                 ratios: tuple = config.TP_SPLIT) -> tuple[list[float], str] | tuple[None, str]:
    """Split qty across TP1..TP3 per ratios (floored to step, sum preserved).

    A level that would fall below minQty is merged into the next level; if
    the final level is still below minQty the trade is un-exitable -> abort.
    """
    splits = [floor_to_step(qty * r, step) for r in ratios]
    # Preserve as much as possible on the last leg (absorbs floor dust);
    # the last leg is floored too so every leg is step-aligned, and any
    # sub-step dust (< step) simply stays out of the TP ladder.
    splits[-1] = floor_to_step(qty - sum(splits[:-1]), step)
    merged: list[float] = []
    carry = 0.0
    for i, s in enumerate(splits):
        s += carry
        carry = 0.0
        if s < min_qty and i < len(splits) - 1:
            carry = s  # merge into the next TP level
            continue
        merged.append(s)
    # Pad merged back to 3 legs with zeros for dropped levels.
    while len(merged) < 3:
        merged.append(0.0)
    if merged[2] < min_qty:
        return None, (f"TP3 slice {merged[2]} < minQty {min_qty} "
                      f"- cannot exit cleanly, aborting trade")
    return merged, f"tp splits {merged}"


def select_top_movers(tickers: list[dict], n: int,
                      min_vol_usd: float) -> list[str]:
    """GAS parity: getTopMovers_ — top-N by |24h %| * log10(24h USD vol).

    Stablecoins are never candidates (GAS CFG STABLECOINS); quiet coins
    below min_vol_usd are cut. Returns Binance-style symbols, best first.
    """
    import math
    stables = set(config.STABLECOINS)
    scored = []
    for t in tickers or []:
        base = str(t.get("base") or "")
        if base in stables:
            continue
        vol = float(t.get("vol_usd_24h") or 0)
        if vol < min_vol_usd:
            continue
        chg = abs(float(t.get("chg_pct_24h") or 0))
        scored.append((chg * math.log10(vol + 1), t.get("symbol")))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in scored[:max(1, n)]]


def resolve_universe(exchange, settings: dict,
                     curated: list[str] | None = None,
                     market=None) -> list[str]:
    """Universe for this scan cycle.

    - "curated" (default): the built-in liquid list (config.UNIVERSE).
    - "top_volume": top-N symbols by 24h quote volume from the exchange.
    - "top_movers": GAS parity — top-N by |24h %| * log10(volume) from OKX
      perpetual tickers (stables excluded). Needs `market` (MarketData).
    - "all": every USDT-margined OKX perpetual (~hundreds of symbols).
      Needs `market`. One bad symbol never kills a scan.
      Any fetch failure falls back to the curated list (fail-safe: keep
      trading the known coins instead of scanning nothing).
    """
    curated = list(curated if curated is not None else config.UNIVERSE)
    mode = settings.get("universe_mode", "curated")
    if mode not in config.UNIVERSE_MODES:
        mode = "curated"
    if mode == "curated":
        return curated
    if mode == "all":
        # Full sweep: every USDT-margined OKX perpetual.
        try:
            syms = market.list_okx_swap_symbols() if market is not None \
                else []
            syms = [s for s in syms
                    if s[:-4] not in config.STABLECOINS] if syms else []
        except Exception as e:  # noqa: BLE001 - fallback, never break a scan
            log.warning("all-coin universe failed (%s), using curated list", e)
            return curated
        if not syms:
            return curated
        log.info("universe: all mode, %d symbols", len(syms))
        return syms
    if mode == "top_movers":
        # GAS parity: fish where the volatility is.
        try:
            n = int(settings.get("universe_top_n",
                                 config.UNIVERSE_TOP_MOVERS_DEFAULT))
        except (TypeError, ValueError):
            n = config.UNIVERSE_TOP_MOVERS_DEFAULT
        n = max(10, min(n, config.UNIVERSE_TOP_N_MAX))
        try:
            min_vol = float(settings.get("min_24h_volume_usd") or 0)
        except (TypeError, ValueError):
            min_vol = 0
        try:
            tickers = market.get_swap_tickers() if market is not None else []
            syms = select_top_movers(tickers, n, min_vol)
        except Exception as e:  # noqa: BLE001 - fallback, never break a scan
            log.warning("top-movers universe failed (%s), using curated", e)
            return curated
        if not syms:
            return curated
        log.info("universe: top_movers mode, %d symbols", len(syms))
        return syms
    try:
        n = int(settings.get("universe_top_n",
                             config.UNIVERSE_TOP_N_DEFAULT))
    except (TypeError, ValueError):
        n = config.UNIVERSE_TOP_N_DEFAULT
    n = max(10, min(n, config.UNIVERSE_TOP_N_MAX))
    try:
        syms = exchange.get_top_volume_symbols(n)
    except Exception as e:  # noqa: BLE001 - fallback, never break a scan
        log.warning("top-volume universe failed (%s), using curated list", e)
        return curated
    if not syms:
        return curated
    log.info("universe: top_volume mode, %d symbols", len(syms))
    return syms


class Executor:
    def __init__(self, market, exchange, risk, state,
                 lifecycle: LifecycleLogger | None = None,
                 universe: list[str] | None = None,
                 poll_interval_min: int = config.EXEC_POLL_INTERVAL_MIN,
                 evaluate_fn=None,
                 fixed_margin_usdt: float | None = None,
                 leverage_override: int | None = None,
                 leverage_tiers: dict | None = None,
                 leverage_default: int | None = None,
                 settings_mode: str | None = None):
        self.market = market
        self.exchange = exchange
        self.risk = risk
        self.state = state
        self.lifecycle = lifecycle or LifecycleLogger()
        self.universe = universe or list(config.UNIVERSE)
        self.poll_interval_min = poll_interval_min
        # Which dashboard settings section applies ("live"/"testnet"/"paper").
        self.settings_mode = settings_mode or "live"
        # Graceful shutdown (1.3): run_executor installs SIGINT/SIGTERM
        # handlers that SET this event (never raise), so an in-flight scan
        # cycle — including any HTTP order request — is never interrupted
        # mid-flight. run_loop checks it only at safe points (between
        # cycles), finishes the current cycle, flushes state, exits cleanly.
        self.shutdown_event = threading.Event()
        # TEST MODE overrides (never for live): fixed margin per position and
        # explicit leverage, bypassing the RISK_PCT_PER_TRADE/MAX_LEVERAGE
        # defaults. Risk gates (kill switch, caps) still apply downstream.
        self.fixed_margin_usdt = fixed_margin_usdt
        self.leverage_override = leverage_override
        # Per-symbol leverage tiers (test mode). None -> config defaults.
        self.leverage_tiers = leverage_tiers
        self.leverage_default = leverage_default
        if fixed_margin_usdt is not None or leverage_override is not None:
            log.warning("TEST MODE: fixed_margin_usdt=%s leverage_override=%s "
                        "- overrides RISK_PCT_PER_TRADE/MAX_LEVERAGE defaults",
                        fixed_margin_usdt, leverage_override)
        if evaluate_fn is None:
            from signal_engine import evaluate_symbol as _ev
            evaluate_fn = _ev
        self.evaluate_fn = evaluate_fn

    # ------------------------------------------------------------- helpers
    def _dash_settings(self) -> dict:
        """Dashboard settings merged over built-in defaults.

        Read fresh on every call (cheap: tiny JSON file) so the dashboard
        takes effect without restarting the loop. Missing/corrupt file ->
        defaults (current behavior, auto-trade ON).

        Single source of truth: dashboard.load_settings(). The executor
        must never reimplement the mode-section/legacy-flat merging here —
        two copies WILL drift (they were byte-identical duplicates until
        this change; 2026-10-15).
        """
        from dashboard import load_settings  # lazy: keep import graph flat
        return load_settings(mode=self.settings_mode)

    def _effective_risk_pct(self) -> float:
        # Dashboard may only TIGHTEN risk, never exceed the code default.
        try:
            v = float(self._dash_settings().get("risk_pct_per_trade",
                                                config.RISK_PCT_PER_TRADE))
        except (TypeError, ValueError):
            v = config.RISK_PCT_PER_TRADE
        return max(0.1, min(v, config.RISK_PCT_PER_TRADE))

    def _effective_max_leverage(self) -> int:
        try:
            v = int(self._dash_settings().get("max_leverage",
                                              config.MAX_LEVERAGE))
        except (TypeError, ValueError):
            v = config.MAX_LEVERAGE
        return max(1, min(v, config.MAX_LEVERAGE))

    def _resolve_leverage(self, symbol: str) -> int:
        """Leverage for a symbol. Resolution order:
        1. explicit single override (--leverage),
        2. per-symbol tier (test mode only),
        3. normal risk default MAX_LEVERAGE.
        The dashboard cap tightens (never loosens) path 3 only; explicit
        test-mode overrides are left untouched. A dashboard "fixed" leverage
        (operator choice, 1-125) applies when no CLI override is set.
        """
        if self.leverage_override:
            return self.leverage_override
        if self.fixed_margin_usdt is not None:
            # CLI tiers merge over config tiers (CLI wins on conflict).
            tiers = dict(config.TEST_LEVERAGE_TIERS)
            if self.leverage_tiers:
                tiers.update(self.leverage_tiers)
            default = (self.leverage_default if self.leverage_default is not None
                       else config.TEST_LEVERAGE_DEFAULT)
            return tiers.get(symbol, default)
        ds = self._dash_settings()
        if ds.get("leverage_mode") == "fixed":
            try:
                v = int(ds.get("leverage", 3))
            except (TypeError, ValueError):
                v = 3
            return max(1, min(v, 125))
        return max(1, min(config.MAX_LEVERAGE, self._effective_max_leverage(),
                          125))

    def _open_positions(self) -> dict:
        return {pid: p for pid, p in self.state.get("positions", {}).items()
                if not p.get("closed")}

    def _leg_id(self, pos_id: str, leg: str) -> str:
        # Binance clientOrderId: [a-zA-Z0-9-_], keep it short.
        return f"SHE-{pos_id[-14:]}-{leg}"

    @staticmethod
    def _entry_client_id(sid: str) -> str:
        """Deterministic entry clientOrderId from the signal id (H7).

        A retry of the same signal reuses the same clientOrderId, so
        Binance rejects it as a duplicate instead of opening a second
        position (exchange-side idempotency for the crash/timeout window).
        """
        h = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
        return f"SHE-{h}-EN"

    # ------------------------------------------------------------ scan loop
    def scan_once(self) -> dict:
        """One full cycle: manage positions, then evaluate the universe."""
        summary: dict = {"approved": 0, "rejected": 0, "opened": 0,
                         "managed": 0, "errors": []}
        balance = self.exchange.get_balance()
        self.risk.ensure_day()
        # Studio settings: applied every cycle, no restart needed.
        config.apply_dashboard_overrides(self._dash_settings())
        if config.DISABLED_VETOES:
            log.warning("scan with vetoes DISABLED by operator: %s",
                        sorted(config.DISABLED_VETOES))
        self._manage_positions(balance, summary)
        # Trading-hours gate (LIVE only): new entries only 05:00-18:00 WIB.
        # Management above always runs; this only skips the entry scan.
        entries_allowed = (
            self.settings_mode != "live" or config.within_trading_hours()
        )
        if not entries_allowed:
            log.info("outside trading hours (%s-%s %s): entries paused",
                     config.TRADING_HOURS_START, config.TRADING_HOURS_END,
                     config.TRADING_HOURS_TZ)
            self.state.save()
            return summary
        universe = resolve_universe(self.exchange, self._dash_settings(),
                                    self.universe, market=self.market)
        universe = self._apply_tradability_prefilter(universe)
        for symbol in universe:
            try:
                self._scan_symbol(symbol, balance, summary)
            except Exception as e:  # noqa: BLE001 - one symbol never kills a scan
                log.exception("scan %s failed", symbol)
                summary["errors"].append(f"{symbol}: {e}")
        # Advance paper-broker fills (no-op on the real client).
        poll = getattr(self.exchange, "poll", None)
        if callable(poll):
            try:
                poll(universe)
            except Exception:  # noqa: BLE001
                log.exception("paper poll failed")
        self.state.save()
        return summary

    def scan_from_db(self, db_path: str = "signals.db") -> dict:
        """One cycle: manage positions, then execute fresh DB signals.

        Stage 2 of the two-stage pipeline. The scanner (stage 1) already ran
        the full STRICT engine on ALL OKX coins; here we only re-validate
        (freshness re-check, risk gates) and execute on Binance. Signals are
        marked consumed whether opened or rejected at execution.
        """
        from signals_db import SignalDB
        summary: dict = {"approved": 0, "rejected": 0, "opened": 0,
                         "managed": 0, "errors": []}
        balance = self.exchange.get_balance()
        self.risk.ensure_day()
        # Studio settings: applied every cycle, no restart needed.
        config.apply_dashboard_overrides(self._dash_settings())
        self._manage_positions(balance, summary)
        # Trading-hours gate (LIVE only): no new entries outside 05:00-18:00.
        if (self.settings_mode == "live"
                and not config.within_trading_hours()):
            log.info("outside trading hours (%s-%s %s): db entries paused",
                     config.TRADING_HOURS_START, config.TRADING_HOURS_END,
                     config.TRADING_HOURS_TZ)
            self.state.save()
            return summary
        db = SignalDB(db_path)
        try:
            db.expire_old()
            rows = db.get_fresh_signals(limit=config.DB_SCAN_LIMIT)
            log.info("db signals: %d fresh", len(rows))
            for row in rows:
                # M5: one bad row must never abort the batch. A crashed
                # row is marked consumed (fail-closed, audit-trailed) so a
                # poison row cannot block every cycle.
                try:
                    sid = row["signal_id"]
                    symbol = row["symbol"]
                    # Live signal expiry (1.4): absolute age gate on created_at,
                    # independent of the scanner-set expires_at. After an executor
                    # crash/restart a row can linger past its usefulness; stale =
                    # fail-closed (marked consumed, logged, never entered).
                    age_h = _signal_age_hours(row)
                    if age_h > config.LIVE_SIGNAL_EXPIRY_HOURS:
                        self.lifecycle.log(
                            sid, symbol, REJECTED,
                            reason=(f"stale db signal: age {age_h:.1f}h > "
                                    f"LIVE_SIGNAL_EXPIRY_HOURS="
                                    f"{config.LIVE_SIGNAL_EXPIRY_HOURS}h"))
                        db.mark_consumed(row["id"])
                        summary["rejected"] += 1
                        continue
                    if self.state.seen_signal(sid):
                        self.lifecycle.log(sid, symbol, REJECTED,
                                           reason="duplicate signal id (db)")
                        db.mark_consumed(row["id"])
                        summary["rejected"] += 1
                        continue
                    signal = db.to_signal(row)
                    self.lifecycle.log(sid, symbol, APPROVED,
                                       quality=signal.get("quality"),
                                       entry=signal.get("entry"),
                                       stop_loss=signal.get("stop_loss"),
                                       tp1=signal.get("tp1"),
                                       tp2=signal.get("tp2"),
                                       tp3=signal.get("tp3"))
                    summary["approved"] += 1
                    if not self._dash_settings().get("auto_trade", True):
                        self.lifecycle.log(sid, symbol, REJECTED,
                                           reason="auto-trade disabled (dashboard)")
                        db.mark_consumed(row["id"])
                        summary["rejected"] += 1
                        continue
                    ok, _note = self._try_open(symbol, signal, sid, balance)
                    db.mark_consumed(row["id"])
                    if ok:
                        summary["opened"] += 1
                    else:
                        summary["rejected"] += 1
                except Exception as e:  # noqa: BLE001 - one row never kills the batch
                    log.exception("db signal %s failed", row.get("signal_id"))
                    summary["errors"].append(f"db row {row.get('signal_id')}: {e}")
                    try:
                        self.lifecycle.log(row.get("signal_id"),
                                           row.get("symbol"), REJECTED,
                                           reason=f"db row crashed: {e}")
                    except Exception:  # noqa: BLE001 - audit trail best effort
                        pass
                    try:
                        db.mark_consumed(row["id"])
                    except Exception:  # noqa: BLE001 - best effort
                        log.warning("db mark_consumed failed for row %s",
                                    row.get("id"))
                    summary["rejected"] += 1

        except Exception as e:  # noqa: BLE001 - db failure never kills manage
            log.exception("db scan failed")
            summary["errors"].append(f"db: {e}")
        finally:
            db.close()
        # Advance paper-broker fills (no-op on the real client).
        poll = getattr(self.exchange, "poll", None)
        if callable(poll):
            try:
                poll(self.universe)
            except Exception:  # noqa: BLE001
                log.exception("paper poll failed")
        self.state.save()
        return summary

    def _apply_tradability_prefilter(self, universe: list[str]) -> list[str]:
        """Drop symbols the exchange can't execute, before engine evaluation.

        Uses the cached exchangeInfo tradable set (no extra API call per
        cycle). Fail-open: on any failure the universe is returned
        unfiltered and the per-symbol execution-time check stays as backstop.
        PaperBroker has no exchangeInfo -> always fail-open (sandbox).
        """
        if not config.TRADABILITY_PREFILTER:
            return universe
        get_tradable = getattr(self.exchange, "tradable_symbols", None)
        if not callable(get_tradable):
            return universe
        try:
            tradable = get_tradable()
        except Exception:  # noqa: BLE001 - fail open
            return universe
        if not tradable:
            return universe
        filtered = [s for s in universe if s in tradable]
        dropped = len(universe) - len(filtered)
        if dropped:
            log.info("tradability pre-filter: %d/%d symbols tradable "
                     "(%d skipped)", len(filtered), len(universe), dropped)
        return filtered

    def _scan_symbol(self, symbol: str, balance: float | None,
                     summary: dict) -> None:
        signal, note = self.evaluate_fn(symbol, self.market)
        close_ms = signal_candle_close_ms(self.market, symbol)
        if signal is None:
            return  # engine already decided; lifecycle stays quiet for
                    # non-signals (only APPROVED/REJECTED signals are logged)
        direction = signal.get("direction", "LONG")
        sid = make_signal_id(symbol, close_ms, direction)
        self.lifecycle.log(sid, symbol, GENERATED,
                           quality=signal.get("quality"),
                           entry=signal.get("entry"))
        if self.state.seen_signal(sid):
            self.lifecycle.log(sid, symbol, REJECTED, reason="duplicate signal id")
            summary["rejected"] += 1
            return
        self.lifecycle.log(sid, symbol, APPROVED,
                           quality=signal.get("quality"),
                           entry=signal.get("entry"),
                           stop_loss=signal.get("stop_loss"),
                           tp1=signal.get("tp1"), tp2=signal.get("tp2"),
                           tp3=signal.get("tp3"))
        summary["approved"] += 1

        # Idempotency mark happens ONLY on a committed open (below), so a
        # rejected signal can be reconsidered on the next scan if conditions
        # change — but the same signal candle can never open twice.
        if not self._dash_settings().get("auto_trade", True):
            self.lifecycle.log(sid, symbol, REJECTED,
                               reason="auto-trade disabled (dashboard)")
            summary["rejected"] += 1
            return
        ok, opened = self._try_open(symbol, signal, sid, balance)
        if ok:
            summary["opened"] += 1
        else:
            summary["rejected"] += 1

    # ------------------------------------------------------------ open flow
    def manual_entry(self, symbol: str, balance: float | None = None,
                     atr_mult: float = 1.5,
                     direction: str = "LONG") -> tuple[bool, str]:
        """Manual market entry (terminal `entry` command).

        BYPASSES every STRICT signal gate — no score, no MTF cascade, no
        vetoes, no volume check. Only the mechanical safety layer still
        applies: live-price freshness, symbol filters, leverage/margin
        setup, fixed-margin sizing, risk gates (kill switch, caps), and
        the standard SL/TP bracket machinery.

        Levels: entry = live market price; SL = entry ∓ atr_mult x ATR(15m);
        TP1/2/3 at the config RR ladder (1.5 / 2.25 / 3.0). Idempotent per
        15m candle via the signal id. The caller (terminal) must confirm
        with the user before invoking — this function does not prompt.
        """
        plan, err = self.preview_manual_entry(symbol, direction=direction,
                                              atr_mult=atr_mult)
        if err:
            return False, err
        signal = plan["signal"]
        symbol = plan["symbol"]
        direction = plan["direction"]
        entry = plan["entry"]
        stop = plan["stop_loss"]
        # Idempotency per 15m candle; then the standard open flow.
        close_ms = signal_candle_close_ms(self.market, symbol)
        sid = f"{symbol}:manual:{direction}:{close_ms}"
        if self.state.seen_signal(sid):
            return False, f"manual entry already attempted this candle ({sid})"
        if balance is None:
            try:
                balance = self.exchange.get_balance()
            except Exception:  # noqa: BLE001
                balance = None
        lev = self._resolve_leverage(symbol)
        margin = self.fixed_margin_usdt
        ok, note = self._try_open(symbol, signal, sid, balance)
        if ok:
            return True, (f"MANUAL {direction} {symbol} @ {entry:.6g} "
                          f"({lev}x isolated"
                          f"{f', ${margin:.0f} margin' if margin else ''}) | "
                          f"SL {stop:.6g} | "
                          f"TP1 {signal['tp1']:.6g} TP2 {signal['tp2']:.6g} "
                          f"TP3 {signal['tp3']:.6g} | {note}")
        return False, f"manual entry rejected: {note}"

    def preview_manual_entry(self, symbol: str, direction: str = "LONG",
                             atr_mult: float = 1.5) -> tuple[dict | None, str]:
        """Build the manual-entry bracket plan WITHOUT placing any order.

        Returns (plan, "") on success or (None, reason). The terminal shows
        the plan and asks for confirmation before calling manual_entry().
        Fail-closed on missing price/ATR data.
        """
        from data import normalize_symbol
        from indicators import atr_wilder

        direction = str(direction or "LONG").upper()
        if direction not in ("LONG", "SHORT"):
            return None, f"direction harus LONG atau SHORT, bukan {direction}"
        is_short = direction == "SHORT"
        symbol = normalize_symbol(symbol)
        if not symbol:
            return None, "unknown symbol"
        try:
            live = self.market.get_live_price(symbol)
        except Exception:  # noqa: BLE001
            live = None
        if live is None or live <= 0:
            return None, "live price unavailable - holding (fail-closed)"
        try:
            candles = self.market.get_klines(symbol, "15m", limit=60)
        except Exception:  # noqa: BLE001
            candles = []
        if len(candles) < config.ATR_PERIOD + 2:
            return None, (f"only {len(candles)} 15m candles - "
                           f"need {config.ATR_PERIOD + 2} for ATR")
        atr = atr_wilder(candles, config.ATR_PERIOD)
        if not atr or atr <= 0:
            return None, "ATR unavailable - holding (fail-closed)"
        entry = float(live)
        stop = entry + atr_mult * atr if is_short else entry - atr_mult * atr
        risk_dist = abs(entry - stop)
        tp = (lambda rr: entry - risk_dist * rr) if is_short else \
             (lambda rr: entry + risk_dist * rr)
        signal = {
            "symbol": symbol, "direction": direction, "interval": "15m",
            "entry": entry, "entry_low": entry, "entry_high": entry,
            "stop_loss": stop,
            "tp1": tp(config.TP1_RR),
            "tp2": tp(config.TP2_RR),
            "tp3": tp(config.TP3_RR),
            "quality": 0, "manual": True,
            "note": "manual market entry - bypasses STRICT gates",
        }
        return {
            "symbol": symbol, "direction": direction, "entry": entry,
            "stop_loss": stop, "tp1": signal["tp1"], "tp2": signal["tp2"],
            "tp3": signal["tp3"], "atr": float(atr),
            "risk_pct": risk_dist / entry * 100 if entry else 0.0,
            "leverage": self._resolve_leverage(symbol),
            "signal": signal,
        }, ""

    def _try_open(self, symbol: str, signal: dict, sid: str,
                  balance: float | None) -> tuple[bool, str]:
        entry = float(signal["entry"])
        direction = str(signal.get("direction", "LONG")).upper()
        is_short = direction == "SHORT"
        entry_side = "SELL" if is_short else "BUY"   # open: sell short / buy long
        exit_side = "BUY" if is_short else "SELL"     # close: buy back / sell
        # 1. Freshness re-check with a LIVE price, right before ordering.
        # Mirrors the engine gate: LONG rejects price ran above the zone,
        # SHORT rejects price ran below the zone.
        # Item 2.5: price comes from the OKX WS ticker cache when fresh;
        # wsfeed.get_live_price() falls back to REST automatically, so
        # this stays fail-closed exactly as before.
        try:
            live = wsfeed.get_live_price(symbol, self.market.get_live_price)
        except Exception:  # noqa: BLE001
            live = None
        if live is None or live <= 0:
            return self._reject(
                symbol, sid, "freshness re-check: live price unavailable "
                             "(fail-closed)")
        if is_short:
            low = float(signal["entry_low"])
            drift = (low - live) / low * 100 if low > 0 else 0.0
            if drift > config.FRESHNESS_MAX_DRIFT_PCT:
                return self._reject(
                    symbol, sid,
                    f"freshness re-check: -{drift:.2f}% below entry zone - stale")
        else:
            drift = (live - signal["entry_high"]) / signal["entry_high"] * 100
            if drift > config.FRESHNESS_MAX_DRIFT_PCT:
                return self._reject(
                    symbol, sid,
                    f"freshness re-check: +{drift:.2f}% above entry zone - stale")

        # 2. Exchange tradability + leverage + margin.
        filters = self.exchange.get_symbol_filters(symbol)
        if not filters:
            return self._reject(symbol, sid, "symbol not tradable on exchange")
        ds = self._dash_settings()
        lev = self._resolve_leverage(symbol)
        margin_mode = ds.get("margin_mode", "ISOLATED")
        if margin_mode not in ("ISOLATED", "CROSS"):
            margin_mode = "ISOLATED"
        self.exchange.set_leverage(symbol, lev)
        self.exchange.set_margin_type(symbol, margin_mode)
        if (margin_mode != "ISOLATED"
                or ds.get("leverage_mode") == "fixed"
                or ds.get("entry_mode") == "fixed_usdt"):
            log.warning("%s operator order config: margin=%s leverage=%sx "
                        "entry=%s", symbol, margin_mode, lev,
                        ds.get("entry_mode", "risk"))

        # 3. Sizing.
        if self.fixed_margin_usdt is not None:
            qty, note = compute_test_qty(self.fixed_margin_usdt, lev, entry,
                                         filters)
        elif ds.get("entry_mode") == "fixed_usdt":
            try:
                fu = float(ds.get("fixed_usdt", 10.0))
            except (TypeError, ValueError):
                fu = 10.0
            qty, note = compute_test_qty(max(1.0, fu), lev, entry, filters)
        else:
            signal_atr = signal.get("atr")
            try:
                atr_val = float(signal_atr) if signal_atr is not None else None
            except (TypeError, ValueError):
                atr_val = None
            qty, note = compute_qty(balance, entry, float(signal["stop_loss"]),
                                    filters,
                                    risk_pct=self._effective_risk_pct(),
                                    atr=atr_val)
        if qty <= 0:
            return self._reject(symbol, sid, f"sizing: {note}")
        notional = qty * entry

        # 4. Risk gates.
        open_count = len(self._open_positions())
        ok, reason = self.risk.check_can_open(symbol, notional, open_count)
        if not ok:
            return self._reject(symbol, sid, f"risk: {reason}")

        # 4b. One position per coin (user 2026-10-16 — the WCTUSDT double
        # entry stacked $2 margin on one coin and the 2nd SL algo order was
        # rejected by Binance -4509). Never open a second entry on a symbol
        # that already has an open position, regardless of direction.
        #
        # M2/B3: the exchange is ground truth — check it FIRST. A position
        # opened manually on the exchange after startup is invisible to the
        # state-based check until the next restart, and a crash between the
        # two checks could leave state/exchange diverged. Fail-closed: an
        # unfetchable snapshot rejects the entry rather than risking a
        # double position.
        try:
            xp_symbols = {str(p.get("symbol") or "")
                          for p in self.exchange.get_positions()}
        except Exception as e:  # noqa: BLE001 - fail-closed on unknown state
            return self._reject(
                symbol, sid,
                f"exchange positions snapshot unavailable ({e}) — "
                "holding (fail-closed)")
        if symbol in xp_symbols:
            return self._reject(
                symbol, sid,
                f"already positioned in {symbol} on the exchange")
        for pid, p in self._open_positions().items():
            if p.get("symbol") == symbol:
                return self._reject(
                    symbol, sid,
                    f"already positioned in {symbol} ({pid})")

        # 5. TP split must be placeable.
        tp_qty, note = split_tp_qty(
            qty, filters.get("stepSize", 0) or 0.0,
            filters.get("minQty", 0) or 0.0)
        if tp_qty is None:
            return self._reject(symbol, sid, f"tp split: {note}")

        # 6. Entry order: MARKET when already inside the zone, else LIMIT.
        pos_id = f"{symbol}_{int(time.time() * 1000)}"
        in_zone = signal["entry_low"] <= live <= signal["entry_high"]
        entry_type = "MARKET" if in_zone else "LIMIT"
        tick = filters.get("tickSize", 0) or 0
        entry_px = quantize_price(entry, tick)
        # Post-only (GTX) entries: maker-only, never cross the spread.
        # Opt-in via EXEC_POST_ONLY_ENTRY (user feature 2026-10-16).
        tif = "GTX" if (entry_type == "LIMIT"
                        and getattr(config, "EXEC_POST_ONLY_ENTRY", False)) else "GTC"
        # H7: mark the signal BEFORE placing the entry order. A crash
        # (or a place_order timeout with the order actually placed) between
        # place_order and mark_signal used to leave a live order with no
        # idempotency mark -> the retry opened a SECOND position. At-most-
        # once: a missed trade beats a double entry, and the deterministic
        # clientOrderId above makes even a retried signal collide instead
        # of double-submitting.
        self.state.mark_signal(sid, symbol, "placing entry")
        self.state.save()
        order = self.exchange.place_order(
            symbol, entry_side, entry_type,
            quantity=qty,
            price=None if entry_type == "MARKET" else entry_px,
            time_in_force=tif,
            client_order_id=self._entry_client_id(sid))
        if not order:
            return self._reject(symbol, sid, f"entry order placement failed (type={entry_type}, tif={tif})")

        self.state.get("positions", {})[pos_id] = {
            "symbol": symbol, "signal_id": sid, "direction": direction,
            "qty": qty, "entry_price": entry_px,
            "leverage": lev,
            "margin_usdt": round(qty * entry_px / lev, 2) if lev else 0.0,
            "stop_loss": float(signal["stop_loss"]),
            "tp": {
                "tp1": {"price": float(signal["tp1"]), "qty": tp_qty[0],
                        "order_id": None, "filled": False},
                "tp2": {"price": float(signal["tp2"]), "qty": tp_qty[1],
                        "order_id": None, "filled": False},
                "tp3": {"price": float(signal["tp3"]), "qty": tp_qty[2],
                        "order_id": None, "filled": False},
            },
            "sl_order_id": None, "entry_order_id": order.get("orderId"),
            "breakeven_moved": False, "brackets_placed": False,
            "opened_at": int(time.time() * 1000),
            "entry_type": entry_type, "unmanaged": False,
            "realized": 0.0,
        }
        self.state.mark_signal(sid, symbol, f"opened {pos_id}")
        self.state.save()
        if entry_type == "MARKET":
            # Immediate fill path is resolved in _manage_positions on the
            # next poll; fast-path it here too for responsiveness.
            self._check_entry_fill(pos_id)
        log.info("opened %s %s qty=%s entry=%s (%s)", symbol, pos_id, qty,
                 entry_px, entry_type)
        return True, pos_id

    def _reject(self, symbol: str, sid: str, reason: str) -> tuple[bool, str]:
        self.lifecycle.log(sid, symbol, REJECTED, reason=reason)
        log.info("reject %s: %s", symbol, reason)
        return False, reason

    # ------------------------------------------------------ position mgmt
    def _manage_positions(self, balance: float | None, summary: dict) -> None:
        for pos_id, pos in list(self._open_positions().items()):
            if pos.get("unmanaged"):
                continue  # adopted at startup; manual review only
            try:
                self._manage_one(pos_id, pos, balance, summary)
            except Exception:  # noqa: BLE001 - one position never kills mgmt
                log.exception("manage %s failed", pos_id)
                summary["errors"].append(f"manage {pos_id} failed")

    def _manage_one(self, pos_id: str, pos: dict, balance: float | None,
                    summary: dict) -> None:
        symbol = pos["symbol"]
        sid = pos["signal_id"]
        if not pos.get("brackets_placed"):
            if self._check_entry_fill(pos_id):
                summary["managed"] += 1
            else:
                self._expire_stale_entry(pos_id, pos, sid)
            return

        # Poll TP/SL order statuses.
        remaining = pos["qty"]
        for leg in ("tp1", "tp2", "tp3"):
            tp = pos["tp"][leg]
            if tp["filled"] or not tp["order_id"]:
                continue
            o = self.exchange.get_order(symbol, order_id=tp["order_id"])
            if o and o.get("status") == "FILLED":
                tp["filled"] = True
                fill_px = float(o.get("avgPrice") or tp["price"])
                fill_qty = float(o.get("executedQty") or tp["qty"])
                sign = -1.0 if pos.get("direction") == "SHORT" else 1.0
                pnl = sign * (fill_px - pos["entry_price"]) * fill_qty
                fee = fill_px * fill_qty * _FEE_PCT / 100
                net = pnl - fee
                pos["realized"] = pos.get("realized", 0.0) + net
                self.risk.record_realized(net)
                remaining -= fill_qty
                self.lifecycle.log(sid, symbol,
                                   { "tp1": TP1_HIT, "tp2": TP2_HIT,
                                     "tp3": TP3_HIT }[leg],
                                   price=fill_px, qty=fill_qty, pnl=round(net, 4))
                log.info("%s %s filled @ %s (pnl %+0.2f)", pos_id, leg,
                         fill_px, net)
        if remaining < 1e-12:
            remaining = 0.0
        pos["qty"] = remaining

        # H2: keep the live SL sized to the remainder and verify the SL
        # leg is still alive on the exchange.
        if pos["qty"] > 0 and not pos.get("closed"):
            self._resize_sl_to_remaining(pos_id, pos, sid)
            self._ensure_sl_alive(pos_id, pos, sid, summary)
            if pos.get("closed"):
                return

        # Breakeven: TP1 filled -> move SL to the entry price.
        if (config.EXEC_BREAKEVEN_AFTER_TP1 and pos["tp"]["tp1"]["filled"]
                and not pos.get("breakeven_moved") and pos["qty"] > 0):
            self._move_sl_to_breakeven(pos_id, pos, sid)

        # Step-wise profit lock: TP2 filled -> move SL to TP1 price.
        if (getattr(config, "EXEC_LOCK_PROFIT_AFTER_TP2", True)
                and pos["tp"]["tp2"]["filled"]
                and not pos.get("profit_locked") and pos["qty"] > 0):
            self._move_sl_to_profit_lock(pos_id, pos, sid)

        # H5: TP dust — split_tp_qty floors every leg to stepSize, so a
        # sub-step residual never sits in any TP leg. Once all TP legs are
        # filled, a remainder below minQty can never be exited (a market
        # close would be rejected) — treat it as fully closed so the dust
        # can't occupy the one-position-per-coin slot forever.
        if pos["qty"] > 0 and all(
                (pos["tp"][leg].get("filled") or
                 (pos["tp"][leg].get("qty") or 0.0) <= 0)
                for leg in ("tp1", "tp2", "tp3")):
            try:
                min_qty = float(
                    (self.exchange.get_symbol_filters(symbol) or {})
                    .get("minQty") or 0.0)
            except Exception:  # noqa: BLE001
                min_qty = 0.0
            dust_cap = max(min_qty, _CLOSE_DUST_QTY)
            if 0 < pos["qty"] <= dust_cap:
                log.info("%s TP dust %s <= minQty %s - marking closed",
                         pos_id, pos["qty"], dust_cap)
                self.lifecycle.log(sid, symbol, "dust_closed",
                                   qty=pos["qty"])
                pos["qty"] = 0.0

        # Terminal state: position fully closed.
        if pos["qty"] <= 0:
            self._close_position(pos_id, pos, sid, summary)
            return
        # Pending-close retry: a previous _flat_position attempt cancelled
        # the brackets and then partially filled (or filled nothing, or the
        # order failed). The residual has NO exchange-side protection, so
        # the close is retried UNCONDITIONALLY until done — never gated on
        # the original trigger firing again (a retreated price must not
        # strand an unprotected residual). Fail-closed: no live price ->
        # skip this cycle, never close blind.
        # If the retry did not finish the job, FALL THROUGH (no return):
        # a still-hot trigger may retry immediately below, and the
        # end-of-cycle cross-check handles a residual that vanished
        # externally (manual close / liquidation) via _close_position —
        # without this, a vanished residual would retry forever.
        pc = pos.get("pending_close")
        if isinstance(pc, dict) and pos["qty"] > 0:
            mark = self.market.get_live_price(symbol)
            if mark:
                self._flat_position(
                    pos_id, pos, sid, summary, mark,
                    pc.get("closed_reason") or "pending_close",
                    pc.get("lifecycle_state") or PROFIT_TAKE,
                    reason=pc.get("reason") or "pending close retry")
                if pos.get("closed"):
                    return
        # Profit-take: market-close the whole position once unrealized PnL
        # reaches +PROFIT_TAKE_PCT% of margin (user 2026-10-16: "+20%
        # langsung TP", live). Fail-closed: no live price -> skip this
        # cycle, never close blind.
        if config.PROFIT_TAKE_PCT > 0 and pos["qty"] > 0:
            mark = self.market.get_live_price(symbol)
            if mark:
                sign = 1.0 if pos.get("direction") == "LONG" else -1.0
                unreal = sign * (mark - pos["entry_price"]) * pos["qty"]
                margin = pos.get("margin_usdt") or 0.0
                if margin > 0 and unreal >= margin * config.PROFIT_TAKE_PCT / 100:
                    self._take_profit(pos_id, pos, sid, summary, mark)
                    return
        # Trailing stop: once unrealized hits TRAIL_TRIGGER_PCT% of margin,
        # lock in gains — market-close on TRAIL_DISTANCE_PCT% pullback from
        # the trailed peak. Bot-side trigger (the exchange SL stays as the
        # hard stop). Fail-closed on price fetch failure.
        if (config.TRAIL_TRIGGER_PCT > 0 and config.TRAIL_DISTANCE_PCT > 0
                and pos["qty"] > 0):
            mark = self.market.get_live_price(symbol)
            if mark and self._trail_check(pos_id, pos, sid, summary, mark):
                return
        # Time stop: stale positions tie up margin and the edge decays —
        # market-close anything open longer than TIME_STOP_HOURS.
        # Fail-closed: no live price -> skip this cycle.
        if config.TIME_STOP_HOURS > 0 and pos["qty"] > 0:
            opened_at = pos.get("opened_at") or 0
            if opened_at <= 0:
                # Unknown age: skip with a warning, never instant-close a
                # position whose open time we cannot prove (review 2026-10-16).
                log.warning("%s time_stop: missing opened_at, skipping",
                            pos_id)
            elif ((time.time() * 1000 - opened_at) / 3_600_000
                    >= config.TIME_STOP_HOURS):
                mark = self.market.get_live_price(symbol)
                if mark:
                    age_h = (time.time() * 1000 - opened_at) / 3_600_000
                    self._flat_position(
                        pos_id, pos, sid, summary, mark, "time_stop",
                        TIME_STOP, reason=f"time_stop after {age_h:.1f}h")
                    return
        # Cross-check against the exchange: SL may have filled without us
        # seeing the order event (e.g. after a restart). FAIL-CLOSED: if the
        # positions snapshot cannot be fetched, skip the check this cycle —
        # never mistake an API error for "position closed" (2026-10-15:
        # transient post-sleep failure orphaned real positions).
        try:
            xp = {p["symbol"]: p for p in self.exchange.get_positions()}
        except Exception:  # noqa: BLE001 - keep managing on stale data
            log.warning("%s: positions fetch failed, skipping gone-check",
                        pos_id)
        else:
            if symbol not in xp and pos.get("brackets_placed"):
                self._close_position(pos_id, pos, sid, summary,
                                    reason="position gone from exchange")
        self.state.save()

    def _check_entry_fill(self, pos_id: str) -> bool:
        """If the entry order filled, place the SL+TP brackets. Returns True
        when brackets are now in place (or the entry is still working)."""
        pos = self.state.get("positions", {}).get(pos_id)
        if not pos or pos.get("brackets_placed"):
            return True
        symbol, sid = pos["symbol"], pos["signal_id"]
        o = self.exchange.get_order(symbol, order_id=pos.get("entry_order_id"))
        if not o:
            return False
        status = o.get("status")
        if status == "FILLED":
            fill_px = float(o.get("avgPrice") or pos["entry_price"])
            pos["entry_price"] = fill_px
            self.lifecycle.log(sid, symbol, ENTRY_TRIGGERED, price=fill_px)
            if self._place_brackets(pos_id, pos):
                pos["brackets_placed"] = True
                self.lifecycle.log(sid, symbol, POSITION_OPENED,
                                   qty=pos["qty"], entry=fill_px)
                self.state.save()
                return True
            # Bracket placement failed: keep retrying each scan (fail-safe,
            # never leave a naked position silently).
            log.error("%s entry filled but brackets FAILED - retrying", pos_id)
            self.state.save()
            return False
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            try:
                filled = float(o.get("executedQty") or 0.0)
            except (TypeError, ValueError):
                filled = 0.0
            if filled > 0 and not pos.get("brackets_placed"):
                # H8: cancelled after a partial fill — the filled portion is
                # a real position. Adopt it (resize + brackets), don't strand
                # it as closed/naked.
                self._adopt_filled_entry(pos_id, pos, sid, filled, o)
                return True
            pos["closed"] = True
            pos["closed_reason"] = f"entry order {status}"
            self.lifecycle.log(sid, symbol, CANCELLED,
                               reason=f"entry order {status}", terminal=True)
            self.state.save()
        return False

    def _expire_stale_entry(self, pos_id: str, pos: dict, sid: str) -> None:
        # Guard: _check_entry_fill may already have closed this position
        # (CANCELLED) in the same cycle — never log a second terminal event.
        if pos.get("closed"):
            return
        # R2 (review 2026-10-17): a pending close owns this position. A
        # never-bracketed position with an armed pending_close holds a live
        # dust residual on the exchange (_flat_position partial fill or a
        # failed close), whose retry runs through _check_entry_fill ->
        # _adopt_filled_entry -> _flat_position. Expiring it here on a
        # transient get_order failure (o=None twice in one cycle, entry
        # older than the timeout) would strand the residual as "entry
        # unfilled" — a phantom expiry. Skip and let the retry resume next
        # cycle; pending_close is only armed when qty > 0.
        if (isinstance(pos.get("pending_close"), dict)
                and (pos.get("qty") or 0.0) > 0):
            return
        age_min = (time.time() * 1000 - pos["opened_at"]) / 60_000
        if age_min < _ENTRY_TIMEOUT_MINUTES:
            return
        symbol = pos["symbol"]
        self.exchange.cancel_order(symbol, order_id=pos.get("entry_order_id"))
        # H8: the entry may have filled (fully or partially) in the race
        # window between _check_entry_fill's get_order and this cancel.
        # Never mark a filled portion as closed — it would become a naked,
        # unmanaged position. Re-fetch and adopt any fill.
        try:
            o = self.exchange.get_order(symbol,
                                        order_id=pos.get("entry_order_id"))
        except Exception:  # noqa: BLE001
            o = None
        filled = 0.0
        if o:
            try:
                filled = float(o.get("executedQty") or 0.0)
            except (TypeError, ValueError):
                filled = 0.0
            if o.get("status") == "FILLED" and filled <= 0:
                filled = pos.get("qty") or 0.0
        if filled > 0:
            self._adopt_filled_entry(pos_id, pos, sid, filled, o or {})
            return
        pos["closed"] = True
        pos["closed_reason"] = f"entry unfilled after {_ENTRY_TIMEOUT_MINUTES}m"
        self.lifecycle.log(sid, symbol, EXPIRED,
                           reason=pos["closed_reason"], terminal=True)
        log.info("%s expired: %s", pos_id, pos["closed_reason"])
        self.state.save()

    def _adopt_filled_entry(self, pos_id: str, pos: dict, sid: str,
                            filled_qty: float, order: dict) -> None:
        """H8: adopt a (partially) filled entry — resize + bracket it.

        Called when the entry filled around the cancel/timeout window.
        The TP ladder was sized for the full requested qty, so it is
        re-split for what actually filled. Never leaves the filled portion
        naked.
        """
        symbol = pos["symbol"]
        try:
            fill_px = float(order.get("avgPrice") or pos["entry_price"])
        except (TypeError, ValueError):
            fill_px = pos["entry_price"]
        pos["entry_price"] = fill_px
        # Review 2026-10-17: re-adopt after a partial dust close must not
        # restore the entry order's original executedQty — _flat_position
        # tracks the residual in pos["qty"], and a reduceOnly close larger
        # than the exchange position is rejected every cycle (-2022),
        # wedging the retry forever. On first adopt pos["qty"] is the
        # requested qty >= filled_qty, so the clamp is a no-op there.
        pos["qty"] = min(filled_qty, pos.get("qty") or filled_qty)
        lev = pos.get("leverage") or 1
        pos["margin_usdt"] = round(filled_qty * fill_px / lev, 2) if lev else 0.0
        try:
            filters = self.exchange.get_symbol_filters(symbol) or {}
        except Exception:  # noqa: BLE001
            filters = {}
        tp_qty, note = split_tp_qty(
            filled_qty, filters.get("stepSize") or 0.0,
            filters.get("minQty") or 0.0)
        if tp_qty is None:
            # Filled qty cannot be exited via the TP ladder (dust): market
            # -close it now rather than hold an un-exitable position.
            log.error("%s adopted fill %s unsplittable (%s) - market-closing",
                      pos_id, filled_qty, note)
            # Close through _flat_position (fill-verified, retry-on-failure)
            # rather than an inline blind close: a failed/partial market
            # close must never be recorded as closed (phantom flat). On a
            # residual the dust branch is re-entered each cycle via
            # _check_entry_fill -> re-adopt (never-bracketed positions take
            # the early return, not the pending_close block); the residual
            # qty is clamped above so the retry closes what the exchange
            # actually holds. The terminal EXPIRED event is logged only
            # once the close actually completes.
            try:
                mark = self.market.get_live_price(symbol)
            except Exception:  # noqa: BLE001
                mark = None
            try:
                self._flat_position(
                    pos_id, pos, sid, {"managed": 0, "errors": []},
                    mark or fill_px,
                    f"adopted dust fill {filled_qty:g} closed",
                    EXPIRED, reason=f"dust fill {note}")
            except Exception:  # noqa: BLE001 - never strand a dust fill
                # _flat_position's place_order is unwrapped by design; the
                # manual_entry path reaches _adopt_filled_entry without a
                # surrounding try/except, so arm the retry here instead of
                # letting an exchange error strand a live residual.
                log.exception("%s dust close raised", pos_id)
                self._mark_pending_close(
                    pos, f"adopted dust fill {filled_qty:g} closed",
                    EXPIRED, reason=f"dust fill {note}")
            self.state.save()
            return
        for leg, q in zip(("tp1", "tp2", "tp3"), tp_qty):
            pos["tp"][leg]["qty"] = q
            pos["tp"][leg]["filled"] = False
            pos["tp"][leg]["order_id"] = None
        self.lifecycle.log(sid, symbol, ENTRY_TRIGGERED, price=fill_px,
                           qty=filled_qty,
                           reason="entry (partially) filled near timeout - adopted")
        if self._place_brackets(pos_id, pos):
            pos["brackets_placed"] = True
            self.lifecycle.log(sid, symbol, POSITION_OPENED,
                               qty=filled_qty, entry=fill_px)
        else:
            # Brackets failed: _check_entry_fill retries next cycle; the
            # CANCELED+executedQty path above re-adopts instead of closing.
            log.error("%s adopted fill but brackets FAILED - retrying",
                      pos_id)
        self.state.save()

    def _place_brackets(self, pos_id: str, pos: dict) -> bool:
        """Place SL (STOP_MARKET via Algo Order API) + TP1-3 (LIMIT, reduceOnly).
        All-or-nothing: on any failure, cancel whatever was placed and False.

        NOTE: Binance migrated conditional orders to /fapi/v1/algoOrder
        (2025-12-09); /fapi/v1/order rejects STOP_MARKET with -4120.
        """
        symbol = pos["symbol"]
        placed_algo: list[int] = []
        placed_regular: list[int] = []
        # Exit side mirrors the position direction: LONG exits with SELL,
        # SHORT exits with BUY (covers SL stop-market + TP limits).
        exit_side = "BUY" if pos.get("direction") == "SHORT" else "SELL"

        def _undo():
            for aid in placed_algo:
                self.exchange.cancel_algo_order(symbol, aid)
            for oid in placed_regular:
                self.exchange.cancel_order(symbol, order_id=oid)

        tick = None
        step = None
        try:
            filters = self.exchange.get_symbol_filters(symbol) or {}
            tick = filters.get("tickSize") or None
            step = filters.get("stepSize") or None
        except Exception:  # noqa: BLE001
            pass

        def _px(p: float) -> float:
            return quantize_price(p, tick)

        sl_qty = floor_to_step(pos["qty"], step) if step else pos["qty"]
        sl = self.exchange.place_algo_order(
            symbol, exit_side, "STOP_MARKET", quantity=sl_qty,
            trigger_price=_px(pos["stop_loss"]), reduce_only=True,
            working_type="MARK_PRICE",
            client_algo_id=self._leg_id(pos_id, "SL"))
        if not sl or not sl.get("algoId"):
            return False
        placed_algo.append(sl["algoId"])
        pos["sl_order_id"] = sl["algoId"]
        pos["sl_is_algo"] = True
        pos["sl_qty"] = sl_qty  # H2: track live SL size for resizing


        for leg in ("tp1", "tp2", "tp3"):
            tp = pos["tp"][leg]
            if tp["qty"] <= 0:
                continue
            o = self.exchange.place_order(
                symbol, exit_side, "LIMIT", quantity=tp["qty"],
                price=_px(tp["price"]), reduce_only=True,
                client_order_id=self._leg_id(pos_id, leg.upper()))
            if not o:
                _undo()
                pos["sl_order_id"] = None
                pos["sl_is_algo"] = False
                return False
            placed_regular.append(o["orderId"])
            tp["order_id"] = o["orderId"]
        return True

    # ------------------------------------------------------- SL (algo) helpers
    def _sl_cancel(self, symbol: str, pos: dict) -> bool:
        """Cancel the SL leg, routing to the algo endpoint when needed."""
        aid = pos.get("sl_order_id")
        if not aid:
            return True
        if pos.get("sl_is_algo"):
            return self.exchange.cancel_algo_order(symbol, aid)
        return self.exchange.cancel_order(symbol, order_id=aid)

    def _sl_status(self, symbol: str, pos: dict) -> dict | None:
        """Fetch the SL leg's current state, or None."""
        aid = pos.get("sl_order_id")
        if not aid:
            return None
        if pos.get("sl_is_algo"):
            return self.exchange.get_algo_order(symbol, aid)
        return self.exchange.get_order(symbol, order_id=aid)

    def _place_sl(self, pos_id: str, pos: dict, qty: float, tag: str) -> bool:
        """Place a new SL STOP_MARKET for qty and swap it in (H2).

        The replacement is placed BEFORE the old leg is cancelled so the
        position is never unprotected. On success the old leg is cancelled
        and pos["sl_qty"] is updated. Returns True on success.
        """
        symbol = pos["symbol"]
        exit_side = "BUY" if pos.get("direction") == "SHORT" else "SELL"
        tick = None
        step = None
        try:
            filters = self.exchange.get_symbol_filters(symbol) or {}
            tick = filters.get("tickSize")
            step = filters.get("stepSize")
        except Exception:  # noqa: BLE001
            pass
        if step:
            qty = floor_to_step(qty, step)
        if qty <= 0:
            return False
        sl = self.exchange.place_algo_order(
            symbol, exit_side, "STOP_MARKET", quantity=qty,
            trigger_price=quantize_price(pos["stop_loss"], tick),
            reduce_only=True, working_type="MARK_PRICE",
            client_algo_id=self._leg_id(pos_id, tag))
        if not (sl and sl.get("algoId")):
            return False
        self._sl_cancel(symbol, pos)
        pos["sl_order_id"] = sl["algoId"]
        pos["sl_is_algo"] = True
        pos["sl_qty"] = qty
        return True

    def _resize_sl_to_remaining(self, pos_id: str, pos: dict, sid: str) -> None:
        """H2: keep the live SL sized to the remaining position qty.

        An oversized reduce-only STOP_MARKET can be rejected at trigger
        time, leaving the tail position unprotected while the bot believes
        it is safe. Called after every TP-fill pass.
        """
        remaining = pos.get("qty") or 0.0
        if remaining <= 0 or pos.get("closed"):
            return
        if pos.get("stop_loss") is None:
            return  # cannot size an SL without a trigger price
        sl_qty = pos.get("sl_qty")
        if sl_qty is None:
            # Legacy position (pre-fix): the live SL was placed with the
            # full entry qty ~= sum of the TP legs.
            sl_qty = sum((pos.get("tp", {}).get(leg, {}) or {}).get("qty", 0.0)
                         for leg in ("tp1", "tp2", "tp3"))
        if remaining >= sl_qty - 1e-9:
            return
        n = int(pos.get("sl_resizes") or 0) + 1
        if self._place_sl(pos_id, pos, remaining, f"SLR{n}"):
            pos["sl_resizes"] = n
            self.lifecycle.log(sid, pos["symbol"], "sl_resized",
                               price=pos["stop_loss"], qty=remaining)
            log.info("%s SL resized to remaining qty %s", pos_id, remaining)
            self.state.save()
        else:
            log.error("%s SL resize FAILED - oversized SL left live", pos_id)

    def _ensure_sl_alive(self, pos_id: str, pos: dict, sid: str,
                         summary: dict) -> None:
        """H2: poll the SL leg every cycle; emergency re-place if dead.

        A rejected/failed SL trigger is otherwise invisible — the bot would
        keep managing a position it believes is protected. On a dead SL
        (CANCELED/REJECTED/EXPIRED), re-place immediately; if re-placement
        keeps failing, market-close rather than hold a naked position.
        """
        if pos.get("closed") or not pos.get("sl_order_id"):
            return
        if (pos.get("qty") or 0.0) <= 0:
            return
        if pos.get("stop_loss") is None:
            return  # cannot re-place an SL without a trigger price
        try:
            st = self._sl_status(pos["symbol"], pos)
        except Exception:  # noqa: BLE001
            return  # poll error: skip this cycle, cross-check still runs
        if st is None:
            return
        status = str(st.get("algoStatus") or st.get("status") or "").upper()
        if status in ("NEW", "WORKING", "TRIGGERING", "PARTIALLY_FILLED",
                      "TRIGGERED", ""):
            # TRIGGERED: the stop fired; the end-of-cycle cross-check will
            # close the position via _close_position. Don't re-place.
            pos.pop("sl_replace_fails", None)
            return
        fails = int(pos.get("sl_replace_fails") or 0)
        log.error("%s SL leg dead (%s) - emergency re-place (attempt %d)",
                  pos_id, status, fails + 1)
        if self._place_sl(pos_id, pos, pos["qty"], f"SLE{fails + 1}"):
            pos.pop("sl_replace_fails", None)
            self.lifecycle.log(sid, pos["symbol"], "sl_replaced",
                               price=pos["stop_loss"], qty=pos["qty"],
                               reason=f"dead SL ({status})")
            log.warning("%s SL re-placed after dead leg (%s)", pos_id, status)
            self.state.save()
            return
        fails += 1
        pos["sl_replace_fails"] = fails
        self.state.save()
        if fails >= 3:
            # No working stop and we cannot make one: close the position
            # rather than hold it naked. Fail-closed on price fetch.
            mark = self.market.get_live_price(pos["symbol"])
            if mark:
                log.error("%s SL re-place failed %d times - market-closing "
                          "unprotected position", pos_id, fails)
                self._flat_position(pos_id, pos, sid, summary, mark,
                                    "sl_dead", "sl_hit",
                                    reason=f"SL dead ({status}), re-place "
                                           f"failed {fails}x")

    def _move_sl_to_breakeven(self, pos_id: str, pos: dict, sid: str) -> None:
        symbol = pos["symbol"]
        tick = None
        step = None
        try:
            filters = self.exchange.get_symbol_filters(symbol) or {}
            tick = filters.get("tickSize")
            step = filters.get("stepSize")
        except Exception:  # noqa: BLE001
            pass
        be = quantize_price(pos["entry_price"], tick)
        exit_side = "BUY" if pos.get("direction") == "SHORT" else "SELL"
        be_qty = pos.get("qty") or 0.0
        if step:
            be_qty = floor_to_step(be_qty, step)
        if be_qty <= 0:
            return
        # STOP_MARKET must go through the algo endpoint: Binance rejects it
        # on /fapi/v1/order with HTTP 400 -4120. Place the new breakeven SL
        # BEFORE cancelling the old one so the position is never unprotected.
        sl = self.exchange.place_algo_order(
            symbol, exit_side, "STOP_MARKET", quantity=be_qty,
            trigger_price=be, reduce_only=True, working_type="MARK_PRICE",
            client_algo_id=self._leg_id(pos_id, "SLBE"))
        if sl and sl.get("algoId"):
            # New SL is live: now cancel the old leg (algo or regular).
            self._sl_cancel(symbol, pos)
            pos["sl_order_id"] = sl["algoId"]
            pos["sl_is_algo"] = True  # breakeven leg is an algo STOP_MARKET
            pos["breakeven_moved"] = True
            pos["stop_loss"] = be
            pos["sl_qty"] = be_qty  # H2: breakeven SL sized to remainder
            log.info("%s SL moved to breakeven @ %s", pos_id, be)
            self.state.save()
        else:
            log.error("%s breakeven SL placement FAILED - original SL left "
                      "live", pos_id)

    def _move_sl_to_profit_lock(self, pos_id: str, pos: dict, sid: str) -> None:
        """Lock in profit: after TP2 fills, advance the live SL to the TP1 price.

        Guarantees that the remaining TP3 slice exits in solid profit even if
        the market subsequently pulls back all the way to entry.
        """
        symbol = pos["symbol"]
        tp1 = (pos.get("tp") or {}).get("tp1") or {}
        tp1_px = tp1.get("price")
        if tp1_px is None or tp1_px <= 0:
            return
        tick = None
        step = None
        try:
            filters = self.exchange.get_symbol_filters(symbol) or {}
            tick = filters.get("tickSize")
            step = filters.get("stepSize")
        except Exception:  # noqa: BLE001
            pass
        lock_px = quantize_price(tp1_px, tick)
        exit_side = "BUY" if pos.get("direction") == "SHORT" else "SELL"
        lock_qty = pos.get("qty") or 0.0
        if step:
            lock_qty = floor_to_step(lock_qty, step)
        if lock_qty <= 0:
            return
        sl = self.exchange.place_algo_order(
            symbol, exit_side, "STOP_MARKET", quantity=lock_qty,
            trigger_price=lock_px, reduce_only=True, working_type="MARK_PRICE",
            client_algo_id=self._leg_id(pos_id, "SLLOCK"))
        if sl and sl.get("algoId"):
            self._sl_cancel(symbol, pos)
            pos["sl_order_id"] = sl["algoId"]
            pos["sl_is_algo"] = True
            pos["profit_locked"] = True
            pos["stop_loss"] = lock_px
            pos["sl_qty"] = lock_qty
            self.lifecycle.log(sid, symbol, "profit_locked", price=lock_px, qty=lock_qty,
                               reason=f"advanced SL to TP1 ({lock_px}) after TP2 fill")
            log.info("%s SL advanced to TP1 profit-lock @ %s", pos_id, lock_px)
            self.state.save()
        else:
            log.error("%s profit-lock SL placement FAILED - existing SL left live", pos_id)

    def _classify_vanished_close(self, symbol: str, pos: dict, sid: str,
                                 pos_id: str) -> tuple:
        """Classify how a vanished position actually closed, using exchange
        userTrades as ground truth. Returns (event, why, net, avg_px).

        Pure: computes the classification WITHOUT mutating pos or recording
        PnL — the caller applies those only after the terminal lifecycle
        event is durably logged, so a failed close can be retried without
        double-counting.

        A profitable close is NEVER labeled sl_hit. Realized PnL comes from
        the exchange's own realizedPnl minus commissions (not 0.0).
        Fail-closed: if trades are unavailable, label sl_hit with no PnL
        rather than fabricating a classification.
        """
        direction = pos.get("direction", "SHORT")
        close_side = "BUY" if direction == "SHORT" else "SELL"
        opened = pos.get("opened_at", 0) or 0
        try:
            # startTime=opened_at: after a long downtime the close fills
            # can't be pushed out of the limit window by later trades on
            # the same symbol (reviewer 2026-10-15).
            trades = self.exchange.get_user_trades(
                symbol, limit=100, start_time=opened or None)
        except Exception as e:  # noqa: BLE001 - fail-closed
            log.warning("%s: userTrades unavailable (%s) — sl_hit fallback",
                        pos_id, e)
            return SL_HIT, "sl_hit (trades unavailable)", 0.0, None
        closes = sorted(
            (t for t in trades
             if t.get("side") == close_side and t.get("time", 0) >= opened),
            key=lambda t: t.get("time", 0), reverse=True)
        if not closes:
            return SL_HIT, "sl_hit (no close fills found)", 0.0, None
        # The final close = latest fills covering the remaining qty (partial
        # TP fills earlier were already accounted in pos["realized"]).
        remaining = pos.get("qty", 0) or 0
        take, acc = [], 0.0
        for t in closes:
            take.append(t)
            acc += float(t.get("qty", 0))
            if remaining > 0 and acc >= remaining - 1e-9:
                break
        qty = sum(float(t.get("qty", 0)) for t in take)
        if qty <= 0:
            return SL_HIT, "sl_hit (no close fills found)", 0.0, None
        avg_px = sum(float(t.get("price", 0)) * float(t.get("qty", 0))
                     for t in take) / qty
        net = sum(float(t.get("realizedPnl", 0)) - float(t.get("commission", 0))
                  for t in take)

        def _near(a, b):
            return b and abs(a - b) / b <= 0.005

        if _near(avg_px, pos.get("stop_loss")):
            event, why = SL_HIT, "sl_hit"
        else:
            event = why = None
            for leg, evt in (("tp3", TP3_HIT), ("tp2", TP2_HIT),
                             ("tp1", TP1_HIT)):
                tpleg = (pos.get("tp") or {}).get(leg) or {}
                tp_px = tpleg.get("price")
                if _near(avg_px, tp_px):
                    event, why = evt, f"{evt} (remainder)"
                    break
            if event is None:
                # Not near any bracket level: manual/unknown close. Label it
                # honestly — a profitable close is not an SL.
                pnl_word = "profit" if net >= 0 else "loss"
                event, why = MANUAL_CLOSE, f"manual_close ({pnl_word})"
        log.info("%s closed: %s @ %s (pnl %+0.4f)", pos_id, why, avg_px, net)
        return event, why, net, avg_px


    def _close_position(self, pos_id: str, pos: dict, sid: str,
                        summary: dict, reason: str = "") -> None:
        symbol = pos["symbol"]
        # Determine the terminal event from what filled. Defensive .get()
        # chain: a malformed legacy position must not KeyError its way out
        # of terminal accounting (startup-reconcile path). (pos.get(leg)
        # or {}): a leg explicitly set to None must not AttributeError.
        tp = pos.get("tp") or {}
        net, avg_px = 0.0, None
        if (pos.get("qty") or 0.0) > 0 or "gone" in reason:
            # If the position still held an open quantity, or vanished from
            # the exchange, ground truth for what closed the remainder lives in
            # exchange userTrades. Prior partial TP fills (e.g. TP1) must NOT
            # mask an SL or manual close of the remaining slice.
            event, why, net, avg_px = self._classify_vanished_close(
                symbol, pos, sid, pos_id)
        elif (tp.get("tp3") or {}).get("filled"):
            event, why = TP3_HIT, "tp3_hit"
        elif (tp.get("tp2") or {}).get("filled"):
            event, why = TP2_HIT, "tp2_hit (remainder)"
        elif (tp.get("tp1") or {}).get("filled"):
            event, why = TP1_HIT, "tp1_hit (remainder)"
        else:
            # Position vanished with no TP leg flagged filled: classify via
            # exchange userTrades (ground truth). Never default a profitable
            # close to sl_hit (2026-10-15: LIT +$0.079 and BOME +$0.074 were
            # profitable closes mislabeled sl_hit with realized $0.00).
            # _classify_vanished_close is pure: it returns (event, why, net,
            # avg_px) without mutating pos or recording PnL.
            event, why, net, avg_px = self._classify_vanished_close(
                symbol, pos, sid, pos_id)
        # Cancel any leftover working orders for the symbol legs.
        for leg in ("tp1", "tp2", "tp3"):
            tpleg = tp.get(leg) or {}
            oid = tpleg.get("order_id")
            if oid and not tpleg.get("filled"):
                self.exchange.cancel_order(symbol, order_id=oid)
        if pos.get("sl_order_id"):
            self._sl_cancel(symbol, pos)
        pos.pop("pending_close", None)  # any armed retry is moot once closed
        pos["closed"] = True
        pos["closed_reason"] = why
        pos["closed_at"] = int(time.time() * 1000)
        # terminal=True marks the one outcome event per signal: report.py
        # must not confuse it with per-leg tp1/2/3 fill events (same state
        # names) when attributing the trade's final PnL.
        self.lifecycle.log(sid, symbol, event, reason=why,
                           realized=round(pos.get("realized", 0.0) + net, 4),
                           terminal=True)
        # Mutations only AFTER the terminal event is durably logged: a
        # failed close can be retried next startup without double-counting.
        if avg_px is not None:
            pos["close_price"] = avg_px
        if net:
            pos["realized"] = pos.get("realized", 0.0) + net
            self.risk.record_realized(net)
        summary["managed"] += 1
        self.state.save()

    def _mark_pending_close(self, pos: dict, closed_reason: str,
                            lifecycle_state: str, reason: str) -> None:
        """Arm the unconditional close retry for a residual left open by a
        failed/partial/zero market close. The brackets are already
        cancelled at this point, so the residual must never wait on the
        original trigger — _manage_one retries _flat_position until done.
        Persisted in state so the retry survives a restart."""
        pos["pending_close"] = {"closed_reason": closed_reason,
                                "lifecycle_state": lifecycle_state,
                                "reason": reason}
        self.state.save()

    def _flat_position(self, pos_id: str, pos: dict, sid: str,
                       summary: dict, mark_px: float, closed_reason: str,
                       lifecycle_state: str, reason: str = "") -> None:
        """Cancel working brackets, then market-close the full position.

        Shared by profit_take / trail_stop / time_stop. Realized PnL is
        computed from the actual fill (fees deducted). A terminal lifecycle
        event is logged once the position is fully closed. A partial market
        fill does NOT mark the position closed: only the filled qty is
        accounted (non-terminal event), the residual stays under management
        and the close is retried UNCONDITIONALLY via pos["pending_close"]
        (never gated on the original trigger — the residual has no
        exchange-side protection left). Never a phantom flat.
        Callers decide when to invoke; this never triggers on its own.
        """
        symbol = pos["symbol"]
        direction = pos.get("direction", "LONG")
        qty = pos["qty"]
        for leg in ("tp1", "tp2", "tp3"):
            tpleg = (pos.get("tp") or {}).get(leg) or {}
            oid = tpleg.get("order_id")
            if oid and not tpleg.get("filled"):
                try:
                    self.exchange.cancel_order(symbol, order_id=oid)
                except Exception:  # noqa: BLE001 - best effort
                    log.warning("%s %s: cancel %s failed", pos_id,
                                closed_reason, leg)
        if pos.get("sl_order_id"):
            self._sl_cancel(symbol, pos)
        side = "SELL" if direction == "LONG" else "BUY"
        order = self.exchange.place_order(symbol, side, "MARKET",
                                          quantity=qty, reduce_only=True)
        if not order:
            log.warning("%s %s market close failed, retry next cycle",
                        pos_id, closed_reason)
            self._mark_pending_close(pos, closed_reason, lifecycle_state,
                                     reason)
            return
        # Partial-fill guard: a MARKET close can partially fill (fast market,
        # reduce-only clamp). Never mark a phantom flat — account only what
        # actually filled and keep the residual under management; the close
        # is retried next cycle by the pending_close path above, NOT gated
        # on the original trigger. Mirrors the executedQty handling of the
        # TP-leg fill path.
        status = str(order.get("status") or "").upper()
        exec_raw = order.get("executedQty")
        if exec_raw is None:
            # Wrapper omitted fill info (e.g. test fakes): trust FILLED as a
            # full fill, anything else as no fill — never invent a quantity.
            closed_qty = qty if status == "FILLED" else 0.0
        else:
            try:
                closed_qty = min(qty, max(0.0, float(exec_raw)))
            except (TypeError, ValueError):
                log.warning("%s non-numeric executedQty %r — treating as "
                            "no fill info", pos_id, exec_raw)
                closed_qty = qty if status == "FILLED" else 0.0
        if closed_qty <= 0:
            log.warning("%s %s market close filled nothing (%s), "
                        "retry next cycle", pos_id, closed_reason, status)
            self._mark_pending_close(pos, closed_reason, lifecycle_state,
                                     reason)
            return
        fill_px = float(order.get("avgPrice") or mark_px)
        sign = 1.0 if direction == "LONG" else -1.0
        pnl = sign * (fill_px - pos["entry_price"]) * closed_qty
        fee = fill_px * closed_qty * _FEE_PCT / 100
        net = pnl - fee
        pos["realized"] = pos.get("realized", 0.0) + net
        self.risk.record_realized(net)
        remaining = qty - closed_qty
        done = remaining <= _CLOSE_DUST_QTY
        pos["qty"] = 0.0 if done else remaining
        if done:
            pos.pop("pending_close", None)
            pos["closed"] = True
            pos["closed_reason"] = closed_reason
            pos["closed_at"] = int(time.time() * 1000)
            pos["close_price"] = fill_px
        else:
            # Residual left open with brackets already cancelled: arm the
            # unconditional retry so it can never strand unprotected.
            self._mark_pending_close(pos, closed_reason, lifecycle_state,
                                     reason)
        # H4: consumers (metrics/report/notify) read "realized" from the
        # terminal event; "pnl" alone leaves them at 0. pos["realized"]
        # already includes net by this point.
        self.lifecycle.log(sid, symbol, lifecycle_state, price=fill_px,
                           qty=closed_qty, pnl=round(net, 4),
                           realized=round(pos["realized"], 4), terminal=done,
                           reason=(reason if done else
                                   f"{reason} (partial fill, "
                                   f"{remaining:g} remaining)"))
        summary["managed"] += 1
        self.state.save()
        log.info("%s %s closed @ %s (pnl %+0.2f)%s", pos_id, closed_reason,
                 fill_px, net, "" if done else f" — {remaining:g} remaining")

    def _take_profit(self, pos_id: str, pos: dict, sid: str,
                     summary: dict, mark_px: float) -> None:
        """Market-close the full position at the +PROFIT_TAKE_PCT% target."""
        self._flat_position(pos_id, pos, sid, summary, mark_px,
                            "profit_take", PROFIT_TAKE,
                            reason=(f"profit_take +{config.PROFIT_TAKE_PCT:g}% "
                                    "of margin"))

    def _trail_check(self, pos_id: str, pos: dict, sid: str,
                     summary: dict, mark: float) -> bool:
        """Trailing-stop state machine. Returns True if the position was
        closed by the trailing stop this cycle.

        Activation: unrealized PnL >= TRAIL_TRIGGER_PCT% of margin.
        After activation the peak price is trailed; a pullback of
        TRAIL_DISTANCE_PCT% from the peak market-closes the position.
        """
        direction = pos.get("direction", "LONG")
        sign = 1.0 if direction == "LONG" else -1.0
        unreal = sign * (mark - pos["entry_price"]) * pos["qty"]
        margin = pos.get("margin_usdt") or 0.0
        if not pos.get("trail_active"):
            if margin > 0 and unreal >= margin * config.TRAIL_TRIGGER_PCT / 100:
                pos["trail_active"] = True
                pos["trail_peak"] = mark
                self.state.save()
                log.info("%s trailing stop activated @ %s", pos_id, mark)
            return False
        peak = pos.get("trail_peak") or mark
        if ((direction == "LONG" and mark > peak)
                or (direction == "SHORT" and mark < peak)):
            pos["trail_peak"] = mark
            peak = mark
            self.state.save()
        dist = config.TRAIL_DISTANCE_PCT / 100
        if direction == "LONG":
            hit = mark <= peak * (1 - dist)
        else:
            hit = mark >= peak * (1 + dist)
        if hit:
            self._flat_position(
                pos_id, pos, sid, summary, mark, "trail_stop", TRAIL_STOP,
                reason=(f"trail_stop -{config.TRAIL_DISTANCE_PCT:g}% "
                        f"from peak {peak:g}"))
            return True
        return False

    def _close_reconciled_positions(self, pos_ids: list) -> None:
        """Attribute terminal lifecycle events to startup-reconciled closes.

        reconcile() marks state positions that vanished from the exchange
        while the bot was down. Without this step those trades vanish from
        lifecycle accounting entirely: no outcome event, no PnL attribution,
        and the daily realized-PnL misses the close. Bracketed positions go
        through the full _close_position path (userTrades ground truth).
        Positions without brackets get their entry order verified first: a
        FILLED entry means the position did exist (crash before brackets),
        so it is classified via _close_position; otherwise the working
        entry order is cancelled (never leave a naked fill) and a CANCELLED
        terminal event is logged.
        """
        summary = {"managed": 0, "errors": []}
        for pos_id in pos_ids or []:
            pos = self.state.get("positions", {}).get(pos_id)
            if not pos:
                continue
            sid = pos.get("signal_id", "")
            symbol = pos.get("symbol", "?")
            if self.lifecycle.has_terminal_event(sid):
                # Retry dedupe (review 2026-10-16): a previous attempt
                # already logged this sid's terminal outcome — attributing
                # again would double-log the event AND double-count PnL
                # (risk.record_realized saves state AFTER lifecycle.log, so
                # a crash in that window retries with PnL already recorded).
                log.warning("reconcile close %s: terminal event for %s "
                            "already logged — skipping", pos_id, sid)
                continue
            try:
                if pos.get("brackets_placed") or self._entry_filled(pos):
                    self._close_position(pos_id, pos, sid, summary,
                                         reason="gone from exchange "
                                         "at startup")
                else:
                    try:
                        self.exchange.cancel_order(
                            symbol, order_id=pos.get("entry_order_id"))
                    except Exception:  # noqa: BLE001 - already gone is fine
                        pass
                    self.lifecycle.log(sid, symbol, CANCELLED,
                                       reason="entry unfilled at startup "
                                       "reconcile", terminal=True)
                    pos["closed"] = True
                    pos["closed_reason"] = "entry unfilled at startup reconcile"
                    pos["closed_at"] = int(time.time() * 1000)
                    summary["managed"] += 1
                    self.state.save()
            except Exception:  # noqa: BLE001 - one position never kills startup
                log.exception("reconcile close %s failed", pos_id)
                summary["errors"].append(f"reconcile close {pos_id} failed")
                # Attribution failed: clear the closed flag so the next
                # startup retries (reconcile only skips already-closed
                # positions). Retry is safe against double-counting: the
                # has_terminal_event guard at the top of the loop skips any
                # sid whose terminal outcome was already logged (a crash
                # between lifecycle.log and the later state.save leaves the
                # PnL recorded but the position un-closed).
                pos.pop("closed", None)
                try:
                    self.state.save()
                except Exception:  # noqa: BLE001 - best effort
                    log.exception("reconcile close %s: state.save failed",
                                  pos_id)
        if summary["managed"] or summary["errors"]:
            log.info("reconcile close: managed=%d errors=%s",
                     summary["managed"], summary["errors"])

    def _entry_filled(self, pos: dict) -> bool:
        """True when the entry order actually filled (crash landed between
        entry fill and bracket placement). Never raises."""
        oid = pos.get("entry_order_id")
        if oid is None:
            return False
        try:
            o = self.exchange.get_order(pos.get("symbol", "?"), order_id=oid)
        except Exception:  # noqa: BLE001 - unknown -> treat as unfilled
            return False
        return bool(o and o.get("status") == "FILLED")

    # ------------------------------------------------------------------ loop
    def run_loop(self, stop_after: int | None = None,
                 from_db: bool = False, db_path: str = "signals.db") -> None:
        """Run scan cycles forever (or stop_after cycles, for tests/smoke).

        from_db=True takes signals from the scanner's signals.db instead of
        scanning the market directly (two-stage pipeline).

        Graceful shutdown (1.3): a SIGINT/SIGTERM handler installed by
        run_executor sets self.shutdown_event. The event is checked only at
        safe points (between cycles) — an in-flight cycle, including any
        order placement, always runs to completion; then state is flushed
        and the loop exits. The pause between cycles is event.wait() so a
        shutdown received during the pause wakes the loop immediately.
        """
        # Startup reconcile: adopt/close vs the real exchange state.
        try:
            rec = self.state.reconcile(self.exchange.get_positions())
            if rec["adopted"] or rec["closed"]:
                log.warning("startup reconcile: adopted=%s closed=%s",
                            rec["adopted"], rec["closed"])
            self._close_reconciled_positions(rec["closed"])
        except Exception:  # noqa: BLE001 - paper broker always succeeds
            log.exception("startup reconcile failed")
        self.cleanup_orphan_orders()
        n = 0
        while not self.shutdown_event.is_set():
            log.info("--- scan cycle %d ---", n + 1)
            try:
                summary = (self.scan_from_db(db_path) if from_db
                           else self.scan_once())
                log.info("cycle summary: %s", summary)
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("scan cycle failed")
            n += 1
            # Heartbeat for the dashboard /api/health check (read by the
            # dashboard only; never fatal to the trading loop).
            try:
                self.state.set("last_cycle_ts", int(time.time() * 1000))
                self.state.save()
            except OSError:
                log.warning("heartbeat: state.save failed")
            if stop_after is not None and n >= stop_after:
                break
            # wait() returns True (and we break) when the shutdown event is
            # set; otherwise it sleeps the full interval like time.sleep did.
            if self.shutdown_event.wait(self.poll_interval_min * 60):
                break
        try:
            self.state.save()
        except Exception:  # noqa: BLE001 - best-effort flush on exit
            log.exception("shutdown: state.save failed")
        log.warning("executor loop exited cleanly after %d cycle(s)", n)

    def cleanup_orphan_orders(self) -> list[str]:
        """Cancel open exchange orders that don't belong to any active managed position.

        Orphans happen when a run crashes after placing orders but before
        tracking them in state. Opt-in via CANCEL_ORPHAN_ORDERS_ON_STARTUP
        (user feature 2026-10-16). Returns list of "SYMBOL:orderId" cancelled.
        """
        if not getattr(config, "CANCEL_ORPHAN_ORDERS_ON_STARTUP", True):
            return []
        cancelled = []

        # Order IDs known to belong to live managed positions. Built from
        # state alone, before either sweep, so the algo sweep can use them
        # even if the regular sweep raises.
        known_order_ids = set()
        for pos in self.state.get("positions", {}).values():
            if pos.get("closed"):
                continue
            if pos.get("entry_order_id"):
                known_order_ids.add(str(pos["entry_order_id"]))
            if pos.get("sl_order_id"):
                known_order_ids.add(str(pos["sl_order_id"]))
            for leg in ("tp1", "tp2", "tp3"):
                tp_data = pos.get("tp", {}).get(leg, {})
                if tp_data.get("order_id"):
                    known_order_ids.add(str(tp_data["order_id"]))

        # H6: adopted (unmanaged) positions' real orders must NOT be
        # cancelled. reconcile() cannot know their order IDs (it stores
        # tp={} / sl_order_id=None), so every live order on those
        # symbols would look like an orphan. Exempt their symbols and
        # surface the orders for manual review instead.
        unmanaged_symbols = {
            str(pos.get("symbol") or "") for pos in
            self.state.get("positions", {}).values()
            if not pos.get("closed") and pos.get("unmanaged")
        }
        unmanaged_symbols.discard("")

        # --- regular-order sweep ---
        try:
            get_open = getattr(self.exchange, "get_open_orders", None)
            open_orders = get_open() if callable(get_open) else []
            for o in open_orders or []:
                oid = str(o.get("orderId", ""))
                sym = o.get("symbol", "")
                if oid and oid not in known_order_ids:
                    if sym in unmanaged_symbols:
                        log.warning(
                            "Keeping order %s (%s %s @ %s): symbol has an "
                            "adopted/unmanaged position - manual review, "
                            "NOT cancelling", oid, sym, o.get("side"),
                            o.get("price"))
                        continue
                    log.warning("Cancelling orphan order %s (%s %s @ %s) not tracked in state",
                                oid, sym, o.get("side"), o.get("price"))
                    try:
                        ok = self.exchange.cancel_order(sym, order_id=int(oid))
                        if ok:
                            cancelled.append(f"{sym}:{oid}")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            log.exception("cleanup_orphan_orders: regular-order sweep failed")

        # --- algo-order sweep (M4) ---
        # Orphan ALGO orders (e.g. a STOP_MARKET SL placed via
        # place_algo_order in _place_brackets, then a crash before the
        # state save) are invisible to the regular-order sweep above. An
        # orphan algo stop can market-close an unrelated later position
        # on the same symbol - so the algo book is swept on EVERY run.
        # (2026-10-17 cyb: this block used to live inside the except
        # handler above, i.e. it only ran when the regular sweep raised -
        # dead code on the happy path.)
        try:
            get_algo = getattr(self.exchange, "get_open_algo_orders", None)
            cancel_algo = getattr(self.exchange, "cancel_algo_order", None)
            if callable(get_algo) and callable(cancel_algo):
                try:
                    algo_orders = get_algo() or []
                except Exception:  # noqa: BLE001
                    algo_orders = []
                    log.warning("orphan sweep: get_open_algo_orders failed")
                for o in algo_orders:
                    aid = str(o.get("algoId", "") or "")
                    sym = str(o.get("symbol", "") or "")
                    if not aid or aid in known_order_ids:
                        continue
                    if sym in unmanaged_symbols:
                        log.warning(
                            "Keeping algo order %s (%s): symbol has an "
                            "adopted/unmanaged position - manual review, "
                            "NOT cancelling", aid, sym)
                        continue
                    log.warning(
                        "Cancelling orphan algo order %s (%s) not tracked "
                        "in state", aid, sym)
                    try:
                        if cancel_algo(sym, int(aid)):
                            cancelled.append(f"{sym}:algo:{aid}")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            log.exception("cleanup_orphan_orders: algo-order sweep failed")
        return cancelled
