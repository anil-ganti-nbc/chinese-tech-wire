"""V0.5 Newsroom Intelligence — StoryLeads from existing clusters.

Deterministic, no LLM required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select

from config import yaml_config
from database.db import get_session
from database.models import (
    Article,
    CommunityThread,
    DocumentaryRecord,
    LeadEvent,
    LeadFeedback,
    StoryCluster,
    StoryLead,
)
from pipeline.entities import extract_entities
from pipeline.alerts import maybe_alert_lead, ensure_policy_activation
from pipeline.notify import send_discord  # legacy article path only

logger = logging.getLogger(__name__)

MEDIA_SOURCES = {
    "ithome", "mydrivers", "expreview", "zol", "jiwei",
    "benchlife", "hkepc", "technews", "xfastest",
}
COMMUNITY = {"chiphell", "mobile01", "ptt", "coolaler"}
DOC_SOURCES = {"jd"}  # active documentary adapters

LEAD_TYPES = (
    "BREAKING_NEWS", "LEAK", "RUMOR", "EARLY_SIGNAL", "DOCUMENTARY_DISCOVERY",
    "RETAIL_DISCOVERY", "PRODUCT_CHANGE", "BENCHMARK_DISCOVERY", "CORROBORATION",
    "TREND", "FOLLOW_UP", "SOURCE_RACE", "UNKNOWN",
)

# Evidence ladder weights (conceptual)
SIGNAL_STRENGTH = {
    "SPECULATION": 15,
    "RUMOR": 25,
    "CLAIM": 35,
    "DISCUSSION": 20,
    "REPOST": 10,
    "FIRSTHAND_CLAIM": 55,
    "PHOTO_EVIDENCE": 70,
    "SCREENSHOT_EVIDENCE": 65,
    "BENCHMARK_EVIDENCE": 75,
    "RETAIL_EVIDENCE": 70,
    "UNKNOWN": 20,
}


def _cfg() -> dict:
    return yaml_config.get("newsroom", {}) or {}


def _weights() -> Dict[str, float]:
    w = _cfg().get("weights", {})
    return {
        "novelty": float(w.get("novelty", 0.18)),
        "evidence": float(w.get("evidence", 0.20)),
        "exclusivity": float(w.get("exclusivity", 0.15)),
        "momentum": float(w.get("momentum", 0.12)),
        "source_diversity": float(w.get("source_diversity", 0.10)),
        "confidence": float(w.get("confidence", 0.12)),
        "relevance": float(w.get("relevance", 0.13)),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hours_ago(dt: Optional[datetime], now: Optional[datetime] = None) -> float:
    if not dt:
        return 999.0
    now = now or _now()
    dt = _aware(dt)
    return max((now - dt).total_seconds() / 3600.0, 0.0)


# ---------------------------------------------------------------------------
# Cluster snapshot for scoring
# ---------------------------------------------------------------------------

def gather_cluster_context(session, cluster_id: int) -> Dict[str, Any]:
    cluster = session.get(StoryCluster, cluster_id)
    arts = session.execute(
        select(Article).where(Article.duplicate_group_id == cluster_id)
    ).scalars().all()
    cts = session.execute(
        select(CommunityThread).where(CommunityThread.story_cluster_id == cluster_id)
    ).scalars().all()
    docs = session.execute(
        select(DocumentaryRecord).where(DocumentaryRecord.story_cluster_id == cluster_id)
    ).scalars().all()

    media = [a for a in arts if a.source in MEDIA_SOURCES]
    sources: Set[str] = set()
    for a in arts:
        sources.add(a.source)
    for c in cts:
        sources.add(c.platform)
    for d in docs:
        sources.add(d.source)

    # Independent community signals (non-repost)
    independent_community = [c for c in cts if c.signal_type != "REPOST"]
    repost_community = [c for c in cts if c.signal_type == "REPOST"]

    titles = []
    if cluster and cluster.representative_title:
        titles.append(cluster.representative_title)
    titles += [a.title_original for a in arts[:5]]
    titles += [c.title_original for c in cts[:5]]
    titles += [d.title or "" for d in docs[:5]]
    blob = " ".join(titles)

    entities = extract_entities(blob)
    entity_names = sorted({e.normalized or e.name for e in entities if e.normalized or e.name})

    first_signal = _aware(cluster.first_signal_at if cluster else None)
    first_doc = _aware(cluster.first_documentary_at if cluster else None)
    first_media = _aware(cluster.first_media_at if cluster else None)

    # activity timestamps
    times = []
    for a in arts:
        times.append(_aware(a.published_at or a.discovered_at))
    for c in cts:
        times.append(_aware(c.first_signal_at or c.created_at or c.discovered_at))
    for d in docs:
        times.append(_aware(d.first_seen_at or d.observed_at))
    times = [t for t in times if t]
    last_activity = max(times) if times else _now()
    first_activity = min(times) if times else _now()

    return {
        "cluster": cluster,
        "articles": arts,
        "media": media,
        "community": cts,
        "independent_community": independent_community,
        "repost_community": repost_community,
        "docs": docs,
        "sources": sources,
        "entities": entity_names,
        "blob": blob,
        "first_signal_at": first_signal,
        "first_documentary_at": first_doc,
        "first_media_at": first_media,
        "first_signal_source": cluster.first_signal_source if cluster else None,
        "first_documentary_source": cluster.first_documentary_source if cluster else None,
        "first_media_source": cluster.first_media_source if cluster else None,
        "last_activity_at": last_activity,
        "first_activity_at": first_activity,
    }


# ---------------------------------------------------------------------------
# Component scores
# ---------------------------------------------------------------------------

def score_novelty(ctx: dict) -> float:
    score = 40.0
    if ctx["independent_community"] and not ctx["media"]:
        score += 25
    if ctx["docs"] and not ctx["media"]:
        score += 20
    if ctx["docs"] and ctx["independent_community"] and not ctx["media"]:
        score += 10
    # anomaly keywords
    blob = ctx["blob"].lower()
    for kw in ("未发布", "未官宣", "工程样", "工程樣", "es ", "rtx 60", "nova lake", "曝光", "泄露", "洩露"):
        if kw in blob:
            score += 8
    # age penalty for novelty
    age_h = _hours_ago(ctx["first_activity_at"])
    if age_h > 48:
        score -= 20
    elif age_h > 12:
        score -= 10
    return max(0.0, min(100.0, score))


def score_evidence(ctx: dict) -> float:
    score = 15.0
    for c in ctx["independent_community"]:
        score += SIGNAL_STRENGTH.get(c.signal_type or "UNKNOWN", 20) * 0.35
        score += (c.evidence_score or 0) * 0.15
    for d in ctx["docs"]:
        if d.record_type == "RETAIL_LISTING":
            score += 30
        elif d.record_type == "BENCHMARK_RECORD":
            score += 35
        elif d.record_type in ("REGULATORY_RECORD", "CERTIFICATION_RECORD"):
            score += 40
        else:
            score += 20
        score += (d.evidence_score or 0) * 0.1
    for a in ctx["media"]:
        score += 12
    # reposts add almost nothing
    score += min(5, len(ctx["repost_community"]) * 1)
    return max(0.0, min(100.0, score))


def score_media_saturation(ctx: dict) -> float:
    n = len(ctx["media"])
    if n <= 0:
        return 0.0
    if n == 1:
        return 22.0
    if n == 2:
        return 40.0
    if n <= 4:
        return 60.0
    if n <= 6:
        return 80.0
    return 95.0


def score_exclusivity(ctx: dict, saturation: float) -> float:
    """High when underreported; falls as saturation rises."""
    base = 85.0
    if not ctx["media"] and (ctx["independent_community"] or ctx["docs"]):
        base = 90.0
    elif len(ctx["media"]) == 1:
        base = 70.0
    elif len(ctx["media"]) >= 4:
        base = 25.0
    # exclusive regional / community-first
    if ctx["first_signal_source"] in COMMUNITY and not ctx["first_media_source"]:
        base = max(base, 88.0)
    if ctx["first_documentary_source"] and not ctx["first_media_source"]:
        base = max(base, 85.0)
    # saturation kills exclusivity
    excl = base * (1.0 - saturation / 120.0)
    return max(0.0, min(100.0, excl))


def score_momentum(ctx: dict, now: Optional[datetime] = None) -> float:
    now = now or _now()
    window_h = float(_cfg().get("momentum_window_hours", 3))
    cutoff = now - timedelta(hours=window_h)
    recent = 0
    for a in ctx["articles"]:
        t = _aware(a.discovered_at or a.published_at)
        if t and t >= cutoff:
            recent += 1
    for c in ctx["community"]:
        t = _aware(c.discovered_at or c.first_signal_at)
        if t and t >= cutoff:
            recent += 1
            # velocity boost
            recent += min(2, int((c.velocity_score or 0) / 40))
    for d in ctx["docs"]:
        t = _aware(d.last_seen_at or d.first_seen_at)
        if t and t >= cutoff:
            recent += 1
    score = min(100.0, recent * 18.0)
    # nothing recent → low momentum
    if recent == 0:
        age = _hours_ago(ctx["last_activity_at"], now)
        score = max(0.0, 25.0 - age * 2)
    return max(0.0, min(100.0, score))


def score_source_diversity(ctx: dict) -> float:
    """Independent layers matter more than repost swarms."""
    layers = 0
    if ctx["independent_community"]:
        layers += 1
    if ctx["docs"]:
        layers += 1
    if ctx["media"]:
        layers += 1
    independent_media = len({a.source for a in ctx["media"]})
    # penalize if most community is repost
    repost_ratio = 0.0
    if ctx["community"]:
        repost_ratio = len(ctx["repost_community"]) / max(len(ctx["community"]), 1)

    score = layers * 28.0
    score += min(30.0, independent_media * 10.0)
    score -= repost_ratio * 25.0
    # multiple independent communities
    plats = {c.platform for c in ctx["independent_community"]}
    if len(plats) >= 2:
        score += 15
    return max(0.0, min(100.0, score))


def score_confidence(ctx: dict) -> float:
    """Factual confidence — not exclusivity."""
    score = 20.0
    score += score_evidence(ctx) * 0.45
    if ctx["docs"] and ctx["independent_community"]:
        score += 15
    if ctx["media"] and ctx["docs"]:
        score += 10
    if len(ctx["media"]) >= 2:
        score += 10
    # pure speculation cap
    only_spec = (
        ctx["independent_community"]
        and all(c.signal_type in ("SPECULATION", "RUMOR", "DISCUSSION", "UNKNOWN") for c in ctx["independent_community"])
        and not ctx["docs"]
        and not ctx["media"]
    )
    if only_spec:
        score = min(score, 35.0)
    return max(0.0, min(100.0, score))


def score_relevance(ctx: dict) -> float:
    score = 40.0
    watch_companies = [x.lower() for x in _cfg().get("watchlist_companies", [])] or [
        "nvidia", "amd", "intel", "apple", "qualcomm", "tsmc", "samsung",
        "sony", "microsoft", "nintendo", "valve", "huawei", "lenovo", "asus",
    ]
    watch_topics = [x.lower() for x in _cfg().get("watchlist_topics", [])] or [
        "gpu", "cpu", "rtx", "ryzen", "semiconductor", "laptop", "handheld",
    ]
    blob = ctx["blob"].lower()
    for c in watch_companies:
        if c in blob or any(c in (e.lower()) for e in ctx["entities"]):
            score += 6
    for t in watch_topics:
        if t in blob:
            score += 4
    # negative filters
    junk = _cfg().get("negative_keywords", []) or [
        "优惠券", "打折", "怎么选", "怎麼選", "请益", "請益", "闲聊", "閒聊", "教程",
    ]
    for j in junk:
        if j in ctx["blob"]:
            score -= 12
    return max(0.0, min(100.0, score))


def time_decay(ctx: dict, lead_type: str, now: Optional[datetime] = None) -> float:
    """Return a negative adjustment (or 0)."""
    now = now or _now()
    age_h = _hours_ago(ctx["last_activity_at"], now)
    # different half-lives
    if lead_type in ("BREAKING_NEWS", "SOURCE_RACE"):
        half = 6.0
    elif lead_type in ("EARLY_SIGNAL", "LEAK", "RUMOR"):
        half = 24.0
    elif lead_type in ("DOCUMENTARY_DISCOVERY", "RETAIL_DISCOVERY", "BENCHMARK_DISCOVERY"):
        half = 48.0
    else:
        half = 36.0
    if age_h <= half:
        return 0.0
    # decay up to -25
    over = age_h - half
    return -min(25.0, over * (25.0 / (half * 2)))


def compute_editorial(ctx: dict, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    novelty = score_novelty(ctx)
    evidence = score_evidence(ctx)
    saturation = score_media_saturation(ctx)
    exclusivity = score_exclusivity(ctx, saturation)
    momentum = score_momentum(ctx, now)
    diversity = score_source_diversity(ctx)
    confidence = score_confidence(ctx)
    relevance = score_relevance(ctx)

    lead_type = classify_lead_type(ctx)
    decay = time_decay(ctx, lead_type, now)
    w = _weights()

    raw = (
        novelty * w["novelty"]
        + evidence * w["evidence"]
        + exclusivity * w["exclusivity"]
        + momentum * w["momentum"]
        + diversity * w["source_diversity"]
        + confidence * w["confidence"]
        + relevance * w["relevance"]
    )
    # popular speculation cannot dominate pure evidence
    if evidence < 30 and momentum > 70:
        raw = min(raw, 55.0)

    final = max(0.0, min(100.0, raw + decay))
    breakdown = {
        "novelty": novelty,
        "evidence": evidence,
        "exclusivity": exclusivity,
        "momentum": momentum,
        "source_diversity": diversity,
        "confidence": confidence,
        "relevance": relevance,
        "media_saturation": saturation,
        "weights": w,
        "weighted": {
            "novelty": round(novelty * w["novelty"], 2),
            "evidence": round(evidence * w["evidence"], 2),
            "exclusivity": round(exclusivity * w["exclusivity"], 2),
            "momentum": round(momentum * w["momentum"], 2),
            "source_diversity": round(diversity * w["source_diversity"], 2),
            "confidence": round(confidence * w["confidence"], 2),
            "relevance": round(relevance * w["relevance"], 2),
        },
        "time_decay": round(decay, 2),
        "final": round(final, 2),
    }
    return {
        "novelty_score": novelty,
        "evidence_score": evidence,
        "exclusivity_score": exclusivity,
        "momentum_score": momentum,
        "source_diversity_score": diversity,
        "confidence_score": confidence,
        "media_saturation_score": saturation,
        "relevance_score": relevance,
        "editorial_value_score": final,
        "priority_score": final,
        "lead_type": lead_type,
        "score_breakdown": breakdown,
    }


def classify_lead_type(ctx: dict) -> str:
    if ctx["docs"] and not ctx["media"] and not ctx["independent_community"]:
        d0 = ctx["docs"][0]
        if d0.record_type == "RETAIL_LISTING":
            return "RETAIL_DISCOVERY"
        if d0.record_type == "BENCHMARK_RECORD":
            return "BENCHMARK_DISCOVERY"
        return "DOCUMENTARY_DISCOVERY"
    if ctx["independent_community"] and ctx["docs"] and not ctx["media"]:
        return "CORROBORATION"
    if ctx["independent_community"] and not ctx["docs"] and not ctx["media"]:
        sigs = {c.signal_type for c in ctx["independent_community"]}
        if sigs & {"PHOTO_EVIDENCE", "SCREENSHOT_EVIDENCE", "BENCHMARK_EVIDENCE"}:
            return "LEAK"
        if sigs & {"SPECULATION", "RUMOR"}:
            return "RUMOR"
        return "EARLY_SIGNAL"
    if ctx["media"] and ctx["first_signal_source"] in COMMUNITY:
        # check race
        if ctx["first_signal_at"] and ctx["first_media_at"]:
            delta = (ctx["first_media_at"] - ctx["first_signal_at"]).total_seconds() / 60
            if 0 < delta < 180:
                return "SOURCE_RACE"
        return "FOLLOW_UP"
    if len(ctx["media"]) >= 3:
        return "BREAKING_NEWS"
    if ctx["media"]:
        return "FOLLOW_UP"
    return "UNKNOWN"


def classify_status(scores: dict, ctx: dict, prev_status: Optional[str] = None) -> str:
    p = scores["priority_score"]
    conf = scores["confidence_score"]
    evid = scores["evidence_score"]
    age_h = _hours_ago(ctx["last_activity_at"])
    stale_h = float(_cfg().get("stale_after_hours", 72))

    actionable_thresh = float(_cfg().get("actionable_threshold", 70))
    watching_thresh = float(_cfg().get("watching_threshold", 45))

    # resurgence: was stale, new activity
    if prev_status == "STALE" and age_h < 6 and (evid >= 40 or conf >= 40):
        if p >= actionable_thresh:
            return "ACTIONABLE"
        return "WATCHING"

    if age_h >= stale_h and p < actionable_thresh + 5:
        return "STALE"

    if p >= actionable_thresh and (evid >= 40 or conf >= 45 or ctx["docs"] or ctx["media"]):
        if len(ctx["media"]) >= 2 and scores["exclusivity_score"] < 40:
            return "ESCALATED"
        return "ACTIONABLE"

    if p >= watching_thresh:
        return "WATCHING"
    if prev_status in ("ACTIONABLE", "ESCALATED") and p < watching_thresh:
        return "STALE"
    return "NEW" if prev_status is None else (prev_status if prev_status != "STALE" else "WATCHING")


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------

def build_why_now(ctx: dict, scores: dict) -> str:
    parts = []
    now = _now()
    if ctx["docs"]:
        latest_doc = max(ctx["docs"], key=lambda d: _aware(d.last_seen_at or d.first_seen_at) or now)
        mins = int(_hours_ago(latest_doc.last_seen_at or latest_doc.first_seen_at) * 60)
        if mins < 120:
            parts.append(f"Documentary evidence from {latest_doc.source} observed ~{mins}m ago.")
        else:
            parts.append(f"Documentary evidence from {latest_doc.source} is in this cluster.")
    if len(ctx["independent_community"]) >= 2:
        plats = sorted({c.platform for c in ctx["independent_community"]})
        parts.append(f"Independent community signals from {', '.join(plats)}.")
    if ctx["first_media_source"] and ctx["first_signal_source"] in COMMUNITY:
        parts.append(
            f"First media pickup by {ctx['first_media_source']} after community signal from {ctx['first_signal_source']}."
        )
    if not ctx["media"] and (ctx["docs"] or ctx["independent_community"]):
        parts.append("No conventional publication coverage yet.")
    if scores["momentum_score"] >= 60:
        parts.append("Elevated recent activity across sources.")
    if not parts:
        parts.append("Cluster activity warrants review.")
    return " ".join(parts)[:500]


def build_why_it_matters(ctx: dict, lead_type: str) -> str:
    ents = ctx["entities"][:5]
    ent_str = ", ".join(ents) if ents else "hardware"
    mapping = {
        "LEAK": f"Possible early visual/physical evidence related to {ent_str}.",
        "RUMOR": f"Unconfirmed claims circulating about {ent_str}.",
        "EARLY_SIGNAL": f"Early community discussion of {ent_str} before wider coverage.",
        "RETAIL_DISCOVERY": f"Retail channel may be exposing product details for {ent_str}.",
        "DOCUMENTARY_DISCOVERY": f"Structured public record related to {ent_str}.",
        "BENCHMARK_DISCOVERY": f"Benchmark metadata may reveal unreleased hardware for {ent_str}.",
        "CORROBORATION": f"Community claim now has matching documentary evidence for {ent_str}.",
        "SOURCE_RACE": f"Story moving from community/documentary into publications for {ent_str}.",
        "BREAKING_NEWS": f"Multiple outlets covering {ent_str}.",
        "FOLLOW_UP": f"Developing coverage of {ent_str}.",
    }
    return mapping.get(lead_type, f"Potential technology story involving {ent_str}.")[:300]


def build_uncertainty(ctx: dict, scores: dict) -> str:
    bits = []
    if not ctx["docs"] and not ctx["media"]:
        bits.append("Based on community signals only; no independent documentary or media confirmation.")
    if ctx["independent_community"] and all(
        c.signal_type in ("SPECULATION", "RUMOR") for c in ctx["independent_community"]
    ):
        bits.append("Claims remain speculative.")
    if ctx["docs"] and any(d.record_type == "RETAIL_LISTING" for d in ctx["docs"]):
        bits.append("Retail listings can contain placeholder or erroneous specifications.")
    if scores["confidence_score"] < 45:
        bits.append("Overall confidence is limited.")
    if ctx["repost_community"] and not ctx["independent_community"]:
        bits.append("Observed discussion may be reposts rather than independent reporting.")
    if not bits:
        if scores["confidence_score"] >= 70:
            bits.append("Multiple evidence layers present; still not an official confirmation unless noted.")
        else:
            bits.append("Treat as a lead for investigation, not a verified fact.")
    return " ".join(bits)[:500]


def build_headline_hint(ctx: dict, lead_type: str) -> str:
    title = ""
    if ctx["cluster"] and ctx["cluster"].representative_title:
        title = ctx["cluster"].representative_title
    elif ctx["articles"]:
        title = ctx["articles"][0].title_original
    elif ctx["community"]:
        title = ctx["community"][0].title_original
    elif ctx["docs"]:
        title = ctx["docs"][0].title or ctx["docs"][0].source_record_id
    title = (title or "Untitled lead")[:80]
    prefix = {
        "LEAK": "Possible leak:",
        "RUMOR": "Rumor:",
        "RETAIL_DISCOVERY": "Retail signal:",
        "DOCUMENTARY_DISCOVERY": "Documentary:",
        "BENCHMARK_DISCOVERY": "Benchmark:",
        "CORROBORATION": "Corroborated:",
        "SOURCE_RACE": "Source race:",
        "EARLY_SIGNAL": "Early signal:",
    }.get(lead_type, "Lead:")
    return f"{prefix} {title}"[:160]


def build_evidence_summary(ctx: dict) -> str:
    parts = []
    for c in ctx["independent_community"][:3]:
        parts.append(f"{c.platform} ({c.signal_type})")
    for d in ctx["docs"][:3]:
        parts.append(f"{d.source}/{d.record_type}")
    for a in ctx["media"][:4]:
        parts.append(a.source)
    return ", ".join(parts)[:400] if parts else "none"


# ---------------------------------------------------------------------------
# Lead upsert
# ---------------------------------------------------------------------------

def upsert_lead_for_cluster(cluster_id: int, dry_run: bool = False) -> Optional[StoryLead]:
    now = _now()
    with get_session() as session:
        ctx = gather_cluster_context(session, cluster_id)
        if not (ctx["articles"] or ctx["community"] or ctx["docs"]):
            return None

        scores = compute_editorial(ctx, now)
        existing = session.execute(
            select(StoryLead).where(StoryLead.cluster_id == cluster_id)
        ).scalar_one_or_none()

        prev_status = existing.lead_status if existing else None
        prev_priority = existing.priority_score if existing else None
        status = classify_status(scores, ctx, prev_status)

        lead_time = None
        if ctx["first_signal_at"] and ctx["first_media_at"]:
            lead_time = (ctx["first_media_at"] - ctx["first_signal_at"]).total_seconds() / 60.0

        fields = dict(
            lead_status=status,
            lead_type=scores["lead_type"],
            headline_hint=build_headline_hint(ctx, scores["lead_type"]),
            primary_entities={"entities": ctx["entities"][:12]},
            updated_at=now,
            first_signal_at=ctx["first_signal_at"],
            last_activity_at=ctx["last_activity_at"],
            news_count=len(ctx["media"]),
            community_count=len(ctx["community"]),
            documentary_count=len(ctx["docs"]),
            source_count=len(ctx["sources"]),
            novelty_score=scores["novelty_score"],
            evidence_score=scores["evidence_score"],
            momentum_score=scores["momentum_score"],
            source_diversity_score=scores["source_diversity_score"],
            exclusivity_score=scores["exclusivity_score"],
            confidence_score=scores["confidence_score"],
            media_saturation_score=scores["media_saturation_score"],
            relevance_score=scores["relevance_score"],
            editorial_value_score=scores["editorial_value_score"],
            priority_score=scores["priority_score"],
            why_now=build_why_now(ctx, scores),
            why_it_matters=build_why_it_matters(ctx, scores["lead_type"]),
            evidence_summary=build_evidence_summary(ctx),
            uncertainty_summary=build_uncertainty(ctx, scores),
            first_signal_source=ctx["first_signal_source"],
            first_documentary_source=ctx["first_documentary_source"],
            first_media_source=ctx["first_media_source"],
            lead_time_minutes=lead_time,
            score_breakdown=scores["score_breakdown"],
        )

        if existing is None:
            lead = StoryLead(cluster_id=cluster_id, created_at=now, notified=False, **fields)
            session.add(lead)
            session.flush()
            session.add(LeadEvent(
                lead_id=lead.id, event_type="LEAD_CREATED", observed_at=now,
                summary=fields["headline_hint"],
            ))
            maybe_alert_lead(session, lead, prev_status=None, dry_run=dry_run)
            logger.info("[NEWSROOM] NEW lead #%s P=%.0f %s | %s", lead.id, lead.priority_score, lead.lead_status, lead.headline_hint)
            return lead

        # update existing — capture prior evidence BEFORE mutating fields
        status_changed = existing.lead_status != status
        prior_evidence = float(existing.evidence_score or 0)
        prior_first_media = existing.first_media_source

        for k, v in fields.items():
            setattr(existing, k, v)

        def _recent_event(etype: str, within_hours: float = 20.0) -> bool:
            """True if same event type already recorded recently (dedupe hourly rebuilds)."""
            cut = now - timedelta(hours=within_hours)
            row = session.execute(
                select(LeadEvent)
                .where(LeadEvent.lead_id == existing.id)
                .where(LeadEvent.event_type == etype)
                .where(LeadEvent.observed_at >= cut)
                .limit(1)
            ).scalar_one_or_none()
            return row is not None

        if status_changed:
            if prev_status == "STALE" and status in ("WATCHING", "ACTIONABLE", "ESCALATED"):
                et = "RESURGED"
            elif status == "ACTIONABLE":
                et = "BECAME_ACTIONABLE"
            elif status == "STALE":
                et = "BECAME_STALE"
            else:
                et = "STATUS_CHANGE"
            if not _recent_event(et, within_hours=6):
                session.add(LeadEvent(
                    lead_id=existing.id, event_type=et, observed_at=now,
                    summary=f"{prev_status} → {status}",
                ))

        evid_delta = float((_cfg().get("alerts") or {}).get("evidence_delta", 5))
        # Compare new evidence against prior value captured before field mutation
        if ctx["docs"] and scores["evidence_score"] > prior_evidence + evid_delta:
            if not _recent_event("DOCUMENTARY_EVIDENCE_ADDED", within_hours=12):
                session.add(LeadEvent(
                    lead_id=existing.id, event_type="DOCUMENTARY_EVIDENCE_ADDED", observed_at=now,
                    summary=f"evidence {prior_evidence:.0f} → {scores['evidence_score']:.0f}",
                ))

        # FIRST_MEDIA_PICKUP once when media appears for the first time
        if ctx["first_media_source"] and not prior_first_media:
            # lifetime dedupe: any prior FIRST_MEDIA_PICKUP for this lead
            prior_pickup = session.execute(
                select(LeadEvent)
                .where(LeadEvent.lead_id == existing.id)
                .where(LeadEvent.event_type == "FIRST_MEDIA_PICKUP")
                .limit(1)
            ).scalar_one_or_none()
            if not prior_pickup:
                session.add(LeadEvent(
                    lead_id=existing.id, event_type="FIRST_MEDIA_PICKUP", observed_at=now,
                    summary=str(ctx["first_media_source"]),
                ))

        maybe_alert_lead(session, existing, prev_status=prev_status, dry_run=dry_run)
        return existing


def _fmt_t(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    dt = _aware(dt)
    return dt.strftime("%H:%M UTC") if dt else ""


def rebuild_leads(limit_clusters: int = 100, dry_run: bool = False) -> int:
    """Rescore recent clusters into StoryLeads."""
    from pipeline.alerts import reset_alert_stats, ensure_policy_activation
    reset_alert_stats()
    try:
        ensure_policy_activation()
    except Exception:
        pass
    cutoff = _now() - timedelta(hours=float(_cfg().get("rebuild_window_hours", 168)))
    n = 0
    with get_session() as session:
        clusters = session.execute(
            select(StoryCluster)
            .where(StoryCluster.updated_at >= cutoff)
            .order_by(StoryCluster.updated_at.desc())
            .limit(limit_clusters)
        ).scalars().all()
        ids = [c.id for c in clusters]
    for cid in ids:
        try:
            if upsert_lead_for_cluster(cid, dry_run=dry_run):
                n += 1
        except Exception as e:
            logger.error("[NEWSROOM] lead upsert cluster=%s failed: %s", cid, e)
    return n


def explain_lead(lead_id: int) -> str:
    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if not lead:
            return f"Lead {lead_id} not found"
        bd = lead.score_breakdown or {}
        w = bd.get("weighted") or {}
        lines = [
            f"Lead #{lead.id}  status={lead.lead_status}  type={lead.lead_type}",
            f"Headline: {lead.headline_hint}",
            "",
            "Score breakdown:",
        ]
        for k in ("novelty", "evidence", "exclusivity", "momentum", "source_diversity", "confidence", "relevance"):
            raw = bd.get(k, getattr(lead, f"{k}_score", None) if k != "source_diversity" else lead.source_diversity_score)
            if k == "source_diversity":
                raw = bd.get("source_diversity", lead.source_diversity_score)
            wt = (bd.get("weights") or {}).get(k, 0)
            contrib = w.get(k, 0)
            lines.append(f"  {k:18} {float(raw or 0):6.1f} × {wt:.2f} = {float(contrib or 0):6.2f}")
        lines.append(f"  {'time_decay':18} {bd.get('time_decay', 0)}")
        lines.append(f"  {'FINAL':18} {lead.priority_score:.2f}")
        lines.append("")
        lines.append(f"Why now: {lead.why_now}")
        lines.append(f"Why it matters: {lead.why_it_matters}")
        lines.append(f"Uncertainty: {lead.uncertainty_summary}")
        lines.append(f"Evidence: {lead.evidence_summary}")
        return "\n".join(lines)


def add_feedback(lead_id: int, feedback: str, note: Optional[str] = None) -> bool:
    feedback = feedback.upper().replace("-", "_")
    allowed = {"USEFUL", "NOT_USEFUL", "WRITTEN", "DUPLICATE", "FALSE_POSITIVE"}
    if feedback not in allowed:
        logger.error("Invalid feedback %s; allowed %s", feedback, allowed)
        return False
    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if not lead:
            return False
        session.add(LeadFeedback(
            lead_id=lead_id,
            feedback=feedback,
            created_at=_now(),
            note=note,
        ))
        if feedback == "WRITTEN":
            lead.lead_status = "RESOLVED"
        elif feedback == "FALSE_POSITIVE":
            lead.lead_status = "DISMISSED"
        # no automatic weight mutation
        return True


def list_leads(
    limit: int = 10,
    since_hours: Optional[float] = None,
    statuses: Optional[List[str]] = None,
) -> List[StoryLead]:
    statuses = statuses or ["ACTIONABLE", "WATCHING", "ESCALATED", "NEW"]
    with get_session() as session:
        q = select(StoryLead).where(StoryLead.lead_status.in_(statuses))
        if since_hours is not None:
            cutoff = _now() - timedelta(hours=since_hours)
            q = q.where(StoryLead.last_activity_at >= cutoff)
        q = q.order_by(StoryLead.priority_score.desc()).limit(limit)
        return list(session.execute(q).scalars().all())


def format_brief(leads: List[StoryLead]) -> str:
    if not leads:
        return "No newsroom leads in scope."
    lines = [
        f"{'Rank':4} {'Score':5} {'Status':10} {'Excl':4} {'Conf':4} {'Sat':3}  Lead",
        "-" * 88,
    ]
    for i, L in enumerate(leads, 1):
        lines.append(
            f"{i:4d} {L.priority_score:5.1f} {L.lead_status:10} "
            f"{L.exclusivity_score:4.0f} {L.confidence_score:4.0f} {L.media_saturation_score:3.0f}  "
            f"{(L.headline_hint or '')[:55]}"
        )
        lines.append(f"     why: {(L.why_now or '')[:70]}")
    return "\n".join(lines)
