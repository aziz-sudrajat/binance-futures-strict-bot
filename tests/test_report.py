# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for report.py terminal-outcome accounting.

Covers the 2026-10-15 lifecycle-accounting fix: per-leg TP fill events reuse
the same state names as terminal events, so the report must pick the LAST
terminal event per signal (marked terminal=True), decide wins by realized
PnL > 0 (not by event name), and include manual_close outcomes instead of
dropping them.
"""
import datetime
import json

from report import daily_report


def _write(tmp_path, events, date_str="2026-10-15"):
    """Write synthetic lifecycle events with timestamps inside the UTC day."""
    start = int(datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc).timestamp() * 1000)
    path = tmp_path / "lifecycle.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for i, e in enumerate(events):
            e = dict(e, ts=start + 1000 * (i + 1))
            f.write(json.dumps(e) + "\n")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"positions": {}, "kill_switch": {}}),
                     encoding="utf-8")
    return daily_report(date_str, str(path), str(state))


def _ev(sid, state, **kw):
    return {"signal_id": sid, "symbol": "BTCUSDT", "state": state, **kw}


def test_tp1_then_sl_counts_win_with_full_pnl(tmp_path):
    # TP1 leg (+9.41) then SL on the remainder: the terminal sl_hit carries
    # the total realized. Old code kept the leg event -> win with PnL 0.
    out = _write(tmp_path, [
        _ev("s1", "tp1_hit", pnl=9.41, price=100.0, qty=1.0),
        _ev("s1", "sl_hit", reason="sl_hit", realized=9.2, terminal=True),
    ])
    assert "closed trades: 1 | wins: 1 | realized PnL: +9.20 USDT" in out


def test_tp1_remainder_loss_is_not_a_win(tmp_path):
    # TP1 leg then remainder stopped at a net loss: event name says tp1_hit
    # but the money says loss.
    out = _write(tmp_path, [
        _ev("s1", "tp1_hit", pnl=2.0, price=100.0, qty=1.0),
        _ev("s1", "tp1_hit", reason="tp1_hit (remainder)", realized=-5.0,
            terminal=True),
    ])
    assert "closed trades: 1 | wins: 0 | realized PnL: -5.00 USDT" in out


def test_manual_close_loss_counted_not_win(tmp_path):
    # manual_close used to be dropped from the report entirely.
    out = _write(tmp_path, [
        _ev("s1", "manual_close", reason="manual_close (loss)", realized=-3.5,
            terminal=True),
    ])
    assert "closed trades: 1 | wins: 0 | realized PnL: -3.50 USDT" in out


def test_manual_close_profit_is_win(tmp_path):
    out = _write(tmp_path, [
        _ev("s1", "manual_close", reason="manual_close (profit)",
            realized=4.0, terminal=True),
    ])
    assert "closed trades: 1 | wins: 1 | realized PnL: +4.00 USDT" in out


def test_expired_excluded_from_trade_stats(tmp_path):
    # Entry never filled: visible in state counts, not a trade.
    out = _write(tmp_path, [
        _ev("s1", "generated"),
        _ev("s1", "expired", reason="entry unfilled after 24h",
            terminal=True),
    ])
    assert "expired: 1" in out
    assert "closed trades: 0 | wins: 0 | realized PnL: +0.00 USDT" in out


def test_full_tp3_path(tmp_path):
    out = _write(tmp_path, [
        _ev("s1", "tp1_hit", pnl=5.0),
        _ev("s1", "tp2_hit", pnl=7.0),
        _ev("s1", "tp3_hit", pnl=8.0),
        _ev("s1", "tp3_hit", reason="tp3_hit", realized=20.0,
            terminal=True),
    ])
    assert "closed trades: 1 | wins: 1 | realized PnL: +20.00 USDT" in out


def test_legacy_log_without_terminal_markers(tmp_path):
    # Old logs lack terminal=True: last outcome event still wins.
    out = _write(tmp_path, [
        _ev("s1", "tp1_hit", pnl=9.41),
        _ev("s1", "sl_hit", reason="sl_hit", realized=9.0),
    ])
    assert "closed trades: 1 | wins: 1 | realized PnL: +9.00 USDT" in out


def test_two_signals_independent(tmp_path):
    out = _write(tmp_path, [
        _ev("s1", "tp3_hit", reason="tp3_hit", realized=10.0,
            terminal=True),
        _ev("s2", "sl_hit", reason="sl_hit", realized=-4.0, terminal=True),
    ])
    assert "closed trades: 2 | wins: 1 | realized PnL: +6.00 USDT" in out


def test_corrupt_realized_does_not_kill_report(tmp_path):
    out = _write(tmp_path, [
        _ev("s1", "sl_hit", reason="sl_hit", realized="N/A", terminal=True),
    ])
    assert "closed trades: 1 | wins: 0 | realized PnL: +0.00 USDT" in out


def test_expire_stale_entry_skips_already_closed():
    # Regression (reviewer siklus 3): CANCELLED lalu EXPIRED = double
    # terminal event untuk satu sinyal.
    import time
    from unittest.mock import MagicMock
    import executor as exmod
    e = object.__new__(exmod.Executor)
    e.exchange = MagicMock()
    e.lifecycle = MagicMock()
    e.state = MagicMock()
    old = int(time.time() * 1000) - 25 * 3600 * 1000
    pos = {"symbol": "BTCUSDT", "closed": True,
           "closed_reason": "entry order CANCELED",
           "opened_at": old, "entry_order_id": 1}
    e._expire_stale_entry("BTCUSDT_1", pos, "s1")
    e.lifecycle.log.assert_not_called()
    e.exchange.cancel_order.assert_not_called()


def _today_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _write_stateful(tmp_path, events, positions, date_str):
    start = int(datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc).timestamp() * 1000)
    path = tmp_path / "lifecycle.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for i, e in enumerate(events):
            e = dict(e, ts=start + 1000 * (i + 1))
            f.write(json.dumps(e) + "\n")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"positions": positions, "kill_switch": {}}),
                     encoding="utf-8")
    return daily_report(date_str, str(path), str(state))


def test_open_position_leg_events_not_counted_as_trades(tmp_path):
    # Day-boundary edge: TP1 leg fired today but the position is STILL OPEN
    # per state.json -> not a closed trade. Was: counted with PnL 0.
    today = _today_utc()
    out = _write_stateful(tmp_path,
                          [_ev("s1", "tp1_hit", pnl=9.41, price=100.0,
                               qty=1.0)],
                          {"p1": {"signal_id": "s1", "symbol": "BTCUSDT"}},
                          today)
    assert "closed trades: 0 | wins: 0 | realized PnL: +0.00 USDT" in out
    # ...but the still-open position shows up in the open-positions section
    assert "open positions: 1" in out


def test_closed_tombstone_still_counted(tmp_path):
    # A position marked closed=True (tombstone, not yet popped) IS a trade.
    today = _today_utc()
    out = _write_stateful(tmp_path,
                          [_ev("s1", "sl_hit", reason="sl_hit", realized=-5.0,
                               terminal=True)],
                          {"p1": {"signal_id": "s1", "closed": True}}, today)
    assert "closed trades: 1 | wins: 0 | realized PnL: -5.00 USDT" in out


def test_past_date_still_excludes_open_positions(tmp_path):
    # signal_id is unique per trade ({symbol}:{close_ms}:{direction},
    # deduped via seen_signal): a sid open NOW cannot have closed on a past
    # date, so the exclusion applies for every report date — including late
    # cron runs for past dates.
    out = _write_stateful(tmp_path,
                          [_ev("s1", "tp1_hit", pnl=9.41, price=100.0,
                               qty=1.0)],
                          {"p1": {"signal_id": "s1", "symbol": "BTCUSDT"}},
                          "2026-10-10")
    assert "closed trades: 0 | wins: 0 | realized PnL: +0.00 USDT" in out


def test_modern_stops_counted_in_report(tmp_path):
    # Test that profit_take, trail_stop, and time_stop are counted as terminal trades
    out = _write(tmp_path, [
        _ev("s1", "profit_take", reason="profit_take +20%", realized=12.5, terminal=True),
        _ev("s2", "trail_stop", reason="trail_stop pullback", realized=8.0, terminal=True),
        _ev("s3", "time_stop", reason="time_stop after 4h", realized=-2.5, terminal=True),
    ])
    assert "closed trades: 3 | wins: 2 | realized PnL: +18.00 USDT" in out
