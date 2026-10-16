# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the binance-futures-strict-bot project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.

"""Structured JSON logging (stdlib only, opt-in).

The default stays the human-readable text format. Enable JSON with
--log-json (run_executor.py) or the BOT_LOG_JSON=1 env var
(config.LOG_JSON).

SECURITY: this formatter actively redacts secrets. Any ``extra`` field
whose key looks sensitive (api key, secret, token, signature, password,
auth, bearer, cookie, private key ...) is replaced with "[REDACTED]",
and common ``key=value`` / ``key: value`` patterns inside the rendered
message are scrubbed the same way. Callers must still never log
credentials on purpose; the formatter is a safety net, not a license.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

REDACTED = "[REDACTED]"

# Two-tier matching so ordinary fields like ``signal_id`` or ``author``
# are NOT redacted while real secrets are:
#  - SENSITIVE_SUBSTRINGS: unambiguous fragments matched against the whole
#    lowercased key ("api_key" never appears inside an innocent word).
#  - SENSITIVE_TOKENS: matched against underscore/dash-separated tokens of
#    the key, so "auth" hits "auth_header" but not "author", and "signature"
#    hits "sig"/"signature" fields but not "signal_id".
SENSITIVE_SUBSTRINGS = (
    "api_key", "apikey", "api-secret", "apisecret",
    "secret", "token", "password", "passwd",
    "private_key", "privatekey", "mnemonic",
)
SENSITIVE_TOKENS = frozenset({
    "secret", "token", "signature", "sig",
    "password", "passwd", "auth", "bearer",
    "cookie", "sessionid", "privatekey", "mnemonic", "seed",
})

_KEY_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# Scrub ``name=value`` / ``name: value`` pairs that appear inside the
# rendered message text (e.g. a query string or a pasted dict fragment).
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret|token|signature|"
    r"password|passwd|bearer|private[_-]?key)\b\s*[:=]\s*"
    r"(?P<val>[\"']?[^\"'\s,}]+[\"']?)"
)

# logging.LogRecord attributes that are NOT user extras.
_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
})


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    if any(part in k for part in SENSITIVE_SUBSTRINGS):
        return True
    return any(tok in SENSITIVE_TOKENS
               for tok in _KEY_SPLIT_RE.split(k) if tok)


def _scrub_text(text: str) -> str:
    """Replace sensitive values inside a rendered message string."""
    return _SENSITIVE_VALUE_RE.sub(
        lambda m: f"{m.group(1)}={REDACTED}", text)


def _safe_value(value):
    """JSON-serializable coercion that never raises."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per log line.

    Fields: ts (ISO-8601 UTC), level, logger, msg, plus any non-reserved
    ``extra`` attributes. Secret-bearing extras are redacted.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = _scrub_text(record.getMessage())
        payload = {
            "ts": datetime.fromtimestamp(record.created,
                                         tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED:
                continue
            if _is_sensitive_key(key):
                payload[key] = REDACTED
            else:
                payload[key] = _safe_value(value)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            # Last-resort: stringify everything.
            return json.dumps({k: repr(v) for k, v in payload.items()},
                              ensure_ascii=False)


READABLE_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def setup_logging(level: int = logging.INFO,
                  json_format: bool = False) -> None:
    """Configure the root logger. ``json_format=True`` switches the
    handler to :class:`JsonFormatter`; otherwise the existing readable
    text format is kept (default behavior unchanged)."""
    if json_format:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
    else:
        logging.basicConfig(level=level, format=READABLE_FORMAT, force=True)
