# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for vetoes.py — ROADMAP ITEM 2 (deterministic hard vetoes).

Verifies the GAS deep-check veto ports:
  leverage_risk_index  <- derivGateAdvanced_    (GAS L1016-1022), veto L3338
  squeeze_risk_index   <- derivGateAdvanced_    (GAS L1024-1034), veto L3360
  funding extreme      <- deepCheckDerivatives_ (GAS L3346-3354), CFG L72
  whale divergence     <- deepCheckDerivatives_ (GAS L3366-3379), CFG L90
  liquidity wall       <- deepCheckLiquidity_  (GAS L3395-3425), CFG L75-76
  BTC circuit breaker  <- deepCheckBtcBreadth_ (GAS L3634-3662), CFG L106

T3: BTC -3%/1h vetoes ALL symbols. T4: each veto fires on synthetic data.
Regression: a signal that passed before vetoes still passes when none fire.
"""

import pytest

import config
import time
import vetoes
from signal_engine import evaluate_symbol
from tests.test_engine import FakeMarket, _benign_4h_for_sr
from tests.test_indicators import candles_from_closes
from vetoes import (
    btc_1h_change_pct,
    leverage_risk_index,
    run_vetoes,
    squeeze_risk_index,
)


@pytest.fixture(autouse=True)
def _reset_veto_caches():
    vetoes.reset_caches()
    yield
    vetoes.reset_caches()


BENIGN_SNAP = {"aggregate_funding": 0.0001}


def blocks_by_code(blocks):
    return {code: (kind, reason) for code, kind, reason in blocks}


# ------------------------------------------------- pure index formulas
def test_leverage_risk_index_formula():
    # Hand-computed per GAS L1016-1022:
    # crowding=|52-50|*2=4; fundingCrowd=min(100, 0.0001*100000)=10;
    # oiVolRisk=min(100, 50/200*100)=25; spikeRisk=min(100, max(0,1-1)*25)=0
    # -> round(4*.28 + 10*.22 + 25*.25 + 0) = round(9.57) = 10
    assert leverage_risk_index(52.0, 0.0001, 50_000_000, 200_000_000, 1.0) == 10
    # Extreme crowding -> 95 >= veto 90:
    # crowding=90; fundingCrowd=90; oiVolRisk=100; spikeRisk=100
    # -> round(90*.28 + 90*.22 + 100*.25 + 100*.25) = round(95.0) = 95
    assert leverage_risk_index(95.0, 0.0009, 400_000_000, 200_000_000, 5.0) == 95
    # Zero volume -> oiVolRisk 0, no ZeroDivisionError (GAS: oiVolumeRatio=0)
    assert leverage_risk_index(60.0, 0.0002, 10_000_000, 0.0, 1.0) == \
        round(20 * 0.28 + 20 * 0.22 + 0 + 0)


def test_squeeze_risk_index_formula():
    # BULLISH, GAS L1024-1029:
    # max(0,70-50)*1.25=25 + max(0,0.0005)*90000=45 + max(0,10)*1.2=12
    # + (long>short ? min(25, 3*6)=18) = 100 -> clamp 100
    assert squeeze_risk_index(70.0, 0.0005, 10.0, 2_000_000, 1_000_000,
                              3.0, "BULLISH") == 100
    # Benign: 2.5 + 9 + 2.4 + 0 (liq equal) = 13.9 -> 14
    assert squeeze_risk_index(52.0, 0.0001, 2.0, 100_000, 100_000,
                              1.0, "BULLISH") == 14
    # BEARISH mirror (GAS L1030-1034): short squeeze on crowded shorts
    assert squeeze_risk_index(30.0, -0.0005, 10.0, 1_000_000, 2_000_000,
                              3.0, "BEARISH") == 100
    # Liq term only when the signal-side liquidations dominate
    assert squeeze_risk_index(70.0, 0.0005, 10.0, 1_000_000, 2_000_000,
                              3.0, "BULLISH") == 82  # 25+45+12, no liq term


def test_btc_1h_change_pct():
    m = FakeMarket()
    m.btc_1h = candles_from_closes([100.0, 96.9])
    assert btc_1h_change_pct(m) == pytest.approx(-3.1)
    vetoes.reset_caches()
    m.btc_1h = candles_from_closes([100.0, 102.0])
    assert btc_1h_change_pct(m) == pytest.approx(2.0)


# ------------------------------------------------- T4: each veto fires
def test_no_veto_on_benign_market():
    assert run_vetoes("BTCUSDT", "BULLISH", FakeMarket(), BENIGN_SNAP) == []


def test_leverage_veto_fires():
    m = FakeMarket()
    m.adv.update({"retail_long": 95.0, "funding": 0.0009,
                  "oi_usd": 400_000_000.0, "volume_24h": 200_000_000.0,
                  "spike": 5.0, "long_liq_usd": 50_000.0,
                  "short_liq_usd": 500_000.0})
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    kind, reason = by["leverage_risk"]
    assert kind == "veto"
    assert "95/100" in reason and str(config.DERIV_LEVERAGE_RISK_VETO) in reason


def test_funding_extreme_veto_fires():
    m = FakeMarket()
    snap = {"aggregate_funding": 0.0012}  # +0.12%: crowded long, against LONG
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, snap))
    assert by["funding"][0] == "veto"
    assert "against BULLISH" in by["funding"][1]


def test_funding_extreme_supporting_direction_no_veto():
    m = FakeMarket()
    snap = {"aggregate_funding": -0.0012}  # negative funding supports longs
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, snap))
    assert "funding" not in by


def test_funding_below_threshold_no_veto():
    m = FakeMarket()
    snap = {"aggregate_funding": 0.0009}
    assert "funding" not in blocks_by_code(
        run_vetoes("BTCUSDT", "BULLISH", m, snap))


def test_squeeze_veto_fires():
    m = FakeMarket()
    m.adv.update({"retail_long": 80.0, "funding": 0.0008, "oi_change": 20.0,
                  "long_liq_usd": 2_000_000.0, "short_liq_usd": 1_000_000.0,
                  "spike": 4.0})
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    kind, reason = by["squeeze"]
    assert kind == "veto"
    assert "long squeeze risk 100/100" in reason
    # leverage must NOT fire here (59 < 90): vetoes are independent
    assert "leverage_risk" not in by


def test_whale_divergence_veto_fires():
    m = FakeMarket()
    m.adv.update({"top_long": 20.0, "divergence": -32.0, "retail_long": 52.0})
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    kind, reason = by["whale"]
    assert kind == "veto"
    assert "20.0%" in reason


def test_whale_penalty_tier_not_a_veto():
    # GAS DERIV_TOP_TRADER_VETO_LONG=35 is a *penalty* tier (-12 delta), not a
    # hard veto — we deliberately do not port it as a veto (roadmap item 3).
    m = FakeMarket()
    m.adv.update({"top_long": 30.0, "divergence": -12.0, "retail_long": 52.0})
    assert "whale" not in blocks_by_code(
        run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))


def test_liq_wall_veto_fires():
    m = FakeMarket()
    m.book = {"bids": [(99.5, 100.0)], "asks": [(100.3, 3000.0)],
              "mid": 100.0, "provider": "fake"}  # $300.9k ask at +0.3%
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    kind, reason = by["liq_wall"]
    assert kind == "veto"
    assert "ask" in reason and "$300,900" in reason


def test_liq_wall_outside_distance_no_veto():
    m = FakeMarket()
    m.book = {"bids": [(99.5, 100.0)], "asks": [(100.8, 3000.0)],
              "mid": 100.0, "provider": "fake"}  # +0.8%: beyond 0.5%
    assert "liq_wall" not in blocks_by_code(
        run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))


def test_liq_wall_below_notional_no_veto():
    m = FakeMarket()
    m.book = {"bids": [(99.5, 100.0)], "asks": [(100.3, 500.0)],
              "mid": 100.0, "provider": "fake"}  # $50k < $250k
    assert "liq_wall" not in blocks_by_code(
        run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))


def test_liq_wall_blocks_bearish_via_bids():
    m = FakeMarket()
    m.book = {"bids": [(99.7, 3000.0)], "asks": [(100.5, 100.0)],
              "mid": 100.0, "provider": "fake"}  # $299k bid at -0.3%
    by = blocks_by_code(run_vetoes("BTCUSDT", "BEARISH", m, BENIGN_SNAP))
    assert by["liq_wall"][0] == "veto"
    assert "bid" in by["liq_wall"][1]


# ------------------------------------------------- T3: BTC circuit breaker
def test_btc_dump_vetoes_all_symbols():
    # -3.1%/1h: every symbol's new LONG signal is vetoed (market-wide).
    for symbol in ("BTCUSDT", "ETHUSDT"):
        m = FakeMarket()
        m.btc_1h = candles_from_closes([100.0, 96.9])
        vetoes.reset_caches()
        by = blocks_by_code(run_vetoes(symbol, "BULLISH", m, BENIGN_SNAP))
        kind, reason = by["btc_breaker"]
        assert kind == "veto", symbol
        assert "circuit breaker" in reason, symbol


def test_btc_dump_at_threshold_vetoes():
    m = FakeMarket()
    m.btc_1h = candles_from_closes([100.0, 97.0])  # exactly -3.0%
    by = blocks_by_code(run_vetoes("ETHUSDT", "BULLISH", m, BENIGN_SNAP))
    assert by["btc_breaker"][0] == "veto"


def test_btc_mild_drop_no_veto():
    # -2%: GAS applies a -15 score penalty (not ported — roadmap item 3),
    # but never a veto. We record no block.
    m = FakeMarket()
    m.btc_1h = candles_from_closes([100.0, 98.0])
    assert "btc_breaker" not in blocks_by_code(
        run_vetoes("ETHUSDT", "BULLISH", m, BENIGN_SNAP))


def test_btc_breaker_long_only():
    # GAS comment L3655: the breaker guards LONG exposure; a BEARISH setup
    # is not penalized for BTC selling off.
    m = FakeMarket()
    m.btc_1h = candles_from_closes([100.0, 96.9])
    assert "btc_breaker" not in blocks_by_code(
        run_vetoes("BTCUSDT", "BEARISH", m, BENIGN_SNAP))


def test_btc_data_failure_holds():
    m = FakeMarket()

    def boom(symbol, interval, limit=220):
        raise RuntimeError("provider timeout")

    m.get_klines = boom
    by = blocks_by_code(run_vetoes("ETHUSDT", "BULLISH", m, BENIGN_SNAP))
    kind, reason = by["btc_breaker"]
    assert kind == "hold"
    assert "fail-closed" in reason


def test_liq_wall_hold_on_order_book_failure():
    # get_order_book raises -> liq_wall HOLD (fail-closed), never a veto.
    m = FakeMarket()
    m.book = None

    def boom(symbol, limit=100):
        raise RuntimeError("no order book")
    m.get_order_book = boom

    by = blocks_by_code(
        run_vetoes("ETHUSDT", "BULLISH", m, BENIGN_SNAP))
    assert "liq_wall" in by
    assert by["liq_wall"][0] == "hold"
    assert "fail-closed" in by["liq_wall"][1]


def test_funding_window_veto_fires_near_snapshot():
    # funding +0.12%, exactly 15m before the 8h snapshot -> veto for LONG.
    snap = {"aggregate_funding": 0.0012}
    import vetoes as _v
    real_time = time.time
    _v.time.time = lambda: 1_000_021_500  # 07:45 UTC -> 15m until snapshot
    try:
        by = blocks_by_code(
            run_vetoes("BTCUSDT", "BULLISH", FakeMarket(), snap))
    finally:
        _v.time.time = real_time
    assert "funding_window" in by
    assert by["funding_window"][0] == "veto"
    assert "0.050%" in by["funding_window"][1]


# ------------------------------------------------- fail-closed holds
def test_adv_derivatives_failure_holds():
    m = FakeMarket()
    m.raise_adv = True
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert by["adv_deriv"][0] == "hold"


def test_order_book_failure_holds():
    m = FakeMarket()
    m.raise_book = True
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    kind, reason = by["liq_wall"]
    assert kind == "hold"
    assert "fail-closed" in reason


def test_leverage_inputs_incomplete_holds():
    m = FakeMarket()
    m.adv["oi_usd"] = None
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert by["leverage_risk"][0] == "hold"


# ------------------------------------------------- end-to-end + regression
def test_veto_rejects_signal_end_to_end():
    m = FakeMarket(bullish=True)
    m.adv.update({"retail_long": 95.0, "funding": 0.0009,
                  "oi_usd": 400_000_000.0, "volume_24h": 200_000_000.0,
                  "spike": 5.0, "long_liq_usd": 50_000.0,
                  "short_liq_usd": 500_000.0})
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert reason.startswith("veto:")
    assert "leverage risk index" in reason


def test_veto_hold_rejects_signal_end_to_end():
    m = FakeMarket(bullish=True)
    m.raise_adv = True
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "holding" in reason


def test_btc_dump_rejects_signal_end_to_end():
    m = FakeMarket(bullish=True)
    m.btc_1h = candles_from_closes([100.0, 96.5])  # -3.5%/1h
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "circuit breaker" in reason


def test_strict_pass_regression_with_vetoes():
    # A signal that passed before ROADMAP ITEM 2 still passes when no veto
    # fires: vetoes add rejections, never approvals.
    # NOTE: the 4h history is lifted so its highest high sits well above the
    # entry — otherwise the new sr_zone veto (correctly) flags this synthetic
    # ramp as "long into resistance". S/R behavior itself is covered by the
    # dedicated sr_zone tests below.
    m = FakeMarket(bullish=True)
    # Benign 4h history: exponential ramp (bullish for MTF) whose high
    # sits far above any entry -> sr_zone stays quiet. V21 (2026-10-17):
    # the last-7 closes must displace >= 1.5% (here ~+1.8%) so the
    # trend-strength gate passes.
    kl4 = []
    for i in range(500):
        c = 180.0 * 1.003 ** i
        kl4.append({"high": c * 1.001, "low": c * 0.999, "close": c,
                    "open": c / 1.003, "volume": 1000.0})
    m.klines["4h"] = kl4
    signal, note = evaluate_symbol("BTCUSDT", m)
    assert signal is not None, note
    assert signal["direction"] == "LONG"
    assert signal["quality"] >= config.MIN_QUALITY_SCORE
    assert "KETAT PASS" in note


def test_btc_pump_vetoes_shorts():
    # +3.1%/1h: every symbol's new SHORT signal is vetoed (mirror of the
    # BTC-dump breaker for longs).
    for symbol in ("BTCUSDT", "ETHUSDT"):
        m = FakeMarket()
        m.btc_1h = candles_from_closes([100.0, 103.1])
        vetoes.reset_caches()
        by = blocks_by_code(run_vetoes(symbol, "BEARISH", m, BENIGN_SNAP))
        kind, reason = by["btc_breaker"]
        assert kind == "veto", symbol
        assert "circuit breaker" in reason and "SHORT" in reason, symbol


def test_btc_pump_no_veto_for_longs():
    # A BTC pump does not veto LONGs (asymmetric guard, like the dump side).
    m = FakeMarket()
    m.btc_1h = candles_from_closes([100.0, 103.1])
    vetoes.reset_caches()
    assert "btc_breaker" not in blocks_by_code(
        run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))


# ------------------------------------------------- V16: S/R location
def _sr_market_4h(n=500, lo=99.0, hi=101.0, support=90.0, resistance=110.0):
    """FakeMarket whose 4h history has known swing extremes (~2 ATR range)."""
    m = FakeMarket()
    kl = [{"high": hi, "low": lo, "close": 100.0} for _ in range(n)]
    kl[10] = {"high": 96.0, "low": support, "close": 94.0}
    kl[20] = {"high": resistance, "low": 104.0, "close": 106.0}
    orig = m.get_klines

    def klines(symbol, interval, limit=220):
        if interval == "4h":
            return kl[:limit]
        return orig(symbol, interval, limit)

    m.get_klines = klines
    return m


def test_sr_zone_vetoes_short_into_support():
    # support=90, ATR(4h)~2 -> zone ~5: entry 92 is inside (90, 95].
    m = _sr_market_4h()
    by = blocks_by_code(run_vetoes("ETHUSDT", "BEARISH", m, BENIGN_SNAP,
                                   entry_px=92.0))
    kind, reason = by["sr_zone"]
    assert kind == "veto"
    assert "SHORT into 4h support" in reason


def test_sr_zone_allows_short_below_support():
    # Breakdown already below support is legitimate — not vetoed.
    m = _sr_market_4h()
    assert "sr_zone" not in blocks_by_code(
        run_vetoes("ETHUSDT", "BEARISH", m, BENIGN_SNAP, entry_px=89.0))


def test_sr_zone_vetoes_long_into_resistance():
    # resistance=110, zone ~5: entry 107 is inside [105, 110).
    m = _sr_market_4h()
    by = blocks_by_code(run_vetoes("ETHUSDT", "BULLISH", m, BENIGN_SNAP,
                                   entry_px=107.0))
    kind, reason = by["sr_zone"]
    assert kind == "veto"
    assert "LONG into 4h resistance" in reason


def test_sr_zone_allows_long_above_resistance():
    # Breakout above resistance is legitimate — not vetoed.
    m = _sr_market_4h()
    assert "sr_zone" not in blocks_by_code(
        run_vetoes("ETHUSDT", "BULLISH", m, BENIGN_SNAP, entry_px=112.0))


def test_sr_zone_disabled_skips():
    m = _sr_market_4h()
    assert "sr_zone" not in blocks_by_code(
        run_vetoes("ETHUSDT", "BEARISH", m, BENIGN_SNAP, entry_px=92.0,
                   disabled={"sr_zone"}))


def test_sr_zone_skipped_without_entry_px():
    # Without an entry price the veto is not applicable (skipped, not held).
    m = _sr_market_4h()
    assert "sr_zone" not in blocks_by_code(
        run_vetoes("ETHUSDT", "BEARISH", m, BENIGN_SNAP))


def test_sr_zone_pol_case():
    # Live 2026-10-15: POL short @ 0.09342 into 4h support ~0.086-0.089.
    # Must be vetoed (this is the trade the user flagged).
    m = _sr_market_4h(n=300, lo=0.0920, hi=0.0940,
                      support=0.08613, resistance=0.12536)
    by = blocks_by_code(run_vetoes("POLUSDT", "BEARISH", m, BENIGN_SNAP,
                                   entry_px=0.09342))
    assert by["sr_zone"][0] == "veto"


# ---------------------------------------------------------------- H3 regression
def test_h3_v17_taker_veto_fires_on_hostile_ratio():
    """H3: V17 must fire when the taker buy/sell ratio opposes the signal.

    The snapshot key is "taker_buy_sell_ratio" (data.py); the old code read
    the non-existent "taker_ratio" so V17 never evaluated at all.
    """
    # BULLISH signal, heavy seller aggression (ratio < 0.70) -> veto.
    snap = dict(BENIGN_SNAP)
    snap["taker_buy_sell_ratio"] = 0.50
    blocks = run_vetoes("BTCUSDT", "BULLISH", None, snap, entry_px=100.0)
    codes = blocks_by_code(blocks)
    assert "cvd_imbalance" in codes, f"V17 did not fire: {codes}"
    assert codes["cvd_imbalance"][0] == "veto"

    # BEARISH signal, heavy buyer aggression (ratio > 1.43) -> veto.
    snap["taker_buy_sell_ratio"] = 1.60
    blocks = run_vetoes("BTCUSDT", "BEARISH", None, snap, entry_px=100.0)
    codes = blocks_by_code(blocks)
    assert "cvd_imbalance" in codes, f"V17 did not fire: {codes}"

    # Neutral ratio -> no V17 veto.
    snap["taker_buy_sell_ratio"] = 1.00
    blocks = run_vetoes("BTCUSDT", "BULLISH", None, snap, entry_px=100.0)
    codes = blocks_by_code(blocks)
    assert "cvd_imbalance" not in codes


# ------------------------------------------------- M1 regression (2026-10-17)
def test_oi_flush_inputs_incomplete_holds():
    # open_interest is a REQUIRED derivatives component: V18 must hold
    # (fail-closed) when the adv feed has no oi_change, not silently skip.
    m = FakeMarket()
    m.adv["oi_change"] = None
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert by["oi_flush"][0] == "hold"
    assert "fail-closed" in by["oi_flush"][1]


def test_oi_flush_fires_on_cascade():
    m = FakeMarket()
    m.adv.update({"oi_change": -6.5, "spike": 3.0})
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert by["oi_flush"][0] == "veto"


def test_cvd_imbalance_missing_taker_is_best_effort():
    # taker_volume is OPTIONAL (engine Stage 8 flags it as deriv_missing and
    # lets the signal through): V17 must not hold on a missing taker feed.
    snap = {"aggregate_funding": 0.0001}  # no taker_buy_sell_ratio key
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", FakeMarket(), snap))
    assert "cvd_imbalance" not in by


def test_cvd_imbalance_fires_when_present():
    snap = {"aggregate_funding": 0.0001, "taker_buy_sell_ratio": 0.5}
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", FakeMarket(), snap))
    assert by["cvd_imbalance"][0] == "veto"


def test_stage10_adv_failure_holds_cleanly(monkeypatch):
    # M6: Stage 10 re-fetches the adv feed with no guard. It is reachable
    # when the operator disabled every earlier adv consumer (Stage 6/7
    # reviews + the adv-served vetoes): the feed raising must then hold the
    # signal cleanly (fail-closed), not crash with a raw exception.
    monkeypatch.setattr(config, "LIQ_REVIEW", False)
    monkeypatch.setattr(config, "WHALE_REVIEW", False)
    monkeypatch.setattr(config, "DISABLED_VETOES",
                        frozenset({"leverage_risk", "squeeze", "whale"}))
    m = _benign_4h_for_sr(FakeMarket(bullish=True))
    m.raise_adv = True
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "derivatives feed unavailable" in reason
    assert "holding" in reason


def test_stage10_adv_incomplete_inputs_holds_cleanly(monkeypatch):
    """M6 extension: When adv feed returns incomplete dict with None fields,
    Stage 10 must hold cleanly (fail-closed) rather than crashing with TypeError.
    """
    monkeypatch.setattr(config, "LIQ_REVIEW", False)
    monkeypatch.setattr(config, "WHALE_REVIEW", False)
    monkeypatch.setattr(config, "DISABLED_VETOES",
                        frozenset({"leverage_risk", "squeeze", "whale"}))
    m = _benign_4h_for_sr(FakeMarket(bullish=True))
    m.adv = {"retail_long": None, "funding": 0.0001, "oi_usd": 1000.0, "volume_24h": 5000.0}
    signal, reason = evaluate_symbol("ETHUSDT", m)
    assert signal is None
    assert "incomplete" in reason or "unavailable" in reason
    assert "holding" in reason


# ------------------------------------------------- V20: Volatility Shock (Phase 4)
def test_volatility_shock_benign_passes():
    m = FakeMarket(bullish=True)
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert "volatility_shock" not in by


def test_volatility_shock_fires_on_extreme_candle():
    m = FakeMarket(bullish=True)
    # Inflate last candle's high so true range is 3.5x ATR
    from indicators import atr_wilder
    atr = atr_wilder(m.klines["15m"][:-1], 14)
    last = m.klines["15m"][-1]
    last["high"] = float(last["low"]) + atr * 3.5
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert "volatility_shock" in by
    assert by["volatility_shock"][0] == "veto"
    assert "extreme volatility shock" in by["volatility_shock"][1]


def test_volatility_shock_missing_klines_holds():
    m = FakeMarket(bullish=True)
    m.klines["15m"] = m.klines["15m"][:5]  # too few klines
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert "volatility_shock" in by
    assert by["volatility_shock"][0] == "hold"
    assert "fail-closed" in by["volatility_shock"][1]


def test_volatility_shock_disabled_toggle():
    m = FakeMarket(bullish=True)
    from indicators import atr_wilder
    atr = atr_wilder(m.klines["15m"][:-1], 14)
    m.klines["15m"][-1]["high"] = float(m.klines["15m"][-1]["low"]) + atr * 4.0
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP,
                                   disabled={"volatility_shock"}))
    assert "volatility_shock" not in by


# ------------------------------------------------- V21: Trend Strength gate (2026-10-17)
def _flat_4h(n=220):
    return candles_from_closes([100.0 + (i % 2) * 0.1 for i in range(n)])


def _trend_4h(n=220, up=True):
    step = 1.005 if up else 0.995
    return candles_from_closes([100.0 * step ** i for i in range(n)])


def _pin_trend_cfg(monkeypatch):
    monkeypatch.setattr(config, "TREND_STRENGTH_VETO_ENABLED", True)
    monkeypatch.setattr(config, "TREND_STRENGTH_MIN_DISPLACEMENT_PCT", 1.5)
    monkeypatch.setattr(config, "TREND_STRENGTH_LOOKBACK_4H", 6)


def test_trend_strength_flat_vetoes_both_directions(monkeypatch):
    _pin_trend_cfg(monkeypatch)
    m = FakeMarket(bullish=True)
    m.klines["4h"] = _flat_4h()
    for direction in ("BULLISH", "BEARISH"):
        by = blocks_by_code(run_vetoes("BTCUSDT", direction, m, BENIGN_SNAP))
        assert "trend_strength" in by, direction
        assert by["trend_strength"][0] == "veto", direction
        assert "no real trend" in by["trend_strength"][1]


def test_trend_strength_with_trend_passes(monkeypatch):
    _pin_trend_cfg(monkeypatch)
    m = FakeMarket(bullish=True)
    m.klines["4h"] = _trend_4h(up=True)  # +3.0% over lookback
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert "trend_strength" not in by
    m2 = FakeMarket(bullish=False)
    m2.klines["4h"] = _trend_4h(up=False)  # -3.0% over lookback
    by = blocks_by_code(run_vetoes("BTCUSDT", "BEARISH", m2, BENIGN_SNAP))
    assert "trend_strength" not in by


def test_trend_strength_counter_trend_vetoes(monkeypatch):
    _pin_trend_cfg(monkeypatch)
    m = FakeMarket(bullish=True)
    m.klines["4h"] = _trend_4h(up=True)  # 4h up +3%, SHORT is counter-trend
    by = blocks_by_code(run_vetoes("BTCUSDT", "BEARISH", m, BENIGN_SNAP))
    assert "trend_strength" in by
    assert by["trend_strength"][0] == "veto"


def test_trend_strength_missing_klines_holds(monkeypatch):
    _pin_trend_cfg(monkeypatch)
    m = FakeMarket(bullish=True)
    m.klines["4h"] = m.klines["4h"][:5]  # too few for lookback+1
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP))
    assert "trend_strength" in by
    assert by["trend_strength"][0] == "hold"
    assert "fail-closed" in by["trend_strength"][1]


def test_trend_strength_disabled(monkeypatch):
    _pin_trend_cfg(monkeypatch)
    m = FakeMarket(bullish=True)
    m.klines["4h"] = _flat_4h()
    by = blocks_by_code(run_vetoes("BTCUSDT", "BULLISH", m, BENIGN_SNAP,
                                   disabled={"trend_strength"}))
    assert "trend_strength" not in by


def test_trend_strength_reproduces_2026_10_17_chop_losses(monkeypatch):
    # Post-mortem: CAP/HUMA/ZEC/CHIP SHORTs were taken with |4h| < 0.6%.
    # A 4h tape drifting only +0.4% over the window must be vetoed for SHORT.
    _pin_trend_cfg(monkeypatch)
    closes = [100.0 * (1 + 0.004 * i / 219) for i in range(220)]
    m = FakeMarket(bullish=False)
    m.klines["4h"] = candles_from_closes(closes)
    by = blocks_by_code(run_vetoes("CAPUSDT", "BEARISH", m, BENIGN_SNAP))
    assert "trend_strength" in by
    assert by["trend_strength"][0] == "veto"
