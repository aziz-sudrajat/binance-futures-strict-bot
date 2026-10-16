"""Unit tests for live performance metrics (item 3.3). No network."""
import math

import metrics
from metrics import compute_metrics, from_lifecycle_file


def _ev(pnl, terminal=True):
    return {"ts": 1, "signal_id": "s", "symbol": "BTCUSDT",
            "state": "CLOSED", "realized": pnl, "terminal": terminal}


def test_empty():
    assert compute_metrics([]) == {}
    assert compute_metrics([_ev(1.0, terminal=False)]) == {}


def test_win_rate_and_totals():
    m = compute_metrics([_ev(2.0), _ev(-1.0), _ev(3.0), _ev(-0.5)])
    assert m["n_trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate_pct"] == 50.0
    assert m["total_pnl"] == 3.5
    assert m["avg_win"] == 2.5
    assert m["avg_loss"] == -0.75
    assert m["profit_factor"] == round(5.0 / 1.5, 3)


def test_profit_factor_infinite_when_no_losses():
    m = compute_metrics([_ev(1.0), _ev(2.0)])
    assert m["profit_factor"] == "inf"


def test_max_drawdown():
    # equity: 5 -> 3 -> 8 -> 2 ; max dd = 8-2 = 6
    m = compute_metrics([_ev(5.0), _ev(-2.0), _ev(5.0), _ev(-6.0)])
    assert m["max_drawdown"] == 6.0


def test_sharpe_per_trade():
    pnls = [1.0, 2.0, 3.0]
    m = compute_metrics([_ev(p) for p in pnls])
    mean = sum(pnls) / 3
    var = sum((p - mean) ** 2 for p in pnls) / 2
    assert m["sharpe_per_trade"] == round(mean / math.sqrt(var), 3)


def test_sharpe_single_trade_is_zero():
    assert compute_metrics([_ev(1.0)])["sharpe_per_trade"] == 0.0


def test_non_terminal_ignored():
    m = compute_metrics([_ev(5.0, terminal=False), _ev(2.0)])
    assert m["n_trades"] == 1
    assert m["total_pnl"] == 2.0


def test_from_lifecycle_file(tmp_path):
    p = tmp_path / "lifecycle.jsonl"
    p.write_text('{"signal_id":"a","symbol":"X","state":"C","realized":1.5,"terminal":true}\n'
                 'not json\n'
                 '{"signal_id":"b","symbol":"X","state":"C","realized":-0.5,"terminal":true}\n')
    m = from_lifecycle_file(str(p))
    assert m["n_trades"] == 2
    assert m["total_pnl"] == 1.0
    assert from_lifecycle_file(str(tmp_path / "missing.jsonl")) == {}
