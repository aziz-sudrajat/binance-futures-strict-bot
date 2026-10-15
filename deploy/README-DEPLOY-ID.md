# Deploy Bot ke Oracle Cloud Always Free (Gratis, $0/bln)

Bot jalan 24/7 di VPS Singapore — IP-nya tidak kena blokir 451 Binance.

## Bagian 1 — Daftar Oracle Cloud (kamu yang lakukan, ~15 menit)

1. Buka https://www.oracle.com/cloud/free/ → **Start for free**
2. Isi data diri + verifikasi email & SMS
3. Masukkan kartu debit/kredit — **hanya untuk verifikasi, tidak di-charge**
4. **Home region: pilih Singapore (`ap-singapore-1`)** — tidak bisa diganti nanti!

## Bagian 2 — Buat VM gratis

1. Login ke console Oracle Cloud
2. Compute → Instances → **Create instance**
   - Image: **Ubuntu 24.04** (aarch64/ARM)
   - Shape: **VM.Standard.A1.Flex** → 2 OCPU, 12 GB RAM (Always Free)
   - Networking: izinkan SSH (port 22) — centang "Assign a public IPv4"
   - SSH key: upload `~/.ssh/id_ed25519.pub` kamu (atau generate baru)
3. Create. Kalau muncul **"Out of capacity"**, coba lagi berkala (pagi hari WIB biasanya lebih longgar).
4. Catat **public IP** VM.

## Bagian 3 — Setup bot (copy-paste)

SSH ke VM, lalu:

```bash
git clone https://github.com/aziz-sudrajat/binance-futures-strict-bot
bash ~/binance-futures-strict-bot/deploy/setup-oracle.sh
```

Script itu: install Python, clone repo, buat venv, install dependency,
pasang systemd service (jalan otomatis tiap boot).

Lalu isi key testnet:

```bash
nano ~/binance-futures-strict-bot/.env
# ganti isi_key_testnet_disini / isi_secret_testnet_disini
sudo systemctl restart binance-testnet.service
```

Cek jalan:

```bash
sudo systemctl status binance-testnet.service
sudo journalctl -u binance-testnet.service -f   # live log, Ctrl+C keluar
```

## Bagian 4 — Whitelist IP di Binance (penting!)

Tanpa ini API key expire tiap 90 hari (error -2015):

1. https://www.binance.com → API Management
2. Edit API key → **Restrict access to trusted IPs only**
3. Masukkan public IP VM tadi → Save

## Catatan

- Service jalan `--testnet --loop --fixed-margin-usdt 10` (parameter test yang sama).
- Update bot: `git -C ~/binance-futures-strict-bot pull && sudo systemctl restart binance-testnet.service`
- **Live key JANGAN ditaruh di VPS ini** sampai kamu eksplisit bilang mau live.
  Nanti pakai `--live` dengan file key terpisah + konfirmasi manual.
