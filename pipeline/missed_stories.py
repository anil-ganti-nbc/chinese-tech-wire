"""V0.5.6 — Manual missed-story recording and deterministic reconstruction.

No automatic English-media crawling. The operator records a miss they
noticed manually; this module helps reconstruct *why* CTW missed it (or
didn't) by reusing the existing explainability (pipeline.explain) and
source-health (pipeline.source_health) machinery — it never invents new
certainty. Findings are labeled "confirmed", "probable", or "unknown"
depending on how directly the evidence supports them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from database.db import get_session
from database.models import MissedStory, StoryCluster, StoryLead

logger = logging.getLogger(__name__)

FAILURE_STAGES = {
    "UNKNOWN",
    "SOURCE_NOT_MONITORED",
    "SOURCE_BLOCKED",
    "SOURCE_FAILED",
    "PARSER_MISSED",
    "ENTITY_MISSED",
    "CLUSTER_MISSED",
    "LOW_SCORE",
    "LIFECYCLE_FILTER",
    "ALERT_FILTER",
    "DUPLICATE_ERROR",
    "TOO_LATE",
    "OTHER",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_miss(
    title: str,
    article_url: Optional[str] = None,
    source: Optional[str] = None,
    related_entities: Optional[str] = None,
    expected_scope: Optional[str] = None,
    failure_stage: str = "UNKNOWN",
    notes: Optional[str] = None,
    matched_lead_id: Optional[int] = None,
    matched_cluster_id: Optional[int] = None,
    reported_at: Optional[datetime] = None,
) -> Optional[MissedStory]:
    failure_stage = (failure_stage or "UNKNOWN").strip().upper()
    if failure_stage not in FAILURE_STAGES:
        logger.error("Invalid failure_stage %r; allowed %s", failure_stage, sorted(FAILURE_STAGES))
        return None
    if not title or not title.strip():
        logger.error("Missed story requires a title")
        return None
    with get_session() as session:
        if matched_lead_id is not None and not session.get(StoryLead, matched_lead_id):
            logger.error("matched_lead_id #%s not found", matched_lead_id)
            return None
        if matched_cluster_id is not None and not session.get(StoryCluster, matched_cluster_id):
            logger.error("matched_cluster_id #%s not found", matched_cluster_id)
            return None
        row = MissedStory(
            title=title.strip(),
            reported_at=reported_at,
            recorded_at=_now(),
            article_url=article_url,
            source=source,
            related_entities=related_entities,
            expected_scope=expected_scope,
            failure_stage=failure_stage,
            notes=notes,
            matched_lead_id=matched_lead_id,
            matched_cluster_id=matched_cluster_id,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return row


def list_missed_stories(limit: int = 50) -> List[Dict[str, Any]]:
    with get_session() as session:
        rows = session.execute(
            select(MissedStory).order_by(desc(MissedStory.recorded_at)).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "recorded_at": r.recorded_at,
                "source": r.source,
                "failure_stage": r.failure_stage,
                "matched_lead_id": r.matched_lead_id,
                "matched_cluster_id": r.matched_cluster_id,
            }
            for r in rows
        ]


def missed_story_report() -> Dict[str, Any]:
    with get_session() as session:
        rows = session.execute(select(MissedStory)).scalars().all()
        by_stage: Dict[str, int] = {}
        for r in rows:
            by_stage[r.failure_stage] = by_stage.get(r.failure_stage, 0) + 1
        return {
            "total": len(rows),
            "by_failure_stage": by_stage,
            "linked_to_lead": sum(1 for r in rows if r.matched_lead_id),
            "linked_to_cluster": sum(1 for r in rows if r.matched_cluster_id and not r.matched_lead_id),
            "unlinked": sum(1 for r in rows if not r.matched_lead_id and not r.matched_cluster_id),
        }


def reconstruct_miss(missed_story_id: int) -> Optional[Dict[str, Any]]:
    """Deterministic, evidence-graded analysis of why a recorded miss
    happened (or didn't). Reuses pipeline.explain and pipeline.source_health
    rather than recomputing anything — never invents certainty."""
    from pipeline.explain import explain_lead_structured

    with get_session() as session:
        miss = session.get(MissedStory, missed_story_id)
        if not miss:
            return None

        result: Dict[str, Any] = {
            "missed_story_id": miss.id,
            "title": miss.title,
            "generated_at": _now(),
        }

        if miss.matched_lead_id:
            lead = session.get(StoryLead, miss.matched_lead_id)
            structured = explain_lead_structured(miss.matched_lead_id)
            alert = structured.alert_decision if structured else None
            result.update({
                "story_existed_in_ctw": "YES",
                "matched_lead_id": lead.id if lead else None,
                "priority": lead.priority_score if lead else None,
                "lifecycle_status": lead.lead_status if lead else None,
                "first_signal_source": lead.first_signal_source if lead else None,
                "first_signal_at": lead.first_signal_at if lead else None,
                "alert_decision": alert.get("reason_code") if alert else None,
                "alert_detail": alert.get("detail") if alert else None,
            })
            # Confirmed vs probable: an explicit suppression reason code
            # directly explains why it never alerted — that's confirmed.
            # In the absence of a confirmed alert-suppression reason, we
            # only have a probable inference from lifecycle status.
            if alert and not alert.get("eligible") and alert.get("reason_code"):
                stage_map = {
                    "SUPPRESSED_SCORE": "LOW_SCORE",
                    "SUPPRESSED_STATUS": "LIFECYCLE_FILTER",
                    "SUPPRESSED_STALE": "LIFECYCLE_FILTER",
                    "SUPPRESSED_EVIDENCE": "LOW_SCORE",
                    "SUPPRESSED_RELEVANCE": "LOW_SCORE",
                    "SUPPRESSED_CONFIDENCE": "LOW_SCORE",
                    "SUPPRESSED_BACKLOG": "ALERT_FILTER",
                    "SUPPRESSED_WEBHOOK": "ALERT_FILTER",
                    "SUPPRESSED_COOLDOWN": "ALERT_FILTER",
                    "SUPPRESSED_NOTIFIED": "ALERT_FILTER",
                }
                likely = stage_map.get(alert.get("reason_code"), "ALERT_FILTER")
                result["likely_failure_stage"] = likely
                result["certainty"] = "confirmed"
                suppress_reason = alert.get("reason_code")
                result["reasoning"] = (
                    f"Lead #{lead.id if lead else '?'} existed with priority "
                    f"{lead.priority_score:.1f} but alert policy suppressed it: {suppress_reason}."
                )
            elif lead and lead.lead_status == "NEW":
                result["likely_failure_stage"] = "LIFECYCLE_FILTER"
                result["certainty"] = "probable"
                result["reasoning"] = f"Lead #{lead.id} never progressed past NEW."
            else:
                result["likely_failure_stage"] = "UNKNOWN"
                result["certainty"] = "unknown"
                result["reasoning"] = "Lead existed and was alert-eligible or already alerted; miss cause unclear from stored data."
            return result

        if miss.matched_cluster_id:
            cluster = session.get(StoryCluster, miss.matched_cluster_id)
            lead = session.execute(
                select(StoryLead).where(StoryLead.cluster_id == miss.matched_cluster_id)
            ).scalar_one_or_none()
            if lead:
                # A lead does exist for this cluster after all.
                structured = explain_lead_structured(lead.id)
                alert = structured.alert_decision if structured else None
                result.update({
                    "story_existed_in_ctw": "YES",
                    "matched_cluster_id": cluster.id if cluster else None,
                    "matched_lead_id": lead.id,
                    "priority": lead.priority_score,
                    "lifecycle_status": lead.lead_status,
                    "alert_decision": alert.get("reason_code") if alert else None,
                    "likely_failure_stage": "LIFECYCLE_FILTER" if lead.lead_status == "NEW" else "UNKNOWN",
                    "certainty": "probable",
                    "reasoning": f"Cluster #{cluster.id if cluster else '?'} produced StoryLead #{lead.id}, status {lead.lead_status}.",
                })
                return result
            result.update({
                "story_existed_in_ctw": "YES",
                "matched_cluster_id": cluster.id if cluster else None,
                "matched_lead_id": None,
                "likely_failure_stage": "CLUSTER_MISSED",
                "certainty": "probable",
                "reasoning": (
                    f"Article/thread stored and clustered as #{cluster.id if cluster else '?'} "
                    "but never produced a StoryLead — likely below the lead-creation bar."
                ),
            })
            return result

        # Neither lead nor cluster matched — story may not exist in CTW at all.
        from pipeline.source_health import _registries, compute_source_health

        monitored = False
        health_now = None
        if miss.source:
            known = {r["name"] for r in _registries()}
            monitored = miss.source in known
            if monitored:
                rows = compute_source_health(source_filter=miss.source)
                health_now = rows[0]["status"] if rows else None

        result["story_existed_in_ctw"] = "NO"
        result["source_monitored"] = "YES" if monitored else ("NO" if miss.source else "UNKNOWN")
        result["source_health_now"] = health_now

        if miss.source and not monitored:
            result["likely_failure_stage"] = "SOURCE_NOT_MONITORED"
            result["certainty"] = "confirmed"
            result["reasoning"] = f"Source {miss.source!r} is not in any active registry."
        elif health_now in ("BLOCKED", "FAILING"):
            result["likely_failure_stage"] = "SOURCE_BLOCKED" if health_now == "BLOCKED" else "SOURCE_FAILED"
            result["certainty"] = "probable"
            result["reasoning"] = (
                f"Source {miss.source!r} is currently {health_now}. Historical health at "
                "the time of the miss is not tracked, so this is the current state, not a "
                "confirmed point-in-time cause."
            )
        else:
            result["likely_failure_stage"] = "UNKNOWN"
            result["certainty"] = "unknown"
            result["reasoning"] = "No matching Article/CommunityThread/DocumentaryRecord/cluster found, and the source (if any) appears healthy — cause not determinable from stored data alone."
        return result


def format_reconstruction_text(r: Dict[str, Any]) -> str:
    lines = [f"MISSED STORY RECONSTRUCTION — #{r['missed_story_id']}", "-" * 60, r["title"], ""]
    lines.append(f"Story existed in CTW: {r['story_existed_in_ctw']}")
    if r.get("matched_lead_id"):
        lines.append(f"StoryLead: #{r['matched_lead_id']}")
        lines.append(f"Priority: {r.get('priority')}")
        lines.append(f"Lifecycle: {r.get('lifecycle_status')}")
        lines.append(f"Alert decision: {r.get('alert_decision')}")
    if r.get("matched_cluster_id"):
        lines.append(f"Cluster: #{r['matched_cluster_id']}")
    if "source_monitored" in r:
        lines.append(f"Source monitored: {r['source_monitored']}")
        lines.append(f"Source health now: {r.get('source_health_now') or '—'}")
    lines.append("")
    lines.append(f"{r['certainty']} failure stage: {r['likely_failure_stage']}")
    lines.append(r.get("reasoning", ""))
    return "\n".join(lines)


def format_missed_stories_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No missed stories recorded yet."
    lines = ["MISSED STORIES", "-" * 78]
    for r in rows:
        ts = r["recorded_at"].strftime("%Y-%m-%d %H:%M") if r["recorded_at"] else "—"
        link = f"lead=#{r['matched_lead_id']}" if r["matched_lead_id"] else (
            f"cluster=#{r['matched_cluster_id']}" if r["matched_cluster_id"] else "unlinked"
        )
        lines.append(f"#{r['id']:<4} {ts}  [{r['failure_stage']:<20}] {link:<12} {r['title'][:60]}")
    return "\n".join(lines)
