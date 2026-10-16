#!/usr/bin/env bash
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

# Switch the bot between LIVE and DEMO key/mode.
#
#   source switch.sh live   -> mainnet keys, BOT_MODE=live (REAL MONEY)
#   source switch.sh demo   -> testnet keys, BOT_MODE=testnet (fake money)
#
# Keys live in .env.live / .env.demo (gitignored, never committed).
# Copy the .example templates first and fill in your keys:
#   cp .env.live.example .env.live && cp .env.demo.example .env.demo
#
# Must be *sourced* (not executed) so the exports land in your shell.
# run_executor.py and dashboard.py both honor BOT_MODE when no explicit
# --live/--testnet/--mode flag is given.

_MODE="${1:-}"
if [ "$_MODE" != "live" ] && [ "$_MODE" != "demo" ]; then
    echo "usage: source switch.sh live|demo"
    return 1 2>/dev/null || exit 1
fi

_ENV_FILE=".env.${_MODE}"
if [ ! -f "$_ENV_FILE" ]; then
    echo "missing $_ENV_FILE"
    echo "  cp ${_ENV_FILE}.example $_ENV_FILE  # then fill in your keys"
    return 1 2>/dev/null || exit 1
fi

set -a
# shellcheck disable=SC1090
. "./$_ENV_FILE"
set +a

if [ "$_MODE" = "live" ]; then
    : "${BINANCE_LIVE_KEY:?BINANCE_LIVE_KEY missing in .env.live}"
    : "${BINANCE_LIVE_SECRET:?BINANCE_LIVE_SECRET missing in .env.live}"
    export BOT_MODE=live
    echo "MODE: LIVE (mainnet, REAL MONEY) — keys from .env.live"
else
    : "${BINANCE_TESTNET_KEY:?BINANCE_TESTNET_KEY missing in .env.demo}"
    : "${BINANCE_TESTNET_SECRET:?BINANCE_TESTNET_SECRET missing in .env.demo}"
    export BOT_MODE=testnet
    echo "MODE: DEMO (testnet, fake money) — keys from .env.demo"
fi
