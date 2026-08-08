"""Database setup, session management, and lightweight schema migrations."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, List, Tuple

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from database.models import Base

logger = logging.getLogger(__name__)

# Default to project data/
DEFAULT_DB = "sqlite:///data/ctw.db"

# Columns added after the original V0.1/V0.2 schema.
# create_all() will not ALTER existing tables — we add them explicitly.
_STORY_CLUSTER_V03_COLUMNS: List[Tuple[str, str]] = [
    ("first_signal_source", "VARCHAR(32)"),
    ("first_signal_at", "DATETIME"),
    ("first_media_source", "VARCHAR(32)"),
    ("first_media_at", "DATETIME"),
    ("first_documentary_source", "VARCHAR(32)"),
    ("first_documentary_at", "DATETIME"),
]


def get_engine(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL", DEFAULT_DB)
    # Ensure parent dir exists for sqlite
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
    )

    # SQLite foreign keys
    if "sqlite" in url:

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
    return {row[1] for row in rows}


def _add_columns(conn, table: str, columns: list) -> None:
    try:
        cols = _existing_columns(conn, table)
    except Exception:
        return
    if not cols:
        return
    for col_name, col_type in columns:
        if col_name not in cols:
            sql = f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
            logger.info("[DB] migrating: %s", sql)
            conn.execute(text(sql))


def migrate_schema(engine) -> None:
    """Add any missing columns/tables without wiping existing data."""
    with engine.begin() as conn:
        _add_columns(conn, "story_clusters", _STORY_CLUSTER_V03_COLUMNS)
        _add_columns(conn, "story_leads", [
            ("last_notified_priority", "FLOAT"),
        ])
        _add_columns(conn, "ingestion_runs", [
            ("alerts_evaluated", "INTEGER DEFAULT 0"),
            ("alerts_eligible", "INTEGER DEFAULT 0"),
            ("alerts_attempted", "INTEGER DEFAULT 0"),
            ("alerts_failed", "INTEGER DEFAULT 0"),
            ("alerts_suppressed", "INTEGER DEFAULT 0"),
            ("alert_decision_summary", "TEXT"),
        ])
        # V0.5.5.1 — layer-aware source-health telemetry. Existing rows are
        # backfilled 'NEWS' below: only the NEWS layer ever wrote this table
        # prior to this migration, so that backfill is factually accurate,
        # not an invented value.
        _add_columns(conn, "source_runs", [
            ("layer", "VARCHAR(16)"),
            ("soft_blocked", "BOOLEAN DEFAULT 0"),
        ])
        conn.execute(text(
            "UPDATE source_runs SET layer = 'NEWS' WHERE layer IS NULL"
        ))
        # Indexes for notification/health queries (IF NOT EXISTS)
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_lead_notif_attempted ON lead_notifications(attempted_at)",
            "CREATE INDEX IF NOT EXISTS idx_lead_notif_outcome ON lead_notifications(outcome)",
            "CREATE INDEX IF NOT EXISTS idx_lead_notif_lead ON lead_notifications(lead_id)",
            "CREATE INDEX IF NOT EXISTS idx_source_runs_source_started ON source_runs(source, started_at)",
            "CREATE INDEX IF NOT EXISTS idx_story_leads_activity ON story_leads(last_activity_at)",
            "CREATE INDEX IF NOT EXISTS idx_story_leads_status_priority ON story_leads(lead_status, priority_score)",
            "CREATE INDEX IF NOT EXISTS idx_lead_events_lead_type ON lead_events(lead_id, event_type)",
            # V0.5.6 — editorial validation
            "CREATE INDEX IF NOT EXISTS idx_lead_outcomes_lead_recorded ON lead_outcomes(lead_id, recorded_at)",
            "CREATE INDEX IF NOT EXISTS idx_missed_stories_lead ON missed_stories(matched_lead_id)",
            "CREATE INDEX IF NOT EXISTS idx_missed_stories_cluster ON missed_stories(matched_cluster_id)",
        ):
            try:
                conn.execute(text(idx_sql))
            except Exception as e:
                logger.debug("[DB] index skip: %s", e)


def init_db(database_url: str | None = None) -> None:
    engine = get_engine(database_url)
    # Create any brand-new tables (community_*, etc.)
    Base.metadata.create_all(engine)
    # Patch columns onto existing tables
    migrate_schema(engine)


@contextmanager
def get_session(database_url: str | None = None) -> Generator[Session, None, None]:
    engine = get_engine(database_url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
