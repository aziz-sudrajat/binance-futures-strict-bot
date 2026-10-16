"""Unit tests for the pluggable sentiment provider (item 3.4).

No network. Verifies the interface contract, the default keyword
provider's sign convention (must match the old _news_sentiment exactly),
and that providers are swappable.
"""
import pytest

import sentiment
from sentiment import (
    KeywordSentimentProvider,
    SentimentProvider,
    get_provider,
    set_provider,
)
from data import _news_sentiment


@pytest.fixture(autouse=True)
def _restore_default_provider():
    yield
    set_provider(KeywordSentimentProvider())


def test_default_provider_is_keyword():
    assert isinstance(get_provider(), KeywordSentimentProvider)


def test_sign_convention_bearish():
    assert KeywordSentimentProvider().score(
        "Bitcoin hacked, funds stolen in exploit") < -4


def test_sign_convention_bullish():
    assert KeywordSentimentProvider().score(
        "Ethereum ETF approval drives rally") > 0


def test_sign_convention_neutral():
    assert KeywordSentimentProvider().score(
        "Bitcoin price dips below $82K on CPI data") == 0


def test_wrapper_matches_provider():
    titles = [
        "Bitcoin hacked, funds stolen in exploit",
        "Ethereum ETF approval drives rally",
        "SEC sued exchange over fraud probe",
        "random market update",
    ]
    for t in titles:
        assert _news_sentiment(t) == KeywordSentimentProvider().score(t)


def test_provider_is_swappable():
    class AlwaysBullish(SentimentProvider):
        def score(self, title: str) -> int:
            return 10

    set_provider(AlwaysBullish())
    assert _news_sentiment("anything at all") == 10
    # and back to default
    set_provider(KeywordSentimentProvider())
    assert _news_sentiment("Bitcoin hacked") < 0


def test_set_provider_rejects_non_provider():
    with pytest.raises(TypeError):
        set_provider(object())


def test_interface_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        SentimentProvider().score("x")
