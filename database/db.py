"""Database setup, session management, and the persistent-state
compatibility barrier (M17 / STD-DEPLOY-COM-002)."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from database.models import Base

logger = logging.getLogger(__name__)

# Default to project data/
DEFAULT_DB = "sqlite:///data/ctw.db"

# Project root, mirroring config.ROOT (imported lazily inside _resolve_sqlite_url
# to avoid a hard import-time dependency on config.py from this low-level
# module). A *relative* sqlite:/// path (the shipped default, and what an
# operator's .env typically has) must always resolve against the repo/exe
# root, never against the current process's working directory — otherwise
# running the exact same command from a different cwd (a scheduled task, a
# shortcut with no explicit "Start in", a shell someone cd'd around in)
# silently creates/reads a brand-new empty database in that cwd instead of
# the real one. This bit Chinese Tech Wire on Windows: the shipped .cmd
# launcher happens to cd into the repo first, but init_db()/get_session()
# are also called directly by scripts, tests, and any future invocation
# that doesn't happen to cd first.
def _resolve_sqlite_url(url: str) -> str:
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


def _database_file_path(url: str) -> Path | None:
    """Filesystem path of a resolved sqlite URL, or None for :memory:."""
    if not url.startswith("sqlite:///") or url.endswith(":memory:"):
        return None
    return Path(url.replace("sqlite:///", "", 1))


def get_engine(database_url: str | None = None):
    url = database_url or os.getenv("DATABASE_URL", DEFAULT_DB)
    url = _resolve_sqlite_url(url)
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


# -- compatibility barrier (M17 / STD-DEPLOY-COM-002) -------------------------
#
# Pre-M17, init_db() ran create_all plus ad-hoc ALTER patches on every
# invocation and swallowed selected DDL errors: ordinary initialization
# mutated schema before any compatibility was known, laundered unknown
# state toward "current", and silently tolerated newer databases. init_db()
# is now the barrier: one read-only inspection decides first, and mutation
# (canonical fresh bootstrap, or the explicit legacy-adoption write) only
# happens where specifically authorized, always followed by re-verification.
# A compatible existing store performs zero schema writes.

_SCHEMA_META_CREATE = (
    "CREATE TABLE IF NOT EXISTS schema_meta ("
    "version INTEGER NOT NULL, "
    "adopted_at TEXT NOT NULL DEFAULT (datetime('now')), "
    "source TEXT NOT NULL DEFAULT 'bootstrap')"
)


def inspect_primary(database_url: str | None = None):
    """Read-only compatibility inspection of the resolved primary store.
    Never mutates; safe to call repeatedly (health, CLI gates)."""
    from .schema_state import inspect_primary_store

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    path = _database_file_path(url)
    if path is None:
        from .schema_state import SchemaState, _verdict
        return _verdict(
            SchemaState.FRESH, 1, None,
            "in-memory store; canonical bootstrap may create it",
        )
    return inspect_primary_store(path)


def _stamp_schema_meta(engine, *, source: str) -> None:
    """Write the durable authority (bootstrap/adoption only)."""
    from .schema_state import EXPECTED_SCHEMA_VERSION, SCHEMA_META_TABLE

    with engine.begin() as conn:
        conn.execute(text(_SCHEMA_META_CREATE))
        conn.execute(
            text(f"INSERT INTO {SCHEMA_META_TABLE} (version, source) VALUES (:v, :s)"),
            {"v": EXPECTED_SCHEMA_VERSION, "s": source},
        )


def _bootstrap_fresh(engine) -> None:
    """Canonical fresh-state bootstrap: the current full declarative schema,
    then the version authority stamped at the expected version."""
    Base.metadata.create_all(engine)
    _stamp_schema_meta(engine, source="bootstrap")


def init_db(database_url: str | None = None):
    """Compatibility-gated initialization. Returns the admission report.

    - FRESH: canonical bootstrap (create_all + schema_meta v1), then
      re-verification before the store is admitted.
    - COMPATIBLE: zero writes; the report is returned unchanged.
    - Everything else (LEGACY_UNADOPTED, PARTIAL, UNKNOWN, CORRUPT,
      INCOMPATIBLE_NEWER, MIGRATION_REQUIRED): raises SchemaStateError with
      the full read-only evidence and the database is left untouched.
      Legacy adoption is a separate, explicit operator action — never a
      side effect of initialization.
    """
    from .schema_state import (
        SCHEMA_META_TABLE,
        SchemaState,
        SchemaStateError,
        inspect_primary_store,
        inspect_schema,
    )

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    path = _database_file_path(url)

    if path is None or not path.exists():
        # Genuinely fresh: nothing to inspect yet — bootstrap and reverify.
        # A bootstrap failure is an admission failure: it surfaces as a
        # compatibility refusal with evidence, never as a raw DDL error.
        engine = get_engine(url)
        try:
            _bootstrap_fresh(engine)
        except sqlite3.Error as exc:
            from .schema_state import SchemaState, _verdict

            post = _verdict(
                SchemaState.UNKNOWN, 1, None,
                f"fresh bootstrap failed and the store is not ready: {exc}",
                admission_failure=f"{type(exc).__name__}: {exc}",
            )
            raise SchemaStateError(post) from exc
        if path is None:
            raw = engine.raw_connection()
            try:
                post = inspect_schema(raw)
            finally:
                raw.close()
        else:
            post = inspect_primary_store(path)
        if post.state is not SchemaState.COMPATIBLE:
            post.evidence["admission_failure"] = "bootstrap did not produce compatible state"
            raise SchemaStateError(post)
        return post

    # Existing file: read-only inspection BEFORE any mutable handle exists.
    ro = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        report = inspect_schema(ro)
    finally:
        ro.close()
    if report.state is SchemaState.COMPATIBLE:
        return report
    if report.state is not SchemaState.FRESH:
        raise SchemaStateError(report)

    # Existing 0-table file: canonical bootstrap, then re-verify read-only.
    engine = get_engine(url)
    _bootstrap_fresh(engine)
    post = inspect_primary_store(path)
    if post.state is not SchemaState.COMPATIBLE:
        post.evidence["admission_failure"] = "bootstrap did not produce compatible state"
        raise SchemaStateError(post)
    return post


def adopt_current_schema(database_url: str | None = None) -> dict:
    """Explicit operator-only legacy adoption.

   LEGACY_UNADOPTED means the database is already structurally complete for
    the expected contract but predates the durable authority. Adoption
    re-proves that structure read-only, writes schema_meta v1, re-inspects,
    and reports durable evidence. It never repairs: a database that is not
    exactly LEGACY_UNADOPTED is refused untouched, and normal code paths
    never call this.
    """
    from .schema_state import (
        SCHEMA_META_TABLE,
        SchemaState,
        SchemaStateError,
        inspect_primary_store,
    )

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    path = _database_file_path(url)
    if path is None or not path.exists():
        report = inspect_primary_store(path) if path else None
        return {
            "adopted": False,
            "reason": "no existing database to adopt (fresh bootstrap "
                      "happens on normal initialization)",
            **({"report": report.as_evidence()} if report else {}),
        }

    report = inspect_primary_store(path)
    if report.state is not SchemaState.LEGACY_UNADOPTED:
        return {
            "adopted": False,
            "reason": (
                "adoption requires exactly LEGACY_UNADOPTED state; "
                f"this database is {report.state.value} and was not modified"
            ),
            "report": report.as_evidence(),
        }

    # Structural proof already succeeded (that is what LEGACY_UNADOPTED
    # means). Write the authority, then re-inspect read-only.
    engine = get_engine(url)
    _stamp_schema_meta(engine, source="legacy-adoption")
    post = inspect_primary_store(path)
    if post.state is not SchemaState.COMPATIBLE:
        post.evidence["adoption_failure"] = (
            f"{SCHEMA_META_TABLE} write did not produce compatible state"
        )
        raise SchemaStateError(post)
    return {
        "adopted": True,
        "state": post.state.value,
        "expected_schema_version": post.expected_version,
        "verified_tables": len(post.evidence.get("user_tables", [])),
        "database": str(path),
    }


@contextmanager
def get_session(database_url: str | None = None) -> Generator["Session", None, None]:
    """ORM session behind the compatibility barrier: unadmittable state is
    refused with evidence before any work; genuinely fresh state is allowed
    through (queries fail honestly on the absent schema; this context never
    mutates). Direct session consumers therefore cannot bypass the gate."""
    from .schema_state import SchemaState, SchemaStateError

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    report = inspect_primary(url)
    if report.state is not SchemaState.COMPATIBLE and report.state is not SchemaState.FRESH:
        raise SchemaStateError(report)
    engine = get_engine(url)
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
