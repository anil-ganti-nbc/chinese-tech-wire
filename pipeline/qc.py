"""Editorial QC contract: the four reviewer decisions, the transactional
archive-on-decide, and the guarantees around both.

Terminology mapping — CTW is a news-lead pipeline, not a retail listing
feed, so the fleet-standard four QC categories (Useful / Not useful /
False positive / Out of stock) map onto this project's existing
USEFUL / NOT_USEFUL / FALSE_POSITIVE / DUPLICATE quick-tap feedback
values (see database.models.LeadFeedback, unchanged since V0.5):

    Useful          -> USEFUL           (lead is real and worth acting on)
    Not useful      -> NOT_USEFUL       (lead is real but not worth acting on)
    False positive  -> FALSE_POSITIVE   (not actually a story / mis-detected)
    Out of stock    -> DUPLICATE        ("this item is no longer available
                                          as a distinct thing to act on" ==
                                          already covered elsewhere / stale)

WRITTEN is a fifth, CTW-specific value (the lead resulted in a published
article) kept for backward compatibility with existing data/tests; it is
handled by this module identically to USEFUL (an affirmative, queue-
clearing decision) and is not part of the fleet-standard four.

Guarantees implemented here (see record_qc_decision):
  1. Transactional archive: the full item + provenance + decision is
     archived to a *separate* database (database.qc_archive, its own
     data/qc_archive.db file) before the operational DB's feedback row and
     status flip are committed. If the archive write fails, the
     operational change is rolled back too — a decision is never "applied"
     without being durably archived, and never "archived" without being
     applied. No destructive loss either way: the source StoryLead row
     itself is never deleted or mutated beyond its status field.
  2. Immediate queue removal: any of the four decisions (or WRITTEN) moves
     the lead's status out of the default active set
     (NEW/WATCHING/ACTIONABLE/ESCALATED) that pipeline.newsroom.list_leads
     and the GUI's default queue query select on — see
     QC_DECISION_TO_LEAD_STATUS below.
  3. No double-QC / race duplication: qc_archive.lead_id carries a UNIQUE
     constraint (the hard backstop). The operational-side check
     (`already_qcd`) is an additional fast-path that avoids even starting
     the archive write for an obviously-already-decided lead. Two
     concurrent requests for the same lead: SQLite serializes writers, so
     the second request's transaction begins only after the first
     commits; by then its own already-QCd check (re-read inside the same
     transaction) or the archive's UNIQUE constraint will reject it. The
     already-QCd caller gets a clear, non-mutating "already decided"
     result rather than a silent second archive row or a lost update.
  4. Restart durability: both databases are plain on-disk SQLite files
     under data/ — nothing here is held only in memory.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from database.db import get_session
from database.models import LeadEvent, LeadFeedback, LeadNotification, LeadOutcome, StoryCluster, StoryLead
from database.qc_archive import QcArchiveEntry, get_qc_session, init_qc_archive

logger = logging.getLogger("ctw.qc")

QC_DECISIONS = {"USEFUL", "NOT_USEFUL", "FALSE_POSITIVE", "DUPLICATE", "WRITTEN"}

# Fleet-standard four -> CTW value, for anything (docs, a future GUI label,
# a sibling project's parity check) that wants the canonical names.
FLEET_LABEL_TO_DECISION = {
    "USEFUL": "USEFUL",
    "NOT_USEFUL": "NOT_USEFUL",
    "FALSE_POSITIVE": "FALSE_POSITIVE",
    "OUT_OF_STOCK": "DUPLICATE",
}

# Every QC decision clears the lead out of the active queue immediately.
# Affirmative outcomes land on RESOLVED (matches the pre-existing WRITTEN
# behavior); everything else lands on DISMISSED (matches the pre-existing
# FALSE_POSITIVE behavior). Historical rows created before this module
# existed (USEFUL/NOT_USEFUL/DUPLICATE feedback that never changed
# lead_status) are unaffected — this only governs decisions made through
# record_qc_decision from here on.
QC_DECISION_TO_LEAD_STATUS = {
    "USEFUL": "RESOLVED",
    "WRITTEN": "RESOLVED",
    "NOT_USEFUL": "DISMISSED",
    "FALSE_POSITIVE": "DISMISSED",
    "DUPLICATE": "DISMISSED",
}

ACTIVE_QUEUE_STATUSES = {"NEW", "WATCHING", "ACTIONABLE", "ESCALATED"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AlreadyQcdError(Exception):
    """Raised when a lead has already been QC'd — the caller should treat
    this as a no-op, not an error to surface loudly (a second click of a
    QC button, or a genuine race, both land here)."""

    def __init__(self, lead_id: int, existing_decision: Optional[str] = None):
        self.lead_id = lead_id
        self.existing_decision = existing_decision
        super().__init__(f"Lead #{lead_id} already QC'd (decision={existing_decision})")


def _jsonable(value: Any) -> Any:
    """Coerce a value to something the JSON column can actually store —
    datetimes are the only non-JSON-native type these models use."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _lead_snapshot(lead: StoryLead) -> Dict[str, Any]:
    """Full column-for-column snapshot of the StoryLead row — independent
    of any ORM lazy-loading, so it stays valid after the session closes."""
    return {
        c.key: _jsonable(getattr(lead, c.key))
        for c in StoryLead.__table__.columns
    }


def _provenance(lead_id: int, session) -> Dict[str, Any]:
    """Everything that explains *why* this lead existed and what happened
    to it before the QC decision: cluster, prior feedback/outcome history,
    lead events, and notification attempts."""
    lead = session.get(StoryLead, lead_id)
    cluster = session.get(StoryCluster, lead.cluster_id) if lead and lead.cluster_id else None
    feedback_rows = list(session.execute(
        select(LeadFeedback).where(LeadFeedback.lead_id == lead_id).order_by(LeadFeedback.created_at)
    ).scalars().all())
    outcome_rows = list(session.execute(
        select(LeadOutcome).where(LeadOutcome.lead_id == lead_id).order_by(LeadOutcome.recorded_at)
    ).scalars().all())
    event_rows = list(session.execute(
        select(LeadEvent).where(LeadEvent.lead_id == lead_id).order_by(LeadEvent.observed_at)
    ).scalars().all())
    notif_rows = list(session.execute(
        select(LeadNotification).where(LeadNotification.lead_id == lead_id).order_by(LeadNotification.attempted_at)
    ).scalars().all())
    return {
        "cluster": (
            {c.key: _jsonable(getattr(cluster, c.key)) for c in StoryCluster.__table__.columns}
            if cluster is not None else None
        ),
        "prior_feedback": [
            {"feedback": f.feedback, "created_at": _jsonable(f.created_at), "note": f.note}
            for f in feedback_rows
        ],
        "prior_outcomes": [
            {"outcome": o.outcome, "recorded_at": _jsonable(o.recorded_at), "recorded_by": o.recorded_by}
            for o in outcome_rows
        ],
        "lead_events": [
            {"event_type": e.event_type, "observed_at": _jsonable(e.observed_at), "summary": e.summary}
            for e in event_rows
        ],
        "notifications": [
            {"outcome": n.outcome, "attempted_at": _jsonable(n.attempted_at), "reason_code": n.reason_code}
            for n in notif_rows
        ],
    }


def already_qcd(lead_id: int) -> Optional[str]:
    """The archived decision for this lead, or None if never QC'd. Cheap
    fast-path check against the archive DB (the source of truth for
    "has this been decided")."""
    init_qc_archive()
    with get_qc_session() as qs:
        row = qs.execute(
            select(QcArchiveEntry).where(QcArchiveEntry.lead_id == lead_id)
        ).scalar_one_or_none()
        return row.decision if row else None


def record_qc_decision(
    lead_id: int,
    decision: str,
    note: Optional[str] = None,
    decided_by: str = "operator",
) -> Dict[str, Any]:
    """Apply and archive one QC decision for a lead. Raises AlreadyQcdError
    if the lead was already decided (by this call or a concurrent one) —
    callers should treat that as a non-fatal, idempotent no-op.

    Ordering guarantees no-destructive-loss + no-double-QC (see module
    docstring): read+snapshot, archive-commit, THEN operational commit.
    """
    decision = (decision or "").strip().upper().replace("-", "_")
    decision = FLEET_LABEL_TO_DECISION.get(decision, decision)
    if decision not in QC_DECISIONS:
        raise ValueError(f"Invalid QC decision {decision!r}; allowed {sorted(QC_DECISIONS)}")

    init_qc_archive()

    # Fast-path: already archived (covers the common "double click" case
    # without even touching the operational DB).
    existing = already_qcd(lead_id)
    if existing is not None:
        raise AlreadyQcdError(lead_id, existing)

    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if lead is None:
            raise ValueError(f"Lead #{lead_id} not found")

        snapshot = _lead_snapshot(lead)
        provenance = _provenance(lead_id, session)
        decided_at = _now()

        # Archive first. If this raises (including IntegrityError from the
        # UNIQUE constraint — the true race backstop), the operational
        # session below never mutates anything: `with get_session()` only
        # commits on clean exit from its block, and we raise out of it.
        try:
            with get_qc_session() as qs:
                qs.add(QcArchiveEntry(
                    lead_id=lead_id,
                    decision=decision,
                    decided_at=decided_at,
                    decided_by=decided_by,
                    note=note,
                    item_snapshot=snapshot,
                    provenance=provenance,
                ))
        except IntegrityError:
            logger.info("QC race detected for lead #%s — already archived by a concurrent request", lead_id)
            raise AlreadyQcdError(lead_id, already_qcd(lead_id))

        # Operational side: legacy LeadFeedback row (unchanged shape/values
        # so existing reports/tests keep working) + immediate queue removal.
        session.add(LeadFeedback(
            lead_id=lead_id,
            feedback=decision,
            created_at=decided_at,
            note=note,
        ))
        lead.lead_status = QC_DECISION_TO_LEAD_STATUS[decision]
        lead.updated_at = decided_at

    return {
        "lead_id": lead_id,
        "decision": decision,
        "decided_at": decided_at,
        "new_lead_status": QC_DECISION_TO_LEAD_STATUS[decision],
    }


def list_recent_qc(limit: int = 50) -> List[Dict[str, Any]]:
    """Recently archived QC decisions, newest first — backs the "Recently
    QCed" GUI view. Reads only from the archive DB, so it reflects true
    QC history even if the operational lead row were later altered."""
    init_qc_archive()
    with get_qc_session() as qs:
        rows = qs.execute(
            select(QcArchiveEntry).order_by(desc(QcArchiveEntry.decided_at)).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "lead_id": r.lead_id,
                "decision": r.decision,
                "decided_at": r.decided_at,
                "decided_by": r.decided_by,
                "note": r.note,
                "headline_hint": (r.item_snapshot or {}).get("headline_hint"),
                "lead_type": (r.item_snapshot or {}).get("lead_type"),
                "priority_score": (r.item_snapshot or {}).get("priority_score"),
                "item_snapshot": r.item_snapshot,
                "provenance": r.provenance,
            }
            for r in rows
        ]
