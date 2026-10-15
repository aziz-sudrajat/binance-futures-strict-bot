# Live Trading dari Termux (Gratis)

Kenapa Termux: VM/server luar kena geo-block Binance (HTTP 451),
VPS gratis (Oracle) susah daftar. HP kamu di Indonesia **bisa** konek
mainnet — sudah tervalidasi.

## Setup sekali

```bash
bash ~/binance-futures-strict-bot/deploy/termux/setup-termux-live.sh
```

Lalu matikan battery optimization:
**Settings → Apps → Termux → Battery → Unrestricted**

## Jalanin live (tiap buka Termux)

```bash
export BINANCE_LIVE_KEY='key_kamu'
export BINANCE_LIVE_SECRET='secret_kamu'
bash ~/binance-futures-strict-bot/deploy/termux/start-live.sh
# ketik LIVE saat diminta konfirmasi
```

Key JANGAN dimasukkan ke `.env` / file repo (itu untuk testnet).
Export manual lebih aman; kalau mau praktis, taruh di `~/.bashrc`
(chmod 600) — hanya kalau HP tidak dipinjam orang.

## Operasional

| Perintah | Fungsi |
|---|---|
| `tmux attach -t bot-live` | lihat log live |
| `Ctrl+B` lalu `D` | lepas tanpa matikan bot |
| `tmux kill-session -t bot-live` | stop bot |

## Batasan jujur

- HP restart → bot mati, harus start ulang manual.
  (Otomatis penuh butuh aplikasi Termux:Boot.)
- Sinyal/WiFi putus → loop retry sendiri, tapi entry bisa terlewat.
- Untuk uang beneran, VPS berbayar tetap lebih ideal long-term.
  Termux ini solusi $0 yang paling realistis sekarang.
