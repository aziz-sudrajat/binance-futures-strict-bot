"""Tests for logutil.py: opt-in structured JSON logging + secret redaction."""
import io
import json
import logging

import pytest

import logutil
from logutil import JsonFormatter


def _record(msg="hello", **extra):
    rec = logging.LogRecord("test.logger", logging.INFO, __file__, 10,
                            msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def _parse(line):
    assert line.count("\n") <= 1, "must be a single JSON line"
    return json.loads(line.strip())


def test_json_formatter_has_required_fields():
    line = JsonFormatter().format(_record("it works"))
    obj = _parse(line)
    for field in ("ts", "level", "logger", "msg"):
        assert field in obj, f"missing {field}"
    assert obj["level"] == "INFO"
    assert obj["logger"] == "test.logger"
    assert obj["msg"] == "it works"
    # one JSON object per line
    assert line.count("{") >= 1 and line.endswith("}")


def test_json_formatter_includes_extras():
    line = JsonFormatter().format(_record("x", symbol="BTCUSDT", quality=72))
    obj = _parse(line)
    assert obj["symbol"] == "BTCUSDT"
    assert obj["quality"] == 72


def test_secret_extras_are_redacted():
    secrets = {
        "api_key": "AKIA-DUMMY-KEY-12345",
        "api_secret": "DUMMY-SECRET-67890",
        "token": "DUMMY-TOKEN-ABC",
        "signature": "DUMMY-SIG-XYZ",
        "password": "DUMMY-PW-999",
        "auth_header": "Bearer DUMMY-BEARER",
    }
    line = JsonFormatter().format(_record("x", **secrets))
    obj = _parse(line)
    for key in secrets:
        assert obj[key] == logutil.REDACTED, f"{key} not redacted"
    for raw in secrets.values():
        assert raw not in line, f"secret leaked: {raw}"


def test_sensitive_value_in_message_is_scrubbed():
    raw = "S3CR3T-DUMMY-VAL"
    line = JsonFormatter().format(
        _record(f"placing order api_key={raw} ok"))
    assert raw not in line
    obj = _parse(line)
    assert logutil.REDACTED in obj["msg"]


def test_innocent_fields_are_not_redacted():
    # signal_id / author must survive: over-redaction would blind debugging.
    line = JsonFormatter().format(
        _record("x", signal_id="abc123", author="shiki", symbol="BTCUSDT"))
    obj = _parse(line)
    assert obj["signal_id"] == "abc123"
    assert obj["author"] == "shiki"
    assert obj["symbol"] == "BTCUSDT"


def test_non_serializable_extra_does_not_crash():
    line = JsonFormatter().format(_record("x", weird=object()))
    obj = _parse(line)  # must parse
    assert "weird" in obj


def test_setup_logging_json_uses_json_formatter():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        logutil.setup_logging(level=logging.INFO, json_format=True)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        # and it actually emits parseable JSON
        stream = io.StringIO()
        root.handlers[0].setStream(stream)
        logging.getLogger("probe").warning("hello %s", "world")
        obj = json.loads(stream.getvalue().strip())
        assert obj["msg"] == "hello world"
        assert obj["level"] == "WARNING"
    finally:
        root.handlers = old_handlers
        root.level = old_level


def test_setup_logging_readable_is_default_unchanged():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    try:
        logutil.setup_logging(level=logging.INFO, json_format=False)
        assert len(root.handlers) == 1
        fmt = root.handlers[0].formatter._fmt
        assert fmt == logutil.READABLE_FORMAT
        assert ("%(asctime)s %(name)s %(levelname)s %(message)s"
                in fmt), "readable format must stay the historical one"
    finally:
        root.handlers = old_handlers
        root.level = old_level


def test_log_json_env_knob_defaults_off(monkeypatch):
    monkeypatch.delenv("BOT_LOG_JSON", raising=False)
    import importlib
    import config
    importlib.reload(config)
    try:
        assert config.LOG_JSON is False
        monkeypatch.setenv("BOT_LOG_JSON", "1")
        importlib.reload(config)
        assert config.LOG_JSON is True
    finally:
        monkeypatch.delenv("BOT_LOG_JSON", raising=False)
        importlib.reload(config)
