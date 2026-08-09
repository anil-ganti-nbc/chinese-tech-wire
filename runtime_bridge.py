"""Thin runtime bridge: version / identity / health for Chinese Tech Wire.

Tier B clank — staging/soak only. This module never runs collectors, never
invents historical data, and never claims a release channel or health state
the deployment hasn't earned. `release_channel` always comes from
`settings.release_channel` (env var `CTW_RELEASE_CHANNEL`), defaulting to the
least-trusted channel, "soaking" — never hard-coded here.

Modeled on the verified pattern in the sibling clank Free Game Tracker
(newsroom/runtime_bridge.py), adapted to this repo's flat module layout
(no `newsroom`-style package, SQLAlchemy `IngestionRun` instead of a
lightweight `source_health` table).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import ROOT, settings

CLANK_ID = "chinese-tech-wire"

# Mirrors web/app.py's CTW_VERSION. Duplicated as a plain constant rather
# than imported: importing web.app constructs a FastAPI app and mounts
# static files as a side effect of import, which is unnecessary weight (and
# an unnecessary dependency on fastapi/jinja2 being importable) for a
# headless `--identity`/`--health` check that must work even when nothing
# GUI-related is being used. Keep in sync with web/app.py:CTW_VERSION.
PACKAGE_VERSION = "0.5.6.1"

RUNTIME_BRIDGE_VERSION = "stage1.0"


def _source_revision() -> str:
    """Full Git SHA this image was built from, baked in at build time.

    Set via the Dockerfile's `GIT_REVISION` build arg -> `CTW_SOURCE_REVISION`
    env var. Never read from a `.git` directory at runtime (none exists in the
    image). Local/non-Docker runs report "unknown" rather than a fabricated
    value. Same pattern proven on OEM Radar's Hetzner deployment.
    """
    return os.environ.get("CTW_SOURCE_REVISION", "unknown")


def _source_revision_short() -> str:
    revision = _source_revision()
    return revision if revision == "unknown" else revision[:12]


def get_version_info() -> Dict[str, str]:
    return {
        "clank_id": CLANK_ID,
        "clank_version": PACKAGE_VERSION,
        "package_name": "chinese-tech-wire",
        "release_channel": settings.release_channel,
        "runtime_bridge": RUNTIME_BRIDGE_VERSION,
        "source_revision": _source_revision(),
        "source_revision_short": _source_revision_short(),
    }


def get_identity() -> Dict[str, Any]:
    """Identity always reflects settings.release_channel — never hard-coded.

    A container image reports whatever CTW_RELEASE_CHANNEL is set to at
    runtime; it defaults to "soaking" (the least-trusted channel) so an
    unconfigured or freshly built image never self-reports a maturity it
    hasn't earned. This clank is Tier B — no "production" channel is wired
    up for it in this phase, by design.
    """
    return {
        "clank_id": CLANK_ID,
        "clank_version": PACKAGE_VERSION,
        "release_channel": settings.release_channel,
        "runtime_bridge": RUNTIME_BRIDGE_VERSION,
        "source_revision": _source_revision(),
        "source_revision_short": _source_revision_short(),
    }


def _db_path() -> Optional[Path]:
    """Resolve settings.database_url to a filesystem path, if it's sqlite."""
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///") :]
    if raw in ("", ":memory:"):
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / raw
    return p


def get_health() -> Dict[str, Any]:
    """Build health from process + DB + `ingestion_runs` history.

    Semantics:
    - process_liveness: always true if this function runs.
    - application_readiness: DB file exists, or its parent directory is
      writable so init_db() could create it.
    - last_attempted_run / last_successful_run / last_run_status come from
      the same `ingestion_runs` table the GUI Health page reads. All are
      null when no run history exists yet — never fabricated.
    - operational_state:
        "unknown"  — no DB, or no ingestion_runs rows yet (a freshly built
                     soak container has not proven anything either way; it
                     is not "healthy" just because nothing has failed).
        "healthy"  — most recent completed run has status SUCCESS.
        "degraded" — most recent completed run has status PARTIAL.
        "failed"   — most recent completed run has status FAILED, or the
                     database is not writable at all.
    """
    reasons: List[str] = []
    db = _db_path()

    db_exists = False
    db_writable = False
    if db is None:
        reasons.append(
            f"unsupported or unset DATABASE_URL for health check: {settings.database_url!r}"
        )
    else:
        db_exists = db.exists()
        parent = db.parent
        db_writable = parent.exists() and os.access(parent, os.W_OK)
        if not db_writable:
            reasons.append(f"database parent not writable: {parent}")
        if not db_exists:
            reasons.append(f"database file missing: {db}")

    last_attempt: Optional[datetime] = None
    last_success: Optional[datetime] = None
    last_status: Optional[str] = None
    total_runs = 0

    if db is not None and db_exists:
        try:
            from sqlalchemy import desc, func, select

            from database.db import get_session, init_db
            from database.models import IngestionRun

            init_db(settings.database_url)
            with get_session(settings.database_url) as session:
                total_runs = (
                    session.execute(select(func.count()).select_from(IngestionRun)).scalar()
                    or 0
                )
                latest_completed = session.execute(
                    select(IngestionRun)
                    .where(IngestionRun.status != "RUNNING")
                    .order_by(desc(IngestionRun.started_at))
                    .limit(1)
                ).scalar_one_or_none()
                if latest_completed is not None:
                    last_attempt = latest_completed.started_at
                    last_status = latest_completed.status
                    if latest_completed.status != "SUCCESS":
                        reasons.append(
                            f"last completed run status={latest_completed.status}: "
                            f"{latest_completed.summary or 'no summary recorded'}"
                        )
                latest_success = session.execute(
                    select(IngestionRun)
                    .where(IngestionRun.status == "SUCCESS")
                    .order_by(desc(IngestionRun.started_at))
                    .limit(1)
                ).scalar_one_or_none()
                if latest_success is not None:
                    last_success = latest_success.started_at
                if total_runs == 0:
                    reasons.append("no ingestion_runs recorded yet")
        except Exception as exc:  # noqa: BLE001 - health must never raise
            reasons.append(f"health query failed: {exc!r}")
            db_writable = False

    if db is None or not db_writable:
        state = "failed"
    elif total_runs == 0:
        state = "unknown"
    elif last_status == "SUCCESS":
        state = "healthy"
    elif last_status == "PARTIAL":
        state = "degraded"
    elif last_status == "FAILED":
        state = "failed"
    else:
        state = "unknown"

    observed = datetime.now(timezone.utc)

    return {
        "operational_state": state,
        "process_liveness": True,
        "application_readiness": bool(db_exists or db_writable),
        "last_attempted_run": last_attempt.isoformat() if last_attempt else None,
        "last_successful_run": last_success.isoformat() if last_success else None,
        "last_run_status": last_status,
        "total_runs": total_runs,
        "database_writable": db_writable,
        "version_info": get_version_info(),
        "status_reasons": reasons,
        "observed_at": observed.isoformat(),
    }
