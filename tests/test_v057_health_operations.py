"""V0.5.7 — Health/operations console regression tests.

Covers: live runtime JSON endpoint, Windows Task Scheduler status reader,
bounded/paginated recent-run table, manual "run collector now" launch with
overlap protection, and manual-run completion correlation (Addendum A).

All subprocess/schtasks calls are mocked — no real collector run, no real
Windows Task Scheduler query, no live network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from database.db import get_session, init_db
from database.models import IngestionRun, SourceRun
from pipeline.operations import (
    STALE_RUNNING_THRESHOLD_HOURS,
    _collector_interpreter,
    active_running_run,
    correlate_new_run,
    failed_sources_for_run,
    runtime_snapshot,
)
from pipeline.scheduler_status import _parse_schtasks_list, get_scheduler_status


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/v057ops.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _run(session, **overrides):
    defaults = dict(
        started_at=_now(), trigger="SCHEDULED", status="SUCCESS",
        finished_at=_now(), duration_seconds=10.0,
    )
    defaults.update(overrides)
    row = IngestionRun(**defaults)
    session.add(row)
    session.flush()
    session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# /api/health/runtime — read-only JSON endpoint
# ---------------------------------------------------------------------------

def test_runtime_endpoint_returns_200(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/api/health/runtime")
    assert r.status_code == 200
    body = r.json()
    assert "scheduler" in body and "current_run" in body and "recent_runs" in body


def test_runtime_endpoint_is_read_only(tmp_path, monkeypatch):
    """GET must never scrape/rescore/recluster/translate/notify/mutate."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(session)
    from fastapi.testclient import TestClient
    from web.app import app
    with patch("sources.ithome.ITHomeSource.fetch_latest") as mocked_scrape, \
         patch("pipeline.notify.send_discord") as mocked_notify:
        client = TestClient(app)
        client.get("/api/health/runtime")
        client.get("/api/health/runtime")
        mocked_scrape.assert_not_called()
        mocked_notify.assert_not_called()
    with get_session() as session:
        assert len(session.execute(select(IngestionRun)).scalars().all()) == 1


def test_recent_runs_bounded(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        for i in range(30):
            _run(session, started_at=_now() - timedelta(hours=i))
    snap = runtime_snapshot(recent_limit=10)
    assert len(snap["recent_runs"]) == 10
    assert snap["recent_runs_total"] == 30


def test_recent_runs_pagination(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        for i in range(30):
            _run(session, started_at=_now() - timedelta(hours=i), summary=f"run{i}")
    page1 = runtime_snapshot(recent_limit=10, recent_offset=0)
    page2 = runtime_snapshot(recent_limit=10, recent_offset=10)
    ids_1 = {r["id"] for r in page1["recent_runs"]}
    ids_2 = {r["id"] for r in page2["recent_runs"]}
    assert ids_1.isdisjoint(ids_2)


def test_recent_runs_newest_first(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        old = _run(session, started_at=_now() - timedelta(hours=5))
        new = _run(session, started_at=_now())
    snap = runtime_snapshot()
    assert snap["recent_runs"][0]["id"] == new.id
    assert snap["recent_runs"][1]["id"] == old.id


def test_current_running_run_returned(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        running = _run(session, status="RUNNING", finished_at=None, started_at=_now())
    snap = runtime_snapshot()
    assert snap["current_run"] is not None
    assert snap["current_run"]["id"] == running.id
    assert snap["current_run"]["status"] == "RUNNING"
    assert snap["current_run"]["elapsed_seconds"] is not None


def test_running_to_success_reflected_on_next_call(tmp_path, monkeypatch):
    """Simulates a RUNNING row completing between two polls."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(session, status="RUNNING", finished_at=None, started_at=_now())
        rid = row.id
    snap1 = runtime_snapshot()
    assert snap1["current_run"]["status"] == "RUNNING"

    with get_session() as session:
        row = session.get(IngestionRun, rid)
        row.status = "SUCCESS"
        row.finished_at = _now()

    snap2 = runtime_snapshot()
    assert snap2["current_run"] is None
    assert snap2["last_completed_run"]["id"] == rid
    assert snap2["last_completed_run"]["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# Overlap protection / stale RUNNING handling
# ---------------------------------------------------------------------------

def test_active_running_run_detects_genuine_run(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(session, status="RUNNING", finished_at=None, started_at=_now())
    assert active_running_run() is not None


def test_stale_running_run_does_not_block(tmp_path, monkeypatch):
    """A RUNNING row far older than the safety threshold (crashed process)
    must not permanently lock out new manual runs."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(
            session, status="RUNNING", finished_at=None,
            started_at=_now() - timedelta(hours=STALE_RUNNING_THRESHOLD_HOURS + 1),
        )
    assert active_running_run() is None


def test_stale_running_row_not_deleted_or_rewritten(tmp_path, monkeypatch):
    """Stale handling must not mutate the historical row."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(
            session, status="RUNNING", finished_at=None,
            started_at=_now() - timedelta(hours=STALE_RUNNING_THRESHOLD_HOURS + 1),
        )
        rid = row.id
    active_running_run()  # should not touch anything
    with get_session() as session:
        row = session.get(IngestionRun, rid)
        assert row is not None
        assert row.status == "RUNNING"  # untouched, not silently rewritten


def test_run_now_rejects_when_active(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(session, status="RUNNING", finished_at=None, started_at=_now())
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    with patch("pipeline.operations.launch_manual_run") as mocked_launch:
        r = client.post("/operations/run-now")
    assert r.status_code == 409
    assert r.json()["reason"] == "ALREADY_RUNNING"
    mocked_launch.assert_not_called()


def test_run_now_allows_launch_when_only_stale_running_exists(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(
            session, status="RUNNING", finished_at=None,
            started_at=_now() - timedelta(hours=STALE_RUNNING_THRESHOLD_HOURS + 1),
        )
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    with patch("pipeline.operations.launch_manual_run") as mocked_launch, \
         patch("pipeline.operations.correlate_new_run", return_value=None):
        mocked_launch.return_value = MagicMock(pid=12345)
        r = client.post("/operations/run-now")
    assert r.status_code == 200
    assert r.json()["started"] is True
    mocked_launch.assert_called_once()


# ---------------------------------------------------------------------------
# Manual run launches the production full-cycle path, non-blocking
# ---------------------------------------------------------------------------

def test_run_now_launches_production_full_cycle_subprocess(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    with patch("pipeline.operations.subprocess.Popen") as mocked_popen, \
         patch("pipeline.operations.correlate_new_run", return_value=None):
        mocked_popen.return_value = MagicMock(pid=999)
        r = client.post("/operations/run-now")
    assert r.status_code == 200
    assert mocked_popen.called
    args = mocked_popen.call_args[0][0]
    assert args[1].endswith("main.py")
    assert "--full-once" in args
    assert "--scheduled" not in args  # MANUAL trigger, not confused with Task Scheduler


def test_collector_interpreter_uses_sys_executable_from_source(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert _collector_interpreter(tmp_path) == sys.executable


def test_collector_interpreter_uses_project_venv_when_frozen(tmp_path, monkeypatch):
    """sys.executable is the packaged app's own bootloader binary when
    frozen — using it to launch main.py would just relaunch the GUI. Must
    use the project's real virtualenv interpreter instead."""
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        assert _collector_interpreter(tmp_path) == str(venv_python)
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_collector_interpreter_raises_when_frozen_without_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    try:
        with pytest.raises(RuntimeError, match="virtualenv"):
            _collector_interpreter(tmp_path)
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_run_now_does_not_block_for_full_collection(tmp_path, monkeypatch):
    """The endpoint only waits briefly for the IngestionRun row to appear —
    never for the collector itself to finish."""
    _db(tmp_path, monkeypatch)
    import time as time_mod
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    with patch("pipeline.operations.subprocess.Popen") as mocked_popen, \
         patch("pipeline.operations.correlate_new_run", return_value=None):
        mocked_popen.return_value = MagicMock(pid=999)
        t0 = time_mod.monotonic()
        r = client.post("/operations/run-now")
        elapsed = time_mod.monotonic() - t0
    assert r.status_code == 200
    assert elapsed < 10  # nowhere near a real multi-minute collector run


def test_correlate_new_run_finds_manual_row(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    launch_time = _now()
    with get_session() as session:
        row = _run(session, trigger="MANUAL", status="RUNNING", finished_at=None, started_at=_now())
        rid = row.id
    found = correlate_new_run(launch_time, timeout=2.0)
    assert found == rid


def test_correlate_new_run_ignores_older_scheduled_run(tmp_path, monkeypatch):
    """A scheduled run that started before the manual launch must not be
    mistaken for the manual run's result."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(session, trigger="SCHEDULED", status="SUCCESS", started_at=_now() - timedelta(minutes=5))
    launch_time = _now()
    found = correlate_new_run(launch_time, timeout=1.0)
    assert found is None  # no MANUAL row created after launch_time yet


# ---------------------------------------------------------------------------
# Windows Task Scheduler status reader
# ---------------------------------------------------------------------------

def test_scheduler_status_parses_schtasks_output():
    output = (
        "Folder: \\\n"
        "HostName:                            DESKTOP\n"
        "TaskName:                            \\ChineseTechWire\n"
        "Status:                               Ready\n"
        "Next Run Time:                        08-08-2026 20:00:00\n"
        "Last Run Time:                        08-08-2026 19:00:01\n"
        "Last Result:                          0\n"
    )
    fields = _parse_schtasks_list(output)
    assert fields["Status"] == "Ready"
    assert fields["Next Run Time"] == "08-08-2026 20:00:00"
    assert fields["Last Result"] == "0"


def test_scheduler_status_windows_success(monkeypatch):
    monkeypatch.setattr("pipeline.scheduler_status.sys.platform", "win32")
    fake = MagicMock(returncode=0, stdout="Status:  Ready\nNext Run Time:  20:00\nLast Run Time:  19:00\nLast Result:  0\n", stderr="")
    with patch("pipeline.scheduler_status.subprocess.run", return_value=fake):
        r = get_scheduler_status()
    assert r["available"] is True
    assert r["status"] == "Ready"


def test_scheduler_status_query_failure_handled_gracefully(monkeypatch):
    monkeypatch.setattr("pipeline.scheduler_status.sys.platform", "win32")
    with patch("pipeline.scheduler_status.subprocess.run", side_effect=OSError("boom")):
        r = get_scheduler_status()
    assert r["available"] is False
    assert r["reason"]


def test_scheduler_status_task_not_found_handled_gracefully(monkeypatch):
    monkeypatch.setattr("pipeline.scheduler_status.sys.platform", "win32")
    fake = MagicMock(returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.")
    with patch("pipeline.scheduler_status.subprocess.run", return_value=fake):
        r = get_scheduler_status()
    assert r["available"] is False


def test_scheduler_status_non_windows():
    with patch("pipeline.scheduler_status.sys.platform", "linux"):
        r = get_scheduler_status()
    assert r["available"] is False
    assert "Windows" in r["reason"]


def test_scheduler_status_included_in_runtime_snapshot(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with patch("pipeline.scheduler_status.get_scheduler_status", return_value={"available": False, "reason": "not windows"}):
        snap = runtime_snapshot()
    assert snap["scheduler"]["available"] is False


# ---------------------------------------------------------------------------
# Health page: run button disabled state, bounded table
# ---------------------------------------------------------------------------

def test_health_page_run_button_disabled_when_running(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(session, status="RUNNING", finished_at=None, started_at=_now())
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert 'id="run-now-btn" disabled' in r.text


def test_health_page_run_button_enabled_when_idle(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _run(session, status="SUCCESS")
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/health")
    assert 'id="run-now-btn" disabled' not in r.text


def test_health_page_recent_runs_bounded_to_20_by_default(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        for i in range(35):
            _run(session, started_at=_now() - timedelta(hours=i))
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/health")
    assert "showing 20 of 35" in r.text
    assert "Older runs" in r.text


def test_health_page_runs_offset_pagination(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        for i in range(35):
            _run(session, started_at=_now() - timedelta(hours=i))
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/health?runs_offset=20")
    assert r.status_code == 200
    assert "Newer" in r.text


# ---------------------------------------------------------------------------
# Addendum A — manual-run completion correlation and summary
# ---------------------------------------------------------------------------

def test_tracked_run_none_before_completion(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(session, trigger="MANUAL", status="RUNNING", finished_at=None, started_at=_now())
        rid = row.id
    snap = runtime_snapshot(manual_run_id=rid)
    assert snap["tracked_run"]["status"] == "RUNNING"
    assert "failed_sources" not in snap["tracked_run"]


def test_tracked_run_success_result(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(
            session, trigger="MANUAL", status="SUCCESS",
            articles_new=17, community_threads_new=2, leads_created=100,
            alerts_sent=1, warning_count=0, error_count=0, duration_seconds=84.0,
        )
        rid = row.id
    snap = runtime_snapshot(manual_run_id=rid)
    tr = snap["tracked_run"]
    assert tr["status"] == "SUCCESS"
    assert tr["articles_new"] == 17
    assert tr["leads_created"] == 100
    assert tr["alerts_sent"] == 1


def test_tracked_run_partial_result_with_failed_sources(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(
            session, trigger="MANUAL", status="PARTIAL",
            articles_new=14, warning_count=2, error_count=1, duration_seconds=112.0,
        )
        rid = row.id
        session.add(SourceRun(
            source="mobile01", layer="COMMUNITY", started_at=row.started_at, finished_at=row.started_at,
            success=False, articles_found=0, articles_new=0, error_message="HTTP 403",
        ))
    snap = runtime_snapshot(manual_run_id=rid)
    tr = snap["tracked_run"]
    assert tr["status"] == "PARTIAL"
    assert tr["failed_sources"]
    assert tr["failed_sources"][0]["source"] == "mobile01"
    assert "403" in tr["failed_sources"][0]["error"]


def test_tracked_run_failed_result(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(session, trigger="MANUAL", status="FAILED", error_count=3, duration_seconds=19.0)
        rid = row.id
    snap = runtime_snapshot(manual_run_id=rid)
    assert snap["tracked_run"]["status"] == "FAILED"
    assert snap["tracked_run"]["error_count"] == 3


def test_tracked_run_counts_match_persisted_ingestion_run(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(
            session, trigger="MANUAL", status="SUCCESS",
            articles_new=9, community_threads_new=3, documentary_records_new=1,
            leads_created=50, alerts_sent=2,
        )
        rid = row.id
    snap = runtime_snapshot(manual_run_id=rid)
    with get_session() as session:
        persisted = session.get(IngestionRun, rid)
        tr = snap["tracked_run"]
        assert tr["articles_new"] == persisted.articles_new
        assert tr["community_threads_new"] == persisted.community_threads_new
        assert tr["documentary_records_new"] == persisted.documentary_records_new
        assert tr["leads_created"] == persisted.leads_created
        assert tr["alerts_sent"] == persisted.alerts_sent


def test_scheduled_run_not_mistaken_for_tracked_manual_run(tmp_path, monkeypatch):
    """A scheduled run happening concurrently must not appear as current_run
    being confused with the tracked manual run — they're reported separately."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        manual = _run(session, trigger="MANUAL", status="SUCCESS", articles_new=5)
        scheduled = _run(session, trigger="SCHEDULED", status="RUNNING", finished_at=None, started_at=_now())
    snap = runtime_snapshot(manual_run_id=manual.id)
    assert snap["tracked_run"]["id"] == manual.id
    assert snap["tracked_run"]["trigger"] == "MANUAL"
    assert snap["current_run"]["id"] == scheduled.id
    assert snap["current_run"]["id"] != snap["tracked_run"]["id"]


def test_tracked_run_polling_is_read_only(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        row = _run(session, trigger="MANUAL", status="RUNNING", finished_at=None, started_at=_now())
        rid = row.id
    runtime_snapshot(manual_run_id=rid)
    runtime_snapshot(manual_run_id=rid)
    with get_session() as session:
        assert len(session.execute(select(IngestionRun)).scalars().all()) == 1


def test_failed_sources_for_run_only_within_window(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        row = _run(session, started_at=now, finished_at=now + timedelta(minutes=1))
        session.add(SourceRun(
            source="hkepc", layer="NEWS", started_at=now, finished_at=now,
            success=False, articles_found=0, articles_new=0, error_message="HTTP 402",
        ))
        session.add(SourceRun(  # outside the run window
            source="xfastest", layer="NEWS", started_at=now - timedelta(hours=5), finished_at=now - timedelta(hours=5),
            success=False, articles_found=0, articles_new=0, error_message="HTTP 403",
        ))
        session.flush()
        rid = row.id
    with get_session() as session:
        run = session.get(IngestionRun, rid)
        failed = failed_sources_for_run(run, session)
    assert len(failed) == 1
    assert failed[0]["source"] == "hkepc"
