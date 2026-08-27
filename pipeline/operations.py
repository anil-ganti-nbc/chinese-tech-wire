"""V0.5.7 — Health/operations console support: live runtime snapshot and
manual "run collector now" launch.

This module never scrapes, scores, reclusters, translates, or sends
notifications itself — it only reads IngestionRun/SourceRun state, and (for
the manual-run launch) starts the exact same `python main.py --full-once`
process the Windows Task Scheduler already uses. All actual collection work
happens in that separate process, not in the web request.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select

from database.db import get_session
from database.models import IngestionRun, SourceRun

logger = logging.getLogger("ctw.operations")

# Matches install_scheduler.ps1's own ExecutionTimeLimit (2 hours) — a
# RUNNING row older than this is treated as an abandoned/crashed process,
# not an active one, so a crash can never permanently lock out manual runs.
STALE_RUNNING_THRESHOLD_HOURS = 2.0

DEFAULT_RECENT_LIMIT = 20
MAX_RECENT_LIMIT = 100

_CORRELATE_POLL_TIMEOUT_SECONDS = 5.0
_CORRELATE_POLL_INTERVAL_SECONDS = 0.15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _project_root() -> Path:
    from web.launcher import project_root
    return project_root()


def _run_to_dict(run: Optional[IngestionRun]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    started = _aware(run.started_at)
    finished = _aware(run.finished_at)
    elapsed = None
    if run.status == "RUNNING" and started:
        elapsed = round((_now() - started).total_seconds(), 1)
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "duration_seconds": run.duration_seconds,
        "elapsed_seconds": elapsed,
        "articles_new": run.articles_new,
        "community_threads_new": run.community_threads_new,
        "documentary_records_new": run.documentary_records_new,
        "documentary_events_new": run.documentary_events_new,
        "leads_created": run.leads_created,
        "alerts_sent": run.alerts_sent,
        "warning_count": run.warning_count,
        "error_count": run.error_count,
        "summary": run.summary,
    }


def active_running_run(session=None) -> Optional[IngestionRun]:
    """The current genuinely-active RUNNING IngestionRun, or None.

    A RUNNING row older than STALE_RUNNING_THRESHOLD_HOURS is treated as a
    crashed/abandoned process, not active — it is never deleted or
    rewritten (historical data stays intact), it simply stops blocking new
    launches.
    """
    def _query(s):
        row = s.execute(
            select(IngestionRun)
            .where(IngestionRun.status == "RUNNING")
            .order_by(desc(IngestionRun.started_at))
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        started = _aware(row.started_at)
        if started and (_now() - started) > timedelta(hours=STALE_RUNNING_THRESHOLD_HOURS):
            return None  # stale — do not treat as active
        return row

    if session is not None:
        return _query(session)
    with get_session() as s:
        return _query(s)


def failed_sources_for_run(run: IngestionRun, session) -> List[Dict[str, Any]]:
    """SourceRun rows in this run's time window that failed — used to build
    the manual-run completion summary's 'Failed source' detail."""
    if not run.started_at:
        return []
    window_end = _aware(run.finished_at) or (_aware(run.started_at) + timedelta(hours=2))
    rows = session.execute(
        select(SourceRun)
        .where(SourceRun.started_at >= run.started_at)
        .where(SourceRun.started_at <= window_end)
        .where(SourceRun.success == False)  # noqa: E712
        .order_by(SourceRun.started_at)
    ).scalars().all()
    return [
        {"source": r.source, "layer": r.layer, "error": r.error_message}
        for r in rows
    ]


def _collector_interpreter(root: Path) -> str:
    """Python interpreter to run main.py with. From source, sys.executable
    already is the running interpreter. When frozen (a packaged .app),
    sys.executable is the app's own bootloader binary, not a python
    interpreter — using it here would just re-launch the GUI itself, so use
    the project's local virtualenv instead."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for candidate in (root / ".venv" / "bin" / "python3", root / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        f"No project virtualenv found at {root / '.venv'} — cannot launch a manual "
        "collector run from the packaged app"
    )


def launch_manual_run(source: Optional[str] = None) -> subprocess.Popen:
    """Starts the exact same production collection path the scheduled task
    uses (`python main.py --full-once`, MANUAL trigger — no --scheduled, so
    it's never confused with a Task Scheduler run) as a detached background
    process. Does not block on collection.

    If `source` is given, runs only that one collector (`--source NAME`,
    the same single-source CLI path `python main.py --source NAME` already
    uses) instead of the full cycle — this backs the Health page's
    per-collector "Run" buttons, distinct from "Run all collectors"."""
    if os.environ.get("CTW_DISABLE_COLLECTOR_LAUNCH") == "1":
        raise RuntimeError("Collector launch is disabled in this local field-test app")
    root = _project_root()
    main_script = root / "main.py"
    if not main_script.exists():
        raise RuntimeError(f"Could not find main.py under resolved project root {root}")
    interpreter = _collector_interpreter(root)
    log_path = root / "logs" / "manual-run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")
    label = f"source={source}" if source else "full cycle"
    log_file.write(f"\n--- manual run launched ({label}) {_now().isoformat()} ---\n")
    log_file.flush()

    args = [interpreter, str(main_script)]
    args += ["--source", source] if source else ["--full-once"]

    proc = subprocess.Popen(
        args,
        cwd=str(root),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    logger.info("Launched manual collector run (%s): pid=%s", label, proc.pid)
    return proc


def correlate_new_run(launch_time: datetime, timeout: float = _CORRELATE_POLL_TIMEOUT_SECONDS) -> Optional[int]:
    """Briefly polls for the IngestionRun row the just-launched manual
    process creates (trigger=MANUAL, started_at >= launch_time). Bounded
    wait for row creation only — NOT for the collector to finish, which can
    take minutes. Returns None if it doesn't appear in time (caller still
    returns success; the frontend falls back to generic polling)."""
    deadline = time.monotonic() + timeout
    launch_time = _aware(launch_time)
    while time.monotonic() < deadline:
        with get_session() as session:
            row = session.execute(
                select(IngestionRun)
                .where(IngestionRun.trigger == "MANUAL")
                .where(IngestionRun.started_at >= launch_time)
                .order_by(desc(IngestionRun.started_at))
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                return row.id
        time.sleep(_CORRELATE_POLL_INTERVAL_SECONDS)
    return None


def runtime_snapshot(manual_run_id: Optional[int] = None, recent_limit: int = DEFAULT_RECENT_LIMIT,
                      recent_offset: int = 0) -> Dict[str, Any]:
    """Assembles the full read-only payload for /api/health/runtime.
    Recent runs are fetched with SQL LIMIT/OFFSET, never the whole table."""
    recent_limit = max(1, min(int(recent_limit), MAX_RECENT_LIMIT))
    recent_offset = max(0, int(recent_offset))

    from pipeline.scheduler_status import get_scheduler_status
    try:
        scheduler = get_scheduler_status()
    except Exception as e:
        scheduler = {"available": False, "reason": f"unexpected error: {e}"}

    with get_session() as session:
        current = active_running_run(session=session)
        last_completed = session.execute(
            select(IngestionRun)
            .where(IngestionRun.status != "RUNNING")
            .order_by(desc(IngestionRun.started_at))
            .limit(1)
        ).scalar_one_or_none()
        recent_runs = session.execute(
            select(IngestionRun)
            .order_by(desc(IngestionRun.started_at))
            .limit(recent_limit)
            .offset(recent_offset)
        ).scalars().all()
        total_runs = session.execute(select(func.count()).select_from(IngestionRun)).scalar() or 0

        tracked_run = None
        if manual_run_id is not None:
            row = session.get(IngestionRun, manual_run_id)
            if row is not None:
                tracked_run = _run_to_dict(row)
                if row.status != "RUNNING":
                    failed = failed_sources_for_run(row, session)
                    if failed:
                        tracked_run["failed_sources"] = failed

        return {
            "now": _now().isoformat(),
            "scheduler": scheduler,
            "current_run": _run_to_dict(current),
            "last_completed_run": _run_to_dict(last_completed),
            "recent_runs": [_run_to_dict(r) for r in recent_runs],
            "recent_runs_total": int(total_runs),
            "recent_runs_limit": recent_limit,
            "recent_runs_offset": recent_offset,
            "tracked_run": tracked_run,
        }
