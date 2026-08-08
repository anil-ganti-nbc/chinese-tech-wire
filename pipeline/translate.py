"""Translation provider abstraction + persistent SQLite cache.

Providers:
  - NoOpTranslator
  - OpenAICompatibleTranslator
  - GeminiTranslator  (official Generative Language REST API via httpx)

Caching sits above providers so every backend benefits.
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
from sqlalchemy import select

from config import settings, yaml_config
from database.db import get_session
from database.models import TranslationCache

logger = logging.getLogger(__name__)

_stats = {
    "requests": 0,
    "cache_hits": 0,
    "failures": 0,
}

TECH_TRANSLATE_SYSTEM = (
    "Translate the following Chinese technology-news headline into concise, natural English. "
    "Preserve product names, model numbers, technical terminology, units, codenames and uncertainty "
    "(reportedly, rumored, allegedly, leaked, expected, tipped). "
    "Do not add facts, commentary or interpretation. "
    "Return only the translated headline."
)


def translation_stats() -> Dict[str, int]:
    return dict(_stats)


def log_translation_stats() -> None:
    provider = (settings.translation_provider or "none").lower()
    model = _active_model_label()
    logger.info(
        "[TRANSLATE] provider=%s model=%s | requests=%d cache_hits=%d failures=%d",
        provider,
        model,
        _stats["requests"],
        _stats["cache_hits"],
        _stats["failures"],
    )


def _active_model_label() -> str:
    p = (settings.translation_provider or "none").lower()
    if p == "gemini":
        return getattr(settings, "gemini_model", None) or "gemini-2.0-flash"
    if p in ("openai", "openai-compatible", "custom"):
        return settings.translation_model or "gpt-4o-mini"
    return "—"


def _cache_hash(
    text: str,
    source_lang: str,
    target_lang: str,
    provider: str,
    model: str,
) -> str:
    raw = f"{provider}|{model}|{source_lang}|{target_lang}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_lookup(
    text: str,
    source_lang: str = "zh",
    target_lang: str = "en",
    provider: str = "",
    model: str = "",
) -> Optional[str]:
    """Return cached translation or None. Never raises."""
    if not text:
        return None
    h = _cache_hash(text, source_lang, target_lang, provider, model)
    try:
        with get_session() as session:
            row = session.execute(
                select(TranslationCache).where(TranslationCache.source_hash == h)
            ).scalar_one_or_none()
            if row and row.translation:
                _stats["cache_hits"] += 1
                return row.translation
    except Exception as e:
        logger.debug("[TRANSLATE] cache lookup failed: %s", e)
    return None


def cache_store(
    text: str,
    translation: str,
    source_lang: str = "zh",
    target_lang: str = "en",
    provider: str = "",
    model: str = "",
) -> None:
    """Persist a translation. Never raises."""
    if not text or not translation:
        return
    h = _cache_hash(text, source_lang, target_lang, provider, model)
    now = datetime.now(timezone.utc)
    try:
        with get_session() as session:
            existing = session.execute(
                select(TranslationCache).where(TranslationCache.source_hash == h)
            ).scalar_one_or_none()
            if existing:
                existing.translation = translation
                existing.updated_at = now
            else:
                session.add(
                    TranslationCache(
                        source_text=text,
                        source_language=source_lang,
                        target_language=target_lang,
                        provider=provider,
                        model=model or "",
                        translation=translation,
                        source_hash=h,
                        created_at=now,
                        updated_at=now,
                    )
                )
    except Exception as e:
        logger.debug("[TRANSLATE] cache store failed: %s", e)


class TranslateSkip(Exception):
    """Soft skip already logged by the provider (rate limit, auth, backoff)."""


class Translator(ABC):
    provider_name: str = "base"
    model_name: str = ""

    @abstractmethod
    def translate_raw(
        self, text: str, source_language: str = "zh", target_language: str = "en"
    ) -> Optional[str]:
        ...

    def translate(
        self, text: str, source_language: str = "zh", target_language: str = "en"
    ) -> Optional[str]:
        """Cache-aware translate. Safe: never raises to caller."""
        if not text or not text.strip():
            return None
        text = text.strip()

        cached = cache_lookup(
            text, source_language, target_language, self.provider_name, self.model_name
        )
        if cached:
            return cached

        try:
            _stats["requests"] += 1
            result = self.translate_raw(text, source_language, target_language)
        except TranslateSkip:
            # Provider already logged (rate limit / auth / backoff) — no extra noise
            _stats["failures"] += 1
            return None
        except Exception as e:
            _stats["failures"] += 1
            logger.warning("[TRANSLATE] %s error: %s", self.provider_name, e)
            return None

        if result is None:
            # Soft None without exception — treat as failure, avoid "empty" spam
            _stats["failures"] += 1
            return None

        if not result.strip():
            _stats["failures"] += 1
            logger.warning("[TRANSLATE] %s returned empty response", self.provider_name)
            return None

        result = result.strip().strip('"').strip("'")
        if result == text:
            _stats["failures"] += 1
            logger.warning("[TRANSLATE] %s returned unchanged text", self.provider_name)
            return None

        cache_store(
            text, result, source_language, target_language, self.provider_name, self.model_name
        )
        return result


class NoOpTranslator(Translator):
    provider_name = "none"
    model_name = ""

    def translate_raw(
        self, text: str, source_language: str = "zh", target_language: str = "en"
    ) -> Optional[str]:
        return None

    def translate(
        self, text: str, source_language: str = "zh", target_language: str = "en"
    ) -> Optional[str]:
        return None


class OpenAICompatibleTranslator(Translator):
    provider_name = "openai"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def translate_raw(
        self, text: str, source_language: str = "zh", target_language: str = "en"
    ) -> Optional[str]:
        if not self.api_key:
            return None
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": TECH_TRANSLATE_SYSTEM},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,
            "max_tokens": 256,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 429:
            logger.warning("[TRANSLATE] OpenAI rate limited; storing article without translation")
            raise RuntimeError("rate_limited")
        if resp.status_code in (401, 403):
            logger.warning("[TRANSLATE] OpenAI auth failed; check TRANSLATION_API_KEY")
            raise RuntimeError("auth_failed")
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


class GeminiTranslator(Translator):
    """Official Gemini Generative Language API (REST) via httpx."""

    provider_name = "gemini"
    API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        timeout: float = 20.0,
        max_retries: int = 2,
    ):
        self.api_key = api_key
        self.model_name = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)
        self._backoff_until = 0.0

    def translate_raw(
        self, text: str, source_language: str = "zh", target_language: str = "en"
    ) -> Optional[str]:
        if not self.api_key:
            logger.warning("[TRANSLATE] Gemini: no GEMINI_API_KEY; skipping")
            raise TranslateSkip("no_api_key")

        now = time.monotonic()
        if now < self._backoff_until:
            # Log once per backoff window, not once per article
            if not getattr(self, "_backoff_logged", False):
                logger.warning(
                    "[TRANSLATE] Gemini rate limited (backoff %.0fs remaining); "
                    "skipping translations until cooldown ends",
                    self._backoff_until - now,
                )
                self._backoff_logged = True
            raise TranslateSkip("rate_limited_backoff")

        url = (
            f"{self.API_BASE}/models/{self.model_name}:generateContent"
            f"?key={self.api_key}"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"{TECH_TRANSLATE_SYSTEM}\n\n{text}"}],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 256,
            },
        }

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(url, json=payload)
                if resp.status_code == 429:
                    self._backoff_until = time.monotonic() + 60.0
                    self._backoff_logged = False  # allow one log when next backoff hits
                    logger.warning(
                        "[TRANSLATE] Gemini rate limited (HTTP 429); "
                        "backing off 60s — articles stored without translation"
                    )
                    raise TranslateSkip("rate_limited")
                if resp.status_code in (401, 403):
                    logger.warning(
                        "[TRANSLATE] Gemini auth failed; check GEMINI_API_KEY (not retrying)"
                    )
                    raise TranslateSkip("auth_failed")
                if resp.status_code >= 500:
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    logger.warning(
                        "[TRANSLATE] Gemini server error %s; storing without translation",
                        resp.status_code,
                    )
                    return None
                resp.raise_for_status()
                data = resp.json()

                candidates = data.get("candidates") or []
                if not candidates:
                    logger.warning("[TRANSLATE] Gemini malformed response (no candidates)")
                    return None
                parts = (candidates[0].get("content") or {}).get("parts") or []
                if not parts:
                    logger.warning("[TRANSLATE] Gemini empty parts")
                    return None
                out = (parts[0].get("text") or "").strip()
                if not out:
                    logger.warning("[TRANSLATE] Gemini empty response")
                    return None
                return out
            except TranslateSkip:
                raise  # rate limit / auth — do not swallow
            except httpx.TimeoutException:
                last_err = RuntimeError("timeout")
                logger.warning("[TRANSLATE] Gemini timeout (attempt %d)", attempt + 1)
                if attempt < self.max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None
            except httpx.HTTPError as e:
                last_err = e
                logger.warning("[TRANSLATE] Gemini HTTP error: %s", e)
                if attempt < self.max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None
            except Exception as e:
                logger.warning("[TRANSLATE] Gemini unexpected error: %s", e)
                return None
        if last_err:
            logger.warning("[TRANSLATE] Gemini failed after retries: %s", last_err)
        return None


def get_translator() -> Translator:
    provider = (settings.translation_provider or "none").lower().strip()
    timeout = float(yaml_config.get("translation", {}).get("timeout_seconds", 20))
    max_retries = int(yaml_config.get("translation", {}).get("max_retries", 2))

    if provider in ("none", "disabled", ""):
        return NoOpTranslator()

    if provider == "gemini":
        key = (getattr(settings, "gemini_api_key", None) or "").strip()
        model = (getattr(settings, "gemini_model", None) or "gemini-2.0-flash").strip()
        if not key:
            logger.warning(
                "[TRANSLATE] TRANSLATION_PROVIDER=gemini but no GEMINI_API_KEY; using no-op"
            )
            return NoOpTranslator()
        logger.info("[TRANSLATE] provider=gemini model=%s", model)
        return GeminiTranslator(
            api_key=key, model=model, timeout=timeout, max_retries=max_retries
        )

    if provider in ("openai", "openai-compatible", "custom"):
        key = (settings.translation_api_key or "").strip()
        if not key:
            logger.warning(
                "[TRANSLATE] OpenAI provider set but no TRANSLATION_API_KEY; using no-op"
            )
            return NoOpTranslator()
        model = settings.translation_model or "gpt-4o-mini"
        logger.info("[TRANSLATE] provider=openai model=%s", model)
        return OpenAICompatibleTranslator(
            api_key=key,
            base_url=settings.translation_base_url or "https://api.openai.com/v1",
            model=model,
            timeout=timeout,
        )

    logger.warning("[TRANSLATE] Unknown TRANSLATION_PROVIDER=%s; using no-op", provider)
    return NoOpTranslator()
