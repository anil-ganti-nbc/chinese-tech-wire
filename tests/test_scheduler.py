
"""V0.5.2 full-cycle orchestration + IngestionRun tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from database.db import get_session, init_db
from database.models import IngestionRun


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/sched.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def test_full_cycle_persists_success(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    with patch("main.run_source", return_value=2), \
         patch("pipeline.community_ingest.run_community_source", return_value=1), \
         patch("pipeline.documentary_ingest.run_documentary_source", return_value=3), \
         patch("pipeline.newsroom.rebuild_leads", return_value=4), \
         patch("sources.SOURCE_REGISTRY", {"ithome": object}), \
         patch("community_sources.COMMUNITY_REGISTRY", {"chiphell": object}), \
         patch("documentary_sources.DOCUMENTARY_REGISTRY", {"jd": object}):
        from pipeline.full_cycle import run_full_cycle
        stats = run_full_cycle(trigger="MANUAL", dry_run=True)

    assert stats["articles_new"] == 2
    assert stats["community_threads_new"] == 1
    assert stats["documentary_records_new"] == 3
    assert stats["leads_created"] == 4
    assert stats["status"] == "SUCCESS"
    assert stats["error_count"] == 0
    assert stats["run_id"] is not None

    with get_session() as session:
        row = session.get(IngestionRun, stats["run_id"])
        assert row is not None
        assert row.trigger == "MANUAL"
        assert row.status == "SUCCESS"
        assert row.finished_at is not None
        assert row.articles_new == 2
        assert row.leads_created == 4


def test_full_cycle_persists_alert_counters(tmp_path, monkeypatch):
    """Alert funnel counters produced during rebuild_leads must be copied onto
    the IngestionRun row, not just kept in the in-process alert-stats dict.

    Regression test for the gap noted in HANDOFF.md #10: full-cycle tests
    previously only checked article/lead counts, not that alerts_eligible/
    alerts_attempted/alerts_suppressed actually persist to the DB row.
    """
    _db(tmp_path, monkeypatch)

    from pipeline.alerts import _bump

    def fake_rebuild_leads(dry_run=False):
        # Simulate what a real rebuild_leads() -> maybe_alert_lead() cycle
        # would record via pipeline.alerts._bump for a mix of leads.
        _bump("alerts_evaluated", 3)
        _bump("alerts_eligible", 2)
        _bump("alerts_attempted", 2)
        _bump("alerts_sent", 1)
        _bump("alerts_failed", 1)
        _bump("alerts_suppressed_score", 1)
        return 3

    with patch("main.run_source", return_value=0), \
         patch("pipeline.community_ingest.run_community_source", return_value=0), \
         patch("pipeline.documentary_ingest.run_documentary_source", return_value=0), \
         patch("pipeline.newsroom.rebuild_leads", side_effect=fake_rebuild_leads), \
         patch("sources.SOURCE_REGISTRY", {"ithome": object}), \
         patch("community_sources.COMMUNITY_REGISTRY", {"chiphell": object}), \
         patch("documentary_sources.DOCUMENTARY_REGISTRY", {"jd": object}):
        from pipeline.full_cycle import run_full_cycle
        stats = run_full_cycle(trigger="MANUAL", dry_run=True)

    assert stats["alerts_evaluated"] == 3
    assert stats["alerts_eligible"] == 2
    assert stats["alerts_attempted"] == 2
    assert stats["alerts_sent"] == 1
    assert stats["alerts_failed"] == 1
    assert stats["alerts_suppressed"] == 1

    with get_session() as session:
        row = session.get(IngestionRun, stats["run_id"])
        assert row is not None
        # These must match the in-memory alert_stats dict exactly — this is
        # the persistence path that was previously untested.
        assert row.alerts_sent == 1
        assert row.alerts_evaluated == 3
        assert row.alerts_eligible == 2
        assert row.alerts_attempted == 2
        assert row.alerts_failed == 1
        assert row.alerts_suppressed == 1
        assert row.alert_decision_summary is not None
        assert row.alert_decision_summary.get("alerts_sent") == 1


def test_full_cycle_partial_on_source_error(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("network")

    with patch("main.run_source", side_effect=boom), \
         patch("pipeline.community_ingest.run_community_source", return_value=1), \
         patch("pipeline.documentary_ingest.run_documentary_source", return_value=0), \
         patch("pipeline.newsroom.rebuild_leads", return_value=1), \
         patch("sources.SOURCE_REGISTRY", {"ithome": object, "zol": object}), \
         patch("community_sources.COMMUNITY_REGISTRY", {"chiphell": object}), \
         patch("documentary_sources.DOCUMENTARY_REGISTRY", {"jd": object}):
        from pipeline.full_cycle import run_full_cycle
        stats = run_full_cycle(trigger="SCHEDULED", dry_run=True)

    assert stats["error_count"] >= 1
    assert stats["community_threads_new"] == 1
    assert stats["status"] in ("PARTIAL", "SUCCESS")
    assert stats["trigger"] == "SCHEDULED"


def test_scheduled_logging_creates_file(tmp_path, monkeypatch):
    from pipeline.scheduled_log import setup_scheduled_logging
    import pipeline.scheduled_log as sl
    sl._configured = False
    path = setup_scheduled_logging(tmp_path / "logs")
    assert path.exists() or path.parent.exists()


def test_install_script_contains_non_overlap_and_venv():
    root = Path(__file__).resolve().parents[1]
    ps1 = (root / "scripts" / "install_scheduler.ps1").read_text(encoding="utf-8")
    assert "ChineseTechWire" in ps1
    assert ".venv" in ps1
    assert "--full-once" in ps1
    assert "--scheduled" in ps1
    assert "schtasks" in ps1
    assert "HOURLY" in ps1
    assert "IgnoreNew" in ps1
    assert "WorkingDirectory" in ps1
    assert "[TimeSpan]::MaxValue" not in ps1


def test_remove_and_status_scripts_exist():
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts" / "remove_scheduler.ps1").exists()
    assert (root / "scripts" / "scheduler_status.ps1").exists()
    status = (root / "scripts" / "scheduler_status.ps1").read_text(encoding="utf-8")
    assert "Get-ScheduledTask" in status


def test_health_shows_ingestion_run(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from database.models import IngestionRun
    now = datetime.now(timezone.utc)
    with get_session() as session:
        session.add(IngestionRun(
            started_at=now, finished_at=now, trigger="SCHEDULED", status="SUCCESS",
            duration_seconds=12.5, articles_new=5, community_threads_new=2,
            documentary_records_new=1, leads_created=3, alerts_sent=0,
            warning_count=0, error_count=0, summary="test run",
        ))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert b"SCHEDULED" in r.content or b"SUCCESS" in r.content
    assert b"Ingestion" in r.content or b"Last" in r.content or b"last" in r.content
