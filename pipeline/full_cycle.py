"""One complete production ingestion cycle (news + community + documentary + leads).

Used by --full-once and Windows Task Scheduler. Does not duplicate scraper logic.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database.db import get_session, init_db
from database.models import IngestionRun

logger = logging.getLogger("ctw.full_cycle")


def run_full_cycle(
    *,
    trigger: str = "MANUAL",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run all active intelligence layers once.

    Returns a stats dict. Individual source failures are isolated and counted;
    they do not abort the rest of the cycle or future scheduled runs.
    """
    init_db()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    trigger = (trigger or "MANUAL").upper()
    if trigger not in ("MANUAL", "SCHEDULED"):
        trigger = "MANUAL"

    from pipeline.alerts import reset_alert_stats, get_alert_stats
    reset_alert_stats()

    stats: Dict[str, Any] = {
        "articles_new": 0,
        "community_threads_new": 0,
        "documentary_records_new": 0,
        "documentary_events_new": 0,
        "leads_created": 0,
        "alerts_sent": 0,
        "warning_count": 0,
        "error_count": 0,
        "status": "RUNNING",
        "trigger": trigger,
    }

    run_id: Optional[int] = None
    with get_session() as session:
        row = IngestionRun(
            started_at=started,
            trigger=trigger,
            status="RUNNING",
        )
        session.add(row)
        session.flush()
        run_id = row.id

    logger.info("[FULL] start id=%s trigger=%s", run_id, trigger)

    # --- News ---
    try:
        from sources import SOURCE_REGISTRY
        # run_source lives in main; import without executing CLI
        import main as main_mod
        run_source = main_mod.run_source

        for name in SOURCE_REGISTRY:
            try:
                n = run_source(name, dry_run=dry_run)
                stats["articles_new"] += int(n or 0)
            except Exception as e:
                stats["error_count"] += 1
                logger.error("[FULL] news source %s failed: %s", name, e)
    except Exception as e:
        stats["error_count"] += 1
        logger.error("[FULL] news layer failed: %s", e)

    # --- Community ---
    try:
        from community_sources import COMMUNITY_REGISTRY
        from pipeline.community_ingest import run_community_source

        for name in COMMUNITY_REGISTRY:
            try:
                n = run_community_source(name, dry_run=dry_run)
                stats["community_threads_new"] += int(n or 0)
            except Exception as e:
                stats["error_count"] += 1
                logger.error("[FULL] community %s failed: %s", name, e)
    except Exception as e:
        stats["error_count"] += 1
        logger.error("[FULL] community layer failed: %s", e)

    # --- Documentary ---
    try:
        from documentary_sources import DOCUMENTARY_REGISTRY
        from pipeline.documentary_ingest import run_documentary_source

        for name in DOCUMENTARY_REGISTRY:
            try:
                n = run_documentary_source(name, dry_run=dry_run)
                stats["documentary_records_new"] += int(n or 0)
            except Exception as e:
                stats["error_count"] += 1
                logger.error("[FULL] documentary %s failed: %s", name, e)
    except Exception as e:
        stats["error_count"] += 1
        logger.error("[FULL] documentary layer failed: %s", e)

    # --- Newsroom leads + StoryLead alerts ---
    try:
        from pipeline.newsroom import rebuild_leads

        n = rebuild_leads(dry_run=dry_run)
        stats["leads_created"] = int(n or 0)
    except Exception as e:
        stats["error_count"] += 1
        logger.error("[FULL] newsroom rebuild failed: %s", e)

    alert_stats = get_alert_stats()
    stats["alerts_evaluated"] = alert_stats.get("alerts_evaluated", 0)
    stats["alerts_eligible"] = alert_stats.get("alerts_eligible", 0)
    stats["alerts_attempted"] = alert_stats.get("alerts_attempted", 0)
    stats["alerts_sent"] = alert_stats.get("alerts_sent", 0)
    stats["alerts_failed"] = alert_stats.get("alerts_failed", 0)
    stats["alerts_suppressed"] = sum(
        alert_stats.get(k, 0)
        for k in (
            "alerts_suppressed_notified", "alerts_suppressed_stale",
            "alerts_suppressed_score", "alerts_suppressed_quality",
            "alerts_suppressed_status", "alerts_suppressed_backlog",
            "alerts_suppressed_webhook", "alerts_suppressed_other",
        )
    )
    stats["alert_decision_summary"] = alert_stats
    logger.info(
        "[FULL] alerts evaluated=%s eligible=%s attempted=%s sent=%s failed=%s suppressed=%s",
        stats["alerts_evaluated"], stats["alerts_eligible"], stats["alerts_attempted"],
        stats["alerts_sent"], stats["alerts_failed"], stats["alerts_suppressed"],
    )

    duration = time.monotonic() - t0
    finished = datetime.now(timezone.utc)

    if stats["error_count"] == 0:
        status = "SUCCESS"
    elif (
        stats["articles_new"]
        or stats["community_threads_new"]
        or stats["documentary_records_new"]
        or stats["leads_created"]
    ):
        status = "PARTIAL"
    else:
        # errors but maybe all sources blocked with zero new — still partial if any layer ran
        status = "PARTIAL" if stats["error_count"] < 20 else "FAILED"

    stats["status"] = status
    stats["duration_seconds"] = round(duration, 2)

    summary = (
        f"articles+={stats['articles_new']} community+={stats['community_threads_new']} "
        f"doc+={stats['documentary_records_new']} leads+={stats['leads_created']} "
        f"alerts_sent={stats.get('alerts_sent', 0)} alerts_fail={stats.get('alerts_failed', 0)} "
        f"err={stats['error_count']} warn={stats['warning_count']}"
    )
    stats["summary"] = summary

    with get_session() as session:
        row = session.get(IngestionRun, run_id)
        if row:
            row.finished_at = finished
            row.status = status
            row.duration_seconds = stats["duration_seconds"]
            row.articles_new = stats["articles_new"]
            row.community_threads_new = stats["community_threads_new"]
            row.documentary_records_new = stats["documentary_records_new"]
            row.documentary_events_new = stats["documentary_events_new"]
            row.leads_created = stats["leads_created"]
            row.alerts_sent = int(stats.get("alerts_sent", 0) or 0)
            if hasattr(row, "alerts_evaluated"):
                row.alerts_evaluated = int(stats.get("alerts_evaluated", 0) or 0)
                row.alerts_eligible = int(stats.get("alerts_eligible", 0) or 0)
                row.alerts_attempted = int(stats.get("alerts_attempted", 0) or 0)
                row.alerts_failed = int(stats.get("alerts_failed", 0) or 0)
                row.alerts_suppressed = int(stats.get("alerts_suppressed", 0) or 0)
                row.alert_decision_summary = stats.get("alert_decision_summary")
            row.warning_count = stats["warning_count"]
            row.error_count = stats["error_count"]
            row.summary = summary

    logger.info(
        "[FULL] finish id=%s status=%s duration=%.1fs %s",
        run_id,
        status,
        duration,
        summary,
    )
    stats["run_id"] = run_id
    return stats
