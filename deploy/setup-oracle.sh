#!/bin/bash
# Setup bot di Oracle Cloud Always Free (Ubuntu 24.04 ARM, Singapore).
# Jalankan sekali sebagai user ubuntu:  bash ~/deploy/setup-oracle.sh
set -euo pipefail

REPO_URL="https://github.com/aziz-sudrajat/binance-futures-strict-bot"
BOT_DIR="$HOME/binance-futures-strict-bot"

echo "== 1/5: paket sistem =="
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 python3-venv python3-pip curl > /dev/null
echo OK

echo "== 2/5: clone repo =="
if [ -d "$BOT_DIR/.git" ]; then
  git -C "$BOT_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$BOT_DIR"
fi
echo OK

echo "== 3/5: virtualenv + dependencies =="
cd "$BOT_DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo OK

echo "== 4/5: file .env =="
if [ ! -f "$BOT_DIR/.env" ]; then
  cat > "$BOT_DIR/.env" << 'ENVEOF'
# Isi dengan key TESTNET kamu (dari demo.binance.com).
# JANGAN commit file ini. chmod 600.
BINANCE_TESTNET_KEY=isi_key_testnet_disini
BINANCE_TESTNET_SECRET=isi_secret_testnet_disini
ENVEOF
  chmod 600 "$BOT_DIR/.env"
  echo "DIBUAT $BOT_DIR/.env — edit dan isi key testnet kamu!"
else
  echo "sudah ada, dilewati"
fi

echo "== 5/5: systemd service =="
sudo cp "$BOT_DIR/deploy/binance-testnet.service" /etc/systemd/system/
sudo sed -i "s|__BOT_DIR__|$BOT_DIR|g; s|__USER__|$USER|g" \
  /etc/systemd/system/binance-testnet.service
sudo systemctl daemon-reload
sudo systemctl enable --now binance-testnet.service
echo OK

echo ""
echo "Selesai. Cek status:  sudo systemctl status binance-testnet.service"
echo "Lihat log:            sudo journalctl -u binance-testnet.service -f"
echo "PENTING: edit $BOT_DIR/.env dan isi key testnet, lalu:"
echo "         sudo systemctl restart binance-testnet.service"
