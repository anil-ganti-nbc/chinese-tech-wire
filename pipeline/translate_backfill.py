"""Deliberate translation backfill for records already stored.

Translation is enrichment: ingestion translates new NEWS titles when a
provider is configured, but any failure leaves the record stored with the
original title and an English column of NULL — never an ingestion failure,
and never a scrape-quality problem. This module repairs those gaps from
stored text only:

  - no re-scraping: everything works from what is already in the database;
  - only MISSING translations are attempted (title_english IS NULL), so the
    pass is resumable and idempotent by construction;
  - the persistent TranslationCache sits under the translator, so an
    identical headline (e.g. the same story syndicated by two outlets)
    never costs a second API call — a re-run of this pass over unchanged
    data is all cache hits;
  - any failure leaves the record untranslated (pending) — nothing is
    destroyed, mutated beyond the enrichment columns, or reclassified;
  - attempted / translated / cached / failed counts are reported so an
    operator knows exactly what happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select

from database.db import get_session
from database.models import Article
from pipeline.translate import Translator, translation_stats

logger = logging.getLogger("ctw.translate_backfill")


@dataclass
class BackfillCounts:
    attempted: int = 0
    translated: int = 0
    cached: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "translated": self.translated,
            "cached": self.cached,
            "failed": self.failed,
            "errors": self.errors[:10],
        }


def _stats_requests() -> int:
    return translation_stats().get("requests", 0)


def _has_provider(translator: Optional[Translator]) -> bool:
    return translator is not None and getattr(translator, "provider_name", "none") != "none"


def _translate_one(translator: Translator, text: str, counts: BackfillCounts) -> Optional[str]:
    """One cache-aware translation, classified into the counts by whether
    the provider was actually contacted (an increment of the global
    request counter means a real API call; a hit means the persistent
    cache served it). Any per-record exception is a failed record — never
    an abort of the pass and never a destructive change."""
    before = _stats_requests()
    try:
        english = translator.translate(text)
    except Exception as exc:  # noqa: BLE001 — one record's failure must not stop the pass
        counts.failed += 1
        counts.errors.append(f"{type(exc).__name__}: {exc}"[:200])
        logger.warning("[BACKFILL] translation failed for a record: %s", exc)
        return None
    if english:
        if _stats_requests() == before:
            counts.cached += 1
        else:
            counts.translated += 1
    else:
        counts.failed += 1
    return english


def backfill_article_translations(
    translator: Translator,
    *,
    limit: int = 200,
    include_summaries: bool = False,
) -> BackfillCounts:
    """Translate title_english (and optionally summary_english) for NEWS
    articles stored without one. Never raises for provider issues; a
    per-record failure leaves that record pending for the next pass."""
    counts = BackfillCounts()
    if not _has_provider(translator):
        logger.info("[BACKFILL] no translation provider configured; nothing attempted")
        return counts
    with get_session() as session:
        rows = session.execute(
            select(Article)
            .where(Article.title_english.is_(None))
            .order_by(Article.discovered_at.desc())
            .limit(limit)
        ).scalars().all()
        for article in rows:
            original = (article.title_original or "").strip()
            if not original:
                continue  # nothing to translate; stays pending, not an error
            counts.attempted += 1
            english = _translate_one(translator, original, counts)
            if not english:
                continue
            article.title_english = english
            if include_summaries and not (article.summary_english or "").strip():
                summary = (article.summary_original or "").strip()
                if summary:
                    summary_en = translator.translate(summary)
                    if summary_en:
                        article.summary_english = summary_en
        if not counts.translated and not counts.cached:
            session.rollback()  # nothing changed: keep the pass read-only
    return counts


def backfill_community_titles(
    translator: Translator,
    *,
    limit: int = 200,
) -> BackfillCounts:
    """Translate missing CommunityThread.title_english. Community titles are
    the ones StoryLead headlines most often anchor on, so an untranslated
    thread title leaks source-language text onto the newsroom until this
    runs."""
    from database.models import CommunityThread

    counts = BackfillCounts()
    if not _has_provider(translator):
        return counts
    with get_session() as session:
        rows = session.execute(
            select(CommunityThread)
            .where(CommunityThread.title_english.is_(None))
            .order_by(CommunityThread.discovered_at.desc())
            .limit(limit)
        ).scalars().all()
        for thread in rows:
            original = (thread.title_original or "").strip()
            if not original:
                continue
            counts.attempted += 1
            english = translator.translate(original)
            if english:
                thread.title_english = english
        if not counts.translated and not counts.cached:
            session.rollback()
    return counts


def translate_missing(
    translator: Optional[Translator],
    *,
    article_limit: int = 200,
    community_limit: int = 200,
) -> dict:
    """Run the full deliberate backfill and return combined counts."""
    articles = backfill_article_translations(translator, limit=article_limit)
    community = backfill_community_titles(translator, limit=community_limit)
    return {
        "articles": articles.as_dict(),
        "community": community.as_dict(),
        "attempted": articles.attempted + community.attempted,
        "translated": articles.translated + community.translated,
        "cached": articles.cached + community.cached,
        "failed": articles.failed + community.failed,
    }
