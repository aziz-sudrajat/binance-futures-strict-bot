#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Terminal UI for the bot — live dashboard inside the terminal, no browser.

Stdlib only (curses). Reuses dashboard.py for status/settings/radar logic,
so everything you see here is the same data and the same settings file the
executor loop reads — edits apply on the next scan, no restart needed.

Run:
    .venv/bin/python tui.py                  # mode follows $BOT_MODE
    .venv/bin/python tui.py --mode live      # explicit mode

Keys (main screen):
    1/2/3  switch mode  live / testnet / paper
    r       run Signal Radar (background, ~30-60s)
    s       settings editor
    a       toggle auto-trade
    q       quit

Keys (settings editor):
    up/down or j/k   move    space  toggle / cycle choice
    enter            edit number    s  back to main
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

try:
    import curses
except ImportError:  # pragma: no cover - Windows has no stdlib curses
    sys.exit("tui.py needs the curses module (Linux/Termux python has it).")

import dashboard

# (key, label, kind) — canonical spec lives in dashboard.SETTING_FIELDS.
FIELDS: list[tuple[str, str, str]] = dashboard.SETTING_FIELDS

MODES = ["live", "testnet", "paper"]


# ------------------------------------------------------------------ logic
# Pure functions (no curses) so they can be unit-tested.

def toggle_bool(settings: dict, key: str) -> dict:
    """Return a copy of settings with a bool field flipped."""
    out = dict(settings)
    out[key] = not bool(settings.get(key))
    return dashboard.validate_settings(out)


def cycle_choice(settings: dict, key: str, choices: list[str]) -> dict:
    """Return a copy of settings with a choice field moved to the next."""
    out = dict(settings)
    cur = str(settings.get(key))
    nxt = choices[(choices.index(cur) + 1) % len(choices)] \
        if cur in choices else choices[0]
    out[key] = nxt
    return dashboard.validate_settings(out)


def set_number(settings: dict, key: str, raw: str, kind: str) -> dict:
    """Return validated settings with a numeric field replaced.

    Raises ValueError on bad input or failed validation.
    """
    try:
        v = int(raw) if kind == "int" else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: '{raw}' bukan angka")
    out = dict(settings)
    out[key] = v
    return dashboard.validate_settings(out)


def fmt_positions(status: dict) -> list[str]:
    lines = []
    for p in status.get("positions", []):
        upnl = p.get("unrealized_pnl")
        upnl_s = f"{upnl:+.2f}" if isinstance(upnl, (int, float)) else "?"
        lines.append(
            f"{p.get('symbol', '?'):10} {p.get('side', '?'):5} "
            f"qty={p.get('qty')} lev={p.get('leverage')}x "
            f"entry={p.get('entry_price')} uPnL={upnl_s}")
    return lines or ["(tidak ada posisi terbuka)"]


def fmt_events(status: dict, limit: int = 12) -> list[str]:
    lines = []
    for e in (status.get("events") or [])[:limit]:
        et = e.get("event", "?")
        sym = e.get("symbol") or e.get("note") or ""
        lines.append(f"{e.get('at_human', '')} {et:16} {sym}"[:78])
    return lines or ["(belum ada event)"]


def fmt_radar(rows: list[dict]) -> list[str]:
    lines = []
    for i, r in enumerate(rows, 1):
        if r.get("state") == "approved":
            lines.append(
                f"{i:2}. {r['symbol']:10} APPROVED Q={r.get('quality')} "
                f"entry={r.get('entry'):.4g} SL={r.get('stop_loss'):.4g}")
        else:
            reason = (r.get("reason") or r.get("state") or "")[:52]
            lines.append(f"{i:2}. {r['symbol']:10} {reason}")
    return lines or ["(kosong)"]


# ------------------------------------------------------------------ TUI

class App:
    def __init__(self, stdscr, mode: str):
        self.std = stdscr
        self.mode = mode
        self.screen = "main"          # main | settings | radar
        self.status: dict = {}
        self.status_err = ""
        self.sel = 0
        self.scroll = 0
        self.msg = ""
        self.msg_at = 0.0
        self.radar_rows: list[dict] | None = None
        self.radar_running = False
        self.radar_err = ""
        self.last_refresh = 0.0
        curses.curs_set(0)
        curses.use_default_colors()
        for i, (fg, bg) in enumerate(
                [(curses.COLOR_GREEN, -1), (curses.COLOR_RED, -1),
                 (curses.COLOR_YELLOW, -1), (curses.COLOR_CYAN, -1)], 1):
            try:
                curses.init_pair(i, fg, bg)
            except curses.error:
                pass
        self.C = {"green": 1, "red": 2, "yel": 3, "cyan": 4}

    # -- helpers ------------------------------------------------------
    def say(self, text: str) -> None:
        self.msg, self.msg_at = text, time.time()

    def add(self, y: int, x: int, text: str, color: int = 0,
            attr: int = 0) -> None:
        h, w = self.std.getmaxyx()
        if y < 0 or y >= h:
            return
        try:
            if color:
                self.std.addstr(y, x, text[: max(0, w - x - 1)],
                                curses.color_pair(color) | attr)
            else:
                self.std.addstr(y, x, text[: max(0, w - x - 1)], attr)
        except curses.error:
            pass

    def settings(self) -> dict:
        return dashboard.load_settings(mode=self.mode)

    def persist(self, new: dict) -> None:
        try:
            dashboard.save_settings(new, mode=self.mode)
            self.say("tersimpan ✓")
        except ValueError as e:
            self.say(f"ditolak: {e}")
        except OSError as e:
            self.say(f"gagal simpan: {e}")

    # -- data ---------------------------------------------------------
    def refresh_status(self) -> None:
        try:
            self.status = dashboard.build_status(self.mode)
            self.status_err = ""
        except Exception as e:  # noqa: BLE001 - TUI must never die
            self.status_err = str(e)[:100]
        self.last_refresh = time.time()

    def start_radar(self) -> None:
        if self.radar_running:
            return
        self.radar_running = True
        self.radar_rows = None
        self.radar_err = ""

        def _run() -> None:
            try:
                self.radar_rows = dashboard.run_radar()
            except Exception as e:  # noqa: BLE001
                self.radar_err = str(e)[:120]
            finally:
                self.radar_running = False

        threading.Thread(target=_run, daemon=True).start()

    # -- screens ------------------------------------------------------
    def draw_main(self) -> None:
        h, w = self.std.getmaxyx()
        st = self.status
        bal = st.get("balance")
        bal_s = f"{bal:.2f} USDT" if isinstance(bal, (int, float)) else "?"
        daily = (st.get("daily") or {}).get("realized_pnl")
        pnl_s = f"{daily:+.2f}" if isinstance(daily, (int, float)) else "?"
        ks = (st.get("kill_switch") or {}).get("tripped")
        sett = self.settings()
        auto = sett.get("auto_trade")

        self.add(0, 0, f" BOT [{self.mode.upper()}]  bal={bal_s}  "
                       f"PnL hari ini={pnl_s} USDT", self.C["cyan"],
                 curses.A_BOLD)
        self.add(0, w - 22, "KILL-SWITCH AKTIF" if ks else "kill-switch ok",
                 self.C["red"] if ks else self.C["green"], curses.A_BOLD)
        ocfg = (f"margin={sett.get('margin_mode')} lev={sett.get('leverage_mode')}"
                f"{':' + str(sett.get('leverage')) + 'x' if sett.get('leverage_mode') == 'fixed' else ''} "
                f"entry={sett.get('entry_mode')}")
        self.add(1, 0, f"auto-trade={'ON' if auto else 'OFF'}  {ocfg}  "
                       f"q{min(9, int(sett.get('min_quality_score', 70)))}",
                 self.C["green"] if auto else self.C["yel"])
        if self.status_err:
            self.add(2, 0, f"! {self.status_err}", self.C["red"])
            y = 3
        else:
            y = 2
        self.add(y, 0, "─ POSISI TERBUKA ─", attr=curses.A_BOLD)
        y += 1
        for line in fmt_positions(st):
            self.add(y, 0, line)
            y += 1
        y += 1
        self.add(y, 0, "─ EVENT TERAKHIR ─", attr=curses.A_BOLD)
        y += 1
        for line in fmt_events(st):
            self.add(y, 0, line)
            y += 1
        self.draw_footer(
            "1/2/3 mode · r radar · d signal DB · s setting · a auto-trade · q keluar")

    def draw_settings(self) -> None:
        h, _w = self.std.getmaxyx()
        sett = self.settings()
        self.add(0, 0, f" SETTING [{self.mode.upper()}] — tersimpan "
                       f"otomatis, berlaku scan berikutnya",
                 self.C["cyan"], curses.A_BOLD)
        n = len(FIELDS)
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + h - 3:
            self.scroll = self.sel - (h - 4)
        for i in range(self.scroll, min(n, self.scroll + h - 3)):
            key, label, kind = FIELDS[i]
            v = sett.get(key)
            if kind == "bool":
                vs = "ON" if v else "OFF"
                col = self.C["green"] if v else self.C["yel"]
            else:
                vs, col = str(v), 0
            marker = ">" if i == self.sel else " "
            self.add(i - self.scroll + 2, 0,
                     f"{marker} {label:24} {vs}", col,
                     curses.A_REVERSE if i == self.sel else 0)
        self.draw_footer("↑↓/jk pilih · spasi toggle · enter edit angka · "
                         "s kembali")

    def load_dbsignals(self) -> None:
        """Load fresh DB signals (fast, local SQLite — no thread needed)."""
        try:
            self.dbsig = dashboard.scanner_status()
            self.dbsig_err = ""
        except Exception as e:  # noqa: BLE001
            self.dbsig = None
            self.dbsig_err = str(e)[:120]

    def draw_dbsignals(self) -> None:
        h, _w = self.std.getmaxyx()
        self.add(0, 0, " SIGNAL DB — scanner OKX → signals.db (tahap 1)",
                 self.C["cyan"], curses.A_BOLD)
        if self.dbsig_err:
            self.add(2, 0, f"! {self.dbsig_err}", self.C["red"])
        elif self.dbsig is not None:
            st = self.dbsig.get("state") or {}
            db = self.dbsig.get("db") or {}
            lines = []
            if st:
                lines.append(f"pass terakhir: {st.get('symbols')} coin, "
                             f"approved {st.get('approved')}, "
                             f"error {st.get('errors')}, "
                             f"{st.get('duration_s')}s")
            lines.append(f"fresh di DB: {db.get('fresh', 0)}  "
                         f"(consumed {db.get('consumed', 0)}, "
                         f"expired {db.get('expired', 0)})")
            lines.append("")
            for s in self.dbsig.get("signals") or []:
                lines.append(f"{s['symbol']:12} {s['direction']:5} "
                             f"Q={s['quality']:<6} entry={s['entry']}  "
                             f"{s['age_min']}m lalu")
            if not lines:
                lines.append("belum ada signal")
            if self.sel < self.scroll:
                self.scroll = self.sel
            if self.sel >= self.scroll + h - 3:
                self.scroll = self.sel - (h - 4)
            for i in range(self.scroll, min(len(lines),
                                            self.scroll + h - 3)):
                self.add(i - self.scroll + 2, 0, lines[i])
        else:
            self.add(2, 0, "tekan d untuk muat", self.C["yel"])
        self.draw_footer("d muat ulang · ↑↓ scroll · s kembali")

    def on_key_dbsignals(self, ch: int) -> bool:
        if ch in (ord("s"), ord("q"), 27):
            self.screen = "main"
        elif ch == ord("d"):
            self.sel = self.scroll = 0
            self.load_dbsignals()
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel += 1
        return True

    def draw_radar(self) -> None:
        h, _w = self.std.getmaxyx()
        self.add(0, 0, " SIGNAL RADAR — engine asli, ranking by quality",
                 self.C["cyan"], curses.A_BOLD)
        if self.radar_running:
            self.add(2, 0, "scanning… (jalan di background)", self.C["yel"])
        elif self.radar_err:
            self.add(2, 0, f"! {self.radar_err}", self.C["red"])
        elif self.radar_rows is not None:
            lines = fmt_radar(self.radar_rows)
            if self.sel < self.scroll:
                self.scroll = self.sel
            if self.sel >= self.scroll + h - 3:
                self.scroll = self.sel - (h - 4)
            for i in range(self.scroll, min(len(lines),
                                            self.scroll + h - 3)):
                self.add(i - self.scroll + 2, 0, lines[i])
        else:
            self.add(2, 0, "tekan r untuk scan", self.C["yel"])
        self.draw_footer("r scan ulang · ↑↓ scroll · s kembali")

    def draw_footer(self, text: str) -> None:
        h, w = self.std.getmaxyx()
        if time.time() - self.msg_at < 4 and self.msg:
            self.add(h - 2, 0, self.msg, self.C["yel"], curses.A_BOLD)
        self.add(h - 1, 0, text[: w - 1], attr=curses.A_DIM)

    # -- input --------------------------------------------------------
    def on_key_main(self, ch: int) -> bool:
        if ch == ord("q"):
            return False
        if ch in (ord("1"), ord("2"), ord("3")):
            self.mode = {"1": "live", "2": "testnet", "3": "paper"}[chr(ch)]
            self.refresh_status()
        elif ch == ord("r"):
            self.screen = "radar"
            self.sel = self.scroll = 0
            self.start_radar()
        elif ch == ord("d"):
            self.screen = "dbsignals"
            self.sel = self.scroll = 0
            self.load_dbsignals()
        elif ch == ord("s"):
            self.screen = "settings"
            self.sel = self.scroll = 0
        elif ch == ord("a"):
            self.persist(toggle_bool(self.settings(), "auto_trade"))
            self.refresh_status()
        return True

    def on_key_settings(self, ch: int) -> bool:
        n = len(FIELDS)
        if ch in (curses.KEY_UP, ord("k")):
            self.sel = (self.sel - 1) % n
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel = (self.sel + 1) % n
        elif ch in (ord("s"), ord("q"), 27):
            self.screen = "main"
        elif ch == ord(" "):
            key, _label, kind = FIELDS[self.sel]
            sett = self.settings()
            try:
                if kind == "bool":
                    self.persist(toggle_bool(sett, key))
                elif kind.startswith("choice:"):
                    self.persist(cycle_choice(
                        sett, key, kind.split(":", 1)[1].split(",")))
            except ValueError as e:
                self.say(str(e))
        elif ch in (curses.KEY_ENTER, 10, 13):
            key, label, kind = FIELDS[self.sel]
            if kind in ("int", "float"):
                raw = self.prompt(f"{label} = ")
                if raw is not None:
                    try:
                        self.persist(set_number(
                            self.settings(), key, raw, kind))
                    except ValueError as e:
                        self.say(str(e))
        return True

    def on_key_radar(self, ch: int) -> bool:
        if ch in (ord("s"), ord("q"), 27):
            self.screen = "main"
        elif ch == ord("r"):
            self.sel = self.scroll = 0
            self.start_radar()
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel += 1
        return True

    def prompt(self, text: str) -> str | None:
        h, w = self.std.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        self.add(h - 2, 0, text + " " * (w - len(text) - 1))
        self.std.move(h - 2, len(text))
        try:
            raw = self.std.getstr(h - 2, len(text), 32).decode(
                "utf-8", "replace").strip()
        except (curses.error, ValueError):
            raw = ""
        finally:
            curses.noecho()
            curses.curs_set(0)
        return raw or None

    # -- loop ---------------------------------------------------------
    def run(self) -> None:
        self.refresh_status()
        self.std.timeout(1000)  # 1s tick: responsive keys + 5s refresh
        alive = True
        while alive:
            if (self.screen == "main"
                    and time.time() - self.last_refresh > 5):
                self.refresh_status()
            self.std.erase()
            if self.screen == "main":
                self.draw_main()
            elif self.screen == "settings":
                self.draw_settings()
            elif self.screen == "dbsignals":
                self.draw_dbsignals()
            else:
                self.draw_radar()
            self.std.refresh()
            ch = self.std.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if self.screen == "main":
                alive = self.on_key_main(ch)
            elif self.screen == "settings":
                alive = self.on_key_settings(ch)
            elif self.screen == "dbsignals":
                alive = self.on_key_dbsignals(ch)
            else:
                alive = self.on_key_radar(ch)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bot terminal UI (no browser)")
    env_mode = (os.environ.get("BOT_MODE") or "").strip().lower()
    if env_mode == "demo":
        env_mode = "testnet"
    if env_mode not in MODES:
        env_mode = "live"
    ap.add_argument("--mode", default=env_mode, choices=MODES,
                    help="mode awal (default: $BOT_MODE)")
    args = ap.parse_args(argv)

    def _run(stdscr) -> None:
        App(stdscr, args.mode).run()

    try:
        curses.wrapper(_run)
    except curses.error as e:
        return f"tui gagal start (terminal tidak dukung curses?): {e}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
