"""Translation + persistent cache tests. Gemini is always mocked — never hits live API."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from database.db import init_db, get_session
from database.models import TranslationCache, Base
from pipeline.translate import (
    GeminiTranslator,
    NoOpTranslator,
    OpenAICompatibleTranslator,
    cache_lookup,
    cache_store,
    get_translator,
    _cache_hash,
    translation_stats,
    _stats,
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Isolate each test on an in-memory / temp SQLite DB."""
    db_url = f"sqlite:///{tmp_path}/test_ctw.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    # Reload settings is hard; instead point get_engine via env and re-init
    from database import db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    # Force settings.database_url
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    # Reset process stats
    _stats["requests"] = 0
    _stats["cache_hits"] = 0
    _stats["failures"] = 0
    yield


def test_noop_returns_none():
    t = NoOpTranslator()
    assert t.translate("英伟达 RTX 5090 曝光") is None


def test_cache_hash_stable():
    h1 = _cache_hash("hello", "zh", "en", "gemini", "gemini-2.0-flash")
    h2 = _cache_hash("hello", "zh", "en", "gemini", "gemini-2.0-flash")
    assert h1 == h2
    h3 = _cache_hash("hello", "zh", "en", "gemini", "other-model")
    assert h1 != h3


def test_cache_store_and_lookup():
    cache_store("测试标题", "Test Title", provider="gemini", model="m1")
    hit = cache_lookup("测试标题", provider="gemini", model="m1")
    assert hit == "Test Title"


def test_cache_survives_new_translator_instance():
    """Simulates app restart: new translator, same DB → cache hit."""
    cache_store("长鑫存储突破", "CXMT breakthrough", provider="gemini", model="g")
    # New instance, no in-memory state beyond DB
    hit = cache_lookup("长鑫存储突破", provider="gemini", model="g")
    assert hit == "CXMT breakthrough"


def test_gemini_parse_success():
    fake_json = {
        "candidates": [
            {"content": {"parts": [{"text": "Nvidia RTX 5090 engineering sample benchmark leaks"}]}}
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_json
    mock_resp.raise_for_status = MagicMock()

    t = GeminiTranslator(api_key="fake-key", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp):
        # Bypass persistent cache for this unit test of raw path
        result = t.translate_raw("英伟达 RTX 5090 工程样机跑分曝光")
    assert result is not None
    assert "RTX 5090" in result
    assert "Nvidia" in result or "nvidia" in result.lower() or "RTX" in result


def test_gemini_key_is_sent_in_header_not_query_string():
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "Translated headline"}]}}]
    }
    mock_resp.raise_for_status = MagicMock()
    t = GeminiTranslator(api_key="sentinel-header-key", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp) as post:
        assert t.translate_raw("测试") == "Translated headline"
    url = post.call_args.args[0]
    assert "sentinel-header-key" not in url
    assert "?key=" not in url
    assert post.call_args.kwargs["headers"] == {"x-goog-api-key": "sentinel-header-key"}


def test_gemini_full_translate_uses_cache_on_second_call():
    fake_json = {
        "candidates": [
            {"content": {"parts": [{"text": "Nvidia RTX 5090 engineering sample spotted"}]}}
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_json
    mock_resp.raise_for_status = MagicMock()

    t = GeminiTranslator(api_key="fake-key", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp) as post:
        r1 = t.translate("英伟达 RTX 5090 工程样机曝光")
        assert r1 is not None
        assert post.call_count == 1

        r2 = t.translate("英伟达 RTX 5090 工程样机曝光")
        assert r2 == r1
        # Second call must NOT hit the API
        assert post.call_count == 1
        assert translation_stats()["cache_hits"] >= 1


def test_gemini_missing_key():
    from pipeline.translate import TranslateSkip
    t = GeminiTranslator(api_key="", model="gemini-2.0-flash")
    with pytest.raises(TranslateSkip):
        t.translate_raw("测试")
    assert t.translate("测试") is None


def test_gemini_rate_limit():
    from pipeline.translate import TranslateSkip
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    t = GeminiTranslator(api_key="fake", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp):
        with pytest.raises(TranslateSkip):
            t.translate_raw("测试标题")
    assert t._backoff_until > time.monotonic()
    # Outer translate() must still return None, not raise
    assert t.translate("另一标题") is None


def test_gemini_auth_failure():
    from pipeline.translate import TranslateSkip
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    t = GeminiTranslator(api_key="bad", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp):
        with pytest.raises(TranslateSkip):
            t.translate_raw("测试")
        assert t.translate("测试") is None


def test_gemini_malformed_response():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"candidates": []}
    mock_resp.raise_for_status = MagicMock()
    t = GeminiTranslator(api_key="fake", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp):
        assert t.translate_raw("测试") is None


def test_gemini_empty_response():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": ""}]}}]
    }
    mock_resp.raise_for_status = MagicMock()
    t = GeminiTranslator(api_key="fake", model="gemini-2.0-flash")
    with patch.object(t._client, "post", return_value=mock_resp):
        assert t.translate_raw("测试") is None


def test_gemini_timeout():
    t = GeminiTranslator(api_key="fake", model="gemini-2.0-flash", max_retries=0)
    with patch.object(t._client, "post", side_effect=httpx.TimeoutException("timeout")):
        assert t.translate_raw("测试") is None


def test_ingestion_continues_after_translation_failure():
    """translate() must return None on failure, never raise."""
    t = GeminiTranslator(api_key="fake", model="gemini-2.0-flash", max_retries=0)
    with patch.object(t._client, "post", side_effect=RuntimeError("boom")):
        # translate() catches and returns None
        assert t.translate("任意标题") is None


def test_existing_title_english_avoids_api(monkeypatch):
    """If article already has title_english, pipeline should not call translate.
    This is enforced in main.py; here we just verify cache short-circuit."""
    cache_store("已有翻译", "Already translated", provider="gemini", model="m")
    t = GeminiTranslator(api_key="fake", model="m")
    with patch.object(t, "translate_raw") as raw:
        result = t.translate("已有翻译")
        assert result == "Already translated"
        raw.assert_not_called()


def test_get_translator_gemini_without_key(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg.settings, "translation_provider", "gemini")
    monkeypatch.setattr(cfg.settings, "gemini_api_key", "")
    t = get_translator()
    assert isinstance(t, NoOpTranslator)


# ---------------------------------------------------------------------------
# OpenRouter provider identity (fleet QoL pass): never cached under OpenAI
# ---------------------------------------------------------------------------


def test_openrouter_translator_identifies_as_openrouter(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.translation_provider", "openrouter")
    monkeypatch.setattr("config.settings.openrouter_api_key", "sk-test")
    monkeypatch.setattr("config.settings.openrouter_base_url",
                        "https://openrouter.ai/api/v1")
    monkeypatch.setattr("config.settings.openrouter_model",
                        "google/gemini-2.5-flash")

    t = get_translator()
    assert t.provider_name == "openrouter"
    assert isinstance(t, OpenAICompatibleTranslator)   # one HTTP implementation
    assert t.base_url == "https://openrouter.ai/api/v1"
    assert t.model_name == "google/gemini-2.5-flash"
    assert t.key_env_name == "OPENROUTER_API_KEY"


def test_openrouter_cache_rows_are_stored_and_served_under_openrouter_identity(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("config.settings.translation_provider", "openrouter")
    monkeypatch.setattr("config.settings.openrouter_api_key", "sk-test")
    t = get_translator()
    assert t.provider_name == "openrouter"

    # a provider call stores its translation under OPENROUTER identity
    fake_response = type("R", (), {})()
    fake_response.status_code = 200
    fake_response.json = lambda: {"choices": [{"message": {"content": "RTX 5090 spotted"}}]}
    fake_response.raise_for_status = lambda: None
    with patch.object(t._client, "post", return_value=fake_response):
        assert t.translate("英伟达 RTX 5090 曝光") == "RTX 5090 spotted"

    from sqlalchemy import select as _select

    from database.models import TranslationCache as _TC

    with get_session() as session:
        cached = session.execute(
            _select(_TC).where(_TC.provider == "openrouter")).scalars().all()
        assert cached, "the OpenRouter translation was not stored under openrouter identity"
        assert all(r.provider == "openrouter" and r.model == "google/gemini-2.5-flash"
                   for r in cached)
        assert session.execute(
            _select(_TC).where(_TC.provider == "openai")).scalars().first() is None

    # a fresh OpenRouter translator is SERVED by that cached row under
    # openrouter identity, while an OpenAI-identity lookup would miss
    from pipeline.translate import cache_lookup

    assert cache_lookup("英伟达 RTX 5090 曝光", provider="openrouter",
                        model="google/gemini-2.5-flash") == "RTX 5090 spotted"
    assert cache_lookup("英伟达 RTX 5090 曝光", provider="openai",
                        model="gpt-4o-mini") is None
