#!/data/data/com.termux/files/usr/bin/bash
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

# Setup live trading di Termux (gratis, tanpa VPS).
# Jalankan sekali:  bash ~/binance-futures-strict-bot/deploy/termux/setup-termux-live.sh
set -euo pipefail

BOT_DIR="$HOME/binance-futures-strict-bot"

echo "== 1/4: paket Termux =="
pkg update -qq -y > /dev/null 2>&1 || true
pkg install -y -q python git tmux termux-api > /dev/null 2>&1 || pkg install -y python git tmux
echo OK

echo "== 2/4: repo terbaru =="
if [ -d "$BOT_DIR/.git" ]; then
  git -C "$BOT_DIR" pull --ff-only || echo "(pull gagal — lanjut)"
else
  git clone https://github.com/aziz-sudrajat/binance-futures-strict-bot "$BOT_DIR"
fi
echo OK

echo "== 3/4: venv + dependency =="
cd "$BOT_DIR"
[ -d .venv ] || python -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo OK

echo "== 4/4: kunci wake-lock (anti Doze) =="
termux-wake-lock 2>/dev/null && echo "wake-lock AKTIF" || echo "(termux-api belum terpasang penuh — install manual: pkg install termux-api)"

echo ""
echo "Selesai. JANGAN taruh live key di .env repo ini (itu key testnet)."
echo "Live key dibaca dari environment — lihat start-live.sh."
echo ""
echo "Matikan battery optimization untuk Termux di:"
echo "  Settings > Apps > Termux > Battery > Unrestricted"
