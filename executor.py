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
"""

from __future__ import annotations

import json
import logging
import time

import config
from lifecycle import LifecycleLogger
from lifecycle import (
    GENERATED, APPROVED, REJECTED, ENTRY_TRIGGERED, POSITION_OPENED,
    TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT, EXPIRED, CANCELLED,
)
from risk import floor_to_step

log = logging.getLogger("executor")

_FEE_PCT = 0.05  # fee estimate per fill for PnL accounting (matches backtest)
_ENTRY_TIMEOUT_HOURS = 6  # cancel an unfilled entry order after this long

# Dashboard settings defaults — canonical source is config.DASHBOARD_DEFAULTS
# (dashboard.py writes dashboard_settings.json; the executor honors it live,
# without a restart).
_DASH_DEFAULTS = config.DASHBOARD_DEFAULTS


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
                filters: dict, risk_pct: float | None = None) -> tuple[float, str]:
    """Risk-based sizing. Returns (qty, note); qty == 0 means skip.

    qty = (balance * RISK_PCT_PER_TRADE/100) / |entry - stop|, floored to
    stepSize, honoring minQty / minNotional and the per-symbol exposure cap.
    risk_pct overrides the config default (dashboard may only tighten it).
    """
    if balance is None or balance <= 0:
        return 0.0, "balance unavailable"
    rp = config.RISK_PCT_PER_TRADE if risk_pct is None else risk_pct
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
                     f"(risk ${risk_usdt:.2f}, stop dist {per_unit})")
    if qty * entry < min_notional:
        return 0.0, (f"notional ${qty * entry:.2f} < minNotional "
                     f"${min_notional:.2f}")
    return qty, (f"risk ${risk_usdt:.2f} / stop-dist {per_unit} -> qty {qty}")


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


def resolve_universe(exchange, settings: dict,
                     curated: list[str] | None = None) -> list[str]:
    """Universe for this scan cycle.

    - "curated" (default): the built-in liquid list (config.UNIVERSE).
    - "top_volume": top-N symbols by 24h quote volume from the exchange.
      Any fetch failure falls back to the curated list (fail-safe: keep
      trading the known coins instead of scanning nothing).
    """
    curated = list(curated if curated is not None else config.UNIVERSE)
    if settings.get("universe_mode") != "top_volume":
        return curated
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

        The file holds per-mode sections {"live": {...}, "testnet": {...}};
        the executor's settings_mode picks its section. A legacy flat file
        (no mode keys) is treated as the live section.
        """
        s = dict(_DASH_DEFAULTS)
        try:
            with open(config.DASHBOARD_SETTINGS_FILE, "r",
                      encoding="utf-8") as f:
                loaded = json.load(f) or {}
            if isinstance(loaded, dict) and any(
                    k in loaded for k in ("live", "testnet", "paper")):
                loaded = loaded.get(self.settings_mode) or {}
            for k in s:
                if k in loaded:
                    s[k] = loaded[k]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return s

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
        universe = resolve_universe(self.exchange, self._dash_settings(),
                                    self.universe)
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
        db = SignalDB(db_path)
        try:
            db.expire_old()
            rows = db.get_fresh_signals(limit=config.DB_SCAN_LIMIT)
            log.info("db signals: %d fresh", len(rows))
            for row in rows:
                sid = row["signal_id"]
                symbol = row["symbol"]
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
                     atr_mult: float = 1.5) -> tuple[bool, str]:
        """Manual market LONG entry (TEST ONLY).

        BYPASSES every STRICT signal gate — no score, no MTF cascade, no
        vetoes, no volume check. Only the mechanical safety layer still
        applies: live-price freshness, symbol filters, leverage/margin
        setup, fixed-margin sizing, risk gates (kill switch, caps), and
        the standard SL/TP bracket machinery.

        Levels: entry = live market price; SL = entry - atr_mult x ATR(15m);
        TP1/2/3 at the config RR ladder (1.5 / 2.25 / 3.0). Idempotent per
        15m candle via the signal id.
        """
        from data import normalize_symbol
        from indicators import atr_wilder

        symbol = normalize_symbol(symbol)
        if not symbol:
            return False, "unknown symbol"
        # 1. Live price (fail-closed).
        try:
            live = self.market.get_live_price(symbol)
        except Exception:  # noqa: BLE001
            live = None
        if live is None or live <= 0:
            return False, "live price unavailable - holding (fail-closed)"
        # 2. ATR from 15m candles (fail-closed on thin data).
        try:
            candles = self.market.get_klines(symbol, "15m", limit=60)
        except Exception:  # noqa: BLE001
            candles = []
        if len(candles) < config.ATR_PERIOD + 2:
            return False, (f"only {len(candles)} 15m candles - "
                           f"need {config.ATR_PERIOD + 2} for ATR")
        atr = atr_wilder(candles, config.ATR_PERIOD)
        if not atr or atr <= 0:
            return False, "ATR unavailable - holding (fail-closed)"
        # 3. Bracket levels around the live price.
        entry = float(live)
        stop = entry - atr_mult * atr
        risk_dist = entry - stop
        signal = {
            "symbol": symbol, "direction": "LONG", "interval": "15m",
            "entry": entry, "entry_low": entry, "entry_high": entry,
            "stop_loss": stop,
            "tp1": entry + risk_dist * config.TP1_RR,
            "tp2": entry + risk_dist * config.TP2_RR,
            "tp3": entry + risk_dist * config.TP3_RR,
            "quality": 0, "manual": True,
            "note": "manual market entry - bypasses STRICT gates",
        }
        # 4. Idempotency per 15m candle; then the standard open flow.
        close_ms = signal_candle_close_ms(self.market, symbol)
        sid = f"{symbol}:manual:{close_ms}"
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
            return True, (f"MANUAL LONG {symbol} @ {entry:.6g} "
                          f"({lev}x isolated"
                          f"{f', ${margin:.0f} margin' if margin else ''}) | "
                          f"SL {stop:.6g} | "
                          f"TP1 {signal['tp1']:.6g} TP2 {signal['tp2']:.6g} "
                          f"TP3 {signal['tp3']:.6g} | {note}")
        return False, f"manual entry rejected: {note}"

    def _try_open(self, symbol: str, signal: dict, sid: str,
                  balance: float | None) -> tuple[bool, str]:
        entry = float(signal["entry"])
        # 1. Freshness re-check with a LIVE price, right before ordering.
        try:
            live = self.market.get_live_price(symbol)
        except Exception:  # noqa: BLE001
            live = None
        if live is None or live <= 0:
            return self._reject(
                symbol, sid, "freshness re-check: live price unavailable "
                             "(fail-closed)")
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
            qty, note = compute_qty(balance, entry, float(signal["stop_loss"]),
                                    filters,
                                    risk_pct=self._effective_risk_pct())
        if qty <= 0:
            return self._reject(symbol, sid, f"sizing: {note}")
        notional = qty * entry

        # 4. Risk gates.
        open_count = len(self._open_positions())
        ok, reason = self.risk.check_can_open(symbol, notional, open_count)
        if not ok:
            return self._reject(symbol, sid, f"risk: {reason}")

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
        entry_px = round(entry / tick) * tick if tick else entry
        order = self.exchange.place_order(
            symbol, "BUY", entry_type,
            quantity=qty,
            price=None if entry_type == "MARKET" else entry_px,
            client_order_id=self._leg_id(pos_id, "EN"))
        if not order:
            return self._reject(symbol, sid, "entry order placement failed")

        self.state.get("positions", {})[pos_id] = {
            "symbol": symbol, "signal_id": sid, "direction": "LONG",
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
                pnl = (fill_px - pos["entry_price"]) * fill_qty
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
        pos["qty"] = max(0.0, remaining)

        # Breakeven: TP1 filled -> move SL to the entry price.
        if (config.EXEC_BREAKEVEN_AFTER_TP1 and pos["tp"]["tp1"]["filled"]
                and not pos.get("breakeven_moved") and pos["qty"] > 0):
            self._move_sl_to_breakeven(pos_id, pos, sid)

        # Terminal state: position fully closed.
        if pos["qty"] <= 0:
            self._close_position(pos_id, pos, sid, summary)
            return
        # Cross-check against the exchange: SL may have filled without us
        # seeing the order event (e.g. after a restart).
        xp = {p["symbol"]: p for p in self.exchange.get_positions()}
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
            pos["closed"] = True
            pos["closed_reason"] = f"entry order {status}"
            self.lifecycle.log(sid, symbol, CANCELLED,
                               reason=f"entry order {status}")
            self.state.save()
        return False

    def _expire_stale_entry(self, pos_id: str, pos: dict, sid: str) -> None:
        age_h = (time.time() * 1000 - pos["opened_at"]) / 3_600_000
        if age_h < _ENTRY_TIMEOUT_HOURS:
            return
        symbol = pos["symbol"]
        self.exchange.cancel_order(symbol, order_id=pos.get("entry_order_id"))
        pos["closed"] = True
        pos["closed_reason"] = f"entry unfilled after {_ENTRY_TIMEOUT_HOURS}h"
        self.lifecycle.log(sid, symbol, EXPIRED,
                           reason=pos["closed_reason"])
        log.info("%s expired: %s", pos_id, pos["closed_reason"])
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

        def _undo():
            for aid in placed_algo:
                self.exchange.cancel_algo_order(symbol, aid)
            for oid in placed_regular:
                self.exchange.cancel_order(symbol, order_id=oid)

        tick = None
        try:
            filters = self.exchange.get_symbol_filters(symbol) or {}
            tick = filters.get("tickSize") or None
        except Exception:  # noqa: BLE001
            pass

        def _px(p: float) -> float:
            return round(p / tick) * tick if tick else p

        sl = self.exchange.place_algo_order(
            symbol, "SELL", "STOP_MARKET", quantity=pos["qty"],
            trigger_price=_px(pos["stop_loss"]), reduce_only=True,
            working_type="MARK_PRICE",
            client_algo_id=self._leg_id(pos_id, "SL"))
        if not sl or not sl.get("algoId"):
            return False
        placed_algo.append(sl["algoId"])
        pos["sl_order_id"] = sl["algoId"]
        pos["sl_is_algo"] = True

        for leg in ("tp1", "tp2", "tp3"):
            tp = pos["tp"][leg]
            if tp["qty"] <= 0:
                continue
            o = self.exchange.place_order(
                symbol, "SELL", "LIMIT", quantity=tp["qty"],
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

    def _move_sl_to_breakeven(self, pos_id: str, pos: dict, sid: str) -> None:
        symbol = pos["symbol"]
        # Route through _sl_cancel: the working SL may be an algo order
        # (sl_is_algo=True); cancel_order alone would leave it live.
        self._sl_cancel(symbol, pos)
        tick = None
        try:
            tick = (self.exchange.get_symbol_filters(symbol) or {}).get("tickSize")
        except Exception:  # noqa: BLE001
            pass
        be = round(pos["entry_price"] / tick) * tick if tick else pos["entry_price"]
        sl = self.exchange.place_order(
            symbol, "SELL", "STOP_MARKET", quantity=pos["qty"],
            stop_price=be, reduce_only=True, working_type="MARK_PRICE",
            client_order_id=self._leg_id(pos_id, "SLBE"))
        if sl:
            pos["sl_order_id"] = sl["orderId"]
            pos["sl_is_algo"] = False  # breakeven leg is a regular STOP_MARKET
            pos["breakeven_moved"] = True
            pos["stop_loss"] = be
            log.info("%s SL moved to breakeven @ %s", pos_id, be)
            self.state.save()
        else:
            log.error("%s breakeven SL placement FAILED - original SL kept "
                      "if still live", pos_id)

    def _close_position(self, pos_id: str, pos: dict, sid: str,
                        summary: dict, reason: str = "") -> None:
        symbol = pos["symbol"]
        # Determine the terminal event from what filled.
        if pos["tp"]["tp3"]["filled"]:
            event, why = TP3_HIT, "tp3_hit"
        elif pos["tp"]["tp2"]["filled"]:
            event, why = TP2_HIT, "tp2_hit (remainder)"
        elif pos["tp"]["tp1"]["filled"]:
            event, why = TP1_HIT, "tp1_hit (remainder)"
        else:
            # SL filled (or position vanished): check the SL order.
            o = self._sl_status(symbol, pos)
            if o and o.get("status") == "FILLED":
                fill_px = float(o.get("avgPrice") or pos["stop_loss"])
                pnl = (fill_px - pos["entry_price"]) * pos["qty"]
                fee = fill_px * pos["qty"] * _FEE_PCT / 100
                net = pnl - fee
                pos["realized"] = pos.get("realized", 0.0) + net
                self.risk.record_realized(net)
                log.info("%s SL hit @ %s (pnl %+0.2f)", pos_id, fill_px, net)
            event, why = SL_HIT, reason or "sl_hit"
        # Cancel any leftover working orders for the symbol legs.
        for leg in ("tp1", "tp2", "tp3"):
            oid = pos["tp"][leg]["order_id"]
            if oid and not pos["tp"][leg]["filled"]:
                self.exchange.cancel_order(symbol, order_id=oid)
        if pos.get("sl_order_id"):
            self._sl_cancel(symbol, pos)
        pos["closed"] = True
        pos["closed_reason"] = why
        pos["closed_at"] = int(time.time() * 1000)
        self.lifecycle.log(sid, symbol, event, reason=why,
                           realized=round(pos.get("realized", 0.0), 4))
        summary["managed"] += 1
        self.state.save()

    # ------------------------------------------------------------------ loop
    def run_loop(self, stop_after: int | None = None,
                 from_db: bool = False, db_path: str = "signals.db") -> None:
        """Run scan cycles forever (or stop_after cycles, for tests/smoke).

        from_db=True takes signals from the scanner's signals.db instead of
        scanning the market directly (two-stage pipeline).
        """
        # Startup reconcile: adopt/close vs the real exchange state.
        try:
            rec = self.state.reconcile(self.exchange.get_positions())
            if rec["adopted"] or rec["closed"]:
                log.warning("startup reconcile: adopted=%s closed=%s",
                            rec["adopted"], rec["closed"])
        except Exception:  # noqa: BLE001 - paper broker always succeeds
            log.exception("startup reconcile failed")
        n = 0
        while True:
            log.info("--- scan cycle %d ---", n + 1)
            try:
                summary = (self.scan_from_db(db_path) if from_db
                           else self.scan_once())
                log.info("cycle summary: %s", summary)
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("scan cycle failed")
            n += 1
            if stop_after is not None and n >= stop_after:
                break
            time.sleep(self.poll_interval_min * 60)
