"""Live dashboard for the Binance Futures bot (stdlib only, no new dependencies).

Serves a mobile-friendly web UI that shows, live:
  - futures balance, open positions, daily PnL, kill-switch state
  - recent signals & lifecycle events (approved / rejected / TP / SL)
  - auto-trade settings (toggle + risk/leverage caps), honored by the
    executor without a restart (see executor._dash_settings)

Run next to the bot, e.g. on Termux:
    .venv/bin/python dashboard.py --mode live --port 8080
Then open http://localhost:8080 in the phone browser.

Modes: live | testnet | paper (query param ?mode= also switches the view).
Keys come ONLY from the environment (BINANCE_LIVE_KEY/SECRET,
BINANCE_TESTNET_KEY/SECRET) and are never exposed to the browser.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config

# ------------------------------------------------------------------ settings
SETTINGS_PATH = config.DASHBOARD_SETTINGS_FILE

# Canonical defaults live in config.DASHBOARD_DEFAULTS (single source of
# truth shared with the executor).
DEFAULTS = config.DASHBOARD_DEFAULTS


def load_settings(path: str | None = None,
                  mode: str = "live") -> dict:
    """Current settings for a mode, merged over defaults.

    The file holds per-mode sections {"live": {...}, "testnet": {...}}.
    path resolves at call time so tests / callers can redirect it.
    A legacy flat file (no mode keys) is treated as the live section.
    Missing/corrupt -> defaults.
    """
    path = path or SETTINGS_PATH
    s = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f) or {}
        if isinstance(loaded, dict) and any(
                k in loaded for k in ("live", "testnet", "paper")):
            loaded = loaded.get(mode) or {}
        for k in s:
            if k in loaded:
                s[k] = loaded[k]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return s


def validate_settings(data: dict) -> dict:
    """Clean + clamp user settings. Raises ValueError on bad input.

    Risk knobs may only TIGHTEN (never exceed the code defaults).
    Engine knobs (signal/TP/SL) move within their documented ranges.
    """
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    out = dict(DEFAULTS)

    def _bool(key: str) -> None:
        if key in data:
            v = data[key]
            if not isinstance(v, bool):
                raise ValueError(f"{key} must be true/false")
            out[key] = v

    _bool("auto_trade")
    _bool("bullish_only")
    _bool("mtf_require_1d")
    _bool("mtf_require_1h")
    _bool("btc_align_required")
    _bool("pattern_required")
    _bool("news_check")
    _bool("liq_review")
    _bool("whale_review")
    _bool("breakeven_after_tp1")
    for code in config.DASH_VETO_CODES:
        _bool(f"veto_{code}")

    # numeric engine knobs: (attr, lo, hi) ranges from config
    for name, (_attr, lo, hi) in config.DASH_NUMERIC_OVERRIDES.items():
        if name in data:
            try:
                v = float(data[name])
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be a number")
            if not lo <= v <= hi:
                raise ValueError(f"{name} must be {lo:g}-{hi:g}")
            out[name] = v

    # risk-per-trade: tighten-only (cap at the code default)
    if "risk_pct_per_trade" in data:
        try:
            v = float(data["risk_pct_per_trade"])
        except (TypeError, ValueError):
            raise ValueError("risk_pct_per_trade must be a number")
        if not 0.1 <= v <= config.RISK_PCT_PER_TRADE:
            raise ValueError(
                f"risk_pct_per_trade must be 0.1-{config.RISK_PCT_PER_TRADE}")
        out["risk_pct_per_trade"] = v

    # max leverage: tighten-only (cap at the code default)
    if "max_leverage" in data:
        try:
            v = int(data["max_leverage"])
        except (TypeError, ValueError):
            raise ValueError("max_leverage must be an integer")
        if not 1 <= v <= config.MAX_LEVERAGE:
            raise ValueError(f"max_leverage must be 1-{config.MAX_LEVERAGE}")
        out["max_leverage"] = v

    if "poll_interval_min" in data:
        try:
            v = int(data["poll_interval_min"])
        except (TypeError, ValueError):
            raise ValueError("poll_interval_min must be an integer")
        if not 1 <= v <= 60:
            raise ValueError("poll_interval_min must be 1-60")
        out["poll_interval_min"] = v

    if "mtf_30m_mode" in data:
        v = data["mtf_30m_mode"]
        if v not in config.DASH_MTF_MODES:
            raise ValueError(
                f"mtf_30m_mode must be one of {list(config.DASH_MTF_MODES)}")
        out["mtf_30m_mode"] = v

    if "signal_mode" in data:
        v = data["signal_mode"]
        if v not in config.SIGNAL_MODES:
            raise ValueError(
                f"signal_mode must be one of {list(config.SIGNAL_MODES)}")
        out["signal_mode"] = v

    for i in (1, 2, 3):
        k = f"tp_split_{i}"
        if k in data:
            try:
                v = float(data[k])
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            # wide range: apply_dashboard_overrides normalizes to sum 1.0
            if not 0.01 <= v <= 10:
                raise ValueError(f"{k} must be 0.01-10")
            out[k] = v

    # ---- order execution (full operator control) ----
    if "margin_mode" in data:
        v = data["margin_mode"]
        if v not in ("ISOLATED", "CROSS"):
            raise ValueError("margin_mode must be ISOLATED or CROSS")
        out["margin_mode"] = v

    if "leverage_mode" in data:
        v = data["leverage_mode"]
        if v not in ("auto", "fixed"):
            raise ValueError("leverage_mode must be auto or fixed")
        out["leverage_mode"] = v

    if "leverage" in data:
        try:
            v = int(data["leverage"])
        except (TypeError, ValueError):
            raise ValueError("leverage must be an integer")
        if not 1 <= v <= 125:
            raise ValueError("leverage must be 1-125")
        out["leverage"] = v

    if "entry_mode" in data:
        v = data["entry_mode"]
        if v not in ("risk", "fixed_usdt"):
            raise ValueError("entry_mode must be risk or fixed_usdt")
        out["entry_mode"] = v

    if "fixed_usdt" in data:
        try:
            v = float(data["fixed_usdt"])
        except (TypeError, ValueError):
            raise ValueError("fixed_usdt must be a number")
        if not 1 <= v <= 100_000:
            raise ValueError("fixed_usdt must be 1-100000")
        out["fixed_usdt"] = v

    # ---- universe ----
    if "universe_mode" in data:
        v = data["universe_mode"]
        if v not in config.UNIVERSE_MODES:
            raise ValueError(
                f"universe_mode must be one of {list(config.UNIVERSE_MODES)}")
        out["universe_mode"] = v

    if "universe_top_n" in data:
        try:
            v = int(data["universe_top_n"])
        except (TypeError, ValueError):
            raise ValueError("universe_top_n must be an integer")
        if not 10 <= v <= config.UNIVERSE_TOP_N_MAX:
            raise ValueError(
                f"universe_top_n must be 10-{config.UNIVERSE_TOP_N_MAX}")
        out["universe_top_n"] = v

    return out


def save_settings(data: dict, path: str | None = None,
                  mode: str = "live") -> dict:
    """Validate + atomically persist a mode's settings. Returns cleaned dict."""
    path = path or SETTINGS_PATH
    cleaned = validate_settings(data)
    try:
        with open(path, "r", encoding="utf-8") as f:
            full = json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        full = {}
    if not isinstance(full, dict) or not any(
            k in full for k in ("live", "testnet", "paper")):
        full = {}  # legacy flat file: start fresh per-mode sections
    full[mode] = cleaned
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=2)
    os.replace(tmp, path)
    return cleaned


# ------------------------------------------------------------------ data
def mode_files(mode: str) -> tuple[str, str]:
    """(state_path, lifecycle_path) for a mode."""
    if mode == "live":
        return config.EXEC_LIVE_STATE_FILE, config.EXEC_LIVE_LIFECYCLE_FILE
    return config.EXEC_STATE_FILE, config.EXEC_LIFECYCLE_FILE


def _read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _exchange_for_mode(mode: str, env: dict | None = None):
    """Exchange client for balance reads. Raises SystemExit without keys."""
    # Imported lazily so dashboard.py stays importable without the executor.
    from run_executor import require_live_keys, require_testnet_keys
    from exchange import ExchangeClient
    from risk import PaperBroker
    from data import MarketData
    env = os.environ if env is None else env
    if mode == "live":
        key, secret = require_live_keys(env)
        base = (env.get("BINANCE_FUTURES_BASE_URL")
                or config.BINANCE_FUTURES_MAINNET_URL)
        return ExchangeClient(key, secret, base_url=base)
    if mode == "testnet":
        key, secret = require_testnet_keys(env)
        base = (env.get("BINANCE_FUTURES_BASE_URL")
                or config.BINANCE_FUTURES_BASE_URL)
        return ExchangeClient(key, secret, base_url=base)
    return PaperBroker(config.EXEC_PAPER_START_BALANCE,
                       price_fn=MarketData().get_live_price)


def _fmt_ts(ts_ms: int) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000)
        return dt.strftime("%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def apply_signal_mode_preset(settings: dict, mode: str) -> dict:
    """Apply a signal-mode preset bundle onto a settings dict (in place).

    The mode is a preset, not a lock: individual knobs can still be
    fine-tuned afterwards. Raises ValueError for an unknown mode.
    """
    try:
        bundle = config.SIGNAL_MODE_PRESETS[mode]
    except KeyError:
        raise ValueError(
            f"signal_mode must be one of {list(config.SIGNAL_MODES)}")
    settings["signal_mode"] = mode
    settings.update(bundle)
    return settings


def scanner_status() -> dict:
    """Scanner (stage 1) status + fresh signals from signals.db."""
    out: dict = {"state": None, "db": {"fresh": 0}, "signals": []}
    try:
        with open("scanner_state.json", encoding="utf-8") as f:
            out["state"] = json.load(f)
    except (OSError, ValueError):
        pass
    try:
        from signals_db import SignalDB
        db = SignalDB("signals.db")
        try:
            db.expire_old()
            out["db"] = db.stats()
            rows = db.get_fresh_signals(limit=20)
            now_ms = int(time.time() * 1000)
            out["signals"] = [{
                "symbol": r["symbol"],
                "direction": r["direction"],
                "quality": r["quality"],
                "entry": r["entry"],
                "age_min": round((now_ms - r["created_at"]) / 60000, 1),
                "expires_min": round((r["expires_at"] - now_ms) / 60000, 1),
            } for r in rows]
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - db may not exist yet
        pass
    return out


# (key, label, kind) — kind: bool | int | float | choice:A,B,C
# Canonical settings field spec shared by the dashboard, TUI and the
# `binance` terminal shell.
SETTING_FIELDS: list[tuple[str, str, str]] = [
    ("signal_mode", "Mode sinyal (preset)", "choice:ketat,sedang,biasa"),
    ("auto_trade", "Auto-trade", "bool"),
    ("universe_mode", "Universe mode", "choice:curated,top_volume,top_movers,all"),
    ("universe_top_n", "Universe top N", "int"),
    ("risk_pct_per_trade", "Risk %/trade", "float"),
    ("max_leverage", "Max leverage", "int"),
    ("poll_interval_min", "Scan interval (min)", "int"),
    ("min_quality_score", "Min quality score", "int"),
    ("mtf_30m_mode", "MTF 30m mode", "choice:STRICT,LEAN"),
    ("mtf_require_1d", "MTF: 1d wajib selaras", "bool"),
    ("bullish_only", "Bullish only", "bool"),
    ("min_24h_volume_usd", "Min 24h volume USD", "float"),
    ("freshness_max_drift_pct", "Freshness drift %", "float"),
    ("tp1_rr", "TP1 (x risk)", "float"),
    ("tp2_rr", "TP2 (x risk)", "float"),
    ("tp3_rr", "TP3 (x risk)", "float"),
    ("stop_atr_min", "Stop ATR min", "float"),
    ("stop_atr_max", "Stop ATR max", "float"),
    ("breakeven_after_tp1", "Breakeven after TP1", "bool"),
    ("margin_mode", "Margin mode", "choice:ISOLATED,CROSS"),
    ("leverage_mode", "Leverage mode", "choice:auto,fixed"),
    ("leverage", "Leverage 1-125", "int"),
    ("entry_mode", "Entry mode", "choice:risk,fixed_usdt"),
    ("fixed_usdt", "Fixed USDT / posisi", "float"),
    ("veto_leverage_risk", "Veto: leverage risk", "bool"),
    ("veto_funding", "Veto: funding extreme", "bool"),
    ("veto_squeeze", "Veto: squeeze risk", "bool"),
    ("veto_whale", "Veto: whale divergence", "bool"),
    ("veto_liq_wall", "Veto: liquidity wall", "bool"),
    ("veto_btc_breaker", "Veto: BTC breaker", "bool"),
]


def run_radar(mode: str = "live", universe_mode: str | None = None) -> list[dict]:
    """Signal Radar: evaluate the whole universe with the CURRENT studio
    settings and rank by quality (approved first). Uses the real engine —
    the same pipeline the executor runs. Respects the universe mode
    (curated / top_volume / top_movers / all); ~1s per symbol.
    `universe_mode` optionally overrides the saved setting for one scan."""
    from data import MarketData
    from signal_engine import evaluate_symbol
    from executor import resolve_universe
    settings = load_settings(mode=mode)
    if universe_mode is not None:
        if universe_mode not in config.UNIVERSE_MODES:
            raise ValueError(
                f"universe_mode must be one of {list(config.UNIVERSE_MODES)}")
        settings = dict(settings, universe_mode=universe_mode)
    config.apply_dashboard_overrides(settings)
    try:
        exchange = _exchange_for_mode(mode)
    except SystemExit:
        exchange = None  # no keys -> curated fallback inside resolve
    market = MarketData()
    universe = resolve_universe(exchange, settings, market=market)
    out = []
    for symbol in universe:
        try:
            signal, note = evaluate_symbol(symbol, market)
        except Exception as e:  # noqa: BLE001 - one symbol never kills radar
            out.append({"symbol": symbol, "state": "error", "quality": None,
                        "reason": str(e)[:160]})
            continue
        if signal is None:
            out.append({"symbol": symbol, "state": "rejected", "quality": None,
                        "reason": note})
        else:
            out.append({
                "symbol": symbol, "state": "approved",
                "quality": signal.get("quality"),
                "strength": signal.get("strength"),
                "direction": signal.get("direction"),
                "entry": signal.get("entry"),
                "entry_low": signal.get("entry_low"),
                "entry_high": signal.get("entry_high"),
                "stop_loss": signal.get("stop_loss"),
                "tp1": signal.get("tp1"), "tp2": signal.get("tp2"),
                "tp3": signal.get("tp3"),
                "rr1": round(signal.get("rr1") or 0, 2),
                "rsi": signal.get("rsi"),
                "regime": signal.get("regime"),
                "volume_ratio": signal.get("volume_ratio"),
            })
    out.sort(key=lambda r: (r["state"] != "approved",
                            -((r.get("quality") or -1)),
                            -((r.get("strength") or -1)),
                            r["symbol"]))
    return out


def read_events(mode: str, limit: int = 60) -> list[dict]:
    """Newest-first lifecycle events with human-readable fields."""
    _, life_path = mode_files(mode)
    events = []
    try:
        with open(life_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return []
    for line in lines[-limit * 3:]:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append({
            "time": _fmt_ts(e.get("ts", 0)),
            "ts": e.get("ts", 0),
            "symbol": e.get("symbol", "-"),
            "state": e.get("state", "?"),
            "quality": e.get("quality"),
            "reason": e.get("reason", ""),
        })
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


def build_status(mode: str) -> dict:
    """Full dashboard snapshot for a mode."""
    state_path, _ = mode_files(mode)
    st = _read_json_file(state_path)
    positions = [dict(pid=pid, **p)
                 for pid, p in (st.get("positions") or {}).items()
                 if not p.get("closed")]
    for p in positions:
        p["opened"] = _fmt_ts(p.get("opened_at", 0))
    balance, balance_ok = None, False
    try:
        balance = _exchange_for_mode(mode).get_balance()
        balance_ok = balance is not None
    except SystemExit:
        pass
    except Exception:  # noqa: BLE001 - dashboard must never crash on this
        pass
    return {
        "mode": mode,
        "at": _fmt_ts(int(time.time() * 1000)),
        "balance": balance,
        "balance_ok": balance_ok,
        "positions": positions,
        "n_positions": len(positions),
        "daily": st.get("daily") or {},
        "kill_switch": st.get("kill_switch") or {},
        "settings": load_settings(mode=mode),
    }


# ------------------------------------------------------------------ HTTP
PAGE = """<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;
     padding:12px;max-width:720px;margin:0 auto}
h1{font-size:20px;margin:8px 0 12px}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tabs button{flex:1;padding:10px;border:1px solid #30363d;border-radius:8px;
     background:#161b22;color:#e6edf3;font-size:15px}
.tabs button.on{background:#1f6feb;border-color:#1f6feb}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;
      padding:12px;margin-bottom:12px}
.card h2{font-size:14px;color:#8b949e;margin-bottom:8px;text-transform:uppercase}
.big{font-size:28px;font-weight:700}
.row{display:flex;justify-content:space-between;padding:6px 0;
     border-bottom:1px solid #21262d;font-size:14px}
.row:last-child{border-bottom:none}
.green{color:#3fb950}.red{color:#f85149}.yel{color:#d29922}.dim{color:#8b949e}
.ev{padding:8px 0;border-bottom:1px solid #21262d;font-size:13px}
.ev:last-child{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;
       font-weight:700;margin-right:6px}
.b-approved{background:#1a3a24;color:#3fb950}
.b-rejected{background:#3a1a1a;color:#f85149}
.b-position_opened{background:#1a2f3a;color:#58a6ff}
.b-tp1_hit,.b-tp2_hit,.b-tp3_hit{background:#2a3a1a;color:#d29922}
.b-sl_hit{background:#3a1a1a;color:#f85149}
.b-generated{background:#21262d;color:#8b949e}
.switch{position:relative;width:52px;height:28px;flex:none}
.switch input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#30363d;border-radius:14px;transition:.2s}
.sl:before{content:"";position:absolute;width:22px;height:22px;left:3px;top:3px;
           background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.sl{background:#1f6feb}
.switch input:checked+.sl:before{transform:translateX(24px)}
.setrow{display:flex;justify-content:space-between;align-items:center;
        padding:8px 0;border-bottom:1px solid #21262d}
.setrow label{font-size:14px}
.setrow input[type=number]{width:90px;padding:8px;background:#0d1117;
   border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-size:15px}
.setrow select{padding:8px;background:#0d1117;border:1px solid #30363d;
   border-radius:8px;color:#e6edf3;font-size:15px}
.veto{display:flex;justify-content:space-between;align-items:center;
      padding:7px 0;border-bottom:1px solid #21262d;font-size:13px}
.save{width:100%;padding:12px;margin-top:10px;background:#1f6feb;color:#fff;
      border:none;border-radius:8px;font-size:16px;font-weight:700}
.save:active{background:#388bfd}
#msg{text-align:center;font-size:13px;min-height:18px;margin-top:6px}
table{width:100%;font-size:13px;border-collapse:collapse}
td,th{padding:6px 4px;border-bottom:1px solid #21262d;text-align:left}
th{color:#8b949e;font-weight:600}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;
          background:#3fb950;margin-right:6px;animation:bl 2s infinite}
@keyframes bl{50%{opacity:.3}}
</style></head><body>
<h1><span class="live-dot"></span>Bot Dashboard <span id="clock" class="dim"
   style="font-size:13px;font-weight:400"></span></h1>
<div class="tabs">
  <button data-mode="live">LIVE</button>
  <button data-mode="testnet">Testnet</button>
  <button data-mode="paper">Paper</button>
</div>
<div class="card"><h2>Saldo &amp; Status</h2>
  <div class="big" id="balance">—</div>
  <div class="row"><span>Mode</span><b id="m-mode">—</b></div>
  <div class="row"><span>Posisi terbuka</span><b id="m-pos">—</b></div>
  <div class="row"><span>PnL hari ini</span><b id="m-pnl">—</b></div>
  <div class="row"><span>Kill switch</span><b id="m-ks">—</b></div>
</div>
<div class="card"><h2>📡 Signal Radar</h2>
  <div class="dim" style="font-size:13px;margin-bottom:8px">Scan seluruh
  universe dengan setting saat ini, diurutkan dari signal terkuat.
  Butuh ~30–60 detik.</div>
  <button class="save" id="scan" style="margin-top:0">Scan Signal Sekarang</button>
  <div id="radar"><span class="dim">belum di-scan</span></div>
</div>
<div class="card"><h2>🛰️ Scanner (OKX → DB)</h2>
  <div class="dim" style="font-size:13px;margin-bottom:8px">scanner.py
  (tahap 1) scan SEMUA coin OKX lalu tulis signal bagus ke signals.db;
  executor --from-db (tahap 2) eksekusi yang fresh dari DB.</div>
  <div id="scanner"><span class="dim">memuat…</span></div>
</div>
<div class="card"><h2>Auto Trade</h2>
  <div class="setrow"><label><b>Auto-trade aktif</b><br>
    <span class="dim" style="font-size:12px">OFF = scan tetap jalan tapi
    tidak buka posisi baru</span></label>
    <label class="switch"><input type="checkbox" id="s-auto">
    <span class="sl"></span></label></div>
  <div class="setrow"><label>Risiko per trade (%)<br>
    <span class="dim" style="font-size:12px">maks 1.0</span></label>
    <input type="number" id="s-risk" step="0.1" min="0.1" max="1.0"></div>
  <div class="setrow"><label>Max leverage<br>
    <span class="dim" style="font-size:12px">maks 3</span></label>
    <input type="number" id="s-lev" step="1" min="1" max="3"></div>
  <div class="setrow"><label>Interval scan (menit)</label>
    <input type="number" id="s-int" step="1" min="1" max="60"></div>
</div>
<div class="card"><h2>🌐 Universe</h2>
  <div class="setrow"><label>Mode universe<br>
    <span class="dim" style="font-size:12px">curated = 55 coin liquid;
    top_volume = top-N volume; top_movers = top-N volatilitas (GAS);
    all = semua perpetual OKX</span></label>
    <select id="s-umode">
      <option value="curated">curated</option>
      <option value="top_volume">top_volume</option>
      <option value="top_movers">top_movers</option>
      <option value="all">all</option>
    </select></div>
  <div class="setrow"><label>Top N (10–100)<br>
    <span class="dim" style="font-size:12px">dipakai kalau top_volume / top_movers</span></label>
    <input type="number" id="s-topn" step="5" min="10" max="100"></div>
  <div class="dim" style="font-size:12px" id="u-count"></div>
</div>
<div class="card"><h2>🎯 Signal — Quality &amp; Filter</h2>
  <div class="setrow"><label><b>Mode sinyal</b><br>
    <span class="dim" style="font-size:12px">preset: ketat / sedang / biasa</span></label>
    <select id="s-mode">
      <option value="ketat">KETAT — GAS STRICT</option>
      <option value="sedang">SEDANG — lebih longgar</option>
      <option value="biasa">BIASA — paling longgar</option>
    </select></div>
  <div class="setrow"><label>Min quality score<br>
    <span class="dim" style="font-size:12px">STRICT default 70</span></label>
    <input type="number" id="s-minq" step="1" min="50" max="95"></div>
  <div class="setrow"><label>MTF 30m mode<br>
    <span class="dim" style="font-size:12px">STRICT = full bullish watch</span></label>
    <select id="s-mtf">
      <option value="STRICT">STRICT</option>
      <option value="LEAN">LEAN</option>
    </select></div>
  <div class="setrow"><label><b>MTF: 1d wajib selaras</b><br>
    <span class="dim" style="font-size:12px">OFF = 1d jadi penalti skor, bukan veto (mode biasa)</span></label>
    <label class="switch"><input type="checkbox" id="s-req1d">
    <span class="sl"></span></label></div>
  <div class="setrow"><label><b>Bullish only</b><br>
    <span class="dim" style="font-size:12px">OFF = izinkan juga signal SHORT</span></label>
    <label class="switch"><input type="checkbox" id="s-bull">
    <span class="sl"></span></label></div>
  <div class="setrow"><label>Min volume 24h (USD)</label>
    <input type="number" id="s-vol" step="100000" min="500000" max="10000000"></div>
  <div class="setrow"><label>Max freshness drift (%)<br>
    <span class="dim" style="font-size:12px">batal entry kalau harga lari</span></label>
    <input type="number" id="s-fresh" step="0.1" min="0.1" max="2.0"></div>
</div>
<div class="card"><h2>🛡️ TP &amp; SL</h2>
  <div class="setrow"><label>TP1 (x risk)</label>
    <input type="number" id="s-tp1" step="0.25" min="0.5" max="5"></div>
  <div class="setrow"><label>TP2 (x risk)</label>
    <input type="number" id="s-tp2" step="0.25" min="1" max="8"></div>
  <div class="setrow"><label>TP3 (x risk)</label>
    <input type="number" id="s-tp3" step="0.25" min="1.5" max="12"></div>
  <div class="setrow"><label>Stop ATR min</label>
    <input type="number" id="s-satrmin" step="0.25" min="0.5" max="3"></div>
  <div class="setrow"><label>Stop ATR max</label>
    <input type="number" id="s-satrmax" step="0.25" min="1" max="5"></div>
  <div class="setrow"><label>Split TP1/TP2/TP3<br>
    <span class="dim" style="font-size:12px">fraksi, dinormalisasi ke 1.0</span></label>
    <span><input type="number" id="s-sp1" step="0.1" min="0.01" max="10" style="width:64px">
    <input type="number" id="s-sp2" step="0.1" min="0.01" max="10" style="width:64px">
    <input type="number" id="s-sp3" step="0.1" min="0.01" max="10" style="width:64px"></span></div>
  <div class="setrow"><label><b>Breakeven setelah TP1</b><br>
    <span class="dim" style="font-size:12px">geser SL ke entry</span></label>
    <label class="switch"><input type="checkbox" id="s-be">
    <span class="sl"></span></label></div>
</div>
<div class="card"><h2>⚙️ Order</h2>
  <div class="setrow"><label>Margin mode</label>
    <select id="s-margin">
      <option value="ISOLATED">ISOLATED</option>
      <option value="CROSS">CROSS</option>
    </select></div>
  <div class="setrow"><label>Leverage mode<br>
    <span class="dim" style="font-size:12px">auto = ikut max leverage di atas</span></label>
    <select id="s-levmode">
      <option value="auto">auto</option>
      <option value="fixed">fixed</option>
    </select></div>
  <div class="setrow"><label>Leverage (1–125)<br>
    <span class="dim" style="font-size:12px">dipakai kalau mode fixed</span></label>
    <input type="number" id="s-levx" step="1" min="1" max="125"></div>
  <div class="setrow"><label>Entry mode<br>
    <span class="dim" style="font-size:12px">risk = ikut risiko %; fixed = nominal tetap</span></label>
    <select id="s-entrymode">
      <option value="risk">risk %</option>
      <option value="fixed_usdt">fixed USDT</option>
    </select></div>
  <div class="setrow"><label>Fixed USDT / posisi</label>
    <input type="number" id="s-fixedu" step="1" min="1" max="100000"></div>
  <div class="dim" style="font-size:12px" id="order-warn"></div>
</div>
<div class="card"><h2>⛔ Vetoes <span class="dim" style="font-size:12px;text-transform:none">
  (matikan = risiko sendiri)</span></h2>
  <div id="vetoes"></div>
  <button class="save" id="save">💾 Simpan Semua Setting</button>
  <div id="msg"></div>
</div>
<div class="card"><h2>Posisi Terbuka</h2><div id="positions">
  <span class="dim">—</span></div></div>
<div class="card"><h2>Signal &amp; Event Terakhir</h2><div id="events">
  <span class="dim">memuat…</span></div></div>
<script>
let mode = new URLSearchParams(location.search).get('mode') || 'live';
const $ = id => document.getElementById(id);
function setTab(){
  document.querySelectorAll('.tabs button').forEach(b=>{
    b.classList.toggle('on', b.dataset.mode===mode);
    b.onclick=()=>{mode=b.dataset.mode;
      history.replaceState(null,'','?mode='+mode); refresh();};
  });
}
async function refresh(){
  setTab();
  try{ refreshScanner(); }catch(e){}
  try{
    const s = await (await fetch('/api/status?mode='+mode)).json();
    $('m-mode').textContent = s.mode.toUpperCase() +
      (s.mode==='live' ? ' (REAL MONEY)' : '');
    $('m-mode').className = s.mode==='live' ? 'red' : '';
    $('balance').textContent = s.balance_ok
      ? s.balance.toFixed(2)+' USDT' : 'tidak tersedia';
    $('m-pos').textContent = s.n_positions;
    const d = s.daily || {};
    const pnl = d.realized_pnl || 0;
    $('m-pnl').innerHTML = '<span class="'+(pnl>=0?'green':'red')+'">'+
      (pnl>=0?'+':'')+pnl.toFixed(2)+' USDT</span>';
    const ks = s.kill_switch || {};
    $('m-ks').innerHTML = ks.active
      ? '<span class="red">AKTIF — '+(ks.reason||'')+'</span>'
      : '<span class="green">nonaktif</span>';
    const st = s.settings || {};
    $('s-auto').checked = !!st.auto_trade;
    $('s-risk').value = st.risk_pct_per_trade;
    $('s-lev').value = st.max_leverage;
    $('s-int').value = st.poll_interval_min;
    $('s-minq').value = st.min_quality_score;
    $('s-mode').value = st.signal_mode || 'ketat';    $('s-mtf').value = st.mtf_30m_mode;
    $('s-req1d').checked = st.mtf_require_1d !== false;
    $('s-bull').checked = !!st.bullish_only;
    $('s-vol').value = st.min_24h_volume_usd;
    $('s-fresh').value = st.freshness_max_drift_pct;
    $('s-tp1').value = st.tp1_rr; $('s-tp2').value = st.tp2_rr;
    $('s-tp3').value = st.tp3_rr;
    $('s-satrmin').value = st.stop_atr_min;
    $('s-satrmax').value = st.stop_atr_max;
    $('s-sp1').value = st.tp_split_1; $('s-sp2').value = st.tp_split_2;
    $('s-sp3').value = st.tp_split_3;
    $('s-be').checked = !!st.breakeven_after_tp1;
    $('s-margin').value = st.margin_mode || 'ISOLATED';
    $('s-umode').value = st.universe_mode || 'top_movers';
    $('s-topn').value = st.universe_top_n;
    $('u-count').innerHTML = st.universe_mode === 'all'
      ? 'scan <b class="cyan">semua</b> perpetual OKX (~ratusan coin)'
      : st.universe_mode === 'top_volume' || st.universe_mode === 'top_movers'
      ? 'scan <b class="cyan">' + st.universe_top_n + '</b> coin ' + st.universe_mode
      : 'scan <b class="cyan">55</b> coin curated';
    $('s-levmode').value = st.leverage_mode || 'auto';
    $('s-levx').value = st.leverage;
    $('s-entrymode').value = st.entry_mode || 'risk';
    $('s-fixedu').value = st.fixed_usdt;
    const warns=[];
    if(st.margin_mode==='CROSS') warns.push('margin CROSS');
    if(st.leverage_mode==='fixed'&&st.leverage>3)
      warns.push('leverage '+st.leverage+'x');
    if(st.entry_mode==='fixed_usdt')
      warns.push('entry fixed '+st.fixed_usdt+' USDT');
    $('order-warn').innerHTML = warns.length
      ? '⚠️ di luar default aman: <b class="yel">'+warns.join(', ')+'</b>'
      : '<span class="dim">order config = default aman</span>';
    renderVetoes(st);
    const pos = $('positions');
    if(!s.positions.length){ pos.innerHTML='<span class="dim">tidak ada posisi</span>'; }
    else{
      let h='<table><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>SL</th><th>TP</th></tr>';
      for(const p of s.positions){
        h+='<tr><td><b>'+p.symbol+'</b> '+(p.leverage||'')+'x</td><td>'+
          p.qty+'</td><td>'+p.entry_price+'</td><td>'+p.stop_loss+'</td><td>'+
          [p.tp1,p.tp2,p.tp3].filter(x=>x).join(' / ')+'</td></tr>';
      }
      pos.innerHTML=h+'</table>';
    }
    const ev = await (await fetch('/api/events?mode='+mode+'&limit=40')).json();
    const box=$('events');
    if(!ev.length){ box.innerHTML='<span class="dim">belum ada event</span>'; }
    else{ box.innerHTML=ev.map(e=>{
      const q = e.quality!=null ? ' <span class="dim">Q'+e.quality+'</span>' : '';
      const r = e.reason ? ' <span class="dim">— '+e.reason+'</span>' : '';
      return '<div class="ev"><span class="badge b-'+e.state+'">'+e.state+
        '</span><b>'+e.symbol+'</b>'+q+'<br><span class="dim">'+e.time+r+
        '</span></div>';
    }).join(''); }
  }catch(err){ /* keep old data on transient failure */ }
  const d=new Date();
  $('clock').textContent='update '+d.toLocaleTimeString('id-ID');
}
const VETOES=[
 ['veto_leverage_risk','Leverage Risk','crowded trade / flush risk'],
 ['veto_funding','Funding Extreme','funding ekstrem berlawanan arah'],
 ['veto_squeeze','Squeeze Risk','risiko stop-hunt'],
 ['veto_whale','Whale Divergence','top trader berlawanan arah'],
 ['veto_liq_wall','Liquidity Wall','tembok order block arah'],
 ['veto_btc_breaker','BTC Breaker','BTC dump -3%/1h'],
];
function renderVetoes(st){
  $('vetoes').innerHTML=VETOES.map(v=>
    '<div class="veto"><span><b>'+v[1]+'</b><br><span class="dim" style="font-size:11px">'+
    v[2]+'</span></span><label class="switch"><input type="checkbox" data-veto="'+
    v[0]+'"'+(st[v[0]]?' checked':'')+'><span class="sl"></span></label></div>'
  ).join('');
  document.querySelectorAll('[data-veto]').forEach(el=>{
    el.onchange=()=>postSettings({[el.dataset.veto]:el.checked}, true);
  });
}
async function postSettings(body, quiet){
  const r = await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json();
  const ok=j.ok;
  $('msg').textContent = ok ? '✓ tersimpan' : '✗ '+(j.error||'gagal');
  $('msg').className = ok ? 'green' : 'red';
  if(!quiet||!ok) setTimeout(()=>$('msg').textContent='',2500);
  if(ok) refresh();
  return ok;
}
$('s-auto').onchange = ()=>postSettings({auto_trade:$('s-auto').checked}, true);
$('s-bull').onchange = ()=>postSettings({bullish_only:$('s-bull').checked}, true);
$('s-req1d').onchange = ()=>postSettings({mtf_require_1d:$('s-req1d').checked}, true);
$('s-be').onchange = ()=>postSettings({breakeven_after_tp1:$('s-be').checked}, true);
$('save').onclick = ()=>{
  postSettings({
    auto_trade:$('s-auto').checked,
    risk_pct_per_trade:parseFloat($('s-risk').value),
    max_leverage:parseInt($('s-lev').value),
    poll_interval_min:parseInt($('s-int').value),
    min_quality_score:parseInt($('s-minq').value),
    mtf_30m_mode:$('s-mtf').value,
    mtf_require_1d:$('s-req1d').checked,
    bullish_only:$('s-bull').checked,
    min_24h_volume_usd:parseFloat($('s-vol').value),
    freshness_max_drift_pct:parseFloat($('s-fresh').value),
    tp1_rr:parseFloat($('s-tp1').value),
    tp2_rr:parseFloat($('s-tp2').value),
    tp3_rr:parseFloat($('s-tp3').value),
    stop_atr_min:parseFloat($('s-satrmin').value),
    stop_atr_max:parseFloat($('s-satrmax').value),
    tp_split_1:parseFloat($('s-sp1').value),
    tp_split_2:parseFloat($('s-sp2').value),
    tp_split_3:parseFloat($('s-sp3').value),
    breakeven_after_tp1:$('s-be').checked,
    margin_mode:$('s-margin').value,
    universe_mode:$('s-umode').value,
    universe_top_n:parseInt($('s-topn').value),
    leverage_mode:$('s-levmode').value,
    leverage:parseInt($('s-levx').value),
    entry_mode:$('s-entrymode').value,
    fixed_usdt:parseFloat($('s-fixedu').value),
  });
};
async function refreshScanner(){
  const el = $('scanner');
  try{
    const r = await (await fetch('/api/scanner')).json();
    const st = r.state, db = r.db || {};
    let h = '';
    if(st){
      const ago = st.at ? Math.round((Date.now()-st.at)/60000) : '?';
      h += '<div class="row"><span>Pass terakhir</span><b>'+ago+' mnt lalu</b></div>'+
        '<div class="row"><span>Coin di-scan</span><b>'+st.symbols+'</b></div>'+
        '<div class="row"><span>Approved / error</span><b class="green">'+
        st.approved+'</b> / <b class="red">'+st.errors+'</b></div>';
    }else{
      h += '<div class="dim">scanner belum pernah jalan</div>';
    }
    h += '<div class="row"><span>Signal fresh di DB</span><b>'+
      (db.fresh||0)+'</b></div>';
    const sigs = r.signals || [];
    if(sigs.length){
      h += '<table><tr><th>Symbol</th><th>Dir</th><th>Q</th>'+
        '<th>Entry</th><th>Umur</th></tr>'+
        sigs.map(x=>'<tr><td><b>'+x.symbol+'</b></td><td>'+
          x.direction+'</td><td><b class="green">'+x.quality+'</b></td><td>'+
          (+x.entry).toFixed(4)+'</td><td class="dim">'+x.age_min+'m</td></tr>'
        ).join('')+'</table>';
    }
    el.innerHTML = h;
  }catch(e){ el.innerHTML='<span class="red">✗</span>'; }
}
$('s-mode').onchange = async ()=>{
  const v = $('s-mode').value;
  if(!confirm('Ganti mode sinyal ke '+v.toUpperCase()+'? Preset akan menimpa knob signal & veto.')){ refresh(); return; }
  const r = await (await fetch('/api/settings?mode='+mode,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({signal_mode:v})})).json();
  if(r.error){ alert('gagal: '+r.error); } else { refresh(); }
};
$('scan').onclick = async ()=>{  $('scan').disabled=true; $('scan').textContent='Scanning…';
  $('radar').innerHTML='<span class="dim">menghubungi exchange…</span>';
  try{
    const r = await (await fetch('/api/radar?mode='+mode)).json();
    if(r.error){ $('radar').innerHTML='<span class="red">✗ '+r.error+'</span>'; }
    else if(!r.length){ $('radar').innerHTML='<span class="dim">kosong</span>'; }
    else{
      $('radar').innerHTML='<table><tr><th>#</th><th>Symbol</th><th>Q</th><th>Entry</th><th>SL</th><th>TP1–3</th></tr>'+
      r.map((x,i)=>{
        if(x.state!=='approved')
          return '<tr><td>'+(i+1)+'</td><td><b>'+x.symbol+'</b></td>'+
            '<td colspan="4"><span class="dim" style="font-size:11px">'+
            (x.reason||x.state).slice(0,90)+'</span></td></tr>';
        return '<tr><td>'+(i+1)+'</td><td><b>'+x.symbol+'</b><br>'+
          '<span class="dim" style="font-size:11px">'+x.regime+'</span></td>'+
          '<td><b class="green">'+x.quality+'</b></td>'+
          '<td>'+(+x.entry).toFixed(4)+'</td><td class="red">'+(+x.stop_loss).toFixed(4)+'</td>'+
          '<td class="yel" style="font-size:11px">'+(+x.tp1).toFixed(4)+'<br>'+
          (+x.tp2).toFixed(4)+'<br>'+(+x.tp3).toFixed(4)+'</td></tr>';
      }).join('')+'</table>';
    }
  }catch(e){ $('radar').innerHTML='<span class="red">✗ gagal: '+e+'</span>'; }
  $('scan').disabled=false; $('scan').textContent='Scan Signal Sekarang';
};
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BotDash/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _mode(self) -> str:
        q = parse_qs(urlparse(self.path).query)
        m = (q.get("mode") or ["live"])[0]
        return m if m in ("live", "testnet", "paper") else "live"

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                self._json(200, build_status(self._mode()))
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        elif path == "/api/events":
            q = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(int((q.get("limit") or ["60"])[0]), 200))
            except (TypeError, ValueError):
                limit = 60
            self._json(200, read_events(self._mode(), limit))
        elif path == "/api/settings":
            self._json(200, load_settings(mode=self._mode()))
        elif path == "/api/radar":
            try:
                self._json(200, run_radar(mode=self._mode()))
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        elif path == "/api/scanner":
            try:
                self._json(200, scanner_status())
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/api/settings":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0 or n > 4096:
            self._json(400, {"error": "empty or oversized body"})
            return
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid JSON"})
            return
        # Merge over the mode's current settings so partial updates (e.g.
        # just the auto-trade toggle) don't reset the other knobs.
        mode = self._mode()
        merged = load_settings(mode=mode)
        incoming = data if isinstance(data, dict) else {}
        # signal_mode is a preset: expand it into the individual knobs
        # before validation so the bundle always lands atomically.
        if ("signal_mode" in incoming
                and incoming["signal_mode"] != merged.get("signal_mode")):
            try:
                apply_signal_mode_preset(merged, incoming["signal_mode"])
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            incoming = {k: v for k, v in incoming.items()
                        if k != "signal_mode"}
        merged.update(incoming)
        try:
            cleaned = save_settings(merged, path=SETTINGS_PATH, mode=mode)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        except OSError as e:
            self._json(500, {"error": f"save failed: {e}"})
            return
        self._json(200, {"ok": True, "settings": cleaned})

    def log_message(self, fmt, *args):  # noqa: N802 - quieter logs
        sys.stderr.write("dash: " + fmt % args + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bot dashboard web UI")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default localhost only)")
    _env_mode = (os.environ.get("BOT_MODE") or "").strip().lower()
    if _env_mode == "demo":
        _env_mode = "testnet"
    if _env_mode not in ("live", "testnet", "paper"):
        _env_mode = "live"
    ap.add_argument("--mode", default=_env_mode,
                    choices=["live", "testnet", "paper"],
                    help="default mode shown on load (default: $BOT_MODE)")
    args = ap.parse_args(argv)
    # Rewrite the default mode into the served page.
    global PAGE
    PAGE = PAGE.replace("|| 'live'", f"|| '{args.mode}'")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}/ (mode={args.mode})",
          flush=True)
    print("keys are read from the environment; nothing secret leaves "
          "this host", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
