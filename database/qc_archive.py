"""QC/editorial-decision archive — a physically separate SQLite database
from the main operational DB (data/ctw.db).

Why separate: a QC decision (Useful / Not useful / False positive /
Duplicate-or-stale — see pipeline/qc.py for the full mapping and rationale)
is an editorial record of what a human reviewer decided and why. It must
survive independent of anything that happens to the operational lead
pipeline later (a rebuild, a bad migration, a `--full-once` re-scoring
pass) — so it lives in its own file, data/qc_archive.db, with its own
engine/session and its own single table. Nothing in this module ever
deletes a row; it is append-only.

Kept intentionally tiny and dependency-light (no import of database.db /
database.models) so a problem in the main operational schema can never
prevent a QC decision from being archived.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

logger = logging.getLogger(__name__)

DEFAULT_QC_ARCHIVE_DB = "sqlite:///data/qc_archive.db"


def _resolve_sqlite_url(url: str) -> str:
    """Same relative-path-resolves-against-repo-root fix as database/db.py's
    _resolve_sqlite_url — duplicated rather than imported so this module
    stays independent of database.db (see module docstring)."""
    if not url.startswith("sqlite:///") or url.endswith(":memory:"):
        return url
    raw_path = url[len("sqlite:///"):]
    if raw_path == ":memory:":
        return url
    path = Path(raw_path)
    if path.is_absolute():
        return url
    from config import ROOT
    resolved = (ROOT / path).resolve()
    return "sqlite:///" + str(resolved).replace("\\", "/")


class QcArchiveBase(DeclarativeBase):
    pass


class QcArchiveEntry(QcArchiveBase):
    """One immutable record of a completed QC decision.

    `lead_id` carries a UNIQUE constraint — this is the hard backstop
    against double-QC/race duplication described in pipeline/qc.py: even
    if two concurrent requests both pass the application-level check, only
    one INSERT here can succeed and the loser is treated as a no-op, not a
    duplicate archive entry.
    """

    __tablename__ = "qc_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # USEFUL | NOT_USEFUL | FALSE_POSITIVE | DUPLICATE | WRITTEN
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Full snapshot of the StoryLead row (and related provenance: cluster,
    # first-signal sources, prior feedback/outcome history) at the moment
    # of decision — the item's complete state, not just its id, so the
    # archive is self-contained even if the lead is later purged upstream.
    item_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("lead_id", name="uq_qc_archive_lead_id"),
    )


def _default_qc_archive_url() -> str:
    """Absent an explicit QC_ARCHIVE_DATABASE_URL, the archive lives next
    to whatever the *operational* DB currently resolves to (same
    directory, filename qc_archive.db) rather than a hardcoded
    data/qc_archive.db. This matters for tests: they isolate the
    operational DB per-test via DATABASE_URL/tmp_path but don't know this
    module exists, so without this the archive would otherwise always
    land in the real repo's data/ dir even from a test run. Production
    behavior is unchanged — DATABASE_URL is unset there too, so this
    still resolves to data/qc_archive.db next to data/ctw.db."""
    from database.db import DEFAULT_DB
    op_url = os.getenv("DATABASE_URL", DEFAULT_DB)
    if op_url.startswith("sqlite:///") and not op_url.endswith(":memory:"):
        op_path = Path(op_url[len("sqlite:///"):])
        return "sqlite:///" + str(op_path.parent / "qc_archive.db").replace("\\", "/")
    return DEFAULT_QC_ARCHIVE_DB


def get_qc_engine(database_url: Optional[str] = None):
    url = database_url or os.getenv("QC_ARCHIVE_DATABASE_URL") or _default_qc_archive_url()
    url = _resolve_sqlite_url(url)
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
    )
    if "sqlite" in url:
        @event.listens_for(engine, "connect")
        def _set_pragma(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


def init_qc_archive(database_url: Optional[str] = None) -> None:
    engine = get_qc_engine(database_url)
    QcArchiveBase.metadata.create_all(engine)


@contextmanager
def get_qc_session(database_url: Optional[str] = None) -> Generator[Session, None, None]:
    engine = get_qc_engine(database_url)
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


def _now() -> datetime:
    return datetime.now(timezone.utc)
