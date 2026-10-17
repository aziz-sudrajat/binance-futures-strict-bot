# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Technical indicators — exact ports from the GAS signal bot.

GAS source: workspace/user/files/AZZATSSINS-20261013-imageperf-v4_5_02o2.gs
- ema_series        <- emaSeries_            (L1648)
- rsi_wilder        <- rsiWilder_            (L1658)
- macd              <- macd_                 (L1701)
- atr_wilder        <- atrWilder_            (L1712)
- adx               <- adx_                  (L1747)
- average           <- average_              (L2372)

Candles are dicts with keys: open, high, low, close, volume
(open_time/close_time optional; only OHLCV are used here).
"""

from __future__ import annotations


def average(values) -> float:
    vals = [float(x or 0) for x in values]
    return sum(vals) / len(vals) if vals else 0.0


def ema_series(values, period: int) -> list[float]:
    """Seeded with values[0], alpha = 2/(period+1)."""
    vals = [float(v) for v in values]
    if not vals:
        return []
    alpha = 2.0 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * alpha + out[-1] * (1 - alpha))
    return out


def rsi_wilder(values, period: int = 14) -> float:
    vals = [float(v) for v in values]
    if len(vals) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = vals[i] - vals[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0)) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def macd(values, fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict:
    vals = [float(v) for v in values]
    if not vals:
        return {"macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0}
    f, s = ema_series(vals, fast), ema_series(vals, slow)
    line = [a - b for a, b in zip(f, s)]
    if not line:
        return {"macd_line": 0.0, "signal_line": 0.0, "histogram": 0.0}
    sig = ema_series(line, signal_period)
    i = len(line) - 1
    return {
        "macd_line": line[i],
        "signal_line": sig[i],
        "histogram": line[i] - sig[i],
    }



def _true_range(candle, prev_close: float) -> float:
    h, l = float(candle["high"]), float(candle["low"])
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def atr_wilder(candles, period: int = 14) -> float:
    if not candles or len(candles) < period + 1:
        return 0.0
    tr = [_true_range(candles[i], float(candles[i - 1]["close"]))
          for i in range(1, len(candles))]
    atr = average(tr[:period])
    for t in tr[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr


def adx(candles, period: int = 14) -> dict:
    if not candles or len(candles) < period * 2 + 2:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}
    tr, pdm, mdm = [], [], []
    for i in range(1, len(candles)):
        up = float(candles[i]["high"]) - float(candles[i - 1]["high"])
        down = float(candles[i - 1]["low"]) - float(candles[i]["low"])
        pdm.append(up if (up > down and up > 0) else 0.0)
        mdm.append(down if (down > up and down > 0) else 0.0)
        tr.append(_true_range(candles[i], float(candles[i - 1]["close"])))
    tr_sm = sum(tr[:period])
    p_sm = sum(pdm[:period])
    m_sm = sum(mdm[:period])
    dx = []
    plus_di = minus_di = 0.0
    for i in range(period, len(tr)):
        if i > period:
            tr_sm = tr_sm - tr_sm / period + tr[i]
            p_sm = p_sm - p_sm / period + pdm[i]
            m_sm = m_sm - m_sm / period + mdm[i]
        plus_di = 100 * p_sm / tr_sm if tr_sm else 0.0
        minus_di = 100 * m_sm / tr_sm if tr_sm else 0.0
        den = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / den if den else 0.0)
    if not dx:
        return {"adx": 0.0, "plus_di": plus_di, "minus_di": minus_di}
    a = average(dx[: min(period, len(dx))])
    for d in dx[period:]:
        a = (a * (period - 1) + d) / period
    return {"adx": a, "plus_di": plus_di, "minus_di": minus_di}
