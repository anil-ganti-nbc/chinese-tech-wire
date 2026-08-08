"""StoryLead Discord alert eligibility, decision telemetry, and ledger writes.

Separated from lifecycle scoring: a WATCHING lead can be alert-worthy without
meeting ACTIONABLE status semantics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from config import yaml_config
from database.db import get_session
from database.models import LeadEvent, LeadNotification, StoryLead
from pipeline.notify import DiscordSendResult, build_storylead_payload, send_discord_result

logger = logging.getLogger(__name__)

# Module-level counters for the current cycle (reset via reset_alert_stats)
_stats: Dict[str, int] = {}


def reset_alert_stats() -> None:
    global _stats
    _stats = {
        "alerts_evaluated": 0,
        "alerts_eligible": 0,
        "alerts_attempted": 0,
        "alerts_sent": 0,
        "alerts_failed": 0,
        "alerts_suppressed_notified": 0,
        "alerts_suppressed_stale": 0,
        "alerts_suppressed_score": 0,
        "alerts_suppressed_quality": 0,
        "alerts_suppressed_status": 0,
        "alerts_suppressed_backlog": 0,
        "alerts_suppressed_webhook": 0,
        "alerts_dry_run": 0,
        "alerts_suppressed_other": 0,
    }


def get_alert_stats() -> Dict[str, int]:
    if not _stats:
        reset_alert_stats()
    return dict(_stats)


def _bump(key: str, n: int = 1) -> None:
    if not _stats:
        reset_alert_stats()
    _stats[key] = _stats.get(key, 0) + n


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _alert_cfg() -> Dict[str, Any]:
    nr = yaml_config.get("newsroom", {}) or {}
    # Prefer nested alerts block; fall back to legacy flat keys
    nested = nr.get("alerts") or {}
    return {
        "enabled": bool(nested.get("enabled", True)),
        "min_priority": float(nested.get("min_priority", nr.get("alert_threshold", 52))),
        "high_priority": float(nested.get("high_priority", 55)),
        "min_evidence": float(nested.get("min_evidence", 15)),
        "min_relevance": float(nested.get("min_relevance", 35)),
        "min_confidence": float(nested.get("min_confidence", 25)),
        "allowed_statuses": list(
            nested.get(
                "allowed_statuses",
                ["WATCHING", "ACTIONABLE", "ESCALATED"],
            )
        ),
        "allowed_lead_types": list(nested.get("allowed_lead_types") or []),
        "blocked_lead_types": list(nested.get("blocked_lead_types") or []),
        "max_age_hours": float(nested.get("max_age_hours", 48)),
        "material_score_delta": float(nested.get("material_score_delta", 8)),
        "cooldown_hours": float(nested.get("cooldown_hours", 12)),
        "suppress_before_activation": bool(nested.get("suppress_before_activation", True)),
        "policy_activated_at": nested.get("policy_activated_at"),
        "max_alerts_per_cycle": int(nested.get("max_alerts_per_cycle", 5)),
        "evidence_delta": float(nested.get("evidence_delta", 5)),
    }


def ensure_policy_activation() -> Optional[str]:
    """If policy_activated_at is unset, stamp it to now in-memory for this process.

    Operators should persist the timestamp into settings.yaml after first deploy.
    Runtime also stores activation on first use via a sentinel LeadNotification
    reason=POLICY_ACTIVATED when suppress_before_activation is true.
    """
    cfg = _alert_cfg()
    if cfg.get("policy_activated_at"):
        return str(cfg["policy_activated_at"])
    # Look for persisted activation row
    try:
        with get_session() as session:
            row = session.execute(
                select(LeadNotification)
                .where(LeadNotification.outcome == "POLICY_ACTIVATED")
                .order_by(LeadNotification.attempted_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if row and row.attempted_at:
                return _aware(row.attempted_at).isoformat()
            # Create activation marker — no Discord send
            now = _now()
            session.add(
                LeadNotification(
                    lead_id=None,
                    cluster_id=None,
                    attempted_at=now,
                    sent_at=None,
                    outcome="POLICY_ACTIVATED",
                    reason_code="POLICY_ACTIVATED",
                    lead_status=None,
                    priority_score=None,
                    evidence_score=None,
                    relevance_score=None,
                    confidence_score=None,
                    webhook_http_status=None,
                    error_text="Alert policy activated; historical backlog suppressed",
                    payload_version="v1",
                )
            )
            return now.isoformat()
    except Exception as e:
        logger.warning("[ALERT] policy activation stamp failed: %s", e)
        return _now().isoformat()


@dataclass
class AlertDecision:
    eligible: bool
    would_send: bool
    reason_code: str
    detail: str = ""
    alert_reason: Optional[str] = None  # FIRST_ELIGIBLE, MATERIAL_SCORE_INCREASE, ...

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_alert_eligibility(
    lead: StoryLead,
    *,
    prev_status: Optional[str] = None,
    policy_activated_at: Optional[str] = None,
    ignore_backlog_gate: bool = False,
) -> AlertDecision:
    """Pure eligibility decision for a StoryLead under current config."""
    cfg = _alert_cfg()
    if not cfg["enabled"]:
        return AlertDecision(False, False, "ALERTS_DISABLED", "newsroom.alerts.enabled is false")

    status = (lead.lead_status or "").upper()
    score = float(lead.priority_score or 0)
    evid = float(lead.evidence_score or 0)
    rel = float(lead.relevance_score or 0)
    conf = float(lead.confidence_score or 0)
    activity = _aware(lead.last_activity_at or lead.updated_at or lead.created_at)
    age_h = ((_now() - activity).total_seconds() / 3600.0) if activity else 9999.0

    if status not in {s.upper() for s in cfg["allowed_statuses"]}:
        return AlertDecision(False, False, "SUPPRESSED_STATUS", f"status={status}")

    ltype = (lead.lead_type or "").upper()
    blocked = {x.upper() for x in cfg.get("blocked_lead_types") or []}
    allowed_types = {x.upper() for x in cfg.get("allowed_lead_types") or []}
    if blocked and ltype in blocked:
        return AlertDecision(False, False, "SUPPRESSED_TYPE", f"type={ltype}")
    if allowed_types and ltype not in allowed_types:
        return AlertDecision(False, False, "SUPPRESSED_TYPE", f"type={ltype} not in allowlist")

    if score < cfg["min_priority"]:
        return AlertDecision(False, False, "SUPPRESSED_SCORE", f"P={score:.1f} < {cfg['min_priority']}")

    if evid < cfg["min_evidence"]:
        return AlertDecision(False, False, "SUPPRESSED_QUALITY", f"evidence={evid:.0f}")
    if rel < cfg["min_relevance"]:
        return AlertDecision(False, False, "SUPPRESSED_QUALITY", f"relevance={rel:.0f}")
    if conf < cfg["min_confidence"]:
        return AlertDecision(False, False, "SUPPRESSED_QUALITY", f"confidence={conf:.0f}")

    if age_h > cfg["max_age_hours"]:
        return AlertDecision(False, False, "SUPPRESSED_STALE", f"age_h={age_h:.1f}")

    # Backlog protection: activity must be on/after policy activation
    if cfg["suppress_before_activation"] and not ignore_backlog_gate:
        act_raw = policy_activated_at or cfg.get("policy_activated_at")
        if act_raw:
            try:
                act = datetime.fromisoformat(str(act_raw).replace("Z", "+00:00"))
                act = _aware(act)
                if activity and act and activity < act:
                    return AlertDecision(
                        False, False, "SUPPRESSED_BACKLOG",
                        f"activity before policy activation {act.isoformat()}",
                    )
            except Exception:
                pass

    # Already notified: only re-alert on meaningful transitions + cooldown
    if lead.notified:
        if lead.last_notified_at:
            ln = _aware(lead.last_notified_at)
            if ln and (_now() - ln).total_seconds() / 3600.0 < cfg.get("cooldown_hours", 12):
                return AlertDecision(False, False, "SUPPRESSED_COOLDOWN", "within cooldown after last notify")
        alert_reason = _realert_reason(lead, prev_status, cfg)
        if not alert_reason:
            return AlertDecision(False, False, "SUPPRESSED_NOTIFIED", "already notified; no material change")
        return AlertDecision(True, True, "ELIGIBLE", "re-alert", alert_reason=alert_reason)

    return AlertDecision(True, True, "ELIGIBLE", "first eligible", alert_reason="FIRST_ELIGIBLE")


def _realert_reason(lead: StoryLead, prev_status: Optional[str], cfg: Dict[str, Any]) -> Optional[str]:
    status = (lead.lead_status or "").upper()
    last_status = (lead.last_notified_status or "").upper() if lead.last_notified_status else None

    if prev_status and prev_status != status:
        if status == "ACTIONABLE" and prev_status in ("WATCHING", "NEW", "STALE"):
            return "BECAME_ACTIONABLE"
        if status == "ESCALATED":
            return "BECAME_ESCALATED"
        if prev_status == "STALE" and status in ("WATCHING", "ACTIONABLE", "ESCALATED"):
            return "RESURGENCE"

    last_score = getattr(lead, "last_notified_priority", None)
    if last_score is None:
        last_score = None
    if last_score is not None:
        delta = float(lead.priority_score or 0) - float(last_score)
        if delta >= cfg["material_score_delta"]:
            return "MATERIAL_SCORE_INCREASE"

    # Documentary / media transitions recorded via LeadEvent elsewhere; callers may pass
    # an explicit alert_reason override. Default: no re-alert.
    return None


def record_ledger(
    session,
    lead: Optional[StoryLead],
    *,
    outcome: str,
    reason_code: str,
    result: Optional[DiscordSendResult] = None,
    alert_reason: Optional[str] = None,
    ingestion_run_id: Optional[int] = None,
) -> None:
    now = _now()
    row = LeadNotification(
        lead_id=lead.id if lead else None,
        cluster_id=lead.cluster_id if lead else None,
        attempted_at=now,
        sent_at=now if (result and result.sent and not result.dry_run) else None,
        outcome=outcome,
        reason_code=reason_code,
        alert_reason=alert_reason,
        lead_status=lead.lead_status if lead else None,
        priority_score=float(lead.priority_score) if lead else None,
        evidence_score=float(lead.evidence_score) if lead else None,
        relevance_score=float(lead.relevance_score) if lead else None,
        confidence_score=float(lead.confidence_score) if lead else None,
        webhook_http_status=result.status_code if result else None,
        error_text=(result.error[:500] if result and result.error else None),
        ingestion_run_id=ingestion_run_id,
        payload_version="v1",
    )
    session.add(row)


def maybe_alert_lead(
    session,
    lead: StoryLead,
    *,
    prev_status: Optional[str] = None,
    dry_run: bool = False,
    ingestion_run_id: Optional[int] = None,
    policy_activated_at: Optional[str] = None,
) -> AlertDecision:
    """Evaluate and optionally send a StoryLead Discord alert. Updates telemetry."""
    _bump("alerts_evaluated")
    decision = evaluate_alert_eligibility(
        lead,
        prev_status=prev_status,
        policy_activated_at=policy_activated_at,
    )

    if not decision.eligible:
        key_map = {
            "SUPPRESSED_STATUS": "alerts_suppressed_status",
            "SUPPRESSED_SCORE": "alerts_suppressed_score",
            "SUPPRESSED_QUALITY": "alerts_suppressed_quality",
            "SUPPRESSED_STALE": "alerts_suppressed_stale",
            "SUPPRESSED_BACKLOG": "alerts_suppressed_backlog",
            "SUPPRESSED_NOTIFIED": "alerts_suppressed_notified",
            "ALERTS_DISABLED": "alerts_suppressed_other",
        }
        _bump(key_map.get(decision.reason_code, "alerts_suppressed_other"))
        return decision

    # Cap per cycle
    if get_alert_stats().get("alerts_sent", 0) + get_alert_stats().get("alerts_attempted", 0) >= _alert_cfg()[
        "max_alerts_per_cycle"
    ] and get_alert_stats().get("alerts_sent", 0) >= _alert_cfg()["max_alerts_per_cycle"]:
        _bump("alerts_suppressed_other")
        decision = AlertDecision(False, False, "SUPPRESSED_CYCLE_CAP", "max_alerts_per_cycle reached")
        return decision

    if get_alert_stats().get("alerts_sent", 0) >= _alert_cfg()["max_alerts_per_cycle"]:
        _bump("alerts_suppressed_other")
        return AlertDecision(False, False, "SUPPRESSED_CYCLE_CAP", "max_alerts_per_cycle reached")

    _bump("alerts_eligible")
    payload = build_storylead_payload(lead, high=float(lead.priority_score or 0) >= _alert_cfg()["high_priority"])
    _bump("alerts_attempted")
    result = send_discord_result(payload, dry_run=dry_run)

    if result.dry_run:
        _bump("alerts_dry_run")
        record_ledger(
            session, lead, outcome="DRY_RUN", reason_code="DRY_RUN",
            result=result, alert_reason=decision.alert_reason,
            ingestion_run_id=ingestion_run_id,
        )
        session.flush()
        logger.info(
            "[ALERT] DRY-RUN lead #%s P=%.1f %s reason=%s",
            lead.id, lead.priority_score, lead.lead_status, decision.alert_reason,
        )
        return decision

    if result.reason == "NO_WEBHOOK":
        _bump("alerts_suppressed_webhook")
        record_ledger(
            session, lead, outcome="FAILED", reason_code="NO_WEBHOOK",
            result=result, alert_reason=decision.alert_reason,
            ingestion_run_id=ingestion_run_id,
        )
        session.flush()
        # Do not mark notified — retryable when webhook appears
        return decision

    if result.sent:
        _bump("alerts_sent")
        lead.notified = True
        lead.last_notified_at = _now()
        lead.last_notified_status = lead.lead_status
        if hasattr(lead, "last_notified_priority"):
            lead.last_notified_priority = float(lead.priority_score or 0)
        record_ledger(
            session, lead, outcome="SENT", reason_code="SENT",
            result=result, alert_reason=decision.alert_reason,
            ingestion_run_id=ingestion_run_id,
        )
        session.add(LeadEvent(
            lead_id=lead.id,
            event_type="ALERT_SENT",
            observed_at=_now(),
            summary=f"{decision.alert_reason} P={lead.priority_score:.0f}",
        ))
        session.flush()
        logger.info(
            "[ALERT] SENT lead #%s P=%.1f %s reason=%s",
            lead.id, lead.priority_score, lead.lead_status, decision.alert_reason,
        )
    else:
        _bump("alerts_failed")
        record_ledger(
            session, lead, outcome="FAILED", reason_code=result.reason or "HTTP_ERROR",
            result=result, alert_reason=decision.alert_reason,
            ingestion_run_id=ingestion_run_id,
        )
        session.flush()
        logger.error(
            "[ALERT] FAILED lead #%s reason=%s status=%s err=%s",
            lead.id, result.reason, result.status_code, (result.error or "")[:120],
        )
        # not marked notified — remains retryable

    return decision


def preview_alerts(
    *,
    since_hours: float = 24,
    limit: int = 25,
    ignore_backlog_gate: bool = False,
) -> List[Dict[str, Any]]:
    """List recent leads with eligibility decisions. No Discord POST."""
    cfg = _alert_cfg()
    activation = None
    if cfg["suppress_before_activation"] and not ignore_backlog_gate:
        activation = cfg.get("policy_activated_at") or ensure_policy_activation()

    cutoff = _now() - timedelta(hours=since_hours)
    rows: List[Dict[str, Any]] = []
    with get_session() as session:
        leads = list(
            session.execute(
                select(StoryLead)
                .where(StoryLead.last_activity_at >= cutoff)
                .order_by(StoryLead.priority_score.desc())
                .limit(max(limit * 5, 100))
            ).scalars().all()
        )
        # also include by created_at if last_activity null
        if len(leads) < limit:
            extra = list(
                session.execute(
                    select(StoryLead)
                    .where(StoryLead.created_at >= cutoff)
                    .order_by(StoryLead.priority_score.desc())
                    .limit(100)
                ).scalars().all()
            )
            seen = {L.id for L in leads}
            for L in extra:
                if L.id not in seen:
                    leads.append(L)

        leads.sort(key=lambda L: float(L.priority_score or 0), reverse=True)
        for L in leads:
            decision = evaluate_alert_eligibility(
                L,
                policy_activated_at=activation,
                ignore_backlog_gate=ignore_backlog_gate,
            )
            activity = _aware(L.last_activity_at or L.created_at)
            age_h = ((_now() - activity).total_seconds() / 3600.0) if activity else None
            rows.append({
                "id": L.id,
                "score": round(float(L.priority_score or 0), 1),
                "status": L.lead_status,
                "type": L.lead_type,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "evidence": round(float(L.evidence_score or 0), 1),
                "relevance": round(float(L.relevance_score or 0), 1),
                "confidence": round(float(L.confidence_score or 0), 1),
                "notified": bool(L.notified),
                "eligible": decision.eligible,
                "would_send": decision.would_send,
                "reason": decision.reason_code,
                "detail": decision.detail,
                "alert_reason": decision.alert_reason,
                "headline": (L.headline_hint or "")[:100],
            })
            if len([r for r in rows if r["eligible"]]) >= limit and len(rows) >= limit:
                break
        rows = rows[:limit]
    return rows


def diagnose_alert_policy() -> Dict[str, Any]:
    """Read-only soak diagnostics: scores, funnel, predicted volume."""
    from sqlalchemy import func, and_, or_

    cfg = _alert_cfg()
    now = _now()

    def percentile(sorted_vals, p):
        if not sorted_vals:
            return None
        i = min(len(sorted_vals) - 1, max(0, int(round((p / 100) * (len(sorted_vals) - 1)))))
        return round(sorted_vals[i], 2)

    score_fields = [
        "priority_score", "evidence_score", "relevance_score", "confidence_score",
        "novelty_score", "exclusivity_score", "momentum_score",
        "source_diversity_score", "media_saturation_score",
    ]

    with get_session() as session:
        leads = list(session.execute(select(StoryLead)).scalars().all())
        n = len(leads)
        status_counts: Dict[str, int] = {}
        type_counts: Dict[str, int] = {}
        age_buckets = {"under_6h": 0, "under_12h": 0, "under_24h": 0, "under_48h": 0, "over_48h": 0}
        distributions: Dict[str, Dict[str, Any]] = {}

        for field in score_fields:
            vals = sorted(float(getattr(L, field) or 0) for L in leads)
            distributions[field] = {
                "min": percentile(vals, 0) if vals else None,
                "p25": percentile(vals, 25),
                "median": percentile(vals, 50),
                "p75": percentile(vals, 75),
                "p90": percentile(vals, 90),
                "p95": percentile(vals, 95),
                "p99": percentile(vals, 99),
                "max": percentile(vals, 100) if vals else None,
            }

        activation = cfg.get("policy_activated_at")
        if cfg["suppress_before_activation"] and not activation:
            try:
                row = session.execute(
                    select(LeadNotification)
                    .where(LeadNotification.outcome == "POLICY_ACTIVATED")
                    .order_by(LeadNotification.attempted_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if row and row.attempted_at:
                    activation = _aware(row.attempted_at).isoformat()
            except Exception:
                pass

        funnel = {
            "total_evaluated": n,
            "alerts_disabled": 0,
            "status_rejected": 0,
            "type_rejected": 0,
            "too_old": 0,
            "pre_policy_backlog": 0,
            "priority_too_low": 0,
            "evidence_too_low": 0,
            "relevance_too_low": 0,
            "confidence_too_low": 0,
            "already_notified": 0,
            "cooldown": 0,
            "eligible": 0,
        }
        if not cfg["enabled"]:
            funnel["alerts_disabled"] = n
        else:
            for L in leads:
                st = (L.lead_status or "").upper()
                status_counts[st] = status_counts.get(st, 0) + 1
                lt = (L.lead_type or "UNKNOWN").upper()
                type_counts[lt] = type_counts.get(lt, 0) + 1
                activity = _aware(L.last_activity_at or L.updated_at or L.created_at)
                age_h = ((now - activity).total_seconds() / 3600.0) if activity else 9999.0
                if age_h < 6:
                    age_buckets["under_6h"] += 1
                if age_h < 12:
                    age_buckets["under_12h"] += 1
                if age_h < 24:
                    age_buckets["under_24h"] += 1
                if age_h < 48:
                    age_buckets["under_48h"] += 1
                else:
                    age_buckets["over_48h"] += 1

                d = evaluate_alert_eligibility(L, policy_activated_at=activation)
                code = d.reason_code
                if d.eligible:
                    funnel["eligible"] += 1
                elif code == "SUPPRESSED_STATUS":
                    funnel["status_rejected"] += 1
                elif code == "SUPPRESSED_TYPE":
                    funnel["type_rejected"] += 1
                elif code == "SUPPRESSED_STALE":
                    funnel["too_old"] += 1
                elif code == "SUPPRESSED_BACKLOG":
                    funnel["pre_policy_backlog"] += 1
                elif code == "SUPPRESSED_SCORE":
                    funnel["priority_too_low"] += 1
                elif code == "SUPPRESSED_QUALITY":
                    # distinguish which quality gate
                    evid = float(L.evidence_score or 0)
                    rel = float(L.relevance_score or 0)
                    conf = float(L.confidence_score or 0)
                    if evid < cfg["min_evidence"]:
                        funnel["evidence_too_low"] += 1
                    elif rel < cfg["min_relevance"]:
                        funnel["relevance_too_low"] += 1
                    else:
                        funnel["confidence_too_low"] += 1
                elif code == "SUPPRESSED_NOTIFIED":
                    funnel["already_notified"] += 1
                elif code == "SUPPRESSED_COOLDOWN":
                    funnel["cooldown"] += 1
                elif code == "ALERTS_DISABLED":
                    funnel["alerts_disabled"] += 1

        # Predicted volume: eligible with activity in window (ignore backlog gate for historical sim uses activation)
        def count_eligible_since(hours: float) -> int:
            cut = now - timedelta(hours=hours)
            n_ok = 0
            for L in leads:
                activity = _aware(L.last_activity_at or L.created_at)
                if not activity or activity < cut:
                    continue
                d = evaluate_alert_eligibility(L, policy_activated_at=activation, ignore_backlog_gate=False)
                if d.eligible and not L.notified:
                    n_ok += 1
            return n_ok

        pred_24 = count_eligible_since(24)
        pred_48 = count_eligible_since(48)
        pred_168 = count_eligible_since(168)

        ledger_sent = ledger_failed = 0
        try:
            ledger_sent = session.execute(
                select(func.count()).select_from(LeadNotification).where(LeadNotification.outcome == "SENT")
            ).scalar() or 0
            ledger_failed = session.execute(
                select(func.count()).select_from(LeadNotification).where(LeadNotification.outcome == "FAILED")
            ).scalar() or 0
        except Exception:
            pass

        # reconcile note
        accounted = (
            funnel["alerts_disabled"] + funnel["status_rejected"] + funnel["type_rejected"]
            + funnel["too_old"] + funnel["pre_policy_backlog"] + funnel["priority_too_low"]
            + funnel["evidence_too_low"] + funnel["relevance_too_low"] + funnel["confidence_too_low"]
            + funnel["already_notified"] + funnel["cooldown"] + funnel["eligible"]
        )

        return {
            "n_leads": n,
            "status_counts": status_counts,
            "type_counts": type_counts,
            "age_buckets": age_buckets,
            "distributions": distributions,
            "funnel": funnel,
            "funnel_accounted": accounted,
            "predicted_alerts_24h": pred_24,
            "predicted_alerts_48h": pred_48,
            "predicted_alerts_7d": pred_168,
            "predicted_daily_avg_7d": round(pred_168 / 7.0, 2) if pred_168 else 0.0,
            "config": cfg,
            "policy_activated_at": activation,
            "ledger_sent": ledger_sent,
            "ledger_failed": ledger_failed,
        }


def feedback_report() -> Dict[str, Any]:
    """Read-only analysis of LeadFeedback. Does not change weights."""
    from sqlalchemy import func
    from database.models import LeadFeedback

    with get_session() as session:
        rows = list(session.execute(select(LeadFeedback)).scalars().all())
        if not rows:
            return {"total": 0, "message": "No feedback recorded yet — sample too small for conclusions."}

        counts: Dict[str, int] = {}
        for r in rows:
            counts[r.feedback] = counts.get(r.feedback, 0) + 1
        total = len(rows)
        lead_ids = {r.lead_id for r in rows}
        leads = {
            L.id: L
            for L in session.execute(select(StoryLead).where(StoryLead.id.in_(lead_ids))).scalars().all()
        }
        by_type: Dict[str, Dict[str, int]] = {}
        score_by_fb: Dict[str, List[float]] = {}
        for r in rows:
            L = leads.get(r.lead_id)
            lt = (L.lead_type if L else "UNKNOWN") or "UNKNOWN"
            by_type.setdefault(lt, {})
            by_type[lt][r.feedback] = by_type[lt].get(r.feedback, 0) + 1
            score_by_fb.setdefault(r.feedback, []).append(float(L.priority_score or 0) if L else 0)

        def rate(key):
            return round(100.0 * counts.get(key, 0) / total, 1) if total else 0.0

        return {
            "total": total,
            "counts": counts,
            "useful_rate_pct": rate("USEFUL"),
            "written_rate_pct": rate("WRITTEN"),
            "not_useful_rate_pct": rate("NOT_USEFUL"),
            "false_positive_rate_pct": rate("FALSE_POSITIVE"),
            "duplicate_rate_pct": rate("DUPLICATE"),
            "by_lead_type": by_type,
            "avg_priority_by_feedback": {
                k: round(sum(v) / len(v), 1) if v else None for k, v in score_by_fb.items()
            },
            "message": (
                "Sample is small; treat rates as directional only."
                if total < 20
                else "Feedback sample usable for directional analysis; weights unchanged."
            ),
        }


