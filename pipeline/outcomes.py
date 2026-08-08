"""V0.5.6 — StoryLead editorial outcome model.

Distinct from three things that already exist and are left untouched:
  - lead_status (lifecycle.py / newsroom.py): what state is the lead in NOW.
  - LeadFeedback (pipeline/newsroom.py:add_feedback): the existing lightweight
    USEFUL/NOT_USEFUL/WRITTEN/DUPLICATE/FALSE_POSITIVE quick-tap buttons.
  - LeadNotification: whether/how a Discord alert was sent.

LeadOutcome answers a fourth, separate question: what ultimately happened
editorially. It's append-only (a lead can go USEFUL -> WRITTEN over time);
the most recent row is the "current" outcome.

This module never mutates scoring, thresholds, or lifecycle status.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from database.db import get_session
from database.models import LeadFeedback, LeadOutcome, StoryLead

logger = logging.getLogger(__name__)

OUTCOME_VALUES = {
    "UNKNOWN",
    "USEFUL",
    "WRITTEN",
    "CONFIRMED",
    "OFFICIALLY_ANNOUNCED",
    "FALSE",
    "DUPLICATE",
    "IGNORED",
    "STALLED",
    "EXPIRED",
    "MISSED_OPPORTUNITY",
}

# Outcomes not expected to change further once recorded.
TERMINAL_OUTCOMES = {
    "WRITTEN",
    "CONFIRMED",
    "OFFICIALLY_ANNOUNCED",
    "FALSE",
    "DUPLICATE",
    "EXPIRED",
    "MISSED_OPPORTUNITY",
}

# Derived projection ONLY — never written back onto the original LeadFeedback
# row. Used solely so editorial_validation reports have one outcome-space to
# reason about when a lead has old-style feedback but no LeadOutcome yet.
FEEDBACK_TO_OUTCOME = {
    "USEFUL": "USEFUL",
    "WRITTEN": "WRITTEN",
    "DUPLICATE": "DUPLICATE",
    "FALSE_POSITIVE": "FALSE",
    "NOT_USEFUL": "IGNORED",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_outcome(
    lead_id: int,
    outcome: str,
    note: Optional[str] = None,
    article_url: Optional[str] = None,
    article_title: Optional[str] = None,
    confirmation_url: Optional[str] = None,
    recorded_by: Optional[str] = "operator",
    outcome_source: str = "MANUAL",
) -> Optional[LeadOutcome]:
    """Record a new outcome event for a lead. Returns the created row, or
    None if the lead doesn't exist or the outcome value is invalid."""
    outcome = (outcome or "").strip().upper().replace("-", "_")
    if outcome not in OUTCOME_VALUES:
        logger.error("Invalid outcome %r; allowed %s", outcome, sorted(OUTCOME_VALUES))
        return None
    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if not lead:
            logger.error("Lead #%s not found", lead_id)
            return None
        row = LeadOutcome(
            lead_id=lead_id,
            outcome=outcome,
            recorded_at=_now(),
            recorded_by=recorded_by,
            notes=note,
            related_article_url=article_url,
            related_article_title=article_title,
            external_confirmation_url=confirmation_url,
            outcome_source=outcome_source,
            is_final=outcome in TERMINAL_OUTCOMES,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return row


def current_outcome(lead_id: int, session=None) -> Optional[LeadOutcome]:
    """Most recent LeadOutcome row for a lead, or None if never recorded."""
    def _query(s):
        return s.execute(
            select(LeadOutcome)
            .where(LeadOutcome.lead_id == lead_id)
            .order_by(desc(LeadOutcome.recorded_at))
            .limit(1)
        ).scalar_one_or_none()

    if session is not None:
        return _query(session)
    with get_session() as s:
        return _query(s)


def outcome_history(lead_id: int, session=None) -> List[LeadOutcome]:
    def _query(s):
        return list(
            s.execute(
                select(LeadOutcome)
                .where(LeadOutcome.lead_id == lead_id)
                .order_by(desc(LeadOutcome.recorded_at))
            ).scalars().all()
        )

    if session is not None:
        return _query(session)
    with get_session() as s:
        return _query(s)


def effective_outcome(lead_id: int, session) -> "tuple[str, str]":
    """(outcome_value, source) where source is OUTCOME | FEEDBACK | UNLABELED.

    Prefers the richer LeadOutcome model; falls back to a derived projection
    of the most recent LeadFeedback row (never mutating that row); otherwise
    UNLABELED — distinct from UNKNOWN, which is an explicit operator choice.
    """
    outcome = current_outcome(lead_id, session=session)
    if outcome:
        return outcome.outcome, "OUTCOME"
    fb = session.execute(
        select(LeadFeedback)
        .where(LeadFeedback.lead_id == lead_id)
        .order_by(desc(LeadFeedback.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if fb:
        return FEEDBACK_TO_OUTCOME.get(fb.feedback, "UNKNOWN"), "FEEDBACK"
    return "UNLABELED", "UNLABELED"


def list_outcomes(limit: int = 50, since_hours: Optional[float] = None) -> List[Dict[str, Any]]:
    with get_session() as session:
        q = select(LeadOutcome).order_by(desc(LeadOutcome.recorded_at)).limit(limit)
        if since_hours is not None:
            from datetime import timedelta
            cutoff = _now() - timedelta(hours=since_hours)
            q = select(LeadOutcome).where(LeadOutcome.recorded_at >= cutoff).order_by(
                desc(LeadOutcome.recorded_at)
            ).limit(limit)
        rows = session.execute(q).scalars().all()
        return [
            {
                "id": r.id,
                "lead_id": r.lead_id,
                "outcome": r.outcome,
                "recorded_at": r.recorded_at,
                "recorded_by": r.recorded_by,
                "notes": r.notes,
                "related_article_url": r.related_article_url,
                "related_article_title": r.related_article_title,
                "external_confirmation_url": r.external_confirmation_url,
                "outcome_source": r.outcome_source,
                "is_final": r.is_final,
            }
            for r in rows
        ]


def format_outcomes_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No outcomes recorded yet."
    lines = []
    for r in rows:
        ts = r["recorded_at"].strftime("%Y-%m-%d %H:%M") if r["recorded_at"] else "—"
        extra = ""
        if r.get("related_article_url"):
            extra += f" article={r['related_article_url']}"
        if r.get("external_confirmation_url"):
            extra += f" confirm={r['external_confirmation_url']}"
        lines.append(
            f"{ts}  lead={r['lead_id']:<6} {r['outcome']:<20} "
            f"final={r['is_final']} by={r.get('recorded_by') or '—'}{extra}"
        )
    return "\n".join(lines)


def outcome_report() -> Dict[str, Any]:
    """Simple coverage summary: how many leads have an outcome vs total."""
    with get_session() as session:
        total_leads = session.execute(select(StoryLead.id)).scalars().all()
        total = len(total_leads)
        outcomes = session.execute(select(LeadOutcome)).scalars().all()
        by_lead: Dict[int, LeadOutcome] = {}
        for o in sorted(outcomes, key=lambda r: r.recorded_at):
            by_lead[o.lead_id] = o  # last write wins -> most recent per lead
        counts: Dict[str, int] = {}
        for o in by_lead.values():
            counts[o.outcome] = counts.get(o.outcome, 0) + 1
        labelled = len(by_lead)
        return {
            "total_leads": total,
            "leads_with_outcome": labelled,
            "outcome_coverage_pct": round(100.0 * labelled / total, 2) if total else 0.0,
            "counts": counts,
        }
