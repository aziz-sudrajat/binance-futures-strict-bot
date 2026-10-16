# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Pluggable news-sentiment providers (item 3.4).

The ketat news review needs a bearish/bullish score per headline. This
module defines the provider INTERFACE so the keyword heuristic can be
swapped for an AI model later without touching callers.

Sign convention (LOCKED — signal_engine thresholds depend on it):
    score > 0  -> bullish news      (e.g. >= +2 counts as "bull")
    score < 0  -> bearish news      (e.g. <= -2 counts as "bear")
    |score| >= 4 -> severe (veto-grade)

Default provider: KeywordSentimentProvider (the original heuristic —
fail-closed, no API key, no network). To plug an AI provider later:

    from sentiment import SentimentProvider, set_provider

    class MyAIProvider(SentimentProvider):
        def score(self, title: str) -> int:
            ...  # call your model, map to the sign convention above ...

    set_provider(MyAIProvider())

Never hardcode API keys/secrets here — read them from env inside your
provider, same rule as the rest of the codebase.
"""
from __future__ import annotations


class SentimentProvider:
    """Interface: score one headline. Positive = bullish, negative = bearish."""

    def score(self, title: str) -> int:
        raise NotImplementedError


class KeywordSentimentProvider(SentimentProvider):
    """Original keyword heuristic (fail-closed, offline).

    Strong bearish words count double. Substring matching is kept
    deliberately (documented quirk, not changed silently).
    """

    NEG_STRONG = ("hack", "exploit", "lawsuit", "fraud", "scam", "bankrupt",
                  "arrest", "stolen", "theft", "breach", "collapse")
    NEG_MILD = ("crash", "dump", "plunge", "tumble", "slump", "fine",
                "penalty", "vulnerab", "attack", "warning", "probe",
                "investigat", "sec", "sued")
    POS = ("etf", "approv", "partnership", "adopt", "upgrade", "launch",
           "all-time high", "record high", "surge", "rally", "bullish",
           "invest", "backing", "integrat")

    def score(self, title: str) -> int:
        t = (title or "").lower()
        bearish = (2 * sum(1 for w in self.NEG_STRONG if w in t)
                   + sum(1 for w in self.NEG_MILD if w in t))
        bullish = sum(1 for w in self.POS if w in t)
        return bullish - bearish  # positive = bullish, negative = bearish


_provider: SentimentProvider = KeywordSentimentProvider()


def get_provider() -> SentimentProvider:
    """The active provider (default: keyword heuristic)."""
    return _provider


def set_provider(p: SentimentProvider) -> None:
    """Swap the active provider (e.g. tests, or a future AI provider)."""
    global _provider
    if not isinstance(p, SentimentProvider):
        raise TypeError("provider must be a SentimentProvider")
    _provider = p
