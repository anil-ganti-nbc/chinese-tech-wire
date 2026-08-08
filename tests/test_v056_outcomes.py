"""V0.5.6 — LeadOutcome model regression tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select, text

from database.db import get_engine, get_session, init_db
from database.models import LeadFeedback, LeadOutcome, StoryLead
from pipeline.outcomes import (
    OUTCOME_VALUES,
    current_outcome,
    effective_outcome,
    outcome_history,
    outcome_report,
    record_outcome,
)


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/v056.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _seed_lead(session, **overrides):
    defaults = dict(
        lead_status="WATCHING", lead_type="EARLY_SIGNAL",
        created_at=_now(), updated_at=_now(),
        priority_score=50, evidence_score=40, relevance_score=50, confidence_score=40,
    )
    defaults.update(overrides)
    lead = StoryLead(**defaults)
    session.add(lead)
    session.flush()
    session.refresh(lead)
    return lead


def test_create_outcome(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
    row = record_outcome(lid, "USEFUL")
    assert row is not None
    assert row.outcome == "USEFUL"
    assert row.is_final is False


def test_update_finalize_outcome(tmp_path, monkeypatch):
    """A lead can accumulate multiple outcomes; current_outcome is the latest."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
    record_outcome(lid, "USEFUL")
    row2 = record_outcome(lid, "WRITTEN", article_url="https://example.com/a")
    assert row2.is_final is True
    cur = current_outcome(lid)
    assert cur.outcome == "WRITTEN"
    hist = outcome_history(lid)
    assert len(hist) == 2


def test_invalid_outcome_rejected(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
    assert record_outcome(lid, "NOT_A_REAL_VALUE") is None
    assert current_outcome(lid) is None


def test_outcome_rejected_for_missing_lead(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert record_outcome(99999, "USEFUL") is None


def test_article_url_stored(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
    row = record_outcome(lid, "WRITTEN", article_url="https://x.com/story", article_title="Story Title")
    assert row.related_article_url == "https://x.com/story"
    assert row.related_article_title == "Story Title"


def test_existing_feedback_preserved(tmp_path, monkeypatch):
    """Recording a LeadOutcome must never mutate/delete existing LeadFeedback rows."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
        session.add(LeadFeedback(lead_id=lid, feedback="NOT_USEFUL", created_at=_now()))
    record_outcome(lid, "WRITTEN")
    with get_session() as session:
        fb = session.execute(select(LeadFeedback).where(LeadFeedback.lead_id == lid)).scalars().all()
        assert len(fb) == 1
        assert fb[0].feedback == "NOT_USEFUL"  # untouched, original value intact


def test_effective_outcome_prefers_outcome_over_feedback(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
        session.add(LeadFeedback(lead_id=lid, feedback="NOT_USEFUL", created_at=_now()))
    record_outcome(lid, "WRITTEN")
    with get_session() as session:
        val, source = effective_outcome(lid, session)
        assert val == "WRITTEN"
        assert source == "OUTCOME"


def test_effective_outcome_falls_back_to_feedback(tmp_path, monkeypatch):
    """No LeadOutcome recorded — derive a projection from old-style feedback
    WITHOUT mutating the stored feedback row."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
        session.add(LeadFeedback(lead_id=lid, feedback="FALSE_POSITIVE", created_at=_now()))
    with get_session() as session:
        val, source = effective_outcome(lid, session)
        assert val == "FALSE"
        assert source == "FEEDBACK"
        fb = session.execute(select(LeadFeedback).where(LeadFeedback.lead_id == lid)).scalar_one()
        assert fb.feedback == "FALSE_POSITIVE"  # stored value never rewritten


def test_effective_outcome_unlabeled(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _seed_lead(session)
        lid = lead.id
        val, source = effective_outcome(lid, session)
        assert val == "UNLABELED"
        assert source == "UNLABELED"


def test_outcome_report_coverage(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        l1 = _seed_lead(session)
        l2 = _seed_lead(session)
        ids = (l1.id, l2.id)
    record_outcome(ids[0], "USEFUL")
    r = outcome_report()
    assert r["total_leads"] == 2
    assert r["leads_with_outcome"] == 1
    assert r["counts"]["USEFUL"] == 1


def test_migration_idempotent(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    init_db(url)
    init_db(url)
    eng = get_engine(url)
    with eng.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert "lead_outcomes" in tables
        assert "missed_stories" in tables
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(lead_outcomes)")).fetchall()}
        assert "outcome" in cols
        assert "is_final" in cols
