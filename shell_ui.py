# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Framework core for the `binance` terminal shell.

This module holds everything that makes the shell feel like a framework
rather than a script:
  - a command registry with metadata (name, usage, description, aliases)
  - shlex-based parsing, per-command help, "did you mean?" suggestions
  - rich rendering: banner, status panel, and tables for positions,
    signals, scan results, events and settings
  - settings value coercion shared by `set` and the guided editor

Pure functions only (no I/O except through an explicit Console) so the
whole framework surface is unit-testable.
"""

from __future__ import annotations

import difflib
import shlex
from dataclasses import dataclass, field

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ------------------------------------------------------- command framework
@dataclass
class Command:
    name: str
    description: str
    usage: str = ""
    aliases: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


COMMANDS: list[Command] = [
    Command("scan", "Scan sinyal dengan engine STRICT (Signal Radar)",
            usage="scan [all] [--top N]",
            examples=("scan", "scan all", "scan --top 10")),
    Command("cari", "Cari coin: analisis 15m/30m/4h/1d + alasan lolos/gagal",
            usage="cari <nama coin>",
            aliases=("search", "cek"),
            examples=("cari btc", "cari WAL")),
    Command("entry", "Entry manual coin (konfirmasi dulu): long / short",
            usage="entry <nama coin> <long|short>",
            aliases=("masuk",),
            examples=("entry btc long", "entry eth short")),
    Command("auto-trade", "Jalankan auto-trade loop (Ctrl+C berhenti)",
            usage="auto-trade [--demo|--live|--paper] [--from-db] [--setting]",
            aliases=("autotrade", "trade"),
            examples=("auto-trade", "auto-trade --live",
                      "auto-trade --demo --from-db", "auto-trade --setting")),
    Command("setting", "Editor setting studio (interaktif)",
            usage="setting",
            examples=("setting",)),
    Command("signal-mode", "Ganti mode sinyal: ketat | sedang | biasa",
            usage="signal-mode [ketat|sedang|biasa]",
            aliases=("sigmode",),
            examples=("signal-mode", "signal-mode sedang")),
    Command("set", "Ubah satu setting langsung",
            usage="set <key> <value>",
            examples=("set min_quality_score 75", "set auto_trade off")),
    Command("position", "Posisi terbuka + ringkasan",
            usage="position", aliases=("positions", "pos"),
            examples=("position",)),
    Command("balance", "Saldo + PnL harian",
            usage="balance", aliases=("bal",),
            examples=("balance",)),
    Command("signals", "Signal fresh dari database scanner (tahap 1)",
            usage="signals", aliases=("sigs",),
            examples=("signals",)),
    Command("scanner", "Kontrol all-coin scanner (OKX → DB)",
            usage="scanner <start|stop|status|once>",
            examples=("scanner status", "scanner start")),
    Command("mode", "Ganti mode / lihat mode aktif",
            usage="mode [live|demo|paper]",
            examples=("mode", "mode live")),
    Command("events", "Event lifecycle terakhir",
            usage="events [--limit N]",
            examples=("events", "events --limit 10")),
    Command("kill", "Status kill-switch",
            usage="kill",
            examples=("kill",)),
    Command("dashboard", "Info menjalankan web dashboard",
            usage="dashboard",
            examples=("dashboard",)),
    Command("clear", "Bersihkan layar", usage="clear"),
    Command("help", "Bantuan perintah",
            usage="help [command]", aliases=("?",),
            examples=("help", "help scan")),
    Command("exit", "Keluar dari shell",
            usage="exit", aliases=("quit", "q")),
]

_BY_NAME: dict[str, Command] = {}
for _c in COMMANDS:
    _BY_NAME[_c.name] = _c
    for _a in _c.aliases:
        _BY_NAME[_a] = _c


def lookup_command(name: str) -> Command | None:
    return _BY_NAME.get(name)


def suggest_command(name: str, n: int = 3) -> list[str]:
    names = [c.name for c in COMMANDS]
    return difflib.get_close_matches(name, names, n=n, cutoff=0.55)


def parse_line(line: str) -> tuple[str, list[str]]:
    """Split a shell line into (command, args) with shlex.

    A leading ':' is tolerated (vim-style ':command' habit).
    """
    line = line.strip()
    if line.startswith(":"):
        line = line[1:].lstrip()
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def help_table(commands: list[Command] | None = None) -> Table:
    t = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    t.add_column("command", style="bold cyan", no_wrap=True)
    t.add_column("description")
    for c in commands or COMMANDS:
        t.add_row(c.name, c.description)
    return t


def command_help(cmd: Command) -> Panel:
    body = [f"[bold]Usage:[/bold] {cmd.usage or cmd.name}"]
    if cmd.aliases:
        body.append(f"[bold]Aliases:[/bold] {', '.join(cmd.aliases)}")
    if cmd.examples:
        body.append("[bold]Examples:[/bold]")
        body.extend(f"  [cyan]{e}[/cyan]" for e in cmd.examples)
    return Panel("\n".join(body), title=f"help: {cmd.name}",
                 border_style="cyan")


# ------------------------------------------------------- rendering
def banner() -> Panel:
    text = Text()
    text.append("Binance Futures Studio\n", style="bold cyan")
    text.append("terminal interaktif — ketik ", style="dim")
    text.append("help", style="bold yellow")
    text.append(" untuk daftar perintah", style="dim")
    return Panel(text, border_style="cyan", padding=(1, 2))


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "?"


def status_panel(status: dict, mode: str) -> Panel:
    bal = status.get("balance")
    bal_ok = status.get("balance_ok")
    daily = status.get("daily") or {}
    pnl = daily.get("realized_pnl")
    ks = status.get("kill_switch") or {}
    ks_active = bool(ks.get("tripped") or ks.get("active"))
    sett = status.get("settings") or {}

    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="dim")
    grid.add_column()
    bal_s = f"{_money(bal)} USDT" if bal_ok else "[red]tidak tersedia[/red]"
    grid.add_row("Mode", f"[bold]{mode.upper()}[/bold]")
    grid.add_row("Saldo", bal_s)
    pnl_color = "green" if (pnl or 0) >= 0 else "red"
    pnl_s = ("?" if pnl is None
             else f"[{pnl_color}]{pnl:+.2f} USDT[/{pnl_color}]")
    grid.add_row("PnL hari ini", pnl_s)
    grid.add_row("Posisi terbuka", str(status.get("n_positions", 0)))
    grid.add_row("Kill switch",
                 "[bold red]AKTIF[/bold red]" if ks_active
                 else "[green]nonaktif[/green]")
    grid.add_row("Auto-trade",
                 "[green]ON[/green]" if sett.get("auto_trade")
                 else "[yellow]OFF[/yellow]")
    return Panel(grid, title="Status", border_style="cyan")


def positions_table(status: dict) -> Table:
    t = Table(title="Posisi Terbuka", show_lines=False)
    for h, j in [("Symbol", "left"), ("Side", "center"), ("Qty", "right"),
                 ("Entry", "right"), ("Mark", "right"), ("uPnL", "right"),
                 ("Lev", "center"), ("SL", "right")]:
        t.add_column(h, justify=j)
    positions = status.get("positions") or []
    if not positions:
        t.add_row("[dim]tidak ada posisi terbuka[/dim]", "", "", "", "",
                  "", "", "")
        return t
    for p in positions:
        # executor state keys: direction (LONG/SHORT), entry_price, qty...
        side = str(p.get("direction", p.get("side", "?"))).upper()
        upnl = p.get("unrealized_pnl", p.get("upnl"))
        mark = p.get("mark", p.get("mark_price"))
        t.add_row(
            f"[bold]{p.get('symbol', '?')}[/bold]",
            (f"[green]{side}[/green]" if side == "LONG"
              else f"[red]{side}[/red]"),
            _money(p.get("qty")),
            _money(p.get("entry_price", p.get("entry"))),
            _money(mark) if mark else "[dim]—[/dim]",
            ("[dim]—[/dim]" if upnl is None
             else (f"[green]{float(upnl):+.2f}[/green]"
                   if float(upnl) >= 0
                   else f"[red]{float(upnl):+.2f}[/red]")),
            f"{p.get('leverage', '?')}x",
            _money(p.get("stop_loss")),
        )
    return t


def radar_table(rows: list[dict], limit: int = 20) -> Table:
    t = Table(title="Signal Radar — ranking by quality")
    for h, j in [("#", "right"), ("Symbol", "left"), ("Dir", "center"),
                 ("Q", "right"), ("Entry", "right"), ("SL", "right"),
                 ("TP1–3", "left")]:
        t.add_column(h, justify=j)
    shown = 0
    for i, r in enumerate(rows):
        if r.get("state") != "approved":
            continue
        shown += 1
        if shown > limit:
            break
        t.add_row(
            str(shown),
            f"[bold]{r.get('symbol')}[/bold]",
            (f"[green]{r.get('direction')}[/green]"
             if r.get('direction') == 'LONG'
             else f"[red]{r.get('direction')}[/red]"),
            f"[bold green]{r.get('quality')}[/bold green]",
            _money(r.get("entry")),
            f"[red]{_money(r.get('stop_loss'))}[/red]",
            f"{_money(r.get('tp1'))} / {_money(r.get('tp2'))} / "
            f"{_money(r.get('tp3'))}",
        )
    if shown == 0:
        t.add_row("", "[dim]tidak ada signal approved[/dim]",
                  "", "", "", "", "")
    return t


def _rejection_category(reason: str) -> str:
    r = (reason or "").lower()
    if "bullish-only" in r:
        return "15m bearish/netral (mode long-only)"
    if "no directional setup" in r or "tanpa setup" in r:
        return "15m netral (tanpa setup)"
    if "cascade" in r or "4h trend" in r or "1d trend" in r or "30m" in r:
        return "cascade MTF tak selaras"
    if "volume" in r:
        return "volume 24h di bawah minimum"
    if r.startswith("veto"):
        return "kena veto"
    if "derivat" in r:
        return "data derivatif tak lengkap"
    if "freshness" in r or "drift" in r:
        return "harga tak fresh"
    if "quality" in r or "skor" in r:
        return "skor quality di bawah minimum"
    return "lainnya / data error"


def rejection_breakdown(rows: list[dict]) -> Table:
    """Kenapa coin-coin ditolak — biar '0 sinyal' ada penjelasannya."""
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for r in rows:
        if r.get("state") == "approved":
            continue
        cat = _rejection_category(str(r.get("reason") or ""))
        counts[cat] = counts.get(cat, 0) + 1
        if cat not in examples:
            examples[cat] = f"{r.get('symbol')}: {str(r.get('reason') or '')[:70]}"
    t = Table(title="Kenapa ditolak", show_header=True)
    t.add_column("Alasan", justify="left")
    t.add_column("Coin", justify="right")
    t.add_column("Contoh", justify="left")
    for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        t.add_row(cat, str(n), f"[dim]{examples[cat]}[/dim]")
    if not counts:
        t.add_row("[dim]tidak ada penolakan[/dim]", "", "")
    return t


def signals_table(rows: list[dict]) -> Table:
    t = Table(title="Signal DB — scanner OKX → signals.db")
    for h, j in [("Symbol", "left"), ("Dir", "center"), ("Q", "right"),
                 ("Entry", "right"), ("Umur", "right")]:
        t.add_column(h, justify=j)
    if not rows:
        t.add_row("[dim]belum ada signal fresh di DB[/dim]", "", "", "", "")
        return t
    for r in rows:
        t.add_row(
            f"[bold]{r.get('symbol')}[/bold]",
            (f"[green]{r.get('direction')}[/green]"
             if r.get('direction') == 'LONG'
             else f"[red]{r.get('direction')}[/red]"),
            f"[bold green]{r.get('quality')}[/bold green]",
            _money(r.get("entry")),
            f"{r.get('age_min', '?')}m",
        )
    return t


def events_table(events: list[dict]) -> Table:
    t = Table(title="Event Terakhir")
    for h, j in [("Waktu", "left"), ("Symbol", "left"), ("State", "center"),
                 ("Q", "right"), ("Info", "left")]:
        t.add_column(h, justify=j)
    if not events:
        t.add_row("[dim]belum ada event[/dim]", "", "", "", "")
        return t
    colors = {"generated": "dim", "approved": "green", "rejected": "yellow",
              "opened": "bold green", "closed": "cyan"}
    for e in events:
        state = str(e.get("state", "?"))
        q = e.get("quality")
        t.add_row(
            str(e.get("time", "-")),
            f"[bold]{e.get('symbol', '-')}[/bold]",
            (f"[{colors.get(state, '')}]{state}[/{colors.get(state, '')}]"
             if colors.get(state, '') else state),
            "" if q is None else f"{q}",
            escape(str(e.get("reason", ""))[:60]),
        )
    return t


def settings_table(settings: dict,
                   fields: list[tuple[str, str, str]]) -> Table:
    t = Table(title="Setting Studio")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Key", style="cyan", no_wrap=True)
    t.add_column("Label")
    t.add_column("Value", justify="right", style="bold")
    for i, (key, label, _kind) in enumerate(fields, 1):
        v = settings.get(key)
        t.add_row(str(i), key, label,
                  "[green]on[/green]" if v is True
                  else "[red]off[/red]" if v is False
                  else str(v))
    return t


# ------------------------------------------------------- settings coercion
def coerce_setting(kind: str, text: str):
    """Parse a raw string into a setting value. Raises ValueError."""
    text = text.strip()
    if kind == "bool":
        low = text.lower()
        if low in ("1", "true", "on", "ya", "y"):
            return True
        if low in ("0", "false", "off", "tidak", "n"):
            return False
        raise ValueError("pakai on/off")
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text.replace(",", "."))
    if kind.startswith("choice:"):
        opts = kind.split(":", 1)[1].split(",")
        if text in opts:
            return text
        raise ValueError(f"pilihan: {', '.join(opts)}")
    raise ValueError(f"kind tidak dikenal: {kind}")


def render_banner(console: Console) -> None:
    console.print(banner())
