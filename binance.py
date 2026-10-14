#!/usr/bin/env python3
"""`binance` — terminal interaktif Binance Futures Studio.

Framework-style interactive shell: ketik perintah, bukan hafal tombol.

    $ ./binance            # atau: .venv/bin/python binance.py
    binance(paper)> help
    binance(paper)> scan
    binance(paper)> auto-trade --demo
    binance(paper)> position
    binance(paper)> setting

Perintah: scan, auto-trade [--demo|--live|--paper] [--from-db] [--setting],
setting, set <key> <value>, position, balance, signals, scanner <start|stop|
status|once>, mode [live|demo|paper], events, kill, dashboard, clear,
help, exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("binance-shell")

try:
    import readline  # noqa: F401 - history & line editing
    readline.set_history_length(200)
except ImportError:
    pass

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import config
import dashboard
import run_executor
import scanner as scanner_mod
import shell_ui
from shell_ui import (COMMANDS, banner, coerce_setting, command_help,
                      events_table, help_table, lookup_command, parse_line,
                      positions_table, radar_table, settings_table,
                      signals_table, status_panel, suggest_command)

MODES = ("live", "testnet", "paper")
MODE_ALIASES = {"demo": "testnet", "testnet": "testnet",
                "live": "live", "paper": "paper"}


def _default_mode() -> str:
    m = os.environ.get("BOT_MODE", "").lower()
    return m if m in MODES else "paper"


def _build_args(mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        paper=mode == "paper", testnet=mode == "testnet", live=mode == "live",
        interval=config.EXEC_POLL_INTERVAL_MIN,
        fixed_margin_usdt=None, leverage=None, leverage_tiers=None,
        leverage_default=None, manual=None, from_db=False, db="signals.db",
    )


class BinanceShell:
    def __init__(self) -> None:
        self.console = Console()
        self.mode = _default_mode()
        self._scanner_thread: threading.Thread | None = None
        self._scanner_stop = threading.Event()
        self._executor_cache: dict[str, object] = {}

    # ---------------------------------------------------------- prompt
    def prompt(self) -> str:
        color = {"live": "red", "testnet": "yellow",
                 "paper": "green"}[self.mode]
        return f"[bold cyan]binance[/bold cyan]([{color}]{self.mode}[/{color}])> "

    # ---------------------------------------------------------- executor
    def get_executor(self, mode: str | None = None):
        mode = mode or self.mode
        if mode not in self._executor_cache:
            executor, _ = run_executor.build(_build_args(mode))
            self._executor_cache[mode] = executor
        return self._executor_cache[mode]

    def drop_executor(self, mode: str | None = None) -> None:
        self._executor_cache.pop(mode or self.mode, None)

    # ---------------------------------------------------------- commands
    def cmd_help(self, args: list[str]) -> None:
        if args:
            cmd = lookup_command(args[0])
            if cmd:
                self.console.print(command_help(cmd))
            else:
                self.console.print(f"[red]perintah tidak dikenal:[/red] "
                                   f"{args[0]}")
        else:
            self.console.print(Panel(
                help_table(), title="Perintah", border_style="cyan"))

    def cmd_scan(self, args: list[str]) -> None:
        top = 20
        if "--top" in args:
            try:
                top = max(1, min(int(args[args.index("--top") + 1]), 100))
            except (ValueError, IndexError):
                self.console.print("[red]--top butuh angka[/red]")
                return
        self.console.print("[dim]scanning dengan engine STRICT…[/dim]")
        try:
            rows = dashboard.run_radar(mode=self.mode)
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[red]scan gagal:[/red] {e}")
            return
        self.console.print(radar_table(rows, limit=top))

    def cmd_mode(self, args: list[str]) -> None:
        if not args:
            self.console.print(f"mode aktif: [bold]{self.mode}[/bold]")
            return
        m = MODE_ALIASES.get(args[0].lower())
        if not m:
            self.console.print("[red]mode: live | demo | paper[/red]")
            return
        self.mode = m
        self.console.print(f"mode → [bold]{self.mode}[/bold]")

    def cmd_balance(self, _args: list[str]) -> None:
        try:
            st = dashboard.build_status(self.mode)
        except SystemExit as e:
            self.console.print(f"[red]{e}[/red]")
            return
        self.console.print(status_panel(st, self.mode))

    def cmd_position(self, _args: list[str]) -> None:
        try:
            st = dashboard.build_status(self.mode)
        except SystemExit as e:
            self.console.print(f"[red]{e}[/red]")
            return
        self.console.print(status_panel(st, self.mode))
        self.console.print(positions_table(st))

    def cmd_signals(self, _args: list[str]) -> None:
        try:
            info = dashboard.scanner_status()
        except Exception as e:  # noqa: BLE001
            self.console.print(f"[red]{e}[/red]")
            return
        self.console.print(signals_table(info.get("signals") or []))
        db = info.get("db") or {}
        self.console.print(
            f"[dim]fresh {db.get('fresh', 0)} · consumed "
            f"{db.get('consumed', 0)} · expired {db.get('expired', 0)}[/dim]")

    def cmd_events(self, args: list[str]) -> None:
        limit = 15
        if "--limit" in args:
            try:
                limit = max(1, min(int(args[args.index("--limit") + 1]), 200))
            except (ValueError, IndexError):
                pass
        self.console.print(events_table(
            dashboard.read_events(self.mode, limit)))

    def cmd_kill(self, _args: list[str]) -> None:
        try:
            st = dashboard.build_status(self.mode)
        except SystemExit as e:
            self.console.print(f"[red]{e}[/red]")
            return
        ks = st.get("kill_switch") or {}
        active = bool(ks.get("tripped") or ks.get("active"))
        if active:
            self.console.print(Panel(
                f"[bold red]KILL SWITCH AKTIF[/bold red]\n"
                f"alasan: {ks.get('reason', '-')}",
                border_style="red", title="Kill Switch"))
        else:
            self.console.print("[green]kill-switch nonaktif — aman[/green]")

    def cmd_dashboard(self, _args: list[str]) -> None:
        self.console.print(Panel(
            "Jalankan di terminal lain:\n"
            "[cyan].venv/bin/python dashboard.py[/cyan]\n"
            "lalu buka http://localhost:8000",
            title="Web Dashboard", border_style="cyan"))

    # -------------------------------------------------- setting
    def cmd_setting(self, _args: list[str]) -> None:
        fields = dashboard.SETTING_FIELDS
        while True:
            sett = dashboard.load_settings(mode=self.mode)
            self.console.print(settings_table(sett, fields))
            raw = Prompt.ask(
                "[dim]nomor setting (enter = kembali)[/dim]",
                default="").strip()
            if not raw:
                return
            try:
                idx = int(raw) - 1
                key, label, kind = fields[idx]
            except (ValueError, IndexError):
                self.console.print("[red]nomor tidak valid[/red]")
                continue
            cur = sett.get(key)
            val = Prompt.ask(f"{label} [{cur}]", default="").strip()
            if not val:
                continue
            try:
                sett[key] = coerce_setting(kind, val)
            except ValueError as e:
                self.console.print(f"[red]{e}[/red]")
                continue
            dashboard.save_settings(sett, mode=self.mode)
            self.console.print(f"[green]✓ {key} = {sett[key]}[/green] "
                               f"[dim](berlaku tanpa restart)[/dim]")

    def cmd_set(self, args: list[str]) -> None:
        if len(args) < 2:
            self.console.print("[red]pakai: set <key> <value>[/red]")
            return
        key, val = args[0], " ".join(args[1:])
        fields = {k: (label, kind)
                  for k, label, kind in dashboard.SETTING_FIELDS}
        if key not in fields:
            close = [k for k in fields if key in k][:5]
            hint = f" mirip: {', '.join(close)}" if close else ""
            self.console.print(f"[red]key tidak dikenal:[/red] {key}.{hint}")
            return
        _label, kind = fields[key]
        try:
            coerced = coerce_setting(kind, val)
        except ValueError as e:
            self.console.print(f"[red]{e}[/red]")
            return
        sett = dashboard.load_settings(mode=self.mode)
        sett[key] = coerced
        dashboard.save_settings(sett, mode=self.mode)
        self.console.print(f"[green]✓ {key} = {coerced}[/green]")

    # -------------------------------------------------- scanner
    def _scanner_loop(self) -> None:
        from data import MarketData
        from signals_db import SignalDB
        market = MarketData()
        db = SignalDB("signals.db")
        try:
            while not self._scanner_stop.is_set():
                try:
                    stats = scanner_mod.run_pass(market, db, expiry_min=30)
                    log.info("scanner pass: %s", stats)
                except Exception:  # noqa: BLE001
                    log.exception("scanner pass failed")
                self._scanner_stop.wait(15 * 60)
        finally:
            db.close()

    def cmd_scanner(self, args: list[str]) -> None:
        sub = (args[0].lower() if args else "status")
        if sub == "status":
            try:
                info = dashboard.scanner_status()
            except Exception as e:  # noqa: BLE001
                self.console.print(f"[red]{e}[/red]")
                return
            st = info.get("state") or {}
            db = info.get("db") or {}
            running = (self._scanner_thread is not None
                       and self._scanner_thread.is_alive())
            t = Table(show_header=False, box=None)
            t.add_column(style="dim")
            t.add_column()
            t.add_row("Thread scanner",
                      "[green]jalan[/green]" if running else "[dim]mati[/dim]")
            if st:
                t.add_row("Pass terakhir",
                          f"{st.get('symbols')} coin, approved "
                          f"{st.get('approved')}, {st.get('duration_s')}s")
            t.add_row("Signal fresh di DB", str(db.get("fresh", 0)))
            self.console.print(Panel(t, title="Scanner", border_style="cyan"))
        elif sub == "start":
            if self._scanner_thread and self._scanner_thread.is_alive():
                self.console.print("[yellow]scanner sudah jalan[/yellow]")
                return
            self._scanner_stop.clear()
            self._scanner_thread = threading.Thread(
                target=self._scanner_loop, daemon=True)
            self._scanner_thread.start()
            self.console.print("[green]✓ scanner jalan di background "
                               "(semua coin OKX → signals.db)[/green]")
        elif sub == "stop":
            self._scanner_stop.set()
            self.console.print("[yellow]scanner dihentikan[/yellow]")
        elif sub == "once":
            self.console.print("[dim]satu pass penuh…[/dim]")
            from data import MarketData
            from signals_db import SignalDB
            db = SignalDB("signals.db")
            try:
                stats = scanner_mod.run_pass(MarketData(), db, expiry_min=30)
            finally:
                db.close()
            self.console.print(
                f"[green]✓[/green] {stats['symbols']} coin, approved "
                f"[bold]{stats['approved']}[/bold], "
                f"error {stats['errors']}, {stats['duration_s']}s")
        else:
            self.console.print("[red]scanner <start|stop|status|once>[/red]")

    # -------------------------------------------------- auto-trade
    def cmd_autotrade(self, args: list[str]) -> None:
        if "--setting" in args:
            self.cmd_setting([])
            return
        for flag in ("--live", "--demo", "--testnet", "--paper"):
            if flag in args:
                self.mode = MODE_ALIASES[flag.lstrip("-")]
                self.console.print(f"mode → [bold]{self.mode}[/bold]")
                break
        from_db = "--from-db" in args
        self.run_autotrade_loop(from_db=from_db)

    def run_autotrade_loop(self, from_db: bool = False) -> None:
        try:
            executor = self.get_executor()
        except SystemExit as e:
            self.console.print(f"[red]{e}[/red]")
            return
        src = "signals.db (scanner)" if from_db else "scan langsung"
        if self.mode == "live":
            self.console.print(Panel(
                "[bold red]LIVE MODE — order beneran di Binance Futures "
                "mainnet.[/bold red]\nCtrl+C untuk berhenti.",
                border_style="red", title="Peringatan"))
            if not Prompt.ask("Lanjut live?", choices=["y", "n"],
                              default="n") == "y":
                self.console.print("[dim]dibatalkan[/dim]")
                return
        self.console.print(
            f"[green]▶ auto-trade [{self.mode}] berjalan[/green] "
            f"[dim](sumber: {src}, Ctrl+C berhenti)[/dim]")
        cycle = 0
        with Live(self._autotrade_view(executor, None, cycle, from_db),
                   console=self.console, refresh_per_second=1) as live:
            try:
                while True:
                    cycle += 1
                    try:
                        summary = (executor.scan_from_db("signals.db")
                                   if from_db else executor.scan_once())
                    except Exception as e:  # noqa: BLE001
                        summary = {"error": str(e)[:100]}
                    live.update(self._autotrade_view(executor, summary,
                                                     cycle, from_db))
                    interval = executor.poll_interval_min * 60
                    for _ in range(int(interval)):
                        time.sleep(1)
            except KeyboardInterrupt:
                pass
        self.console.print("[yellow]■ auto-trade berhenti[/yellow]")

    def _autotrade_view(self, executor, summary, cycle: int,
                        from_db: bool):
        try:
            st = dashboard.build_status(self.mode)
        except Exception:  # noqa: BLE001
            st = {}
        layout = Table.grid(padding=(0, 0))
        layout.add_row(status_panel(st, self.mode))
        layout.add_row(positions_table(st))
        if summary:
            s = {k: v for k, v in summary.items() if k != "errors"}
            layout.add_row(Panel(
                f"cycle #{cycle}  " + "  ".join(f"{k}=[bold]{v}[/bold]"
                                               for k, v in s.items()),
                border_style="dim", title="Cycle terakhir"))
        try:
            ev = dashboard.read_events(self.mode, 6)
            if ev:
                layout.add_row(events_table(ev))
        except Exception:  # noqa: BLE001
            pass
        return Panel(layout, border_style="cyan",
                     title=f"Auto-trade [{self.mode}]"
                           f"{' — dari DB' if from_db else ''} "
                           "(Ctrl+C berhenti)")

    # ---------------------------------------------------------- dispatch
    def dispatch(self, line: str) -> bool:
        """Run one line. Returns False when the shell should exit."""
        name, args = parse_line(line)
        if not name:
            return True
        cmd = lookup_command(name)
        if cmd is None:
            sug = suggest_command(name)
            hint = f" — maksudnya [cyan]{sug[0]}[/cyan]?" if sug else ""
            self.console.print(f"[red]perintah tidak dikenal:[/red] "
                               f"{name}{hint}")
            return True
        handler = {
            "help": self.cmd_help, "scan": self.cmd_scan,
            "mode": self.cmd_mode, "balance": self.cmd_balance,
            "position": self.cmd_position, "signals": self.cmd_signals,
            "events": self.cmd_events, "kill": self.cmd_kill,
            "dashboard": self.cmd_dashboard, "setting": self.cmd_setting,
            "set": self.cmd_set, "scanner": self.cmd_scanner,
            "auto-trade": self.cmd_autotrade,
        }.get(cmd.name)
        if cmd.name == "clear":
            self.console.clear()
            return True
        if cmd.name == "exit":
            return False
        if handler is None:  # pragma: no cover - registry/handler mismatch
            self.console.print(f"[red]belum diimplementasi:[/red] {cmd.name}")
            return True
        try:
            handler(args)
        except KeyboardInterrupt:
            self.console.print("\n[dim]dibatalkan[/dim]")
        except Exception as e:  # noqa: BLE001 - shell never dies on a command
            self.console.print(f"[red]error:[/red] {e}")
        return True

    def repl(self) -> None:
        self.console.print(banner())
        self.console.print(
            f"[dim]mode: [bold]{self.mode}[/bold] — ketik "
            f"[yellow]help[/yellow] untuk daftar perintah[/dim]\n")
        while True:
            try:
                line = Prompt.ask(self.prompt(), console=self.console)
            except (KeyboardInterrupt, EOFError):
                self.console.print()
                break
            try:
                if not self.dispatch(line):
                    break
            except SystemExit:
                break
        self.console.print("[dim]sampai jumpa 👋[/dim]")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Binance Futures Studio — terminal interaktif")
    ap.add_argument("command", nargs="*",
                    help="perintah sekali jalan, mis. scan / position "
                         "(kosong = mode interaktif)")
    args = ap.parse_args(argv)
    shell = BinanceShell()
    if args.command:
        shell.console.print(banner())
        alive = shell.dispatch(" ".join(args.command))
        return 0 if alive else 0
    shell.repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
