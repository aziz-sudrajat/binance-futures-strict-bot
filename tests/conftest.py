"""Suite-wide guard: the OKX WS price feed (wsfeed, item 2.5) must never
touch the real network during unit tests.

The executor's freshness re-check routes through wsfeed.get_live_price(),
which lazily starts a background thread to wss://ws.okx.com. That is
correct in production but forbidden here: any test that wants WS behavior
must construct its own WsPriceFeed against a local fake server
(tests/test_wsfeed.py). Everything else gets pure-REST behavior.

Hardening (2026-10-17, Shiki): the flag alone was not enough. Two holes
let the real network in during the full suite and made
test_signal_expiry::test_fresh_signal_accepted flaky:

1. apply_dashboard_overrides(self._dash_settings()) re-enables
   WS_PRICE_FEED_ENABLED from dashboard defaults (True) in the middle of
   an executor cycle, defeating the monkeypatched flag.
2. _shared_feed() is a process-wide singleton: once some earlier test
   warmed it with a REAL cached BTC price (~$81k), any later test hitting
   get_live_price() with the re-enabled flag got the real price instead
   of the fake market's price.

This fixture now also drops the singleton before every test and, for all
modules except test_wsfeed.py, replaces the singleton factory with a
no-network stub (no subscriptions, no cached prices), so get_live_price()
always falls back to REST here.
"""
import pytest

import config


class _NoNetworkFeed:
    """Stand-in for WsPriceFeed: never touches the network."""

    def ensure_symbols(self, symbols):
        return None

    def fresh_price(self, inst_id, stale_sec):
        return None


@pytest.fixture(autouse=True)
def _ws_price_feed_disabled(monkeypatch, request):
    monkeypatch.setattr(config, "WS_PRICE_FEED_ENABLED", False)
    import wsfeed
    # Hole 2: drop any singleton a previous test left behind (a warm feed
    # holding a real cached price would leak into this test the moment some
    # code path re-enables the flag mid-test).
    wsfeed._reset_shared_feed()
    if "test_wsfeed" not in request.node.nodeid:
        # Hole 1: even with the flag re-enabled mid-test, unit tests must
        # never dial wss://ws.okx.com. The stub keeps get_live_price() on
        # the pure-REST path. test_wsfeed.py keeps the real factory — it
        # drives its own feeds against a local fake server.
        monkeypatch.setattr(wsfeed, "_shared_feed",
                            lambda: _NoNetworkFeed())
