"""Canonical StoryLead explainability, provenance timeline, and material-change audit.

Deterministic. Derived only from stored records and configured rules.
Shared by CLI and GUI — do not fork explanation logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from config import yaml_config
from database.db import get_session
from database.models import (
    Article,
    CommunityThread,
    DocumentaryEvent,
    DocumentaryRecord,
    LeadEvent,
    LeadFeedback,
    LeadNotification,
    StoryCluster,
    StoryLead,
)
from pipeline.alerts import evaluate_alert_eligibility, ensure_policy_activation, _alert_cfg

logger = logging.getLogger(__name__)

EXPLANATION_VERSION = "v1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _hours_ago(dt: Optional[datetime], now: Optional[datetime] = None) -> float:
    now = now or _now()
    dt = _aware(dt)
    if not dt:
        return 9999.0
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _cfg_newsroom() -> Dict[str, Any]:
    return yaml_config.get("newsroom", {}) or {}


def _weights() -> Dict[str, float]:
    w = (_cfg_newsroom().get("weights") or {}).copy()
    defaults = {
        "novelty": 0.18,
        "evidence": 0.20,
        "exclusivity": 0.15,
        "momentum": 0.12,
        "source_diversity": 0.10,
        "confidence": 0.12,
        "relevance": 0.13,
    }
    for k, v in defaults.items():
        w.setdefault(k, v)
    return {k: float(v) for k, v in w.items()}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Contribution:
    component: str
    raw: float
    weight: float
    contribution: float
    facts: List[str] = field(default_factory=list)


@dataclass
class ThresholdCheck:
    code: str
    message: str
    actual: Any
    threshold: Any
    unit: str = ""
    passed: bool = True
    source_rule: str = ""


@dataclass
class TimelineEvent:
    timestamp: Optional[str]
    event_type: str
    layer: str
    source: str
    title: str
    summary: str
    url: Optional[str] = None
    record_id: Optional[str] = None
    evidence_type: Optional[str] = None
    score_or_value: Optional[float] = None
    is_first_of_type: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    inferred: bool = False

    def sort_key(self) -> Tuple:
        ts = self.timestamp or ""
        return (ts, self.layer, self.event_type, self.source, self.title)


@dataclass
class MaterialChange:
    field: str
    before: Any
    after: Any
    message: str


@dataclass
class LeadExplanation:
    lead_id: int
    cluster_id: Optional[int]
    headline: Optional[str]
    lead_type: str
    lead_status: str
    priority_score: float
    component_scores: Dict[str, float]
    weighted_contributions: List[Dict[str, Any]]
    time_decay: float
    caps_and_penalties: List[str]
    status_reasons: List[str]
    alert_decision: Dict[str, Any]
    evidence_summary: Optional[str]
    uncertainty_summary: Optional[str]
    chronology_summary: str
    source_summary: Dict[str, Any]
    material_changes: List[Dict[str, Any]]
    generated_at: str
    explanation_version: str = EXPLANATION_VERSION
    human_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Load cluster context for factual statements
# ---------------------------------------------------------------------------

def _load_cluster_facts(session, lead: StoryLead) -> Dict[str, Any]:
    arts: List[Article] = []
    cts: List[CommunityThread] = []
    docs: List[DocumentaryRecord] = []
    if lead.cluster_id:
        arts = list(
            session.execute(
                select(Article)
                .where(Article.duplicate_group_id == lead.cluster_id)
                .order_by(Article.discovered_at.asc())
            ).scalars().all()
        )
        cts = list(
            session.execute(
                select(CommunityThread)
                .where(CommunityThread.story_cluster_id == lead.cluster_id)
                .order_by(CommunityThread.discovered_at.asc())
            ).scalars().all()
        )
        docs = list(
            session.execute(
                select(DocumentaryRecord)
                .where(DocumentaryRecord.story_cluster_id == lead.cluster_id)
                .order_by(DocumentaryRecord.first_seen_at.asc())
            ).scalars().all()
        )
    return {"articles": arts, "community": cts, "docs": docs}


def _component_facts(lead: StoryLead, facts: Dict[str, Any], component: str) -> List[str]:
    """Deterministic supporting facts from stored members — never invent."""
    out: List[str] = []
    arts, cts, docs = facts["articles"], facts["community"], facts["docs"]
    age_h = _hours_ago(lead.last_activity_at or lead.updated_at)

    if component == "novelty":
        if lead.first_signal_source and not lead.first_media_source:
            out.append(f"First signal from {lead.first_signal_source}; no media pickup recorded yet.")
        elif lead.first_signal_source and lead.first_media_source:
            out.append(
                f"First signal {lead.first_signal_source}; first media {lead.first_media_source}."
            )
        if age_h < 12:
            out.append(f"Last activity {age_h:.1f}h ago (under 12h).")
        elif age_h < 48:
            out.append(f"Last activity {age_h:.1f}h ago.")
        if not out:
            out.append("Novelty derived from stored first-signal / media chronology fields.")

    elif component == "evidence":
        photo = [c for c in cts if (c.signal_type or "").upper() in ("PHOTO_EVIDENCE", "LEAK", "FIRSTHAND")]
        if photo:
            out.append(
                f"{len(photo)} community thread(s) with signal_type in "
                f"PHOTO_EVIDENCE/LEAK/FIRSTHAND (e.g. {photo[0].platform})."
            )
        if docs:
            out.append(f"{len(docs)} documentary record(s) in cluster (e.g. {docs[0].source}).")
        else:
            out.append("No documentary records linked to this cluster.")
        if not cts and not docs:
            out.append("Evidence based on news articles only." if arts else "No linked evidence members.")

    elif component == "exclusivity":
        media_sources = sorted({a.source for a in arts})
        out.append(f"{len(media_sources)} distinct media source(s): {', '.join(media_sources) or 'none'}.")
        if lead.first_signal_source:
            out.append(f"First signal source field: {lead.first_signal_source}.")
        if lead.media_saturation_score and lead.media_saturation_score >= 60:
            out.append(f"Media saturation score {lead.media_saturation_score:.0f} reduces exclusivity.")

    elif component == "momentum":
        out.append(
            f"Counts on lead: news={lead.news_count}, community={lead.community_count}, "
            f"documentary={lead.documentary_count}."
        )
        window = float(_cfg_newsroom().get("momentum_window_hours", 3))
        out.append(f"Momentum window configured at {window}h.")

    elif component == "source_diversity":
        layers = []
        if lead.community_count:
            layers.append("community")
        if lead.news_count:
            layers.append("news")
        if lead.documentary_count:
            layers.append("documentary")
        out.append(f"Layers present: {', '.join(layers) or 'none'} (source_count={lead.source_count}).")

    elif component == "confidence":
        if docs:
            out.append("Documentary members present — confidence can rise from structured records.")
        if cts:
            types = sorted({(c.signal_type or 'UNKNOWN') for c in cts})
            out.append(f"Community signal types: {', '.join(types)}.")
        if not docs and not cts:
            out.append("Confidence driven primarily by news coverage density.")

    elif component == "relevance":
        ents = []
        if isinstance(lead.primary_entities, dict):
            ents = lead.primary_entities.get("entities") or []
        if ents:
            out.append(f"Primary entities: {', '.join(str(e) for e in ents[:8])}.")
        else:
            out.append("No primary_entities list stored on this lead.")
        watch = _cfg_newsroom().get("watchlist_companies") or []
        if watch and ents:
            hits = [e for e in ents if any(str(w).lower() in str(e).lower() for w in watch)]
            if hits:
                out.append(f"Watchlist hits among entities: {', '.join(str(h) for h in hits[:5])}.")

    elif component == "media_saturation":
        out.append(f"news_count={lead.news_count}; media_saturation_score={lead.media_saturation_score:.1f}.")

    return out


def _caps_and_penalties(lead: StoryLead, bd: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    evid = float(lead.evidence_score or 0)
    mom = float(lead.momentum_score or 0)
    sat = float(lead.media_saturation_score or 0)
    if evid < 30 and mom > 70:
        notes.append(
            "Speculation cap: evidence < 30 and momentum > 70 — raw score capped at 55 before time decay "
            "(rule in compute_editorial)."
        )
    if sat >= 70:
        notes.append(f"High media saturation ({sat:.0f}) typically lowers exclusivity contribution.")
    decay = float(bd.get("time_decay") or 0)
    if decay < 0:
        notes.append(f"Time decay applied: {decay:+.2f} (age of last activity).")
    if not notes:
        notes.append("No hard speculation/noise caps recorded for this lead beyond standard time decay.")
    return notes


def _status_reasons(lead: StoryLead) -> List[str]:
    cfg = _cfg_newsroom()
    actionable = float(cfg.get("actionable_threshold", 70))
    watching = float(cfg.get("watching_threshold", 45))
    stale_h = float(cfg.get("stale_after_hours", 72))
    p = float(lead.priority_score or 0)
    evid = float(lead.evidence_score or 0)
    conf = float(lead.confidence_score or 0)
    age_h = _hours_ago(lead.last_activity_at or lead.updated_at)
    st = (lead.lead_status or "NEW").upper()
    reasons: List[str] = [f"Status: {st}"]

    if st == "ACTIONABLE":
        reasons.append(
            f"Priority {p:.1f} ≥ actionable_threshold {actionable} "
            f"and evidence/confidence/docs/media gates in classify_status were met."
        )
    elif st == "ESCALATED":
        reasons.append(
            f"Priority {p:.1f} met actionable path with elevated media coverage "
            f"(news_count={lead.news_count}) and lower exclusivity."
        )
    elif st == "WATCHING":
        reasons.append(f"Priority {p:.1f} is below actionable_threshold {actionable}.")
        if p >= watching:
            reasons.append(f"Priority {p:.1f} ≥ watching_threshold {watching}.")
        reasons.append(f"Evidence {evid:.0f}, confidence {conf:.0f} support continued monitoring.")
        if age_h < stale_h:
            reasons.append(f"Last activity {age_h:.1f}h ago is within stale_after_hours {stale_h}.")
    elif st == "STALE":
        reasons.append(
            f"Last activity {age_h:.1f}h ago exceeds or nears stale_after_hours {stale_h}, "
            f"or priority {p:.1f} fell below watch band."
        )
    elif st == "NEW":
        reasons.append(f"Priority {p:.1f} below watching_threshold {watching} or newly created.")
    elif st == "RESOLVED":
        reasons.append("Marked RESOLVED (typically after WRITTEN feedback).")
    elif st == "DISMISSED":
        reasons.append("Marked DISMISSED (typically after FALSE_POSITIVE feedback).")
    else:
        reasons.append(f"Status {st} stored as-is; see LeadEvent history for transitions.")
    return reasons


def _alert_decision_dict(lead: StoryLead) -> Dict[str, Any]:
    cfg = _alert_cfg()
    activation = cfg.get("policy_activated_at")
    try:
        activation = activation or ensure_policy_activation()
    except Exception:
        pass
    decision = evaluate_alert_eligibility(lead, policy_activated_at=activation)
    age_h = _hours_ago(lead.last_activity_at or lead.updated_at)
    checks = [
        ThresholdCheck(
            "STATUS", f"status {lead.lead_status}", lead.lead_status,
            cfg["allowed_statuses"], "", lead.lead_status in cfg["allowed_statuses"],
            "newsroom.alerts.allowed_statuses",
        ),
        ThresholdCheck(
            "PRIORITY", "priority vs min_priority", round(float(lead.priority_score or 0), 2),
            cfg["min_priority"], "score",
            float(lead.priority_score or 0) >= cfg["min_priority"],
            "newsroom.alerts.min_priority",
        ),
        ThresholdCheck(
            "AGE", "age vs max_age_hours", round(age_h, 2), cfg["max_age_hours"], "hours",
            age_h <= cfg["max_age_hours"], "newsroom.alerts.max_age_hours",
        ),
        ThresholdCheck(
            "EVIDENCE", "evidence gate", round(float(lead.evidence_score or 0), 2),
            cfg["min_evidence"], "score",
            float(lead.evidence_score or 0) >= cfg["min_evidence"],
            "newsroom.alerts.min_evidence",
        ),
        ThresholdCheck(
            "RELEVANCE", "relevance gate", round(float(lead.relevance_score or 0), 2),
            cfg["min_relevance"], "score",
            float(lead.relevance_score or 0) >= cfg["min_relevance"],
            "newsroom.alerts.min_relevance",
        ),
        ThresholdCheck(
            "CONFIDENCE", "confidence gate", round(float(lead.confidence_score or 0), 2),
            cfg["min_confidence"], "score",
            float(lead.confidence_score or 0) >= cfg["min_confidence"],
            "newsroom.alerts.min_confidence",
        ),
    ]
    return {
        "eligible": decision.eligible,
        "would_send": decision.would_send,
        "reason_code": decision.reason_code,
        "detail": decision.detail,
        "alert_reason": decision.alert_reason,
        "notified": bool(lead.notified),
        "last_notified_at": _aware(lead.last_notified_at).isoformat() if lead.last_notified_at else None,
        "last_notified_status": lead.last_notified_status,
        "last_notified_priority": getattr(lead, "last_notified_priority", None),
        "checks": [asdict(c) for c in checks],
        "config_snapshot": {
            "min_priority": cfg["min_priority"],
            "max_age_hours": cfg["max_age_hours"],
            "allowed_statuses": cfg["allowed_statuses"],
            "cooldown_hours": cfg.get("cooldown_hours"),
        },
    }


def _chronology_summary(lead: StoryLead, facts: Dict[str, Any]) -> str:
    parts: List[str] = []
    fs, fm, fd = lead.first_signal_source, lead.first_media_source, lead.first_documentary_source
    t_s, t_m, t_d = _aware(lead.first_signal_at), _aware(lead.first_media_at) if hasattr(lead, "first_media_at") else None, _aware(lead.first_documentary_at) if hasattr(lead, "first_documentary_at") else None
    # first_media_at may only be on cluster — use lead fields we have
    if fs and fm and lead.first_signal_at and lead.first_media_source:
        # lead stores first_media_source; timestamp may be on cluster
        pass
    if fs and not fm and not fd:
        parts.append(f"{fs} is recorded as first signal; no media or documentary first-source fields set.")
    elif fs and fm:
        if lead.first_signal_at and hasattr(lead, "lead_time_minutes") and lead.lead_time_minutes is not None:
            parts.append(
                f"{fs} first signal; {fm} first media; lead_time_minutes={lead.lead_time_minutes:.0f}."
            )
        else:
            parts.append(f"{fs} recorded as first signal; {fm} as first media.")
    elif fm and not fs:
        parts.append(f"First media source {fm}; no community first_signal_source stored.")
    if fd:
        parts.append(f"First documentary source field: {fd}.")
    if not parts:
        parts.append(
            f"Layer counts — community={lead.community_count}, news={lead.news_count}, "
            f"documentary={lead.documentary_count}."
        )
    return " ".join(parts)


def _material_changes_from_events(session, lead_id: int) -> List[Dict[str, Any]]:
    """Derive material changes from structured LeadEvent rows (no hourly noise)."""
    material_types = {
        "DOCUMENTARY_EVIDENCE_ADDED",
        "FIRST_MEDIA_PICKUP",
        "BECAME_ACTIONABLE",
        "BECAME_STALE",
        "RESURGED",
        "STATUS_CHANGE",
        "ALERT_SENT",
        "MATERIAL_SCORE_CHANGE",
        "LEAD_CREATED",
    }
    events = list(
        session.execute(
            select(LeadEvent)
            .where(LeadEvent.lead_id == lead_id)
            .where(LeadEvent.event_type.in_(list(material_types)))
            .order_by(desc(LeadEvent.observed_at))
            .limit(25)
        ).scalars().all()
    )
    out = []
    for e in events:
        out.append({
            "event_type": e.event_type,
            "observed_at": _aware(e.observed_at).isoformat() if e.observed_at else None,
            "summary": e.summary,
            "metadata": e.raw_metadata if isinstance(e.raw_metadata, dict) else None,
        })
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def explain_lead_structured(lead_id: int) -> Optional[LeadExplanation]:
    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if not lead:
            return None
        facts = _load_cluster_facts(session, lead)
        bd = lead.score_breakdown if isinstance(lead.score_breakdown, dict) else {}
        w = bd.get("weights") or _weights()
        weighted = bd.get("weighted") or {}

        components = [
            "novelty", "evidence", "exclusivity", "momentum",
            "source_diversity", "confidence", "relevance",
        ]
        contribs: List[Dict[str, Any]] = []
        for c in components:
            raw = float(bd.get(c, getattr(lead, f"{c}_score", 0) or 0) or 0)
            if c == "source_diversity":
                raw = float(bd.get("source_diversity", lead.source_diversity_score or 0) or 0)
            weight = float(w.get(c, 0) or 0)
            contribution = float(weighted.get(c, round(raw * weight, 2)))
            contribs.append(asdict(Contribution(
                component=c,
                raw=round(raw, 2),
                weight=weight,
                contribution=contribution,
                facts=_component_facts(lead, facts, c),
            )))

        # media saturation is informational
        sat = float(bd.get("media_saturation", lead.media_saturation_score or 0) or 0)
        contribs.append(asdict(Contribution(
            component="media_saturation",
            raw=round(sat, 2),
            weight=0.0,
            contribution=0.0,
            facts=_component_facts(lead, facts, "media_saturation"),
        )))

        decay = float(bd.get("time_decay") or 0)
        component_scores = {c["component"]: c["raw"] for c in contribs}
        component_scores["time_decay"] = decay

        alert = _alert_decision_dict(lead)
        material = _material_changes_from_events(session, lead_id)
        chrono = _chronology_summary(lead, facts)
        source_summary = {
            "first_signal_source": lead.first_signal_source,
            "first_media_source": lead.first_media_source,
            "first_documentary_source": lead.first_documentary_source,
            "news_count": lead.news_count,
            "community_count": lead.community_count,
            "documentary_count": lead.documentary_count,
            "source_count": lead.source_count,
            "member_articles": len(facts["articles"]),
            "member_community": len(facts["community"]),
            "member_docs": len(facts["docs"]),
        }

        exp = LeadExplanation(
            lead_id=lead.id,
            cluster_id=lead.cluster_id,
            headline=lead.headline_hint,
            lead_type=lead.lead_type or "UNKNOWN",
            lead_status=lead.lead_status or "NEW",
            priority_score=float(lead.priority_score or 0),
            component_scores=component_scores,
            weighted_contributions=contribs,
            time_decay=decay,
            caps_and_penalties=_caps_and_penalties(lead, bd),
            status_reasons=_status_reasons(lead),
            alert_decision=alert,
            evidence_summary=lead.evidence_summary,
            uncertainty_summary=lead.uncertainty_summary,
            chronology_summary=chrono,
            source_summary=source_summary,
            material_changes=material,
            generated_at=_now().isoformat(),
        )
        exp.human_text = format_explanation_human(exp)
        return exp


def format_explanation_human(exp: LeadExplanation) -> str:
    lines: List[str] = []
    lines.append(f"Lead #{exp.lead_id} — {exp.headline or '(no headline)'}")
    lines.append(f"Type: {exp.lead_type} | Status: {exp.lead_status} | Priority: {exp.priority_score:.1f}")
    lines.append("")
    lines.append("=== WHY THIS SCORE ===")
    recon = exp.time_decay
    for c in exp.weighted_contributions:
        if c["component"] == "media_saturation":
            lines.append(f"  {c['component']:18} raw={c['raw']:.1f} (not directly weighted)")
            for f in c["facts"]:
                lines.append(f"    - {f}")
            continue
        lines.append(
            f"  {c['component']:18} raw={c['raw']:.1f}  weight={c['weight']:.2f}  "
            f"contrib={c['contribution']:+.2f}"
        )
        for f in c["facts"]:
            lines.append(f"    - {f}")
        recon += c["contribution"]
    lines.append(f"  {'time_decay':18} {exp.time_decay:+.2f}")
    lines.append(f"  {'reconciled≈':18} {recon:.2f}  (stored priority {exp.priority_score:.2f})")
    lines.append("")
    lines.append("Caps / penalties:")
    for n in exp.caps_and_penalties:
        lines.append(f"  - {n}")
    lines.append("")
    lines.append("=== WHY THIS STATUS ===")
    for r in exp.status_reasons:
        lines.append(f"  - {r}")
    lines.append("")
    lines.append("=== DISCORD DECISION ===")
    ad = exp.alert_decision
    lines.append(f"  eligible={ad.get('eligible')} would_send={ad.get('would_send')}")
    lines.append(f"  reason={ad.get('reason_code')} detail={ad.get('detail')}")
    lines.append(f"  notified={ad.get('notified')} last_notified_at={ad.get('last_notified_at')}")
    for ch in ad.get("checks") or []:
        mark = "PASS" if ch.get("passed") else "FAIL"
        lines.append(
            f"  [{mark}] {ch.get('code')}: actual={ch.get('actual')} threshold={ch.get('threshold')} "
            f"({ch.get('source_rule')})"
        )
    lines.append("")
    lines.append("=== CHRONOLOGY ===")
    lines.append(f"  {exp.chronology_summary}")
    lines.append("")
    lines.append("=== EVIDENCE / UNCERTAINTY ===")
    lines.append(f"  Evidence: {exp.evidence_summary or '—'}")
    lines.append(f"  Uncertainty: {exp.uncertainty_summary or '—'}")
    lines.append("")
    lines.append("=== MATERIAL CHANGES (from LeadEvent) ===")
    if not exp.material_changes:
        lines.append("  (none recorded)")
    for m in exp.material_changes[:12]:
        lines.append(f"  - {m.get('observed_at')} {m.get('event_type')}: {m.get('summary')}")
    lines.append("")
    lines.append(f"explanation_version={exp.explanation_version} generated_at={exp.generated_at}")
    return "\n".join(lines)


def build_lead_timeline(lead_id: int, limit: int = 200) -> List[TimelineEvent]:
    events: List[TimelineEvent] = []
    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if not lead:
            return []
        facts = _load_cluster_facts(session, lead)

        # Lead lifecycle markers from stored first-* fields
        if lead.first_signal_at and lead.first_signal_source:
            events.append(TimelineEvent(
                timestamp=_aware(lead.first_signal_at).isoformat(),
                event_type="FIRST_SIGNAL",
                layer="COMMUNITY",
                source=lead.first_signal_source,
                title="First signal (stored on lead)",
                summary=f"first_signal_source={lead.first_signal_source}",
                is_first_of_type=True,
            ))
        if lead.first_documentary_source and getattr(lead, "first_documentary_at", None):
            events.append(TimelineEvent(
                timestamp=_aware(lead.first_documentary_at).isoformat() if lead.first_documentary_at else None,
                event_type="DOCUMENTARY_DISCOVERY",
                layer="DOCUMENTARY",
                source=lead.first_documentary_source,
                title="First documentary (stored on lead)",
                summary=str(lead.first_documentary_source),
                is_first_of_type=True,
            ))
        if lead.first_media_source:
            # Prefer article published/discovered times for first matching source
            ts = None
            url = None
            title = "First media pickup (stored on lead)"
            for a in facts["articles"]:
                if a.source == lead.first_media_source:
                    ts = _aware(a.published_at or a.discovered_at)
                    url = a.url
                    title = a.title_english or a.title_original or title
                    break
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type="FIRST_MEDIA_PICKUP",
                layer="NEWS",
                source=lead.first_media_source,
                title=title[:200],
                summary="first_media_source field",
                url=url,
                is_first_of_type=True,
                inferred=ts is None,
            ))

        for c in facts["community"]:
            ts = _aware(c.first_signal_at or c.created_at or c.discovered_at)
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type="COMMUNITY_SIGNAL",
                layer="COMMUNITY",
                source=c.platform or "community",
                title=(c.title_original or "")[:200],
                summary=c.signal_type or "",
                url=c.url,
                record_id=str(c.id),
                evidence_type=c.signal_type,
                inferred=ts is None,
            ))

        for d in facts["docs"]:
            ts = _aware(d.first_seen_at or getattr(d, "observed_at", None))
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type="DOCUMENTARY_DISCOVERY",
                layer="DOCUMENTARY",
                source=d.source or "documentary",
                title=(d.title or d.product or d.model_number or "documentary record")[:200],
                summary=d.record_type or "",
                url=d.url or d.canonical_url,
                record_id=str(d.id),
                inferred=ts is None,
            ))

        for a in facts["articles"]:
            ts = _aware(a.published_at or a.discovered_at)
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type="MEDIA_PICKUP",
                layer="NEWS",
                source=a.source or "news",
                title=(a.title_english or a.title_original or "")[:200],
                summary="article",
                url=a.url,
                record_id=str(a.id),
                inferred=a.published_at is None,
                metadata={"discovery": _aware(a.discovered_at).isoformat() if a.discovered_at else None},
            ))

        for e in session.execute(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id).order_by(LeadEvent.observed_at.asc())
        ).scalars().all():
            ts = _aware(e.observed_at)
            layer = "NEWSROOM"
            et = e.event_type or "LEAD_EVENT"
            if et in ("ALERT_SENT", "HIGH_PRIORITY"):
                et = "ALERT_SENT"
                layer = "DISCORD"
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type=et,
                layer=layer,
                source="newsroom",
                title=et,
                summary=e.summary or "",
                record_id=str(e.id),
            ))

        for n in session.execute(
            select(LeadNotification)
            .where(LeadNotification.lead_id == lead_id)
            .order_by(LeadNotification.attempted_at.asc())
        ).scalars().all():
            ts = _aware(n.attempted_at)
            et = {
                "SENT": "ALERT_SENT",
                "FAILED": "ALERT_FAILED",
                "DRY_RUN": "ALERT_ELIGIBLE",
                "SUPPRESSED": "ALERT_SUPPRESSED",
            }.get(n.outcome or "", "ALERT_SUPPRESSED")
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type=et,
                layer="DISCORD",
                source="discord",
                title=n.outcome or "",
                summary=f"{n.reason_code or ''} {n.alert_reason or ''}".strip(),
                score_or_value=n.priority_score,
                record_id=str(n.id),
                metadata={"http": n.webhook_http_status},
            ))

        for fb in session.execute(
            select(LeadFeedback).where(LeadFeedback.lead_id == lead_id).order_by(LeadFeedback.created_at.asc())
        ).scalars().all():
            ts = _aware(fb.created_at)
            events.append(TimelineEvent(
                timestamp=ts.isoformat() if ts else None,
                event_type="FEEDBACK_ADDED",
                layer="FEEDBACK",
                source="operator",
                title=fb.feedback,
                summary=fb.note or "",
                record_id=str(fb.id),
            ))

    events.sort(key=lambda e: e.sort_key())
    return events[:limit]


def format_timeline_human(events: List[TimelineEvent]) -> str:
    if not events:
        return "No timeline events."
    lines = [f"{'When':22} {'Layer':11} {'Type':22} Source / Title", "-" * 100]
    for e in events:
        when = (e.timestamp or "unknown")[:19]
        flag = " ~" if e.inferred else "  "
        lines.append(
            f"{when:22}{flag}{e.layer:11} {e.event_type:22} {e.source}: {(e.title or '')[:50]}"
        )
        if e.summary:
            lines.append(f"{'':24}{e.summary[:80]}")
        if e.url:
            lines.append(f"{'':24}{e.url}")
    return "\n".join(lines)


def lead_audit(lead_id: int) -> Optional[Dict[str, Any]]:
    exp = explain_lead_structured(lead_id)
    if not exp:
        return None
    timeline = build_lead_timeline(lead_id, limit=50)
    with get_session() as session:
        fbs = list(
            session.execute(
                select(LeadFeedback)
                .where(LeadFeedback.lead_id == lead_id)
                .order_by(desc(LeadFeedback.created_at))
            ).scalars().all()
        )
        last_sent = session.execute(
            select(LeadNotification)
            .where(LeadNotification.lead_id == lead_id)
            .where(LeadNotification.outcome == "SENT")
            .order_by(desc(LeadNotification.attempted_at))
            .limit(1)
        ).scalar_one_or_none()
        last_fail = session.execute(
            select(LeadNotification)
            .where(LeadNotification.lead_id == lead_id)
            .where(LeadNotification.outcome == "FAILED")
            .order_by(desc(LeadNotification.attempted_at))
            .limit(1)
        ).scalar_one_or_none()
    return {
        "lead_id": lead_id,
        "current": {
            "status": exp.lead_status,
            "type": exp.lead_type,
            "priority": exp.priority_score,
            "headline": exp.headline,
        },
        "alert_decision": exp.alert_decision,
        "material_changes": exp.material_changes,
        "last_successful_alert": {
            "at": _aware(last_sent.sent_at or last_sent.attempted_at).isoformat() if last_sent else None,
            "priority": last_sent.priority_score if last_sent else None,
            "reason": last_sent.alert_reason if last_sent else None,
        },
        "last_failed_alert": {
            "at": _aware(last_fail.attempted_at).isoformat() if last_fail else None,
            "error": last_fail.error_text if last_fail else None,
            "http": last_fail.webhook_http_status if last_fail else None,
        },
        "feedback": [
            {"feedback": f.feedback, "at": _aware(f.created_at).isoformat() if f.created_at else None, "note": f.note}
            for f in fbs
        ],
        "timeline_preview": [asdict(e) for e in timeline[:15]],
        "chronology_summary": exp.chronology_summary,
        "explanation_version": EXPLANATION_VERSION,
    }


# ---------------------------------------------------------------------------
# Primary source URL (newsroom list "SOURCE" column)
# ---------------------------------------------------------------------------

def _known_dead_source(source_name: Optional[str]) -> bool:
    """True if `source_name` is a source deliberately disabled from active
    production (see community_sources/documentary_sources DISABLED_*
    sets) — e.g. ptt, whose own web gateway is currently returning HTTP 500
    site-wide. Used to skip picking that source's URL as a lead's primary
    link even when provenance would otherwise put it first, without ever
    touching the underlying stored record."""
    if not source_name:
        return False
    try:
        from community_sources import DISABLED_COMMUNITY_SOURCES
    except Exception:
        DISABLED_COMMUNITY_SOURCES = {}
    try:
        from documentary_sources import DISABLED_DOCUMENTARY_SOURCES
    except Exception:
        DISABLED_DOCUMENTARY_SOURCES = {}
    return source_name in DISABLED_COMMUNITY_SOURCES or source_name in DISABLED_DOCUMENTARY_SOURCES


def primary_source_url(lead: StoryLead, session=None) -> Optional[str]:
    """The single best external URL representing where this lead came from.

    Never invents a URL — only returns one backed by an actual stored
    Article/CommunityThread/DocumentaryRecord row. Preference order:
      1. the community thread matching first_signal_source (community-first leads)
      2. the documentary record matching first_documentary_source
      3. the article matching first_media_source (conventional-media leads)
      4. otherwise the earliest available record of any kind in the cluster

    A record from a source known to be currently unreachable (disabled
    from active production — see _known_dead_source) is skipped at every
    tier in favor of the next real record, rather than linking to a page
    already confirmed dead. If the cluster has no other usable record,
    returns None — the caller renders "—", never a guessed/dead link.

    Reused by both the newsroom list column and anywhere else a single
    canonical link for a lead is needed — do not duplicate this selection
    logic in a template.
    """

    def _url(obj) -> Optional[str]:
        return getattr(obj, "canonical_url", None) or getattr(obj, "url", None) or None

    def _source_name(obj) -> Optional[str]:
        return getattr(obj, "source", None) or getattr(obj, "platform", None)

    def _query(s) -> Optional[str]:
        if not lead.cluster_id:
            return None
        arts = s.execute(
            select(Article).where(Article.duplicate_group_id == lead.cluster_id)
            .order_by(Article.discovered_at.asc())
        ).scalars().all()
        cts = s.execute(
            select(CommunityThread).where(CommunityThread.story_cluster_id == lead.cluster_id)
            .order_by(CommunityThread.discovered_at.asc())
        ).scalars().all()
        docs = s.execute(
            select(DocumentaryRecord).where(DocumentaryRecord.story_cluster_id == lead.cluster_id)
            .order_by(DocumentaryRecord.first_seen_at.asc())
        ).scalars().all()

        if lead.first_signal_source and not _known_dead_source(lead.first_signal_source):
            match = next((c for c in cts if c.platform == lead.first_signal_source), None)
            if match and _url(match):
                return _url(match)

        if lead.first_documentary_source and not _known_dead_source(lead.first_documentary_source):
            match = next((d for d in docs if d.source == lead.first_documentary_source), None)
            if match and _url(match):
                return _url(match)

        if lead.first_media_source and not _known_dead_source(lead.first_media_source):
            match = next((a for a in arts if a.source == lead.first_media_source), None)
            if match and _url(match):
                return _url(match)

        for group in (arts, cts, docs):
            for obj in group:
                if _known_dead_source(_source_name(obj)):
                    continue
                u = _url(obj)
                if u:
                    return u
        return None

    if session is not None:
        return _query(session)
    with get_session() as s:
        return _query(s)
