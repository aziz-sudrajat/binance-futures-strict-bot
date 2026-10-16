#!/usr/bin/env python3
"""Poll a lifecycle .jsonl log and forward NEW events to Telegram/Discord.

Same external pattern as the WhatsApp notifier cron: the trading
executor is untouched; this script only READS the log.

Usage:
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
        python tools/notify_lifecycle.py --file lifecycle-live.jsonl [--once]

    --once      process only events already in the file, then exit
    (default)   tail forever, forwarding new terminal events

State: last-processed line count is kept in <file>.notify_offset next
to the log. Env configures the channels (see notify.py).
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notify  # noqa: E402


def _offset_path(path: str) -> str:
    return path + ".notify_offset"


def _read_offset(path: str) -> int:
    try:
        with open(_offset_path(path), encoding="utf-8") as f:
            return max(0, int(f.read().strip() or 0))
    except (OSError, ValueError):
        return 0


def _write_offset(path: str, n: int) -> None:
    try:
        with open(_offset_path(path), "w", encoding="utf-8") as f:
            f.write(str(n))
    except OSError:
        pass


def _new_events(path: str, offset: int):
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return [], offset
    for line in lines[offset:]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events, len(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="lifecycle .jsonl path")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--all-events", action="store_true",
                    help="forward every event, not just terminal ones")
    args = ap.parse_args()

    if not notify.telegram_configured() and not notify.discord_configured():
        print("no channel configured (set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
              "or DISCORD_WEBHOOK_URL)", file=sys.stderr)
        return 2

    offset = _read_offset(args.file)
    while True:
        events, offset = _new_events(args.file, offset)
        for e in events:
            if not args.all_events and not e.get("terminal"):
                continue
            notify.notify_all(notify.format_trade_event(e))
        _write_offset(args.file, offset)
        if args.once:
            break
        time.sleep(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
