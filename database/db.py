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
    from .schema_state import EXPECTED_SCHEMA_VERSION, inspect_primary_store

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    path = _database_file_path(url)
    if path is None:
        from .schema_state import SchemaState, _verdict
        return _verdict(
            SchemaState.FRESH, EXPECTED_SCHEMA_VERSION, None,
            "in-memory store; canonical bootstrap may create it",
        )
    return inspect_primary_store(path)


def _admit_schema_authorities(
    engine, *, marker_source: str, established_by: str, reason: str,
) -> dict:
    """Write BOTH durable authorities in one transaction, then report what
    was written.

    The compatibility marker and the observation-continuity registry are
    separate authorities with separate meanings (STD-DEPLOY-COM-002 vs
    STD-DATA-COM-001), but they are admitted together: every canonical
    action that carries a store to the current contract — fresh bootstrap,
    explicit adoption, explicit marked migration — establishes the
    observation-continuity boundary at the same instant it stamps the
    version authority, so the two facts can never drift apart. The
    continuity fact is seeded FIRST and the marker claims the current
    contract only after it exists.

    Called only from those canonical actions; normal compatible opens
    perform zero writes (see init_db).
    """
    from .observation_continuity import create_table_sql, read_active_epoch, seed_epoch
    from .schema_state import EXPECTED_SCHEMA_VERSION, SCHEMA_META_TABLE

    with engine.begin() as conn:
        raw = conn.connection.dbapi_connection
        conn.execute(text(create_table_sql()))
        # The continuity boundary is established only when the store records
        # none: a store that already carries regime history keeps it (a
        # marked migration must not fabricate a second boundary for state
        # whose continuity is already recorded), while a genuinely
        # continuity-less store gets its first, honest boundary here.
        existing = read_active_epoch(raw)
        epoch = existing or seed_epoch(
            raw, established_by=established_by, reason=reason,
        )
        conn.execute(text(_SCHEMA_META_CREATE))
        conn.execute(
            text(f"INSERT INTO {SCHEMA_META_TABLE} (version, source) VALUES (:v, :s)"),
            {"v": EXPECTED_SCHEMA_VERSION, "s": marker_source},
        )
    return dict(epoch)


def _bootstrap_fresh(engine) -> None:
    """Canonical fresh-state bootstrap: the current full declarative schema,
    then both durable authorities stamped in one transaction.

    STD-DATA-COM-001: a genuinely fresh store establishes its initial
    observation-continuity regime honestly — observations begin with this
    bootstrap, and nothing before it is claimed or reconstructed."""
    Base.metadata.create_all(engine)
    _admit_schema_authorities(
        engine,
        marker_source="bootstrap",
        established_by="fresh-bootstrap",
        reason=(
            "canonical fresh bootstrap: observation continuity begins with "
            "this initial store; no prior history exists to claim"
        ),
    )


def init_db(database_url: str | None = None):
    """Compatibility-gated initialization. Returns the admission report.

    - FRESH: canonical bootstrap (create_all + both authorities stamped at
      the current contract), then
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
            from .schema_state import EXPECTED_SCHEMA_VERSION, SchemaState, _verdict

            post = _verdict(
                SchemaState.UNKNOWN, EXPECTED_SCHEMA_VERSION, None,
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
    the pre-authority application contract but predates the durable
    authorities. Adoption re-proves that structure read-only, writes BOTH
    authorities (schema version + the initial observation-continuity
    boundary), re-inspects, and reports durable evidence. It never repairs:
    a database that is not exactly LEGACY_UNADOPTED is refused untouched,
    and normal code paths never call this.

    STD-DATA-COM-001: adoption establishes the observation-continuity
    boundary at the adoption instant and claims NOTHING for pre-adoption
    rows — their prior continuity is unrecorded, and reads classify them
    PRE_EPOCH rather than fabricating a regime for them.
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
    # means). Write both authorities in one transaction, then re-inspect
    # read-only.
    engine = get_engine(url)
    epoch = _admit_schema_authorities(
        engine,
        marker_source="legacy-adoption",
        established_by="legacy-adoption",
        reason=(
            "explicit operator adoption: observation continuity is recorded "
            "from this boundary forward; no continuity is claimed for "
            "pre-adoption rows, whose history predates any recorded regime"
        ),
    )
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
        "observation_continuity": epoch,
        "database": str(path),
    }


def migrate_current_schema(database_url: str | None = None) -> dict:
    """Explicit operator-only marked migration of an older marked store to
    the current schema contract (the canonical vN -> vN+1 mechanism the
    version authority anticipates).

    Currently defined transition: v1 -> v2 (STD-DATA-COM-001), which adds
    the observation-continuity authority. The migration:

    - requires exactly MIGRATION_REQUIRED state with a readable older
      marker, and proves the store is structurally complete for the older
      contract before writing anything (a genuinely incomplete store is
      PARTIAL and is refused untouched);
    - creates the observation-continuity table and seeds the boundary epoch
      with established_by='migration-boundary', recording HONESTLY that
      continuity is claimed only from this instant forward — pre-migration
      rows are not fabricated into any regime and reads classify them
      PRE_EPOCH;
    - stamps schema_meta at the expected version, re-inspects read-only,
      and refuses to report success unless the store really is COMPATIBLE.

    It never runs implicitly: normal startup refuses MIGRATION_REQUIRED
    state with evidence and leaves the store untouched until the operator
    invokes this.
    """
    import sqlite3 as _sqlite3

    from .schema_state import (
        _APPLICATION_TABLES,
        SchemaState,
        _missing_columns,
        inspect_primary_store,
    )

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    path = _database_file_path(url)
    if path is None or not path.exists():
        report = inspect_primary_store(path) if path else None
        return {
            "migrated": False,
            "reason": (
                "no existing database to migrate (a fresh store bootstraps "
                "directly at the current contract)"
            ),
            **({"report": report.as_evidence()} if report else {}),
        }

    report = inspect_primary_store(path)
    if report.state is not SchemaState.MIGRATION_REQUIRED:
        return {
            "migrated": False,
            "reason": (
                "migration requires exactly MIGRATION_REQUIRED state; "
                f"this database is {report.state.value} and was not modified"
            ),
            "report": report.as_evidence(),
        }

    # Prove the store is genuinely complete for the contract its marker
    # claims before carrying it forward. A v1-marked store missing v1
    # structure is not a migration candidate: it is damaged state, and
    # migration must not launder it toward "current".
    con = _sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing_tables = sorted(set(_APPLICATION_TABLES) - tables)
        missing_columns = {
            table: absent
            for table, absent in _missing_columns(con, tables).items()
            if table in _APPLICATION_TABLES
        }
    finally:
        con.close()
    if missing_tables or missing_columns:
        return {
            "migrated": False,
            "reason": (
                "refusing to migrate: the store claims an older marked "
                "contract but is not structurally complete for it; damaged "
                "state must not be laundered toward current by migration"
            ),
            "report": report.as_evidence(),
            "missing_tables": missing_tables,
            "tables_missing_columns": sorted(missing_columns),
        }

    engine = get_engine(url)
    epoch = _admit_schema_authorities(
        engine,
        marker_source="marked-migration",
        established_by="migration-boundary",
        reason=(
            "explicit marked migration to the current contract "
            "(STD-DATA-COM-001): observation continuity is recorded from "
            "this boundary forward; no continuity is claimed for "
            "pre-migration rows, whose prior regime is unrecorded"
        ),
    )
    post = inspect_primary_store(path)
    if post.state is not SchemaState.COMPATIBLE:
        post.evidence["migration_failure"] = (
            f"{SCHEMA_META_TABLE} write did not produce compatible state"
        )
        raise SchemaStateError(post)
    return {
        "migrated": True,
        "state": post.state.value,
        "expected_schema_version": post.expected_version,
        "observed_schema_version": report.observed_version,
        "verified_tables": len(post.evidence.get("user_tables", [])),
        "observation_continuity": epoch,
        "database": str(path),
    }


def record_observation_discontinuity(
    reason: str, database_url: str | None = None,
) -> dict:
    """Explicit operator action: declare the observation history
    discontinuous and begin a new regime.

    STD-DATA-COM-001 makes a discontinuity (data loss, restore from an
    older backup, a re-baseline, authoritative-state replacement) a fact
    that must be recorded as durably as the records on either side of it.
    This is the CTW operator action that records it: the previous regime's
    row remains untouched, a new epoch begins at the declared instant, and
    the reason is stored with it.

    Guardrails: the store must be currently compatible (a damaged store is
    never written); an ingestion run must not be in flight (the boundary
    must not split a run); and nothing here ever rewrites historical rows —
    pre-boundary observations classify to their own earlier regime or to
    PRE_EPOCH exactly as before.
    """
    if not reason or not reason.strip():
        return {
            "recorded": False,
            "reason": "a continuity-break declaration requires a stated reason",
        }

    from .observation_continuity import (
        ESTABLISHED_BY_OPERATOR,
        read_active_epoch,
        seed_epoch,
    )
    from .schema_state import SchemaState

    url = _resolve_sqlite_url(database_url or os.getenv("DATABASE_URL", DEFAULT_DB))
    report = inspect_primary(url)
    if report.state is not SchemaState.COMPATIBLE:
        return {
            "recorded": False,
            "reason": (
                "recording a discontinuity requires a currently compatible "
                f"store; this database is {report.state.value} and was not "
                "modified"
            ),
            "report": report.as_evidence(),
        }

    # A compatible v2 store always carries the continuity table (verified by
    # the structural manifest), so no DDL happens here — only the append.
    engine = get_engine(url)
    with engine.begin() as conn:
        raw = conn.connection.dbapi_connection
        running = conn.execute(
            text("SELECT COUNT(*) FROM ingestion_runs WHERE status = 'RUNNING'")
        ).fetchone()[0]
        if running:
            return {
                "recorded": False,
                "reason": (
                    f"{running} ingestion run(s) are still RUNNING; a "
                    "continuity boundary must not split a run in flight"
                ),
            }
        previous = read_active_epoch(raw)
        epoch = seed_epoch(
            raw,
            established_by=ESTABLISHED_BY_OPERATOR,
            reason=reason.strip(),
            previous_epoch_uid=previous["epoch_uid"] if previous else None,
        )
    return {
        "recorded": True,
        **epoch,
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
