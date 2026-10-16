# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Tests for the dynamic universe (curated list + top-volume mode)."""
import pytest
import config
from exchange import ExchangeClient
from executor import resolve_universe
from dashboard import validate_settings


def _client_with_tickers(tickers):
    c = ExchangeClient("k", "s")
    c._request = lambda *a, **k: tickers  # noqa: E731 - stub public endpoint
    return c


TICKERS = [
    {"symbol": "BTCUSDT", "quoteVolume": "500000000"},
    {"symbol": "ETHUSDT", "quoteVolume": "300000000"},
    {"symbol": "BTCUPUSDT", "quoteVolume": "999999999"},   # leveraged -> out
    {"symbol": "ETHDOWNUSDT", "quoteVolume": "888888888"},  # leveraged -> out
    {"symbol": "BNBUSDT", "quoteVolume": "100000000"},
    {"symbol": "DOGEUSD", "quoteVolume": "777777777"},      # not USDT-M -> out
    {"symbol": "XRPUSDT", "quoteVolume": "0"},              # zero vol -> out
]


def test_top_volume_sorted_and_filtered():
    syms = _client_with_tickers(TICKERS).get_top_volume_symbols(10)
    assert syms == ["BTCUSDT", "ETHUSDT", "BNBUSDT"]


def test_top_volume_capped():
    syms = _client_with_tickers(TICKERS).get_top_volume_symbols(2)
    assert syms == ["BTCUSDT", "ETHUSDT"]


def test_resolve_curated_default():
    assert resolve_universe(object(), {}) == config.UNIVERSE
    assert resolve_universe(object(), {"universe_mode": "curated"}) == \
        config.UNIVERSE


def test_resolve_top_volume():
    syms = resolve_universe(_client_with_tickers(TICKERS),
                            {"universe_mode": "top_volume",
                             "universe_top_n": 10})
    assert syms == ["BTCUSDT", "ETHUSDT", "BNBUSDT"]  # n clamped min 10


def test_resolve_top_volume_failure_falls_back():
    class Bad:
        def get_top_volume_symbols(self, n):
            raise RuntimeError("no network")
    assert resolve_universe(Bad(), {"universe_mode": "top_volume"}) == \
        config.UNIVERSE


def test_resolve_top_volume_empty_falls_back():
    assert resolve_universe(_client_with_tickers([]),
                            {"universe_mode": "top_volume"}) == \
        config.UNIVERSE


def test_resolve_top_n_clamped():
    c = _client_with_tickers(TICKERS)
    syms = resolve_universe(c, {"universe_mode": "top_volume",
                                "universe_top_n": 9999})
    assert len(syms) == 3  # only 3 valid tickers in fixture


def test_curated_universe_is_large_and_unique():
    assert len(config.UNIVERSE) >= 50
    assert len(set(config.UNIVERSE)) == len(config.UNIVERSE)
    assert all(s.endswith("USDT") for s in config.UNIVERSE)


def test_universe_settings_validate():
    out = validate_settings({"universe_mode": "top_volume",
                             "universe_top_n": 50})
    assert out["universe_mode"] == "top_volume"
    assert out["universe_top_n"] == 50


@pytest.mark.parametrize("bad", [
    {"universe_mode": "bogus"},
    {"universe_top_n": 5},
    {"universe_top_n": 101},
    {"universe_top_n": "banyak"},
])
def test_universe_settings_reject(bad):
    with pytest.raises(ValueError):
        validate_settings(bad)


@pytest.mark.parametrize("mode", ["curated", "top_volume", "top_movers", "all"])
def test_universe_settings_accept_all_modes(mode):
    out = validate_settings({"universe_mode": mode})
    assert out["universe_mode"] == mode


# ------------------------------------------------ top_movers (GAS parity)

def _movers_market(tickers, symbols=None):
    class M:
        def get_swap_tickers(self):
            return tickers

        def list_okx_swap_symbols(self):
            return symbols if symbols is not None else []
    return M()


MOVERS_TICKERS = [
    # symbol, base, vol_usd_24h, chg_pct_24h
    {"symbol": "PUMPUSDT", "base": "PUMP", "last": 2.0,
     "vol_usd_24h": 50_000_000, "chg_pct_24h": 40.0},   # top mover
    {"symbol": "DUMPUSDT", "base": "DUMP", "last": 1.0,
     "vol_usd_24h": 50_000_000, "chg_pct_24h": -35.0},  # 2nd (|chg|)
    {"symbol": "CALMUSDT", "base": "CALM", "last": 1.0,
     "vol_usd_24h": 500_000_000, "chg_pct_24h": 1.0},   # huge vol, tiny move
    {"symbol": "USDCUSDT", "base": "USDC", "last": 1.0,
     "vol_usd_24h": 900_000_000, "chg_pct_24h": 25.0},  # stablecoin -> out
    {"symbol": "QUIETUSDT", "base": "QUIET", "last": 1.0,
     "vol_usd_24h": 100_000, "chg_pct_24h": 60.0},      # below min vol -> out
]


def test_select_top_movers_ranking_and_filters():
    from executor import select_top_movers
    out = select_top_movers(MOVERS_TICKERS, 10, min_vol_usd=500_000)
    # |40|*log10(50M) > |35|*log10(50M) > |1|*log10(500M); stables & quiet cut
    assert out == ["PUMPUSDT", "DUMPUSDT", "CALMUSDT"]
    assert "USDCUSDT" not in out
    assert "QUIETUSDT" not in out


def test_select_top_movers_n_clamped():
    from executor import select_top_movers
    assert select_top_movers(MOVERS_TICKERS, 1, 0) == ["PUMPUSDT"]
    assert select_top_movers([], 10, 0) == []


def test_resolve_top_movers():
    syms = resolve_universe(object(),
                            {"universe_mode": "top_movers",
                             "universe_top_n": 10,
                             "min_24h_volume_usd": 500_000},
                            market=_movers_market(MOVERS_TICKERS))
    assert syms == ["PUMPUSDT", "DUMPUSDT", "CALMUSDT"]


def test_resolve_top_movers_failure_falls_back():
    class Bad:
        def get_swap_tickers(self):
            raise RuntimeError("no network")
    assert resolve_universe(object(), {"universe_mode": "top_movers"},
                            market=Bad()) == config.UNIVERSE


def test_resolve_all():
    syms = resolve_universe(
        object(), {"universe_mode": "all"},
        market=_movers_market([], symbols=["BTCUSDT", "USDCUSDT", "ETHUSDT"]))
    assert syms == ["BTCUSDT", "ETHUSDT"]  # stables excluded


def test_resolve_all_failure_falls_back():
    class Bad:
        def list_okx_swap_symbols(self):
            raise RuntimeError("no network")
    assert resolve_universe(object(), {"universe_mode": "all"},
                            market=Bad()) == config.UNIVERSE


def test_resolve_unknown_mode_falls_back_to_curated():
    assert resolve_universe(object(), {"universe_mode": "bogus"}) == \
        config.UNIVERSE
