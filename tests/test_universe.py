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
    {"universe_mode": "all"},
    {"universe_top_n": 5},
    {"universe_top_n": 101},
    {"universe_top_n": "banyak"},
])
def test_universe_settings_reject(bad):
    with pytest.raises(ValueError):
        validate_settings(bad)
