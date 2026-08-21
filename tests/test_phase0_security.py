from __future__ import annotations

import io
import json
import logging
import traceback

from fastapi.testclient import TestClient

from security.redaction import (
    install_logging_redaction,
    protect_handler,
    redact_text,
    sanitize_artifact,
    sanitize_payload,
)
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
    monkeypatch.delenv("CTW_DASHBOARD_AUTH_TOKEN", raising=False)
    app.state.mutation_authorizer = None
    with TestClient(app) as client:
        response = client.post("/operations/run-now")
    assert response.status_code == 403


def test_handlers_created_after_installation_are_redacted(monkeypatch):
    sentinel = "sentinel-future-handler-secret"
    monkeypatch.setenv("EMBEDDING_API_KEY", sentinel)
    install_logging_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.error.phase0-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.error("late handler Authorization: Bearer %s", sentinel)
    assert sentinel not in stream.getvalue()


def test_diagnostics_crash_and_artifacts_never_emit_sentinel(tmp_path, monkeypatch):
    sentinel = "sentinel-artifact-secret"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", sentinel)
    try:
        raise RuntimeError(f"crash token={sentinel}")
    except RuntimeError as exc:
        crash = traceback.format_exc()
        payload = sanitize_payload({"diagnostic": str(exc), "crash": crash, "nested": [sentinel]})
    encoded = json.dumps(payload)
    assert sentinel not in encoded

    source = tmp_path / "diagnostic.txt"
    output = tmp_path / "diagnostic.redacted.txt"
    source.write_text(f"Authorization: Bearer {sentinel}\n", encoding="utf-8")
    sanitize_artifact(source, output)
    assert sentinel in source.read_text(encoding="utf-8")
    assert sentinel not in output.read_text(encoding="utf-8")


def test_known_provider_key_formats_are_redacted():
    google_key = "AIza" + "A" * 35
    github_key = "github_pat_" + "B" * 30
    output = redact_text(f"keys {google_key} {github_key}")
    assert google_key not in output
    assert github_key not in output


def test_redaction_covers_basic_auth_cookies_passwords_and_url_userinfo():
    sentinel = "phase0-credential-sentinel"
    raw = (
        f"Authorization: Basic c2VjcmV0 Cookie: session={sentinel} "
        f"password={sentinel} https://operator:{sentinel}@example.test/run?client_secret={sentinel}"
    )
    output = redact_text(raw)
    assert sentinel not in output
    assert "c2VjcmV0" not in output
    assert "operator" not in output
    assert output.count("[REDACTED]") >= 4
