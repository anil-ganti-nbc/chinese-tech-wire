from __future__ import annotations

import logging
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_QUERY_KEYS = {"key", "api_key", "apikey", "token", "access_token", "auth", "authorization"}
SECRET_ENV_KEYS = (
    "GEMINI_API_KEY", "TRANSLATION_API_KEY", "EMBEDDING_API_KEY",
    "DISCORD_WEBHOOK_URL", "CTW_DASHBOARD_AUTH_TOKEN",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|authorization)\b([\s:=]+)([^\s,;&]+)"
)
_WEBHOOK = re.compile(r"https://(?:discord(?:app)?\.com)/api/webhooks/[^\s]+", re.I)
_URL = re.compile(r"https?://[^\s<>'\"]+")


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
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def redact_text(value: object) -> str:
    text = str(value)
    for name in SECRET_ENV_KEYS:
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _WEBHOOK.sub("[REDACTED_WEBHOOK]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KEY_VALUE.sub(r"\1\2[REDACTED]", text)
    text = _URL.sub(lambda match: redact_url(match.group(0)), text)
    return text


def safe_error(exc: BaseException) -> dict[str, str]:
    return {"error_type": type(exc).__name__, "error": redact_text(exc)}


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
    root = logging.getLogger()
    for handler in root.handlers:
        protect_handler(handler)
