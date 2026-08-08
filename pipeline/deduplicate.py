"""Layered deduplication and story clustering.

Levels:
  1. Exact canonical URL
  2. High fuzzy title similarity (RapidFuzz)
  3. Strong entity overlap + moderate title similarity

Conservative by design: false merges are worse than missed merges.
Never deletes articles — only clusters them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import yaml_config
from database.models import Article, StoryCluster
from pipeline.entities import Entity, extract_entities

logger = logging.getLogger(__name__)


def normalize_title_for_sim(title: str) -> str:
    t = title or ""
    for noise in ["【", "】", "[", "]", "！", "!", "？", "?", "…", "...", "：", ":", "（", "）", "(", ")"]:
        t = t.replace(noise, " ")
    return " ".join(t.split())


def title_similarity(a: str, b: str) -> float:
    return fuzz.token_set_ratio(normalize_title_for_sim(a), normalize_title_for_sim(b)) / 100.0


def entity_overlap_count(ents_a: List[Entity], ents_b: List[Entity]) -> int:
    set_a = {(e.normalized or e.name).lower() for e in ents_a}
    set_b = {(e.normalized or e.name).lower() for e in ents_b}
    return len(set_a & set_b)


def find_matching_cluster(
    session: Session,
    title: str,
    canonical_url: str,
    entities: List[Entity],
    published_at: Optional[datetime],
    source: str,
) -> Optional[StoryCluster]:
    """Return an existing cluster if this article belongs to one."""
    cfg = yaml_config.get("dedup", {})
    sim_thresh = float(cfg.get("title_similarity_threshold", 0.78))  # conservative
    soft_thresh = float(cfg.get("title_similarity_soft", 0.62))
    window_h = int(cfg.get("time_window_hours", 48))
    min_ents = int(cfg.get("entity_overlap_min", 2))

    # Level 1 — exact canonical URL
    if canonical_url:
        existing = session.execute(
            select(Article).where(Article.canonical_url == canonical_url).limit(1)
        ).scalar_one_or_none()
        if existing and existing.duplicate_group_id:
            return session.get(StoryCluster, existing.duplicate_group_id)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_h)
    recent = session.execute(
        select(Article)
        .where(Article.discovered_at >= cutoff)
        .order_by(Article.discovered_at.desc())
        .limit(300)
    ).scalars().all()

    best_cluster: Optional[StoryCluster] = None
    best_score = 0.0
    best_reason = ""

    for art in recent:
        # Same source + identical title → already handled by unique constraint path
        if art.source == source and art.title_original == title:
            if art.duplicate_group_id:
                return session.get(StoryCluster, art.duplicate_group_id)
            continue

        sim = title_similarity(title, art.title_original)

        # Level 2 — strong title match alone
        if sim >= sim_thresh:
            if sim > best_score and art.duplicate_group_id:
                best_score = sim
                best_cluster = session.get(StoryCluster, art.duplicate_group_id)
                best_reason = f"title_sim={sim:.2f}"
            continue

        # Level 3 — moderate title + strong entity overlap
        if sim >= soft_thresh and entities and min_ents > 0:
            other_ents = extract_entities(art.title_original or "")
            overlap = entity_overlap_count(entities, other_ents)
            if overlap >= min_ents:
                combined = sim + 0.05 * overlap
                if combined > best_score and art.duplicate_group_id:
                    best_score = combined
                    best_cluster = session.get(StoryCluster, art.duplicate_group_id)
                    best_reason = f"title_sim={sim:.2f}+ents={overlap}"

    if best_cluster and best_score > 0:
        logger.info(
            "[DEDUP] Matched cluster %s (%s)", best_cluster.id, best_reason
        )
        return best_cluster
    return None


def get_or_create_cluster(
    session: Session,
    article: Article,
    entities: Optional[List[Entity]] = None,
) -> StoryCluster:
    """Attach article to existing or new cluster."""
    if article.duplicate_group_id:
        cl = session.get(StoryCluster, article.duplicate_group_id)
        if cl:
            return cl

    ents = entities or extract_entities(article.title_original or "")
    cluster = find_matching_cluster(
        session,
        article.title_original,
        article.canonical_url or article.url,
        ents,
        article.published_at,
        article.source,
    )
    now = datetime.now(timezone.utc)
    if cluster is None:
        cluster = StoryCluster(
            first_seen_source=article.source,
            first_seen_at=article.discovered_at or now,
            representative_title=article.title_original,
            created_at=now,
            updated_at=now,
        )
        session.add(cluster)
        session.flush()
        logger.info("[DEDUP] New cluster %s for %s", cluster.id, article.source)
    else:
        cluster.updated_at = now
        logger.info("[DEDUP] Joined existing cluster %s", cluster.id)

    article.duplicate_group_id = cluster.id
    return cluster
