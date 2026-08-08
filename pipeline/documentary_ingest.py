"""Documentary ingest: snapshot → change events → cluster → notify."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

from sqlalchemy import select

from config import yaml_config
from database.db import get_session
from database.models import (
    Article,
    CommunityThread,
    DocumentaryEvent,
    DocumentaryRecord,
    DocumentarySnapshot,
    SourceRun,
    StoryCluster,
)
from documentary_sources.base import RawDocumentary
from pipeline.deduplicate import title_similarity
from pipeline.documentary_change import detect_changes
from pipeline.documentary_score import score_documentary
from pipeline.notify import send_discord

logger = logging.getLogger(__name__)

MEDIA_SOURCES: Set[str] = {
    "ithome", "mydrivers", "expreview", "zol", "jiwei",
    "benchlife", "hkepc", "technews", "xfastest",
}
COMMUNITY_PLATFORMS: Set[str] = {"chiphell", "mobile01", "ptt", "coolaler"}


def ingest_raw_documentary(raw: RawDocumentary, dry_run: bool = False) -> Optional[DocumentaryRecord]:
    try:
        scores = score_documentary(raw)
        now = datetime.now(timezone.utc)
        chash = raw.content_hash()

        with get_session() as session:
            existing = session.execute(
                select(DocumentaryRecord).where(
                    DocumentaryRecord.source == raw.source,
                    DocumentaryRecord.source_record_id == raw.source_record_id,
                )
            ).scalar_one_or_none()

            if existing:
                return _refresh_existing(session, existing, raw, scores, chash, now, dry_run)

            rec = DocumentaryRecord(
                record_type=raw.record_type,
                source=raw.source,
                source_record_id=raw.source_record_id,
                url=raw.url,
                canonical_url=raw.canonical_url or raw.url,
                region=raw.region,
                language_variant=raw.language_variant,
                title=raw.title,
                manufacturer=raw.manufacturer,
                brand=raw.brand,
                product=raw.product,
                model_number=raw.model_number,
                first_seen_at=now,
                last_seen_at=now,
                published_at=raw.published_at,
                observed_at=now,
                record_status="ACTIVE",
                evidence_type=raw.record_type,
                evidence_score=scores["evidence_score"],
                novelty_score=scores["novelty_score"],
                relevance_score=scores["relevance_score"],
                priority_score=scores["priority_score"],
                source_quality=scores["source_quality"],
                corroboration_score=0.0,
                raw_metadata=raw.raw_metadata or {},
                raw_hash=chash,
                missing_streak=0,
                notified=False,
            )
            session.add(rec)
            session.flush()

            session.add(
                DocumentarySnapshot(
                    record_id=rec.id,
                    observed_at=now,
                    content_hash=chash,
                    structured_metadata=raw.structured,
                )
            )
            for etype, before, after in detect_changes(None, raw.structured, is_new=True):
                session.add(
                    DocumentaryEvent(
                        record_id=rec.id,
                        event_type=etype,
                        observed_at=now,
                        summary=after,
                        before_value=before,
                        after_value=after,
                    )
                )

            cluster = _attach_cluster(session, rec)
            rec.story_cluster_id = cluster.id if cluster else None
            _update_cluster_chronology(session, cluster)
            _cross_corroborate(session, rec)

            _maybe_notify(session, rec, scores, event_type="NEW_RECORD", dry_run=dry_run)
            logger.info(
                "[DOC] NEW %s/%s P=%.0f | %s",
                rec.source,
                rec.source_record_id,
                rec.priority_score,
                (rec.title or "")[:50],
            )
            return rec
    except Exception as e:
        logger.error("[DOC] ingest failed: %s", e)
        return None


def _refresh_existing(session, existing, raw, scores, chash, now, dry_run):
    existing.last_seen_at = now
    existing.observed_at = now
    existing.missing_streak = 0
    if existing.record_status in ("MISSING", "REMOVED"):
        existing.record_status = "REAPPEARED"
        session.add(
            DocumentaryEvent(
                record_id=existing.id,
                event_type="RECORD_REAPPEARED",
                observed_at=now,
                summary="record returned after absence",
            )
        )
    else:
        existing.record_status = "ACTIVE"

    # Previous snapshot
    prev_snap = session.execute(
        select(DocumentarySnapshot)
        .where(DocumentarySnapshot.record_id == existing.id)
        .order_by(DocumentarySnapshot.observed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    prev_struct = (prev_snap.structured_metadata if prev_snap else None) or {}

    if chash != existing.raw_hash:
        events = detect_changes(prev_struct, raw.structured, is_new=False)
        session.add(
            DocumentarySnapshot(
                record_id=existing.id,
                observed_at=now,
                content_hash=chash,
                structured_metadata=raw.structured,
            )
        )
        existing.raw_hash = chash
        existing.title = raw.title or existing.title
        existing.model_number = raw.model_number or existing.model_number
        for etype, before, after in events:
            session.add(
                DocumentaryEvent(
                    record_id=existing.id,
                    event_type=etype,
                    observed_at=now,
                    summary=f"{before} → {after}" if before else after,
                    before_value=before,
                    after_value=after,
                )
            )
            # notify on meaningful changes
            if etype in (
                "PRICE_ADDED",
                "SPEC_ADDED",
                "MODEL_REVEALED",
                "BENCHMARK_APPEARED",
                "CERTIFICATION_APPEARED",
            ):
                existing.priority_score = max(existing.priority_score, scores["priority_score"])
                _maybe_notify(session, existing, scores, event_type=etype, dry_run=dry_run)
        if events:
            logger.info(
                "[DOC] CHANGE %s/%s events=%s",
                existing.source,
                existing.source_record_id,
                [e[0] for e in events],
            )
    else:
        # identical meaningful content — still snapshot occasionally? skip to save space
        pass

    existing.evidence_score = scores["evidence_score"]
    existing.relevance_score = scores["relevance_score"]
    # novelty decays on repeats
    existing.novelty_score = min(existing.novelty_score, scores["novelty_score"] * 0.5)
    existing.priority_score = scores["priority_score"] * 0.7  # repeat runs less urgent
    return existing


def mark_missing_records(source: str, seen_ids: Set[str], dry_run: bool = False) -> int:
    """Mark ACTIVE records not seen this run; escalate to REMOVED after streak."""
    confirm = int(yaml_config.get("documentary", {}).get("removal_confirmations", 2))
    removed = 0
    now = datetime.now(timezone.utc)
    with get_session() as session:
        rows = session.execute(
            select(DocumentaryRecord).where(
                DocumentaryRecord.source == source,
                DocumentaryRecord.record_status.in_(["ACTIVE", "MISSING", "REAPPEARED"]),
            )
        ).scalars().all()
        for rec in rows:
            if rec.source_record_id in seen_ids:
                continue
            rec.missing_streak = (rec.missing_streak or 0) + 1
            rec.record_status = "MISSING"
            if rec.missing_streak >= confirm:
                rec.record_status = "REMOVED"
                session.add(
                    DocumentaryEvent(
                        record_id=rec.id,
                        event_type="RECORD_REMOVED",
                        observed_at=now,
                        summary=f"missing streak={rec.missing_streak}",
                    )
                )
                removed += 1
                logger.info("[DOC] REMOVED %s/%s", rec.source, rec.source_record_id)
    return removed


def _attach_cluster(session, rec: DocumentaryRecord) -> Optional[StoryCluster]:
    cfg = yaml_config.get("dedup", {})
    sim_thresh = float(cfg.get("title_similarity_soft", 0.62))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(cfg.get("time_window_hours", 72)))
    title = rec.title or rec.product or ""
    best = None
    best_sim = 0.0

    for art in session.execute(
        select(Article).where(Article.discovered_at >= cutoff).limit(150)
    ).scalars().all():
        sim = title_similarity(title, art.title_original)
        if sim >= sim_thresh and sim > best_sim and art.duplicate_group_id:
            best_sim = sim
            best = session.get(StoryCluster, art.duplicate_group_id)

    for ct in session.execute(
        select(CommunityThread).where(CommunityThread.discovered_at >= cutoff).limit(100)
    ).scalars().all():
        sim = title_similarity(title, ct.title_original)
        if sim >= sim_thresh and sim > best_sim and ct.story_cluster_id:
            best_sim = sim
            best = session.get(StoryCluster, ct.story_cluster_id)

    for other in session.execute(
        select(DocumentaryRecord)
        .where(DocumentaryRecord.id != rec.id)
        .where(DocumentaryRecord.first_seen_at >= cutoff)
        .limit(100)
    ).scalars().all():
        sim = title_similarity(title, other.title or "")
        if sim >= 0.85 and sim > best_sim and other.story_cluster_id:
            # near-identical hardware config → same cluster (benchmark repeats)
            best_sim = sim
            best = session.get(StoryCluster, other.story_cluster_id)

    now = datetime.now(timezone.utc)
    if best is None:
        best = StoryCluster(
            first_seen_source=rec.source,
            first_seen_at=rec.first_seen_at or now,
            representative_title=title,
            created_at=now,
            updated_at=now,
            first_documentary_source=rec.source,
            first_documentary_at=rec.first_seen_at or now,
        )
        session.add(best)
        session.flush()
    else:
        best.updated_at = now
    return best


def _update_cluster_chronology(session, cluster: Optional[StoryCluster]) -> None:
    if not cluster:
        return
    docs = session.execute(
        select(DocumentaryRecord).where(DocumentaryRecord.story_cluster_id == cluster.id)
    ).scalars().all()
    if docs:
        first = min(docs, key=lambda d: d.first_seen_at)
        cluster.first_documentary_source = first.source
        cluster.first_documentary_at = first.first_seen_at

    cts = session.execute(
        select(CommunityThread).where(CommunityThread.story_cluster_id == cluster.id)
    ).scalars().all()
    if cts:
        first = min(cts, key=lambda t: t.first_signal_at or t.created_at or t.discovered_at)
        cluster.first_signal_source = first.platform
        cluster.first_signal_at = first.first_signal_at or first.created_at or first.discovered_at

    arts = session.execute(
        select(Article).where(Article.duplicate_group_id == cluster.id)
    ).scalars().all()
    media = [a for a in arts if a.source in MEDIA_SOURCES]
    if media:
        first_m = min(media, key=lambda a: a.published_at or a.discovered_at)
        cluster.first_media_source = first_m.source
        cluster.first_media_at = first_m.published_at or first_m.discovered_at


def _cross_corroborate(session, rec: DocumentaryRecord) -> None:
    """Documentary evidence boosts related community threads in same cluster."""
    if not rec.story_cluster_id:
        return
    cts = session.execute(
        select(CommunityThread).where(
            CommunityThread.story_cluster_id == rec.story_cluster_id
        )
    ).scalars().all()
    for ct in cts:
        if ct.signal_type == "REPOST":
            continue
        # model/entity overlap
        blob = (ct.title_original or "") + " " + (ct.op_text_original or "")
        rec_blob = (rec.title or "") + " " + (rec.model_number or "")
        sim = title_similarity(blob[:200], rec_blob[:200])
        if sim < 0.55:
            continue
        boost = 30.0 if rec.record_type == "BENCHMARK_RECORD" else 25.0
        ct.corroboration_score = max(ct.corroboration_score or 0, boost)
        from pipeline.community_score import compute_priority
        ct.priority_score = compute_priority(
            ct.relevance_score,
            ct.novelty_score,
            ct.evidence_score,
            ct.firsthand_score,
            ct.velocity_score,
            ct.community_score,
            ct.corroboration_score,
            ct.signal_type,
        )


def _maybe_notify(session, rec, scores, event_type: str, dry_run: bool) -> None:
    thresh = float(yaml_config.get("documentary", {}).get("notify_threshold", 75))
    if rec.priority_score < thresh and event_type == "NEW_RECORD":
        return
    if event_type != "NEW_RECORD" and rec.priority_score < thresh - 10:
        return
    if rec.notified and event_type == "NEW_RECORD":
        return

    emoji = "📄"
    label = "DOCUMENTARY SIGNAL"
    if rec.record_type == "RETAIL_LISTING":
        emoji = "🛒"
        label = "RETAIL SIGNAL"
    elif rec.record_type == "BENCHMARK_RECORD":
        emoji = "📊"
        label = "BENCHMARK SIGNAL"

    fields = [
        {"name": "Type", "value": rec.record_type, "inline": True},
        {"name": "Source", "value": rec.source, "inline": True},
        {"name": "Event", "value": event_type, "inline": True},
        {"name": "Evidence", "value": f"{rec.evidence_score:.0f}", "inline": True},
        {"name": "Novelty", "value": f"{rec.novelty_score:.0f}", "inline": True},
        {"name": "Priority", "value": f"{rec.priority_score:.0f}", "inline": True},
    ]
    if rec.model_number:
        fields.append({"name": "Model", "value": rec.model_number[:100], "inline": True})
    meta = scores
    payload = {
        "content": f"{emoji} {label} — {rec.priority_score:.0f}/100",
        "embeds": [
            {
                "title": (rec.title or rec.source_record_id)[:250],
                "url": rec.url,
                "color": 0x3498DB,
                "fields": fields,
                "footer": {"text": "Chinese Tech Wire V0.4 Documentary Intelligence"},
            }
        ],
    }
    try:
        send_discord(payload, dry_run=dry_run)
        if not dry_run and event_type == "NEW_RECORD":
            rec.notified = True
    except Exception as e:
        logger.error("[DOC] notify failed: %s", e)


def run_documentary_source(name: str, dry_run: bool = False) -> int:
    from documentary_sources import DOCUMENTARY_REGISTRY

    cls = DOCUMENTARY_REGISTRY.get(name)
    if not cls:
        logger.error("Unknown documentary source: %s", name)
        return 0
    src = cls()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    count = 0
    found = 0
    req_err = 0
    parse_err = 0
    seen: Set[str] = set()
    records: List = []

    try:
        logger.info("[%s] Fetching documentary records", name)
        records = src.fetch_latest()
        found = len(records)
        logger.info("[%s] %d candidates", name, found)
    except Exception as e:
        logger.error("[%s] documentary fetch failed: %s", name, e)
        req_err = 1

    for raw in records:
        seen.add(raw.source_record_id)
        rec = ingest_raw_documentary(raw, dry_run=dry_run)
        if rec:
            count += 1
        else:
            parse_err += 1

    try:
        mark_missing_records(name, seen, dry_run=dry_run)
    except Exception as e:
        logger.error("[%s] mark_missing_records failed: %s", name, e)

    # See main.run_source for why soft_blocked/success are derived this way:
    # soft_fetch_html() swallows individual URL failures (e.g. JD's anti-bot
    # interstitials) so a genuinely blocked source must not look identical
    # to a source that simply has nothing new right now.
    soft_errors = list(getattr(src, "fetch_error_log", None) or [])
    soft_blocked = bool(soft_errors)
    success = (req_err == 0) and not (found == 0 and soft_blocked)
    error_message = "; ".join(soft_errors[:3])[:500] if soft_blocked else None

    try:
        with get_session() as session:
            session.add(SourceRun(
                source=name,
                layer="DOCUMENTARY",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                success=success,
                articles_found=found,
                articles_new=count,
                parse_errors=parse_err,
                request_errors=req_err + len(soft_errors),
                response_time_ms=int((time.monotonic() - t0) * 1000),
                error_message=error_message,
                soft_blocked=soft_blocked,
            ))
    except Exception as e:
        logger.error("[%s] failed to record SourceRun: %s", name, e)

    src.close()
    logger.info("[%s] documentary run done (processed≈%d)", name, count)
    return count
