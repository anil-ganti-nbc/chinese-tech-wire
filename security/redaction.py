from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_QUERY_KEYS = {
    "key", "api_key", "apikey", "token", "access_token", "auth",
    "authorization", "password", "passwd", "client_secret",
}
SECRET_ENV_KEYS = (
    "GEMINI_API_KEY", "TRANSLATION_API_KEY", "EMBEDDING_API_KEY",
    "DISCORD_WEBHOOK_URL", "CTW_DASHBOARD_AUTH_TOKEN",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+")
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|client[_-]?secret|password|passwd|authorization|cookie|set-cookie)\b([\s:=]+)([^\s,;&]+)"
)
_WEBHOOK = re.compile(r"https://(?:discord(?:app)?\.com)/api/webhooks/[^\s]+", re.I)
_URL = re.compile(r"https?://[^\s<>'\"]+")
_KNOWN_KEY = re.compile(
    r"(?:AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[opsu]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,})"
)
_installed = False
_previous_factory = logging.getLogRecordFactory()


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    if not parsed.scheme or not parsed.netloc:
        return value
    query = [
        (key, "[REDACTED]" if key.lower() in SENSITIVE_QUERY_KEYS else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    netloc = (
        f"[REDACTED]@{parsed.netloc.rsplit('@', 1)[-1]}"
        if "@" in parsed.netloc
        else parsed.netloc
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), ""))


def redact_text(value: object) -> str:
    text = str(value)
    for name in SECRET_ENV_KEYS:
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _WEBHOOK.sub("[REDACTED_WEBHOOK]", text)
    text = _KNOWN_KEY.sub("[REDACTED_KEY]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _BASIC.sub("Basic [REDACTED]", text)
    text = _KEY_VALUE.sub(r"\1\2[REDACTED]", text)
    text = _URL.sub(lambda match: redact_url(match.group(0)), text)
    return text


def safe_error(exc: BaseException) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": redact_text(exc)}


def sanitize_payload(value: Any) -> Any:
    """Recursively sanitize values before diagnostics or artifacts persist."""
    if isinstance(value, dict):
        return {str(key): sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, BaseException):
        return safe_error(value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def sanitize_artifact(source: Path, destination: Path, *, max_bytes: int = 5 * 1024 * 1024) -> None:
    """Write a redacted text artifact without modifying the source."""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("artifact redaction must never overwrite its source")
    if source.stat().st_size > max_bytes:
        raise ValueError("artifact exceeds the bounded redaction size")
    destination.write_text(redact_text(source.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Render once before redaction so a regex cannot consume text around a
        # printf placeholder and leave the argument count inconsistent.
        record.msg = redact_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            exc = record.exc_info[1]
            record.msg = f"{record.msg} error_type={type(exc).__name__} error={redact_text(exc)}"
            record.exc_info = None
            record.exc_text = None
        return True


def protect_handler(handler: logging.Handler) -> logging.Handler:
    if not any(isinstance(item, RedactingFilter) for item in handler.filters):
        handler.addFilter(RedactingFilter())
    return handler


def install_logging_redaction() -> None:
    global _installed
    if not _installed:
        def redacting_factory(*args, **kwargs):
            record = _previous_factory(*args, **kwargs)
            record.msg = redact_text(record.getMessage())
            record.args = ()
            if record.exc_info:
                exc = record.exc_info[1]
                record.msg = (
                    f"{record.msg} error_type={type(exc).__name__} "
                    f"error={redact_text(exc)}"
                )
                record.exc_info = None
                record.exc_text = None
            return record

        logging.setLogRecordFactory(redacting_factory)
        _installed = True
    root = logging.getLogger()
    for handler in root.handlers:
        protect_handler(handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            protect_handler(handler)
