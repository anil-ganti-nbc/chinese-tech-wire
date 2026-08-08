"""Community thread ingestion: score → store → cluster → corroborate → notify."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select

from config import yaml_config
from community_sources.base import RawThread
from database.db import get_session
from database.models import (
    Article,
    AuthorProfile,
    CommunityPost,
    CommunityThread,
    SourceRun,
    StoryCluster,
    ThreadMetrics,
)
from pipeline.community_score import (
    compute_corroboration,
    compute_priority,
    score_thread,
    score_velocity,
    score_velocity_longitudinal,
)
from pipeline.deduplicate import title_similarity
from pipeline.notify import send_discord

logger = logging.getLogger(__name__)

MEDIA_SOURCES: Set[str] = {
    "ithome", "mydrivers", "expreview", "zol", "jiwei",
    "benchlife", "hkepc", "technews", "xfastest",
}
COMMUNITY_PLATFORMS: Set[str] = {"chiphell", "mobile01", "ptt", "coolaler"}


def _hours_old(created: Optional[datetime]) -> float:
    if not created:
        return 2.0
    now = datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max((now - created).total_seconds() / 3600.0, 0.25)


def _load_metric_snapshots(session, thread_db_id: int) -> list:
    rows = session.execute(
        select(ThreadMetrics)
        .where(ThreadMetrics.thread_db_id == thread_db_id)
        .order_by(ThreadMetrics.observed_at.asc())
    ).scalars().all()
    return [(r.observed_at, r.reply_count, r.view_count) for r in rows]


def _recompute_priority(ct: CommunityThread) -> float:
    return compute_priority(
        ct.relevance_score,
        ct.novelty_score,
        ct.evidence_score,
        ct.firsthand_score,
        ct.velocity_score,
        ct.community_score,
        ct.corroboration_score,
        ct.signal_type,
    )


def _apply_corroboration(session, ct: CommunityThread) -> None:
    """After clustering, look for independent peer community signals."""
    if not ct.story_cluster_id:
        return
    peers_rows = session.execute(
        select(CommunityThread).where(
            CommunityThread.story_cluster_id == ct.story_cluster_id,
            CommunityThread.id != ct.id,
        )
    ).scalars().all()
    peers = []
    for p in peers_rows:
        urls = []
        if p.external_urls and isinstance(p.external_urls, dict):
            urls = p.external_urls.get("urls") or []
        peers.append(
            {
                "platform": p.platform,
                "title": p.title_original,
                "urls": urls,
                "signal_type": p.signal_type,
            }
        )
    this_urls = []
    if ct.external_urls and isinstance(ct.external_urls, dict):
        this_urls = ct.external_urls.get("urls") or []
    score = compute_corroboration(
        ct.platform, ct.title_original, this_urls, ct.signal_type, peers
    )
    if score > (ct.corroboration_score or 0):
        ct.corroboration_score = score
        ct.priority_score = _recompute_priority(ct)
        # also lightly boost peers that are independent
        for p in peers_rows:
            if p.signal_type == "REPOST":
                continue
            # mutual: if this is independent of p
            p_urls = []
            if p.external_urls and isinstance(p.external_urls, dict):
                p_urls = p.external_urls.get("urls") or []
            p_score = compute_corroboration(
                p.platform,
                p.title_original,
                p_urls,
                p.signal_type,
                [
                    {
                        "platform": ct.platform,
                        "title": ct.title_original,
                        "urls": this_urls,
                        "signal_type": ct.signal_type,
                    }
                ],
            )
            if p_score > (p.corroboration_score or 0):
                p.corroboration_score = p_score
                p.priority_score = _recompute_priority(p)


def _store_selected_posts(session, ct: CommunityThread, raw: RawThread) -> int:
    """Store OP always; selected replies only up to max_selected_replies."""
    max_replies = int(yaml_config.get("community", {}).get("max_selected_replies", 20))
    stored = 0
    for post in raw.posts or []:
        is_op = bool(post.get("is_op"))
        if not is_op and stored >= max_replies:
            continue
        if not is_op and not post.get("is_selected_reply", True):
            continue
        pid = str(post.get("post_id") or f"{raw.thread_id}-{stored}")
        exists = session.execute(
            select(CommunityPost).where(
                CommunityPost.platform == raw.platform,
                CommunityPost.post_id == pid,
            )
        ).scalar_one_or_none()
        if exists:
            continue
        session.add(
            CommunityPost(
                thread_db_id=ct.id,
                platform=raw.platform,
                post_id=pid,
                author_name=post.get("author_name"),
                author_id=post.get("author_id"),
                text_original=(post.get("text_original") or "")[:5000] or None,
                created_at=raw.created_at if is_op else None,
                image_count=int(post.get("image_count") or 0),
                attachment_count=int(post.get("attachment_count") or 0),
                external_urls={"urls": post.get("external_urls") or []},
                is_op=is_op,
                is_selected_reply=not is_op,
            )
        )
        if not is_op:
            stored += 1
    return stored


def ingest_raw_thread(
    raw: RawThread,
    dry_run: bool = False,
    enrich: bool = True,
    source_adapter=None,
) -> Optional[CommunityThread]:
    """Score, store, cluster, corroborate one community thread. Never raises."""
    try:
        scores = score_thread(raw, hours_old=_hours_old(raw.created_at))
        now = datetime.now(timezone.utc)

        with get_session() as session:
            existing = session.execute(
                select(CommunityThread).where(
                    CommunityThread.platform == raw.platform,
                    CommunityThread.thread_id == raw.thread_id,
                )
            ).scalar_one_or_none()

            if existing:
                existing.reply_count = max(existing.reply_count, raw.reply_count)
                existing.view_count = max(existing.view_count, raw.view_count)
                existing.updated_at = now
                if raw.op_text_original and not existing.op_text_original:
                    existing.op_text_original = raw.op_text_original[:5000]
                if raw.image_count:
                    existing.image_count = max(existing.image_count, raw.image_count)
                session.add(
                    ThreadMetrics(
                        thread_db_id=existing.id,
                        observed_at=now,
                        reply_count=existing.reply_count,
                        view_count=existing.view_count,
                    )
                )
                session.flush()
                # Longitudinal velocity
                snaps = _load_metric_snapshots(session, existing.id)
                if len(snaps) >= 2:
                    existing.velocity_score = score_velocity_longitudinal(snaps)
                else:
                    existing.velocity_score = score_velocity(
                        existing.reply_count,
                        existing.view_count,
                        _hours_old(existing.created_at),
                    )
                existing.priority_score = _recompute_priority(existing)
                _apply_corroboration(session, existing)
                if raw.posts:
                    _store_selected_posts(session, existing, raw)
                return existing

            ct = CommunityThread(
                platform=raw.platform,
                region=raw.region,
                language_variant=raw.language_variant,
                thread_id=raw.thread_id,
                url=raw.url,
                canonical_url=raw.canonical_url or raw.url,
                title_original=raw.title_original,
                author_name=raw.author_name,
                author_id=raw.author_id,
                created_at=raw.created_at,
                updated_at=raw.updated_at or now,
                discovered_at=now,
                category=raw.category,
                board=raw.board,
                op_text_original=(raw.op_text_original or "")[:5000] or None,
                reply_count=raw.reply_count,
                view_count=raw.view_count,
                image_count=raw.image_count,
                attachment_count=raw.attachment_count,
                external_urls={"urls": scores.get("external_urls") or []},
                signal_type=scores["signal_type"],
                signal_confidence=scores["signal_confidence"],
                firsthand_score=scores["firsthand_score"],
                evidence_score=scores["evidence_score"],
                community_score=scores["community_score"],
                velocity_score=scores["velocity_score"],
                relevance_score=scores["relevance_score"],
                novelty_score=scores["novelty_score"],
                corroboration_score=scores["corroboration_score"],
                priority_score=scores["priority_score"],
                first_signal_at=raw.created_at or now,
                notified=False,
                raw_metadata=raw.raw_metadata or {},
            )
            session.add(ct)
            session.flush()

            if raw.op_text_original or raw.posts:
                _store_selected_posts(session, ct, raw)
            elif raw.op_text_original is None and not raw.posts:
                # Store a minimal OP placeholder only if we later enrich
                pass

            if raw.author_id or raw.author_name:
                aid = raw.author_id or raw.author_name or "unknown"
                ap = session.execute(
                    select(AuthorProfile).where(
                        AuthorProfile.platform == raw.platform,
                        AuthorProfile.author_id == aid,
                    )
                ).scalar_one_or_none()
                if not ap:
                    session.add(
                        AuthorProfile(
                            platform=raw.platform,
                            author_id=aid,
                            author_name=raw.author_name,
                            first_seen_at=now,
                            last_seen_at=now,
                            claim_count=1,
                        )
                    )
                else:
                    ap.last_seen_at = now
                    ap.claim_count = (ap.claim_count or 0) + 1

            session.add(
                ThreadMetrics(
                    thread_db_id=ct.id,
                    observed_at=now,
                    reply_count=raw.reply_count,
                    view_count=raw.view_count,
                )
            )

            cluster = _attach_cluster(session, ct)
            ct.story_cluster_id = cluster.id if cluster else None
            _update_cluster_chronology(session, cluster)
            _apply_corroboration(session, ct)

            # Enrich high-value threads with full OP / selective replies
            enrich_thresh = float(
                yaml_config.get("community", {}).get("enrich_threshold", 55)
            )
            if (
                enrich
                and source_adapter is not None
                and ct.priority_score >= enrich_thresh
                and not ct.op_text_original
            ):
                try:
                    full = source_adapter.fetch_thread(
                        ct.thread_id, url=ct.canonical_url or ct.url
                    )
                    if full and full.op_text_original:
                        ct.op_text_original = full.op_text_original[:5000]
                        ct.author_name = ct.author_name or full.author_name
                        ct.image_count = max(ct.image_count, full.image_count)
                        ct.attachment_count = max(
                            ct.attachment_count, full.attachment_count
                        )
                        if full.external_urls:
                            ct.external_urls = {"urls": full.external_urls}
                        # Re-score with OP text
                        rescored = score_thread(full, hours_old=_hours_old(full.created_at))
                        ct.signal_type = rescored["signal_type"]
                        ct.evidence_score = rescored["evidence_score"]
                        ct.firsthand_score = rescored["firsthand_score"]
                        ct.relevance_score = rescored["relevance_score"]
                        ct.novelty_score = rescored["novelty_score"]
                        ct.priority_score = rescored["priority_score"]
                        _store_selected_posts(session, ct, full)
                        _apply_corroboration(session, ct)
                except TypeError:
                    # adapters with fetch_thread(thread_id) only
                    try:
                        full = source_adapter.fetch_thread(ct.thread_id)
                        if full and full.op_text_original:
                            ct.op_text_original = full.op_text_original[:5000]
                            _store_selected_posts(session, ct, full)
                    except Exception as e:
                        logger.debug("[%s] enrich skip: %s", ct.platform, e)
                except Exception as e:
                    logger.debug("[%s] enrich failed: %s", ct.platform, e)

            thresh = float(yaml_config.get("community", {}).get("notify_threshold", 75))
            high = float(
                yaml_config.get("community", {}).get("high_priority_threshold", 90)
            )
            if ct.priority_score >= thresh and not ct.notified:
                try:
                    _notify_community(
                        ct, scores, is_high=ct.priority_score >= high, dry_run=dry_run
                    )
                    if not dry_run:
                        ct.notified = True
                except Exception as e:
                    logger.error("[COMMUNITY] notify failed (ignored): %s", e)

            logger.info(
                "[COMMUNITY] %s/%s P=%.0f sig=%s cor=%.0f | %s",
                ct.platform,
                ct.thread_id,
                ct.priority_score,
                ct.signal_type,
                ct.corroboration_score,
                ct.title_original[:50],
            )
            return ct
    except Exception as e:
        logger.error("[COMMUNITY] ingest failed: %s", e)
        return None


def _attach_cluster(session, ct: CommunityThread) -> Optional[StoryCluster]:
    cfg = yaml_config.get("dedup", {})
    sim_thresh = float(cfg.get("title_similarity_threshold", 0.78))
    soft_thresh = float(cfg.get("title_similarity_soft", 0.62))
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=int(cfg.get("time_window_hours", 48))
    )

    recent_arts = (
        session.execute(
            select(Article)
            .where(Article.discovered_at >= cutoff)
            .order_by(Article.discovered_at.desc())
            .limit(150)
        )
        .scalars()
        .all()
    )

    best_cluster = None
    best_sim = 0.0
    for art in recent_arts:
        sim = title_similarity(ct.title_original, art.title_original)
        if sim >= sim_thresh and sim > best_sim and art.duplicate_group_id:
            best_sim = sim
            best_cluster = session.get(StoryCluster, art.duplicate_group_id)

    recent_ct = (
        session.execute(
            select(CommunityThread)
            .where(CommunityThread.discovered_at >= cutoff)
            .where(CommunityThread.id != ct.id)
            .order_by(CommunityThread.discovered_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    for other in recent_ct:
        sim = title_similarity(ct.title_original, other.title_original)
        # community-community: allow soft threshold for corroboration candidates
        thresh = soft_thresh if other.platform != ct.platform else sim_thresh
        if sim >= thresh and sim > best_sim and other.story_cluster_id:
            best_sim = sim
            best_cluster = session.get(StoryCluster, other.story_cluster_id)

    now = datetime.now(timezone.utc)
    if best_cluster is None:
        best_cluster = StoryCluster(
            first_seen_source=ct.platform,
            first_seen_at=ct.created_at or ct.discovered_at or now,
            representative_title=ct.title_original,
            created_at=now,
            updated_at=now,
            first_signal_source=ct.platform,
            first_signal_at=ct.created_at or ct.discovered_at or now,
        )
        session.add(best_cluster)
        session.flush()
    else:
        best_cluster.updated_at = now
    return best_cluster


def _update_cluster_chronology(session, cluster: Optional[StoryCluster]) -> None:
    if not cluster:
        return
    cts = (
        session.execute(
            select(CommunityThread).where(
                CommunityThread.story_cluster_id == cluster.id
            )
        )
        .scalars()
        .all()
    )
    if cts:
        first = min(
            cts,
            key=lambda t: t.first_signal_at or t.created_at or t.discovered_at,
        )
        cluster.first_signal_source = first.platform
        cluster.first_signal_at = (
            first.first_signal_at or first.created_at or first.discovered_at
        )

    arts = (
        session.execute(
            select(Article).where(Article.duplicate_group_id == cluster.id)
        )
        .scalars()
        .all()
    )
    media = [a for a in arts if a.source in MEDIA_SOURCES]
    if media:
        first_m = min(media, key=lambda a: a.published_at or a.discovered_at)
        cluster.first_media_source = first_m.source
        cluster.first_media_at = first_m.published_at or first_m.discovered_at


def _notify_community(
    ct: CommunityThread, scores: dict, is_high: bool, dry_run: bool
) -> None:
    ents = [e.name for e in scores.get("entities", [])][:8]
    header = (
        f"🚨 COMMUNITY HIGH PRIORITY — {ct.priority_score:.0f}/100"
        if is_high
        else f"👀 COMMUNITY SIGNAL — {ct.priority_score:.0f}/100"
    )
    color = 0xE74C3C if is_high else 0x9B59B6
    why = (
        f"Signal={ct.signal_type}; evidence={ct.evidence_score:.0f}; "
        f"firsthand={ct.firsthand_score:.0f}; corroboration={ct.corroboration_score:.0f}"
    )
    if ct.image_count:
        why += f"; images={ct.image_count}"
    payload = {
        "content": header,
        "embeds": [
            {
                "title": (ct.title_original or "")[:250],
                "url": ct.url,
                "color": color,
                "fields": [
                    {"name": "Platform", "value": ct.platform, "inline": True},
                    {"name": "Signal", "value": ct.signal_type, "inline": True},
                    {
                        "name": "Evidence",
                        "value": f"{ct.evidence_score:.0f}",
                        "inline": True,
                    },
                    {
                        "name": "Firsthand",
                        "value": f"{ct.firsthand_score:.0f}",
                        "inline": True,
                    },
                    {
                        "name": "Velocity",
                        "value": f"{ct.velocity_score:.0f}",
                        "inline": True,
                    },
                    {
                        "name": "Replies/Views",
                        "value": f"{ct.reply_count}/{ct.view_count}",
                        "inline": True,
                    },
                    {
                        "name": "Entities",
                        "value": " • ".join(ents) if ents else "—",
                        "inline": False,
                    },
                    {"name": "Why flagged", "value": why[:1020], "inline": False},
                ],
                "footer": {"text": "Chinese Tech Wire V0.3 Community Intelligence"},
            }
        ],
    }
    send_discord(payload, dry_run=dry_run)


def run_community_source(name: str, dry_run: bool = False) -> int:
    from community_sources import COMMUNITY_REGISTRY

    cls = COMMUNITY_REGISTRY.get(name)
    if not cls:
        logger.error("Unknown community source: %s", name)
        return 0
    src = cls()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    new_count = 0
    found = 0
    req_err = 0
    parse_err = 0
    threads: List[RawThread] = []

    try:
        logger.info("[%s] Fetching recent threads", name)
        threads = src.fetch_recent_threads()
        found = len(threads)
        logger.info("[%s] %d threads found", name, found)
    except Exception as e:
        logger.error("[%s] community fetch failed: %s", name, e)
        req_err = 1

    for raw in threads:
        before = datetime.now(timezone.utc)
        ct = ingest_raw_thread(raw, dry_run=dry_run, enrich=True, source_adapter=src)
        if ct is None:
            parse_err += 1
            continue
        if ct.discovered_at and (before - timedelta(seconds=2)) <= (
            ct.discovered_at if ct.discovered_at.tzinfo else ct.discovered_at.replace(tzinfo=timezone.utc)
        ):
            # roughly "new this run"
            new_count += 1

    # See main.run_source for why soft_blocked/success are derived this way:
    # soft_fetch_html() swallows individual URL failures so a genuinely
    # blocked source (anti-bot, geo-block) must not look identical to a
    # source that simply has nothing new right now.
    soft_errors = list(getattr(src, "fetch_error_log", None) or [])
    soft_blocked = bool(soft_errors)
    success = (req_err == 0) and not (found == 0 and soft_blocked)
    error_message = "; ".join(soft_errors[:3])[:500] if soft_blocked else None

    try:
        with get_session() as session:
            session.add(SourceRun(
                source=name,
                layer="COMMUNITY",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                success=success,
                articles_found=found,
                articles_new=new_count,
                parse_errors=parse_err,
                request_errors=req_err + len(soft_errors),
                response_time_ms=int((time.monotonic() - t0) * 1000),
                error_message=error_message,
                soft_blocked=soft_blocked,
            ))
    except Exception as e:
        logger.error("[%s] failed to record SourceRun: %s", name, e)

    src.close()
    logger.info("[%s] community run finished (approx new=%d)", name, new_count)
    return new_count
