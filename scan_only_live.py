# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import dashboard

from data import MarketData
from signal_engine import evaluate_symbol
from executor import resolve_universe
settings = dashboard.load_settings(mode='live')
config.apply_dashboard_overrides(settings)
print('signal_mode:', config.SIGNAL_MODE, '| bullish_only:', config.BULLISH_ONLY)
market = MarketData()
universe = resolve_universe(None, settings, None, market=market)
print('universe:', len(universe), sorted(universe))
for s in sorted(universe):
    try:
        signal, note = evaluate_symbol(s, market)
    except Exception as e:
        print(f'{s}: ERROR {e}')
        continue
    if signal:
        print(f'{s}: PASS q={signal.get("quality")} dir={signal.get("direction")}')
    else:
        print(f'{s}: TOLAK {note}')
