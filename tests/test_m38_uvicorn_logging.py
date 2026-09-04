"""Access logging must survive the redaction record factory.

install_logging_redaction() replaces the global LogRecordFactory so every
record arrives already redacted and already formatted, with args cleared --
clearing args is what stops the original unredacted arguments being
re-interpolated downstream.

uvicorn's AccessFormatter ignores record.msg and instead unpacks five
positional values out of record.args, so against a redacted record it raised

    ValueError: not enough values to unpack (expected 5, got 0)

on every single request, while the route itself still returned 200.
"""
from __future__ import annotations

import logging

import pytest

from security.redaction import install_logging_redaction, uvicorn_log_config


def _access_record() -> logging.LogRecord:
    return logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:51234", "GET", "/health", "1.1", 200),
        None,
    )


def test_redaction_factory_clears_args_by_design():
    """Pin the deliberate behaviour the logging config has to accommodate."""
    install_logging_redaction()
    assert _access_record().args == ()


def test_uvicorn_access_formatter_is_incompatible_with_redacted_records():
    """The exact defect, pinned so it cannot silently return."""
    uvicorn_logging = pytest.importorskip("uvicorn.logging")
    install_logging_redaction()
    with pytest.raises(ValueError, match="not enough values to unpack"):
        uvicorn_logging.AccessFormatter().format(_access_record())


def test_configured_formatter_renders_the_access_line_without_crashing():
    """Nothing is suppressed: the factory already rendered the same line."""
    install_logging_redaction()
    rendered = logging.Formatter("%(message)s").format(_access_record())
    assert rendered == '127.0.0.1:51234 - "GET /health HTTP/1.1" 200'


def test_log_config_avoids_uvicorn_structural_formatters():
    cfg = uvicorn_log_config("info")
    serialized = repr(cfg)
    assert "AccessFormatter" not in serialized
    assert "DefaultFormatter" not in serialized
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert name in cfg["loggers"], f"{name} must be configured explicitly"
    # Access logging is reconfigured, never disabled.
    assert cfg["loggers"]["uvicorn.access"]["handlers"]
    assert cfg["disable_existing_loggers"] is False


def test_log_config_is_accepted_by_dictconfig_and_renders_cleanly():
    import logging.config

    logging.config.dictConfig(uvicorn_log_config("info"))
    install_logging_redaction()
    handler = logging.getLogger("uvicorn.access").handlers[0]
    assert handler.format(_access_record()).endswith(
        '127.0.0.1:51234 - "GET /health HTTP/1.1" 200'
    )


def test_launch_sites_pass_the_config():
    """A correct helper is useless if the servers do not use it."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("web/app.py", "native/macos/launcher.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "uvicorn_log_config" in src, f"{rel} does not pass log_config"
