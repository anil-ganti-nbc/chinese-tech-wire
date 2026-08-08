"""Normalize RawArticle into the canonical schema ready for DB."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from sources.base import REGION_TIMEZONES, RawArticle

logger = logging.getLogger(__name__)


def canonicalize_url(url: str) -> str:
    """Strip tracking params, fragments, normalize scheme/host."""
    try:
        p = urlparse(url)
        path = p.path.rstrip("/")
        clean = urlunparse((p.scheme or "https", p.netloc.lower(), path, "", "", ""))
        return clean
    except Exception:
        return url


def _ensure_utc(published: Optional[datetime], raw: RawArticle) -> Optional[datetime]:
    """Ensure published_at is timezone-aware UTC.

    Adapters should already return UTC-aware datetimes.  If a naive value
    slips through, interpret it using region/timezone metadata — never as UTC.
    """
    if published is None:
        return None
    if published.tzinfo is not None:
        return published.astimezone(timezone.utc)

    # Unexpected naive timestamp — recover from metadata
    meta = raw.raw_metadata or {}
    tz_name = meta.get("timezone")
    if not tz_name:
        region = meta.get("region") or "CN"
        tz_name = REGION_TIMEZONES.get(region, "Asia/Shanghai")
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    logger.warning(
        "[normalize] naive published_at for %s/%s — interpreting as %s",
        raw.source,
        raw.source_article_id,
        tz_name,
    )
    return published.replace(tzinfo=zone).astimezone(timezone.utc)


def normalize_raw(raw: RawArticle, discovered_at: Optional[datetime] = None) -> dict:
    """Produce a dict matching Article model fields (without id)."""
    now = discovered_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    published = _ensure_utc(raw.published_at, raw)

    return {
        "source": raw.source,
        "source_article_id": str(raw.source_article_id),
        "title_original": raw.title_original.strip(),
        "title_english": None,
        "url": raw.url,
        "canonical_url": canonicalize_url(raw.canonical_url or raw.url),
        "published_at": published,
        "discovered_at": now,
        "category": raw.category,
        "summary_original": (raw.summary_original or "").strip() or None,
        "summary_english": None,
        "source_type": "UNKNOWN",
        "upstream_source": None,
        "upstream_url": None,
        "rumor_flag": False,
        "rumor_confidence": 0.0,
        "novelty_score": 50.0,
        "relevance_score": 50.0,
        "priority_score": 50.0,
        "duplicate_group_id": None,
        "notified": False,
        "raw_metadata": raw.raw_metadata or {},
    }
