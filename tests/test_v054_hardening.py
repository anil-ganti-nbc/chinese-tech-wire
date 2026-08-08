"""V0.5.4 post-soak hardening tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from database.db import get_engine, get_session, init_db
from database.models import (
    IngestionRun,
    LeadEvent,
    LeadFeedback,
    LeadNotification,
    SourceRun,
    StoryCluster,
    StoryLead,
)
from pipeline.alerts import evaluate_alert_eligibility, feedback_report, reset_alert_stats
from pipeline.source_health import classify_source, compute_source_health


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/v054.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def test_migration_idempotent(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    init_db(url)
    init_db(url)
    eng = get_engine(url)
    with eng.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(story_leads)")).fetchall()}
        assert "last_notified_priority" in cols
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert "lead_notifications" in tables


def test_source_health_failing(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        for i in range(4):
            session.add(SourceRun(
                source="ithome",
                started_at=now - timedelta(hours=i),
                finished_at=now - timedelta(hours=i),
                success=False,
                articles_found=0,
                articles_new=0,
                request_errors=1,
                error_message="HTTP 403",
            ))
    rows = compute_source_health()
    ithome = next(r for r in rows if r["source"] == "ithome")
    assert ithome["status"] == "FAILING"


def test_source_health_quiet_zero(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="expreview",
            started_at=now - timedelta(hours=i),
            finished_at=now - timedelta(hours=i),
            success=True,
            articles_found=10,
            articles_new=0,
        )
        for i in range(6)
    ]
    row = classify_source("expreview", "NEWS", runs, counts_24h=0, counts_7d=0)
    assert row["status"] in ("QUIET", "DEGRADED")


def test_source_health_disabled_geekbench():
    row = classify_source("geekbench", "DOCUMENTARY", [], 0, 0)
    assert row["status"] == "DISABLED"


def test_source_health_never_proven():
    row = classify_source("chiphell", "COMMUNITY", [], 0, 0)
    assert row["status"] == "NEVER_PROVEN"


def test_cooldown_suppresses_realert(monkeypatch):
    import config as cfg
    nr = dict(cfg.yaml_config.get("newsroom") or {})
    nr["alerts"] = {
        "enabled": True,
        "min_priority": 50,
        "min_evidence": 10,
        "min_relevance": 10,
        "min_confidence": 10,
        "allowed_statuses": ["WATCHING", "ACTIONABLE"],
        "max_age_hours": 48,
        "material_score_delta": 8,
        "cooldown_hours": 12,
        "suppress_before_activation": False,
        "max_alerts_per_cycle": 5,
    }
    monkeypatch.setitem(cfg.yaml_config, "newsroom", nr)
    lead = StoryLead(
        lead_status="ACTIONABLE",
        lead_type="LEAK",
        created_at=_now(),
        updated_at=_now(),
        last_activity_at=_now(),
        priority_score=60,
        evidence_score=50,
        relevance_score=50,
        confidence_score=50,
        notified=True,
        last_notified_at=_now() - timedelta(hours=1),
        last_notified_status="WATCHING",
        last_notified_priority=50,
    )
    d = evaluate_alert_eligibility(lead, prev_status="WATCHING")
    assert d.reason_code == "SUPPRESSED_COOLDOWN"


def test_feedback_report_empty(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    r = feedback_report()
    assert r["total"] == 0


def test_feedback_report_counts(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = StoryLead(
            lead_status="WATCHING",
            lead_type="LEAK",
            created_at=_now(),
            updated_at=_now(),
            priority_score=55,
            evidence_score=40,
            relevance_score=60,
            confidence_score=40,
        )
        session.add(lead)
        session.flush()
        session.add(LeadFeedback(lead_id=lead.id, feedback="USEFUL", created_at=_now()))
        session.add(LeadFeedback(lead_id=lead.id, feedback="FALSE_POSITIVE", created_at=_now()))
    r = feedback_report()
    assert r["total"] == 2
    assert r["counts"]["USEFUL"] == 1


def test_gui_notifications_route(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        session.add(LeadNotification(
            lead_id=None,
            attempted_at=_now(),
            outcome="POLICY_ACTIVATED",
            reason_code="POLICY_ACTIVATED",
        ))
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    resp = client.get("/notifications")
    assert resp.status_code == 200
    assert b"notification" in resp.content.lower() or b"ledger" in resp.content.lower()


def test_gui_health_includes_source_table(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert b"Source health" in resp.content or b"source" in resp.content.lower()


def test_gui_get_does_not_scrape(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    with patch("sources.ithome.ITHomeSource.fetch_latest") as mocked:
        client.get("/newsroom")
        client.get("/health")
        client.get("/notifications")
        mocked.assert_not_called()


def test_event_dedupe_documentary(tmp_path, monkeypatch):
    """DOCUMENTARY_EVIDENCE_ADDED should not fire twice within dedupe window without evidence rise."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(
            first_seen_source="ithome",
            first_seen_at=now,
            representative_title="test",
            created_at=now,
            updated_at=now,
        )
        session.add(c)
        session.flush()
        lead = StoryLead(
            cluster_id=c.id,
            lead_status="WATCHING",
            lead_type="EARLY_SIGNAL",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
            priority_score=50,
            evidence_score=20,
            relevance_score=50,
            confidence_score=40,
        )
        session.add(lead)
        session.flush()
        session.add(LeadEvent(
            lead_id=lead.id,
            event_type="DOCUMENTARY_EVIDENCE_ADDED",
            observed_at=now - timedelta(hours=1),
            summary="prior",
        ))
        session.flush()
        n1 = len(list(session.execute(
            select(LeadEvent).where(LeadEvent.event_type == "DOCUMENTARY_EVIDENCE_ADDED")
        ).scalars().all()))
        assert n1 == 1
