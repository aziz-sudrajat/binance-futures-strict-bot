"""Suite-wide guard: the OKX WS price feed (wsfeed, item 2.5) must never
touch the real network during unit tests.

The executor's freshness re-check routes through wsfeed.get_live_price(),
which lazily starts a background thread to wss://ws.okx.com. That is
correct in production but forbidden here: any test that wants WS behavior
must construct its own WsPriceFeed against a local fake server
(tests/test_wsfeed.py). Everything else gets pure-REST behavior.
"""
import pytest

import config


@pytest.fixture(autouse=True)
def _ws_price_feed_disabled(monkeypatch):
    monkeypatch.setattr(config, "WS_PRICE_FEED_ENABLED", False)
