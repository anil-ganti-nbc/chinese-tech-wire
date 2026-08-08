"""V0.5.5 explainability + timeline tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from database.db import get_session, init_db
from database.models import (
    Article,
    CommunityThread,
    LeadEvent,
    LeadFeedback,
    LeadNotification,
    StoryCluster,
    StoryLead,
)
from pipeline.explain import (
    EXPLANATION_VERSION,
    build_lead_timeline,
    explain_lead_structured,
    format_explanation_human,
    lead_audit,
)


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/explain.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _seed_lead(session, **kwargs):
    now = _now()
    c = StoryCluster(
        first_seen_source="chiphell",
        first_seen_at=now - timedelta(hours=3),
        representative_title="RTX test",
        created_at=now,
        updated_at=now,
        first_signal_source="chiphell",
        first_signal_at=now - timedelta(hours=3),
        first_media_source="benchlife",
        first_media_at=now - timedelta(hours=2),
    )
    session.add(c)
    session.flush()
    bd = {
        "novelty": 70,
        "evidence": 40,
        "exclusivity": 60,
        "momentum": 30,
        "source_diversity": 40,
        "confidence": 45,
        "relevance": 70,
        "media_saturation": 20,
        "weights": {
            "novelty": 0.18,
            "evidence": 0.20,
            "exclusivity": 0.15,
            "momentum": 0.12,
            "source_diversity": 0.10,
            "confidence": 0.12,
            "relevance": 0.13,
        },
        "weighted": {
            "novelty": 12.6,
            "evidence": 8.0,
            "exclusivity": 9.0,
            "momentum": 3.6,
            "source_diversity": 4.0,
            "confidence": 5.4,
            "relevance": 9.1,
        },
        "time_decay": -1.5,
        "final": 50.2,
    }
    defaults = dict(
        cluster_id=c.id,
        lead_status="WATCHING",
        lead_type="EARLY_SIGNAL",
        headline_hint="RTX 6090 PCB photo",
        created_at=now - timedelta(hours=3),
        updated_at=now,
        last_activity_at=now - timedelta(hours=1),
        priority_score=50.2,
        evidence_score=40,
        relevance_score=70,
        confidence_score=45,
        novelty_score=70,
        exclusivity_score=60,
        momentum_score=30,
        source_diversity_score=40,
        media_saturation_score=20,
        editorial_value_score=50.2,
        news_count=1,
        community_count=1,
        documentary_count=0,
        source_count=2,
        first_signal_source="chiphell",
        first_signal_at=now - timedelta(hours=3),
        first_media_source="benchlife",
        lead_time_minutes=60,
        score_breakdown=bd,
        evidence_summary="Chiphell PHOTO thread",
        uncertainty_summary="No documentary confirmation",
        why_now="Community photo before media",
        primary_entities={"entities": ["Nvidia", "RTX 6090"]},
        notified=False,
    )
    defaults.update(kwargs)
    lead = StoryLead(**defaults)
    session.add(lead)
    session.flush()

    session.add(CommunityThread(
        platform="chiphell",
        thread_id="t-1",
        title_original="RTX 6090 photo",
        url="https://example.com/ch/1",
        discovered_at=now - timedelta(hours=3),
        created_at=now - timedelta(hours=3),
        first_signal_at=now - timedelta(hours=3),
        signal_type="PHOTO_EVIDENCE",
        story_cluster_id=c.id,
    ))
    session.add(Article(
        source="benchlife",
        source_article_id="bl1",
        url="https://example.com/bl/1",
        canonical_url="https://example.com/bl/1",
        title_original="RTX 6090 曝光",
        title_english="RTX 6090 spotted",
        published_at=now - timedelta(hours=2),
        discovered_at=now - timedelta(hours=2),
        priority_score=50,
        relevance_score=70,
        novelty_score=60,
        duplicate_group_id=c.id,
    ))
    session.add(LeadEvent(
        lead_id=lead.id,
        event_type="LEAD_CREATED",
        observed_at=now - timedelta(hours=3),
        summary="created",
    ))
    session.add(LeadEvent(
        lead_id=lead.id,
        event_type="FIRST_MEDIA_PICKUP",
        observed_at=now - timedelta(hours=2),
        summary="benchlife",
    ))
    session.flush()
    return lead


def test_explain_reconciles_weighted_sum(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
    exp = explain_lead_structured(lid)
    assert exp is not None
    assert exp.explanation_version == EXPLANATION_VERSION
    recon = exp.time_decay + sum(
        c["contribution"] for c in exp.weighted_contributions if c["component"] != "media_saturation"
    )
    assert abs(recon - exp.priority_score) < 1.0


def test_explain_includes_facts_and_status(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lid = _seed_lead(session).id
    exp = explain_lead_structured(lid)
    assert exp.lead_status == "WATCHING"
    assert any("actionable_threshold" in r for r in exp.status_reasons)
    novelty = next(c for c in exp.weighted_contributions if c["component"] == "novelty")
    assert novelty["facts"]


def test_alert_explanation_uses_production_evaluator(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    import config as cfg
    nr = dict(cfg.yaml_config.get("newsroom") or {})
    nr["alerts"] = {
        "enabled": True,
        "min_priority": 52,
        "min_evidence": 15,
        "min_relevance": 35,
        "min_confidence": 25,
        "allowed_statuses": ["WATCHING", "ACTIONABLE"],
        "max_age_hours": 48,
        "suppress_before_activation": False,
        "cooldown_hours": 12,
        "max_alerts_per_cycle": 5,
    }
    monkeypatch.setitem(cfg.yaml_config, "newsroom", nr)
    with get_session() as session:
        lid = _seed_lead(session, priority_score=50.2).id
    exp = explain_lead_structured(lid)
    assert exp.alert_decision["reason_code"] in (
        "SUPPRESSED_SCORE", "ELIGIBLE", "SUPPRESSED_QUALITY", "SUPPRESSED_STATUS"
    )
    assert "checks" in exp.alert_decision


def test_timeline_community_before_media(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lid = _seed_lead(session).id
    events = build_lead_timeline(lid)
    types = [e.event_type for e in events]
    assert "COMMUNITY_SIGNAL" in types or "FIRST_SIGNAL" in types
    assert "MEDIA_PICKUP" in types or "FIRST_MEDIA_PICKUP" in types
    # first community-ish timestamp <= first media-ish
    comm = next(e for e in events if e.layer == "COMMUNITY" and e.timestamp)
    media = next(e for e in events if e.layer == "NEWS" and e.timestamp)
    assert comm.timestamp <= media.timestamp


def test_timeline_includes_feedback_and_notification(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        session.add(LeadFeedback(lead_id=lead.id, feedback="USEFUL", created_at=_now()))
        session.add(LeadNotification(
            lead_id=lead.id,
            attempted_at=_now(),
            outcome="FAILED",
            reason_code="HTTP_ERROR",
            priority_score=50,
        ))
        lid = lead.id
    events = build_lead_timeline(lid)
    assert any(e.event_type == "FEEDBACK_ADDED" for e in events)
    assert any(e.event_type == "ALERT_FAILED" for e in events)


def test_human_and_json_roundtrip(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lid = _seed_lead(session).id
    exp = explain_lead_structured(lid)
    text = format_explanation_human(exp)
    assert "WHY THIS SCORE" in text
    d = exp.to_dict()
    assert d["lead_id"] == lid
    assert "weighted_contributions" in d


def test_audit_structure(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lid = _seed_lead(session).id
    audit = lead_audit(lid)
    assert audit["lead_id"] == lid
    assert "alert_decision" in audit
    assert "material_changes" in audit


def test_gui_lead_detail_uses_structured(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lid = _seed_lead(session).id
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    with patch("sources.ithome.ITHomeSource.fetch_latest") as mocked:
        resp = client.get(f"/leads/{lid}")
        mocked.assert_not_called()
    assert resp.status_code == 200
    assert b"Why this score" in resp.content or b"score" in resp.content.lower()


def test_gui_lead_404(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    resp = client.get("/leads/999999")
    assert resp.status_code == 404


def test_missing_lead_explain_returns_none(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert explain_lead_structured(999999) is None
