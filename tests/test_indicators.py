# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Unit tests for indicators.py — exact-port verification against GAS.

Reference: emaSeries_ (L1648), rsiWilder_ (L1658), macd_ (L1701),
           atrWilder_ (L1712), adx_ (L1747).
"""

import math

import pytest

from indicators import adx, atr_wilder, ema_series, macd, rsi_wilder


def uptrend(n=220, start=100.0, step=0.003):
    return [start * (1 + step) ** i for i in range(n)]


def candles_from_closes(closes, vol=1000.0, last_vol_mult=1.0):
    out = []
    prev = closes[0]
    for idx, c in enumerate(closes):
        o = prev
        out.append({
            "open": o, "high": max(o, c) * 1.001,
            "low": min(o, c) * 0.999, "close": c,
            "volume": vol * (last_vol_mult if idx == len(closes) - 1 else 1.0),
        })
        prev = c
    return out


def test_ema_series_hand_computed():
    # alpha = 2/3; seeded with values[0]
    got = ema_series([10.0, 11.0, 12.0], 2)
    assert got[0] == pytest.approx(10.0)
    assert got[1] == pytest.approx(10.6666666667)
    assert got[2] == pytest.approx(11.5555555556)
    assert ema_series([], 9) == []


def test_rsi_wilder_extremes():
    assert rsi_wilder(uptrend(60), 14) == pytest.approx(100.0)  # no down moves -> al==0
    down = [100.0 * (0.99) ** i for i in range(60)]
    assert rsi_wilder(down, 14) == pytest.approx(0.0)
    assert rsi_wilder([100.0] * 10, 14) == 50.0  # too short -> 50


def test_macd_accelerating_rise_positive_hist():
    closes = [100 + 0.1 * i * i for i in range(60)]  # accelerating
    m = macd(closes)
    assert set(m) == {"macd_line", "signal_line", "histogram"}
    assert m["histogram"] > 0
    closes_d = [200 - 0.1 * i * i for i in range(60)]  # decelerating fall
    assert macd(closes_d)["histogram"] < 0


def test_atr_wilder_flat_then_spike():
    cs = candles_from_closes([100.0] * 16)
    # helper builds high=100.1/low=99.9 -> flat TR = 0.2
    cs[-1]["high"] = 110.0  # TR = max(10.1, |110-100|, |99.9-100|) = 10.1
    # seed = average(first 14 TRs) = 0.2; one step: (0.2*13 + 10.1)/14
    assert atr_wilder(cs, 14) == pytest.approx((0.2 * 13 + 10.1) / 14)
    assert atr_wilder(candles_from_closes([100.0] * 16), 14) == pytest.approx(0.2)
    assert atr_wilder(cs[:10], 14) == 0.0  # too short


def test_adx_strong_uptrend():
    pack = adx(candles_from_closes(uptrend(120)), 14)
    assert pack["adx"] > 20
    assert pack["plus_di"] > pack["minus_di"]
    flat = adx(candles_from_closes([100.0] * 120), 14)
    assert flat["adx"] == pytest.approx(0.0, abs=1e-9)


def test_adx_too_short():
    assert adx(candles_from_closes([100.0] * 20), 14) == {
        "adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}


def test_macd_empty_input():
    res = macd([])
    assert res == {"macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0}

