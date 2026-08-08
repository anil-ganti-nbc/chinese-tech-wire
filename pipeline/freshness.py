"""V0.5.7 — Addendum C: newsroom freshness / mothball policy.

"Mothball" = removed from the *default* active newsroom view, never
deleted. Nothing in this module deletes or rewrites any row; it only
computes read-only age buckets and a dynamic (not persisted) archive
classification derived from StoryLead.last_activity_at.

Root cause this exists to fix (see Addendum B investigation): /newsroom had
no default time window at all, and its sort put status+priority ahead of
recency, so 14+ day old WATCHING leads with decent scores could sit at the
top of page 1 next to genuinely fresh ones. last_activity_at itself was
already reliable — it's a MAX over real record timestamps in the cluster,
recomputed every rebuild but only *changing* when a genuinely new record
arrives (verified by reading pipeline.newsroom.upsert_lead_for_cluster) —
so this module filters/reports on it, it does not change how it's computed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, select

from database.db import get_session
from database.models import LeadNotification, LeadOutcome, StoryLead

ACTIVE_WINDOW_DAYS = 7.0

BUCKET_ORDER = ["<6h", "6-12h", "12-24h", "1-2d", "2-3d", "3-7d", "7-14d", ">14d"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def active_cutoff(now: Optional[datetime] = None) -> datetime:
    return (now or _now()) - timedelta(days=ACTIVE_WINDOW_DAYS)


def is_active(last_activity_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True if a lead counts as 'active' (within the default newsroom
    window). A lead with no last_activity_at at all is treated as active
    (never silently hidden due to missing data)."""
    dt = _aware(last_activity_at)
    if dt is None:
        return True
    return dt >= active_cutoff(now)


def age_bucket(dt: Optional[datetime], now: Optional[datetime] = None) -> str:
    dt = _aware(dt)
    if dt is None:
        return "NULL"
    now = now or _now()
    age_h = (now - dt).total_seconds() / 3600.0
    if age_h < 6:
        return "<6h"
    if age_h < 12:
        return "6-12h"
    if age_h < 24:
        return "12-24h"
    if age_h < 48:
        return "1-2d"
    if age_h < 72:
        return "2-3d"
    if age_h < 168:
        return "3-7d"
    if age_h < 336:
        return "7-14d"
    return ">14d"


def freshness_report() -> Dict[str, Any]:
    """Read-only age-bucket report for StoryLeads, plus archived-population
    breakdowns. Used by both `python main.py --freshness-report` and any
    future GUI surface — one implementation, no duplicated bucketing logic."""
    now = _now()
    with get_session() as session:
        leads = session.execute(select(StoryLead)).scalars().all()

        buckets: Dict[str, int] = {b: 0 for b in BUCKET_ORDER}
        buckets["NULL"] = 0
        active = 0
        archived = 0
        archived_by_status: Dict[str, int] = {}
        archived_alerted = 0
        archived_written = 0

        lead_ids = [l.id for l in leads]
        alerted_ids = set()
        if lead_ids:
            alerted_ids = {
                lid for lid in session.execute(
                    select(LeadNotification.lead_id)
                    .where(LeadNotification.outcome == "SENT")
                    .where(LeadNotification.lead_id.in_(lead_ids))
                ).scalars().all() if lid is not None
            }

        written_lead_ids = set()
        if lead_ids:
            written_lead_ids = {
                lid for lid in session.execute(
                    select(LeadOutcome.lead_id)
                    .where(LeadOutcome.outcome == "WRITTEN")
                    .where(LeadOutcome.lead_id.in_(lead_ids))
                ).scalars().all()
            }

        for l in leads:
            b = age_bucket(l.last_activity_at, now)
            buckets[b] = buckets.get(b, 0) + 1
            if is_active(l.last_activity_at, now):
                active += 1
            else:
                archived += 1
                archived_by_status[l.lead_status] = archived_by_status.get(l.lead_status, 0) + 1
                if l.id in alerted_ids:
                    archived_alerted += 1
                if l.id in written_lead_ids:
                    archived_written += 1

        return {
            "total_leads": len(leads),
            "buckets": buckets,
            "active_leads": active,
            "archived_leads": archived,
            "archived_by_status": archived_by_status,
            "archived_watching": archived_by_status.get("WATCHING", 0),
            "archived_actionable": archived_by_status.get("ACTIONABLE", 0),
            "archived_alerted": archived_alerted,
            "archived_written": archived_written,
            "active_window_days": ACTIVE_WINDOW_DAYS,
            "generated_at": now.isoformat(),
        }


def format_freshness_report_text(r: Dict[str, Any]) -> str:
    lines = ["StoryLead freshness", ""]
    b = r["buckets"]
    label_map = {
        "<6h": "<6h", "6-12h": "6-12h", "12-24h": "12-24h", "1-2d": "1-2d",
        "2-3d": "2-3d", "3-7d": "3-7d", "7-14d": "7-14d", ">14d": ">14d",
    }
    for key in BUCKET_ORDER:
        lines.append(f"{label_map[key]:<10} {b.get(key, 0)}")
    if b.get("NULL"):
        lines.append(f"{'NULL':<10} {b['NULL']}")
    lines.append("")
    lines.append(f"Active (<{int(r['active_window_days'])}d): {r['active_leads']}")
    lines.append(f"Archived:    {r['archived_leads']}")
    lines.append("")
    lines.append(f"Archived WATCHING:   {r['archived_watching']}")
    lines.append(f"Archived ACTIONABLE: {r['archived_actionable']}")
    lines.append(f"Archived alerted:    {r['archived_alerted']}")
    lines.append(f"Archived WRITTEN:    {r['archived_written']}")
    return "\n".join(lines)
