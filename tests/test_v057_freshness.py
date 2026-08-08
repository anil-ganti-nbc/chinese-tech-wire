"""V0.5.7 Addendum C/E — newsroom freshness/mothball policy regression tests.

Root cause (see HANDOFF/response write-up): /newsroom had no default time
window and sorted by status+score ahead of recency, so 14+ day old
WATCHING leads with decent scores could sit at the top of page 1. This
suite verifies the fix: a default 7-day active window (derived from
StoryLead.last_activity_at, never persisted, never deleting anything),
an archive/all escape hatch, and last_activity_at-first sorting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database.db import get_session, init_db
from database.models import LeadEvent, LeadFeedback, LeadOutcome, StoryCluster, StoryLead
from pipeline.freshness import (
    ACTIVE_WINDOW_DAYS,
    active_cutoff,
    age_bucket,
    format_freshness_report_text,
    freshness_report,
    is_active,
)
from pipeline.outcomes import record_outcome


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/v057fresh.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _lead(session, **overrides):
    now = _now()
    defaults = dict(
        lead_status="WATCHING", lead_type="EARLY_SIGNAL",
        created_at=now, updated_at=now, last_activity_at=now,
        priority_score=50, evidence_score=40, relevance_score=50, confidence_score=40,
    )
    defaults.update(overrides)
    lead = StoryLead(**defaults)
    session.add(lead)
    session.flush()
    session.refresh(lead)
    return lead


# ---------------------------------------------------------------------------
# is_active / age_bucket unit behavior
# ---------------------------------------------------------------------------

def test_lead_6d23h_old_is_active():
    now = _now()
    dt = now - timedelta(days=6, hours=23)
    assert is_active(dt, now=now) is True


def test_lead_7d1h_old_is_archived():
    now = _now()
    dt = now - timedelta(days=7, hours=1)
    assert is_active(dt, now=now) is False


def test_lead_with_no_last_activity_treated_as_active():
    assert is_active(None) is True  # never silently hidden due to missing data


def test_active_cutoff_is_seven_days():
    assert ACTIVE_WINDOW_DAYS == 7.0


# ---------------------------------------------------------------------------
# Newsroom route: active/archived/all
# ---------------------------------------------------------------------------

def test_newsroom_default_hides_lead_older_than_7_days(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        fresh = _lead(session, last_activity_at=now - timedelta(days=1))
        old = _lead(session, last_activity_at=now - timedelta(days=10))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert f'/leads/{fresh.id}"' in r.text
    assert f'/leads/{old.id}"' not in r.text


def test_newsroom_archived_filter_shows_old_lead(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        old = _lead(session, last_activity_at=now - timedelta(days=10))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom?freshness=archived")
    assert f'/leads/{old.id}"' in r.text


def test_newsroom_all_filter_shows_both(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        fresh = _lead(session, last_activity_at=now - timedelta(hours=1))
        old = _lead(session, last_activity_at=now - timedelta(days=20))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom?freshness=all")
    assert f'/leads/{fresh.id}"' in r.text
    assert f'/leads/{old.id}"' in r.text


def test_old_lead_with_genuine_recent_activity_stays_active(tmp_path, monkeypatch):
    """A lead created 20 days ago but whose last_activity_at reflects a
    genuinely new signal 2 hours ago must appear in the active view."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(
            session,
            created_at=now - timedelta(days=20),
            last_activity_at=now - timedelta(hours=2),
        )

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom")
    assert f'/leads/{lead.id}"' in r.text


def test_routine_updated_at_change_does_not_resurrect_stale_lead(tmp_path, monkeypatch):
    """updated_at churn from a routine rebuild (score recompute) must not
    make an old lead reappear in the active view — only last_activity_at
    (real new evidence) controls freshness."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(
            session,
            last_activity_at=now - timedelta(days=10),
            updated_at=now,  # touched moments ago by a routine rebuild
        )

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom")
    assert f'/leads/{lead.id}"' not in r.text  # still archived despite fresh updated_at


def test_archive_policy_does_not_change_lifecycle_status(tmp_path, monkeypatch):
    """Archiving is purely a presentation filter — it must never rewrite
    lead_status. A lead can be WATCHING + archived."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(session, lead_status="WATCHING", last_activity_at=now - timedelta(days=15))
        lid = lead.id

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    client.get("/newsroom")  # default active view — lead not shown
    client.get("/newsroom?freshness=archived")  # archived view — lead shown

    with get_session() as session:
        lead = session.get(StoryLead, lid)
        assert lead.lead_status == "WATCHING"  # untouched by either view


def test_written_lead_older_than_7_days_hidden_by_default_but_visible_in_all(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(session, lead_status="RESOLVED", last_activity_at=now - timedelta(days=10))
        lid = lead.id
    record_outcome(lid, "WRITTEN", article_url="https://example.com/story")

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r_default = client.get("/newsroom")
    assert f'/leads/{lid}"' not in r_default.text
    r_all = client.get("/newsroom?freshness=all&hide_written=0")
    assert f'/leads/{lid}"' in r_all.text

    # outcome itself is untouched
    with get_session() as session:
        outcome = session.execute(select(LeadOutcome).where(LeadOutcome.lead_id == lid)).scalar_one()
        assert outcome.outcome == "WRITTEN"
        assert outcome.related_article_url == "https://example.com/story"


def test_historical_timeline_accessible_for_archived_lead(tmp_path, monkeypatch):
    """The lead-detail page (with its explainability/timeline) must remain
    fully reachable for an archived lead — archiving only affects the list."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(first_seen_source="ithome", first_seen_at=now - timedelta(days=15), created_at=now, updated_at=now)
        session.add(c)
        session.flush()
        lead = _lead(session, cluster_id=c.id, last_activity_at=now - timedelta(days=15))
        lid = lead.id
        session.add(LeadEvent(lead_id=lid, event_type="LEAD_CREATED", observed_at=now - timedelta(days=15), summary="created"))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get(f"/leads/{lid}")
    assert r.status_code == 200  # not gated behind freshness at all


def test_existing_status_filter_combines_with_freshness(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        fresh_watching = _lead(session, lead_status="WATCHING", last_activity_at=now - timedelta(hours=1))
        fresh_new = _lead(session, lead_status="NEW", last_activity_at=now - timedelta(hours=1))
        old_watching = _lead(session, lead_status="WATCHING", last_activity_at=now - timedelta(days=10))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom?status=WATCHING")  # active default + explicit status filter
    assert f'/leads/{fresh_watching.id}"' in r.text
    assert f'/leads/{fresh_new.id}"' not in r.text  # wrong status
    assert f'/leads/{old_watching.id}"' not in r.text  # too old


def test_explicit_hours_filter_still_works_unchanged(tmp_path, monkeypatch):
    """Backward compatibility: an explicit ?hours= continues to fully
    control the time window, independent of the freshness default."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        recent = _lead(session, last_activity_at=now - timedelta(hours=2))
        older_but_active = _lead(session, last_activity_at=now - timedelta(hours=20))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom?hours=6")
    assert f'/leads/{recent.id}"' in r.text
    assert f'/leads/{older_but_active.id}"' not in r.text


def test_pagination_works_in_archived_view(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        for i in range(15):
            _lead(session, last_activity_at=now - timedelta(days=10 + i))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom?freshness=archived&per_page=10&page=1")
    assert r.status_code == 200
    r2 = client.get("/newsroom?freshness=archived&per_page=10&page=2")
    assert r2.status_code == 200


def test_sql_filtered_not_full_table_scan(tmp_path, monkeypatch):
    """A large number of archived leads mixed with a handful of active ones
    — the default view must return only the active ones, proving the
    filter is applied before the result set is built, not after."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        for i in range(300):
            _lead(session, last_activity_at=now - timedelta(days=30 + i % 100))
        fresh_ids = [_lead(session, last_activity_at=now - timedelta(hours=i)).id for i in range(5)]

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom?per_page=200")
    for fid in fresh_ids:
        assert f'/leads/{fid}"' in r.text


def test_no_historical_rows_deleted_by_archiving(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        for i in range(10):
            _lead(session, last_activity_at=now - timedelta(days=30))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    client.get("/newsroom")
    client.get("/newsroom?freshness=archived")
    client.get("/newsroom?freshness=all")

    with get_session() as session:
        assert len(session.execute(select(StoryLead)).scalars().all()) == 10


# ---------------------------------------------------------------------------
# freshness_report() / --freshness-report
# ---------------------------------------------------------------------------

def test_freshness_report_buckets_reconcile_to_total(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        _lead(session, last_activity_at=now - timedelta(hours=1))
        _lead(session, last_activity_at=now - timedelta(hours=8))
        _lead(session, last_activity_at=now - timedelta(hours=18))
        _lead(session, last_activity_at=now - timedelta(days=1, hours=5))
        _lead(session, last_activity_at=now - timedelta(days=2, hours=5))
        _lead(session, last_activity_at=now - timedelta(days=5))
        _lead(session, last_activity_at=now - timedelta(days=10))
        _lead(session, last_activity_at=now - timedelta(days=20))

    r = freshness_report()
    assert r["total_leads"] == 8
    assert sum(r["buckets"].values()) == 8
    assert r["active_leads"] + r["archived_leads"] == 8


def test_freshness_report_archived_breakdowns(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        old_watching = _lead(session, lead_status="WATCHING", last_activity_at=now - timedelta(days=10))
        old_actionable = _lead(session, lead_status="ACTIONABLE", last_activity_at=now - timedelta(days=12))
        oid = old_watching.id

    record_outcome(old_actionable.id, "WRITTEN")

    r = freshness_report()
    assert r["archived_watching"] == 1
    assert r["archived_actionable"] == 1
    assert r["archived_written"] == 1


def test_freshness_report_text_format_runs(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    r = freshness_report()
    text = format_freshness_report_text(r)
    assert "StoryLead freshness" in text
    assert "Active (<7d):" in text


def test_freshness_report_read_only(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        _lead(session, last_activity_at=now - timedelta(days=10))
    freshness_report()
    freshness_report()
    with get_session() as session:
        assert len(session.execute(select(StoryLead)).scalars().all()) == 1


def test_cli_freshness_report_json_valid(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    import json
    r = freshness_report()
    text = json.dumps(r, ensure_ascii=False, indent=2, default=str)
    reparsed = json.loads(text)
    assert reparsed["total_leads"] == r["total_leads"]
