# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for backtest.py — ROADMAP ITEM 4 (honest backtester).

T8:  backtest trade simulation on a synthetic fixture with a hand-computed
     outcome (documented step-by-step below).
T12: same-candle SL+TP -> conservative SL-first ordering for longs.
Plus: NO_ENTRY/expiry, MAX_HOLD, BacktestMarket no-look-ahead, metrics math,
and degraded-mode pipeline wiring (evaluate_symbol runs the production
pipeline on a time-sliced market without raising).
"""

import pytest

import config
import vetoes
from backtest import (
    BacktestMarket,
    compute_metrics,
    simulate_trade,
)
from signal_engine import evaluate_symbol
from tests.test_indicators import candles_from_closes, uptrend


@pytest.fixture(autouse=True)
def _reset_veto_caches():
    vetoes.reset_caches()
    yield
    vetoes.reset_caches()


def mk_candle(t_ms, o, h, l, c, v=100.0):
    return {"open_time": t_ms, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "close_time": t_ms + 900_000 - 1}


def stamp_15m(candles, t0=1_700_000_000_000):
    """Add 15m open/close times to candles built by candles_from_closes()."""
    out = []
    for i, c in enumerate(candles):
        t = t0 + i * 900_000
        out.append({**c, "open_time": t, "close_time": t + 900_000 - 1})
    return out


def mk_signal(**kw):
    s = {"symbol": "BTCUSDT", "direction": "LONG", "entry": 100.0,
         "entry_low": 99.8, "entry_high": 100.2, "stop_loss": 98.0,
         "tp1": 103.0, "tp2": 104.5, "tp3": 106.0,
         "quality": 80, "strength": 4, "regime": "TRENDING BULLISH"}
    s.update(kw)
    return s


# ------------------------------------------------------------------ T8
def test_t8_hand_computed_trade():
    """Hand-computed fixture. signal: entry=100, SL=98 (risk 2), TP 103 /
    104.5 / 106, risk_usd=100 -> qty=50. per_side cost = 0.07%, funding
    0.01%/8h on open notional.

    c1: low 99.8 touches entry -> fill 50 @100.
        funding 50*100*0.0001/32 = 0.015625
    c2: high 103.5 -> TP1 fills 20 @103 (+60 gross, fee 1.442); SL->100.
        funding 0.015625 (cum 0.03125)
    c3: high 104.6 -> TP2 fills 15 @104.5 (+67.5 gross, fee 1.09725).
        funding 30*100*0.0001/32 = 0.009375 (cum 0.040625)
    c4: low 99.9 <= breakeven SL 100 -> remaining 15 stopped @100
        (+0 gross, fee 1.05). funding 15*100*0.0001/32 = 0.0046875
        (cum 0.0453125)
    entry fee 50*100*0.0007 = 3.50.
    realized = 60 + 67.5 + 0 = 127.5
    fees     = 3.50 + 1.442 + 1.09725 + 1.05 = 7.08925
    pnl      = 127.5 - 7.08925 - 0.0453125 = 120.3654375
    R        = 1.203654375
    """
    t0 = 1_700_000_000_000
    candles = [
        mk_candle(t0, 100.5, 101.0, 100.0, 100.5),          # c0 signal candle
        mk_candle(t0 + 900_000, 100.2, 100.4, 99.8, 100.1),  # c1 entry
        mk_candle(t0 + 1_800_000, 100.6, 103.5, 100.5, 102.0),  # c2 TP1
        mk_candle(t0 + 2_700_000, 102.0, 104.6, 101.5, 104.0),  # c3 TP2
        mk_candle(t0 + 3_600_000, 100.8, 101.0, 99.9, 100.3),   # c4 BE stop
    ]
    tr = simulate_trade(mk_signal(), candles, 0, 100.0)
    assert tr["status"] == "CLOSED"
    assert tr["exit_reason"] == "SL"  # breakeven stop after TP1+TP2
    assert tr["tp_fills"] == [True, True, False]
    assert tr["qty"] == pytest.approx(50.0)
    assert tr["fees_usd"] == pytest.approx(7.08925, abs=1e-6)
    assert tr["funding_usd"] == pytest.approx(0.0453125, abs=1e-9)
    assert tr["pnl_usd"] == pytest.approx(120.3654375, abs=1e-6)
    assert tr["r_multiple"] == pytest.approx(1.203654375, abs=1e-9)


def test_backtest_lock_profit_after_tp2(monkeypatch):
    """When BACKTEST_LOCK_PROFIT_AFTER_TP2 is enabled, after TP2 fills,
    SL moves to TP1 price (103.0) instead of remaining at breakeven (100.0).
    In fixture c4, low 99.9 triggers stop at 103.0.
    Remaining 15 exits @ 103 (+45 gross profit vs breakeven).
    Fees: 7.12075.
    PnL: 127.5 + 45 - 7.12075 - 0.0453125 = 165.3339375.
    """
    monkeypatch.setattr(config, "BACKTEST_LOCK_PROFIT_AFTER_TP2", True)
    t0 = 1_700_000_000_000
    candles = [
        mk_candle(t0, 100.5, 101.0, 100.0, 100.5),          # c0 signal candle
        mk_candle(t0 + 900_000, 100.2, 100.4, 99.8, 100.1),  # c1 entry
        mk_candle(t0 + 1_800_000, 100.6, 103.5, 100.5, 102.0),  # c2 TP1
        mk_candle(t0 + 2_700_000, 102.0, 104.6, 101.5, 104.0),  # c3 TP2
        mk_candle(t0 + 3_600_000, 100.8, 101.0, 99.9, 100.3),   # c4 stop at TP1
    ]
    tr = simulate_trade(mk_signal(), candles, 0, 100.0)
    assert tr["status"] == "CLOSED"
    assert tr["exit_reason"] == "SL"
    assert tr["tp_fills"] == [True, True, False]
    assert tr["fees_usd"] == pytest.approx(7.12075, abs=1e-6)
    assert tr["pnl_usd"] == pytest.approx(165.3339375, abs=1e-6)
    assert tr["r_multiple"] == pytest.approx(1.653339375, abs=1e-9)


# ------------------------------------------------------------------ T12
def test_t12_same_candle_sl_tp_conservative():
    """A candle touching BOTH stop and target must resolve to the STOP for
    longs (conservative). A TP-first implementation would show a profit here;
    the correct implementation shows a ~-1.14R loss.

    Hand calc: qty=100/1=100. c2 low 98.5<=SL 99 -> stopped.
    realized=-100; fees=7.0+6.93=13.93; funding=2*0.03125=0.0625.
    pnl=-113.9925, R=-1.139925.
    """
    t0 = 1_700_000_000_000
    candles = [
        mk_candle(t0, 100.0, 100.3, 99.9, 100.1),
        mk_candle(t0 + 900_000, 100.1, 100.5, 99.5, 100.0),   # entry
        mk_candle(t0 + 1_800_000, 100.2, 103.0, 98.5, 101.0),  # SL+TP same candle
    ]
    sig = mk_signal(stop_loss=99.0, tp1=102.0, tp2=103.5, tp3=105.0)
    tr = simulate_trade(sig, candles, 0, 100.0)
    assert tr["status"] == "CLOSED"
    assert tr["exit_reason"] == "SL"
    assert tr["tp_fills"] == [False, False, False]
    assert tr["pnl_usd"] == pytest.approx(-113.9925, abs=1e-6)
    assert tr["r_multiple"] == pytest.approx(-1.139925, abs=1e-9)


# ------------------------------------------------- expiry / max-hold
def test_no_entry_when_price_never_touches():
    t0 = 1_700_000_000_000
    candles = [mk_candle(t0 + i * 900_000, 101.0, 101.5, 100.8, 101.2)
               for i in range(200)]  # always above entry 100
    tr = simulate_trade(mk_signal(), candles, 0, 100.0)
    assert tr["status"] == "NO_ENTRY"
    assert tr["pnl_usd"] == 0.0
    assert tr["r_multiple"] == 0.0


def test_expiry_before_touch_is_no_entry(monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_SIGNAL_EXPIRY_HOURS", 0.25)  # 1 candle
    t0 = 1_700_000_000_000
    candles = [mk_candle(t0 + i * 900_000, 101.0, 101.5, 100.8, 101.2)
               for i in range(10)]
    # touch only on candle 5 — after the 1-candle expiry window
    candles[5] = mk_candle(t0 + 5 * 900_000, 100.5, 100.8, 99.5, 100.2)
    tr = simulate_trade(mk_signal(), candles, 0, 100.0)
    assert tr["status"] == "NO_ENTRY"


def test_max_hold_closes_at_market(monkeypatch):
    monkeypatch.setattr(config, "BACKTEST_MAX_HOLD_HOURS", 0.5)  # 2 candles
    t0 = 1_700_000_000_000
    candles = [
        mk_candle(t0, 100.0, 100.3, 99.9, 100.1),
        mk_candle(t0 + 900_000, 100.1, 100.4, 99.8, 100.2),   # entry
        mk_candle(t0 + 1_800_000, 100.2, 100.6, 99.9, 100.5),  # hold
        mk_candle(t0 + 2_700_000, 100.5, 100.9, 100.1, 100.8),  # hold->close@100.8
        mk_candle(t0 + 3_600_000, 100.8, 101.0, 100.4, 100.9),
    ]
    tr = simulate_trade(mk_signal(), candles, 0, 100.0)
    assert tr["status"] == "CLOSED"
    assert tr["exit_reason"] == "MAX_HOLD"
    # realized = 50*(100.8-100) = 40; must be positive but below +1R after costs
    assert tr["pnl_usd"] > 0
    assert tr["r_multiple"] < 1.0


# ------------------------------------------------------- market integrity
def test_run_backtest_signal_trade_accounting():
    """Regression: every passing signal must produce exactly one trade entry
    (CLOSED or NO_ENTRY) unless skipped for an already-open position. A
    wall-clock/step misalignment once made all signals vanish silently."""
    from backtest import run_backtest
    c15 = stamp_15m(candles_from_closes(uptrend(400), last_vol_mult=2.0))
    hist = {"BTCUSDT": {
        "15m": c15,
        "30m": _aggregate(c15, 2),
        "1h": _aggregate(c15, 4),
        "4h": _aggregate(c15, 16),
        "1d": _aggregate(c15, 96),
    }}
    t0, t1 = c15[300]["close_time"], c15[-1]["close_time"]
    trades, stats = run_backtest(["BTCUSDT"], t0, t1, 6, hist)
    skips = stats["reject_reasons"].get("skip: position already open", 0)
    assert stats["n_evaluated"] > 0
    assert stats["n_signals"] == len(trades) + skips
    for tr in trades:
        assert tr["status"] in ("CLOSED", "NO_ENTRY", "INVALID")


def test_backtest_market_no_look_ahead():
    closes = [100.0 + i * 0.1 for i in range(300)]
    c15 = stamp_15m(candles_from_closes(closes))
    t_mid = c15[150]["close_time"]
    m = BacktestMarket({"BTCUSDT": {"15m": c15, "30m": [], "1h": [],
                                    "4h": [], "1d": []}})
    m.set_time(t_mid)
    kl = m.get_klines("BTCUSDT", "15m", 220)
    assert all(c["close_time"] <= t_mid for c in kl)
    assert kl[-1]["close"] == pytest.approx(closes[150])
    assert m.get_live_price("BTCUSDT") == pytest.approx(closes[150])
    # 24h volume from candles at-or-before t only
    vol = m.get_24h_quote_volume_usd("BTCUSDT")
    assert vol == pytest.approx(sum(c["close"] * c["volume"]
                                    for c in c15[55:151]), rel=1e-9)


def test_backtest_market_insufficient_data_raises():
    c15 = stamp_15m(candles_from_closes([100.0] * 10))
    m = BacktestMarket({"BTCUSDT": {"15m": c15, "30m": [], "1h": [],
                                    "4h": [], "1d": []}})
    m.set_time(c15[-1]["close_time"])
    with pytest.raises(RuntimeError):
        m.get_klines("BTCUSDT", "15m", 220)
    assert m.get_24h_quote_volume_usd("BTCUSDT") is None  # fail-closed


def _aggregate(candles, factor):
    out = []
    for i in range(0, len(candles) - factor + 1, factor):
        grp = candles[i:i + factor]
        out.append({"open_time": grp[0]["open_time"], "open": grp[0]["open"],
                    "high": max(c["high"] for c in grp),
                    "low": min(c["low"] for c in grp),
                    "close": grp[-1]["close"],
                    "volume": sum(c["volume"] for c in grp),
                    "close_time": grp[-1]["close_time"]})
    return out


def test_degraded_pipeline_runs_production_gates():
    """evaluate_symbol() runs unmodified on a time-sliced market: the same
    15m/MTF/quality/freshness gates execute; only derivatives-dependent
    gates/deltas are skipped and flagged."""
    c15 = stamp_15m(candles_from_closes(uptrend(400), last_vol_mult=2.0))
    hist = {"BTCUSDT": {
        "15m": c15,
        "30m": _aggregate(c15, 2),
        "1h": _aggregate(c15, 4),
        "4h": _aggregate(c15, 16),
        "1d": _aggregate(c15, 96),
    }}
    m = BacktestMarket(hist)
    m.set_time(c15[-1]["close_time"])
    signal, note = evaluate_symbol("BTCUSDT", m)  # must not raise
    assert isinstance(note, str) and note
    if signal is not None:
        assert signal["deriv_degraded"] is True
        assert signal["direction"] == "LONG"
        assert signal["stop_loss"] < signal["entry"] < signal["tp1"]


# ---------------------------------------------------------------- metrics
def _mk_trade(pnl, r, exit_ms, symbol="BTCUSDT", regime="TRENDING BULLISH"):
    return {"status": "CLOSED", "exit_reason": "TP_ALL", "symbol": symbol,
            "regime": regime, "pnl_usd": pnl, "r_multiple": r,
            "fees_usd": 1.0, "funding_usd": 0.0, "exit_time": exit_ms}


def test_compute_metrics_hand_checked():
    t0 = 1_700_000_000_000
    trades = [
        _mk_trade(120.0, 1.2, t0),
        _mk_trade(60.0, 0.6, t0 + 86_400_000),
        _mk_trade(-110.0, -1.1, t0 + 2 * 86_400_000),
        _mk_trade(-105.0, -1.05, t0 + 3 * 86_400_000),
    ]
    m = compute_metrics(trades, 10_000.0)
    assert m["n_trades"] == 4
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["expectancy_r"] == pytest.approx(-0.0875)
    assert m["profit_factor"] == pytest.approx(180.0 / 215.0)
    assert m["avg_win_r"] == pytest.approx(0.9)
    assert m["avg_loss_r"] == pytest.approx(-1.075)
    assert m["max_consec_losses"] == 2
    assert m["max_consec_wins"] == 2  # the two opening wins
    # equity: 10000 -> 10120 -> 10180 (peak) -> 10070 -> 9965
    assert m["max_drawdown_pct"] == pytest.approx(215.0 / 10180 * 100)
    assert m["total_return_pct"] == pytest.approx(-35.0 / 10000 * 100)
    assert m["per_asset"]["BTCUSDT"]["n"] == 4
    assert m["per_regime"]["TRENDING BULLISH"]["win_rate"] == pytest.approx(0.5)
    assert m["exit_reasons"] == {"TP_ALL": 4, "SL": 0, "MAX_HOLD": 0}


def test_compute_metrics_no_trades():
    m = compute_metrics([{"status": "NO_ENTRY"}], 10_000.0)
    assert m["n_trades"] == 0
    assert m["note"] == "no closed trades"
