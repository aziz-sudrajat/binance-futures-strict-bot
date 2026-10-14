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

DEFAULTS = {
    "auto_trade": True,
    "risk_pct_per_trade": 1.0,
    "max_leverage": 3,
    "poll_interval_min": 5,
}


def load_settings(path: str = SETTINGS_PATH) -> dict:
    """Current settings merged over defaults. Missing/corrupt -> defaults."""
    s = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f) or {}
        for k in s:
            if k in loaded:
                s[k] = loaded[k]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return s


def validate_settings(data: dict) -> dict:
    """Clean + clamp user settings. Raises ValueError on bad input.

    The dashboard may only TIGHTEN risk, never exceed the code defaults.
    """
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    out = dict(DEFAULTS)

    if "auto_trade" in data:
        v = data["auto_trade"]
        if not isinstance(v, bool):
            raise ValueError("auto_trade must be true/false")
        out["auto_trade"] = v

    if "risk_pct_per_trade" in data:
        try:
            v = float(data["risk_pct_per_trade"])
        except (TypeError, ValueError):
            raise ValueError("risk_pct_per_trade must be a number")
        if not 0.1 <= v <= config.RISK_PCT_PER_TRADE:
            raise ValueError(
                f"risk_pct_per_trade must be 0.1-{config.RISK_PCT_PER_TRADE}")
        out["risk_pct_per_trade"] = v

    if "max_leverage" in data:
        try:
            v = int(data["max_leverage"])
        except (TypeError, ValueError):
            raise ValueError("max_leverage must be an integer")
        if not 1 <= v <= config.MAX_LEVERAGE:
            raise ValueError(
                f"max_leverage must be 1-{config.MAX_LEVERAGE}")
        out["max_leverage"] = v

    if "poll_interval_min" in data:
        try:
            v = int(data["poll_interval_min"])
        except (TypeError, ValueError):
            raise ValueError("poll_interval_min must be an integer")
        if not 1 <= v <= 60:
            raise ValueError("poll_interval_min must be 1-60")
        out["poll_interval_min"] = v

    return out


def save_settings(data: dict, path: str = SETTINGS_PATH) -> dict:
    """Validate + atomically persist settings. Returns the cleaned dict."""
    cleaned = validate_settings(data)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
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
        "settings": load_settings(),
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
  <button class="save" id="save">Simpan Setting</button>
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
$('s-auto').onchange = async ()=>{
  const r = await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({auto_trade:$('s-auto').checked})});
  const j = await r.json();
  $('msg').textContent = j.ok ? '✓ auto-trade '+(j.settings.auto_trade?'ON':'OFF')
                              : '✗ '+j.error;
  $('msg').className = j.ok ? 'green' : 'red';
  setTimeout(()=>$('msg').textContent='',2500);
};
$('save').onclick = async ()=>{
  const body={auto_trade:$('s-auto').checked,
    risk_pct_per_trade:parseFloat($('s-risk').value),
    max_leverage:parseInt($('s-lev').value),
    poll_interval_min:parseInt($('s-int').value)};
  const r = await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json();
  $('msg').textContent = j.ok ? '✓ setting tersimpan' : '✗ '+j.error;
  $('msg').className = j.ok ? 'green' : 'red';
  setTimeout(()=>$('msg').textContent='',2500);
  refresh();
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
            self._json(200, load_settings())
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
        # Merge over current settings so partial updates (e.g. just the
        # auto-trade toggle) don't reset the other knobs.
        merged = load_settings()
        merged.update(data if isinstance(data, dict) else {})
        try:
            cleaned = validate_settings(merged)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        tmp = SETTINGS_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, indent=2)
            os.replace(tmp, SETTINGS_PATH)
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
    ap.add_argument("--mode", default="live",
                    choices=["live", "testnet", "paper"],
                    help="default mode shown on load")
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
