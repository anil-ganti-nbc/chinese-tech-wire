from __future__ import annotations

import io
import logging

from fastapi.testclient import TestClient

from security.redaction import protect_handler, redact_text
from web.app import app


def test_redaction_covers_urls_headers_webhooks_and_known_keys(monkeypatch):
    sentinel = "sentinel-" + "secret-value"
    monkeypatch.setenv("GEMINI_API_KEY", sentinel)
    raw = (
        f"https://example.test/run?key={sentinel}&safe=yes "
        f"Authorization: Bearer {sentinel} api_key={sentinel} "
        f"https://discord.com/api/webhooks/123/{sentinel}"
    )
    redacted = redact_text(raw)
    assert sentinel not in redacted
    assert "safe=yes" in redacted
    assert "[REDACTED]" in redacted


def test_exception_logging_cannot_emit_sentinel(monkeypatch):
    sentinel = "sentinel-" + "crash-secret"
    monkeypatch.setenv("TRANSLATION_API_KEY", sentinel)
    stream = io.StringIO()
    handler = protect_handler(logging.StreamHandler(stream))
    logger = logging.getLogger("ctw.phase0.redaction.test")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError(f"request failed at https://api.test/run?token={sentinel}")
        except RuntimeError:
            logger.exception("provider request failed Authorization: Bearer %s", sentinel)
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert sentinel not in output
    assert "RuntimeError" in output


def test_unauthenticated_dashboard_mutation_is_rejected(monkeypatch):
    monkeypatch.delenv("CTW_TEST_ALLOW_UNAUTH_MUTATIONS", raising=False)
    monkeypatch.delenv("CTW_DASHBOARD_AUTH_TOKEN", raising=False)
    with TestClient(app) as client:
        response = client.post("/operations/run-now")
    assert response.status_code == 403
