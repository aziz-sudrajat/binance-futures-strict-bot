#!/data/data/com.termux/files/usr/bin/bash
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

# Jalankan live auto-trading di Termux dalam tmux (persisten).
# UANG BENERAN. Baca dulu sebelum jalanin.
set -euo pipefail

BOT_DIR="$HOME/binance-futures-strict-bot"
SESSION="bot-live"

if [ -z "${BINANCE_LIVE_KEY:-}" ] || [ -z "${BINANCE_LIVE_SECRET:-}" ]; then
  echo "ERROR: set dulu key live di session ini:"
  echo ""
  echo "  export BINANCE_LIVE_KEY='key_kamu'"
  echo "  export BINANCE_LIVE_SECRET='secret_kamu'"
  echo ""
  echo "Jangan taruh di file / repo. Export manual tiap buka Termux,"
  echo "atau simpan di ~/.bashrc (chmod 600) kalau HP hanya dipakai kamu."
  exit 1
fi

# Cegah double-run
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session $SESSION sudah jalan. Attach: tmux attach -t $SESSION"
  exit 0
fi

termux-wake-lock 2>/dev/null || true

echo "=== KONFIRMASI LIVE ==="
echo "Bot akan trading dengan UANG BENERAN di Binance Futures mainnet."
echo "Risk default: 1% per trade, max 3x leverage, state terpisah (state-live.json)."
echo ""
read -r -p "Ketik LIVE untuk lanjut: " ans
[ "$ans" = "LIVE" ] || { echo "dibatalkan"; exit 1; }

tmux new-session -d -s "$SESSION" \
  "cd $BOT_DIR && .venv/bin/python run_executor.py --live --loop 2>&1 | tee -a live-loop.log"

echo ""
echo "Live bot jalan di tmux session '$SESSION'."
echo "  Lihat log : tmux attach -t $SESSION   (Ctrl+B lalu D untuk lepas)"
echo "  Stop      : tmux kill-session -t $SESSION"
echo "  Wake-lock : biarkan aktif + battery Termux = Unrestricted"
