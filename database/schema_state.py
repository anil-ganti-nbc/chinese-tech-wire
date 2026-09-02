"""Persistent-state compatibility for the CTW primary store (M17).

STD-DEPLOY-COM-002, family CREATE_ALL_BOOTSTRAP_WITH_NEW_VERSION_AUTHORITY:
historically CTW treated `Base.metadata.create_all` plus ad-hoc ALTER
patches as an implicit, unversioned schema authority — any open could
silently create missing tables, silently patch recognized columns, and
silently tolerate newer state. There was no durable version authority
anywhere (verified against the canonical source and the real database
during M16), so an existing marker-less database could not honestly be
distinguished from unknown or partial state.

M17 introduces this module as the narrow, explicit, durable schema
authority:

- `schema_meta` — a single-row SQLite table recording the schema version
  (integer, starting at 1). Written only by bootstrap, future marked
  migrations, or the explicit `adopt_current_schema` operator action.
- `EXPECTED_SCHEMA_VERSION` — the expected version constant, in one
  authoritative place. Never derived from application/package versions or
  from model metadata at runtime.
- A structural contract that corroborates the marker: the expected table
  set plus, per table, the expected columns. `MARKER_EXISTS !=
  COMPATIBLE`; a marker claiming v1 over missing structure is PARTIAL.

The marker must NOT become a substitute for structural verification.

State model: FRESH, LEGACY_UNADOPTED, MIGRATION_REQUIRED, COMPATIBLE,
INCOMPATIBLE_NEWER, UNKNOWN, CORRUPT, PARTIAL.

- LEGACY_UNADOPTED is not FRESH and not COMPATIBLE: it is the expected
  state of a structurally complete pre-M17 database that lacks the new
  authority. It fails closed everywhere and may only be promoted by the
  explicit `adopt_current_schema` operator action, which re-proves the
  full structural contract before writing anything.
- Inspection is strictly read-only (quick_check, sqlite_master,
  table_info) and happens before any mutable initialization.

Skew contract: FORWARD_ONLY_EXPLICIT. A database marked newer than this
software understands fails closed; SQLAlchemy's ORM tolerance for extra
columns is not compatibility. No downgrade path exists or is claimed.

Structural contract scope — what is and is not verified:
- verified: required table presence; required column presence per table
  (the complete declarative column sets, derived from the same models
  `create_all` builds so the manifest cannot drift from a fresh
  bootstrap); presence of the authority itself.
- not verified: column SQL types and nullability (SQLite type affinity
  and SQLAlchemy DDL serialization make byte-level type assertions
  dishonest), index presence (indexes affect query performance, not
  result correctness), and byte-for-byte DDL equivalence. The goal is to
  detect meaningful compatibility contradictions, not to reproduce
  SQLAlchemy's internal schema serialization.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# The expected persistent-state contract of THIS software version. Single
# source of truth; written by bootstrap/adoption only, read everywhere.
EXPECTED_SCHEMA_VERSION = 1

# The authoritative name of the durable version marker table.
SCHEMA_META_TABLE = "schema_meta"

# Required tables of the primary store: the declarative application tables
# plus the authority itself. Static (auditable) list matching
# database/models.py at the M17 contract.
_APPLICATION_TABLES: tuple[str, ...] = (
    "article_entities", "articles", "author_profiles", "community_posts",
    "community_threads", "documentary_events", "documentary_records",
    "documentary_snapshots", "entities", "ingestion_runs", "lead_events",
    "lead_feedback", "lead_notifications", "lead_outcomes", "missed_stories",
    "notifications", "source_runs", "story_clusters", "story_leads",
    "thread_metrics", "translation_cache",
)

# Required columns per application table, derived once at import from the
# declarative models — the exact definition `create_all` bootstraps from, so
# the manifest and a fresh bootstrap can never disagree. This is a
# *required-subset* manifest: extra columns (e.g. from never-released newer
# code) are tolerated by inspection and caught by the version marker instead.
EXPECTED_TABLES: frozenset[str] = frozenset(_APPLICATION_TABLES) | {SCHEMA_META_TABLE}


def _expected_columns() -> dict[str, frozenset[str]]:
    from database.models import Base

    manifest: dict[str, frozenset[str]] = {}
    for name, table in Base.metadata.tables.items():
        manifest[name] = frozenset(column.name for column in table.columns)
    return manifest


EXPECTED_COLUMNS: dict[str, frozenset[str]] = _expected_columns()


class SchemaState(str, Enum):
    """Adjudication verdicts. FRESH != LEGACY_UNADOPTED != COMPATIBLE;
    DB_OPEN_SUCCESS != COMPATIBLE; MARKER_EXISTS != COMPATIBLE;
    STRUCTURE_EXISTS != COMPATIBLE; MIGRATION_CAN_RUN != COMPATIBLE."""

    FRESH = "FRESH"
    LEGACY_UNADOPTED = "LEGACY_UNADOPTED"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE_NEWER = "INCOMPATIBLE_NEWER"
    UNKNOWN = "UNKNOWN"
    CORRUPT = "CORRUPT"
    PARTIAL = "PARTIAL"


# Verdicts that can never participate in normal work, and that health
# reports as degraded/not-ready when observed read-only.
UNADMITTABLE_STATES = frozenset({
    SchemaState.LEGACY_UNADOPTED,
    SchemaState.INCOMPATIBLE_NEWER,
    SchemaState.UNKNOWN,
    SchemaState.CORRUPT,
    SchemaState.PARTIAL,
})


@dataclass(frozen=True)
class SchemaStateReport:
    """Read-only verdict on one persistent store, with the evidence that
    produced it. `as_evidence()` is the machine-readable refusal record."""

    state: SchemaState
    expected_version: int
    observed_version: int | None
    reason: str
    evidence: dict = field(default_factory=dict)

    def as_evidence(self) -> dict:
        return {
            "compatibility_state": self.state.value,
            "expected_schema_version": self.expected_version,
            "observed_schema_version": self.observed_version,
            "reason": self.reason,
            **self.evidence,
        }

    def __str__(self) -> str:
        return (
            f"{self.state.value}: {self.reason} "
            f"(expected schema v{self.expected_version}, "
            f"observed {'none' if self.observed_version is None else f'v{self.observed_version}'})"
        )


class SchemaStateError(RuntimeError):
    """Raised when a store is refused because its persistent state is not
    (or not yet) compatible with this software. `.report` carries the full
    read-only evidence; the database was not mutated by the refusal."""

    def __init__(self, report: SchemaStateReport) -> None:
        super().__init__(
            "persistent-state compatibility refused: "
            f"{report} — normal work was not admitted; the database was "
            f"left untouched for diagnosis"
        )
        self.report = report


def _verdict(state, expected_version, observed_version, reason, **evidence):
    return SchemaStateReport(
        state=state, expected_version=expected_version,
        observed_version=observed_version, reason=reason, evidence=evidence,
    )


def _read_marker(con: sqlite3.Connection) -> list[int] | None:
    """Version values from schema_meta, or None when the table does not
    have the expected single-value shape (a `version` column)."""
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({SCHEMA_META_TABLE})")}
    if "version" not in columns:
        return None
    return [row[0] for row in con.execute(f"SELECT version FROM {SCHEMA_META_TABLE}")]


def _parse_versions(raw: list) -> list[int] | None:
    values: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, str):
                try:
                    values.append(int(value))
                    continue
                except ValueError:
                    return None
            return None
        values.append(value)
    return values


def inspect_schema(
    con: sqlite3.Connection, *, expected_version: int = EXPECTED_SCHEMA_VERSION
) -> SchemaStateReport:
    """Adjudicate one open SQLite connection against the expected contract.
    Strictly read-only: never stamps, creates, alters, or repairs."""
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            return _verdict(
                SchemaState.CORRUPT, expected_version, None,
                f"quick_check reported {quick!r}", quick_check=str(quick),
            )
        tables = {
            row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    except sqlite3.DatabaseError as exc:
        return _verdict(
            SchemaState.CORRUPT, expected_version, None,
            f"not a usable SQLite database: {exc}", sqlite_error=str(exc),
        )

    app_tables_present = sorted(set(_APPLICATION_TABLES) & tables)

    if not tables:
        return _verdict(
            SchemaState.FRESH, expected_version, None,
            "no persistent state yet (zero user tables); canonical bootstrap "
            "may create it",
            user_tables=[],
        )

    if SCHEMA_META_TABLE not in tables:
        # No authority. Distinguish structurally complete pre-M17 state
        # (LEGACY_UNADOPTED) from everything else (fail closed). Completeness
        # is measured against the application tables only: schema_meta is
        # absent by definition in a marker-less database.
        missing_tables = sorted(set(_APPLICATION_TABLES) - tables)
        missing_columns = _missing_columns(con, tables)
        complete = not missing_tables and not missing_columns
        if complete:
            return _verdict(
                SchemaState.LEGACY_UNADOPTED, expected_version, None,
                "structurally complete pre-M17 database without the "
                f"{SCHEMA_META_TABLE} authority; normal work is refused "
                "until the operator explicitly adopts it "
                "(adopt-current-schema)",
                user_tables=sorted(tables),
            )
        if app_tables_present:
            return _verdict(
                SchemaState.PARTIAL, expected_version, None,
                "recognized CTW tables present but incomplete: "
                f"{len(missing_tables)} table(s) and "
                f"{len(missing_columns)} table(s) missing required columns; "
                "partial state must not be repaired automatically",
                missing_tables=missing_tables,
                tables_missing_columns=sorted(missing_columns),
                user_tables=sorted(tables),
            )
        return _verdict(
            SchemaState.UNKNOWN, expected_version, None,
            f"existing database with {len(tables)} unrelated table(s) and "
            "no CTW schema; not fresh and not recognizable",
            user_tables=sorted(tables),
        )

    raw = _read_marker(con)
    if raw is None:
        return _verdict(
            SchemaState.UNKNOWN, expected_version, None,
            f"{SCHEMA_META_TABLE} exists but lacks the expected 'version' "
            "column; the authority is unreadable",
            user_tables=sorted(tables),
        )
    versions = _parse_versions(raw)
    if versions is None:
        return _verdict(
            SchemaState.UNKNOWN, expected_version, None,
            f"{SCHEMA_META_TABLE} contains non-integer version data "
            f"({raw!r}); the authority is corrupt",
            user_tables=sorted(tables),
        )
    if not versions:
        return _verdict(
            SchemaState.UNKNOWN, expected_version, 0,
            f"{SCHEMA_META_TABLE} exists but records no version; state is "
            "neither fresh nor versioned",
            user_tables=sorted(tables),
        )

    observed = max(versions)
    if observed > expected_version:
        return _verdict(
            SchemaState.INCOMPATIBLE_NEWER, expected_version, observed,
            f"persistent state is newer (v{observed}) than this software "
            f"understands (v{expected_version}); the skew contract is "
            "FORWARD_ONLY_EXPLICIT and older software must not open it",
            user_tables=sorted(tables),
        )
    if observed < expected_version:
        return _verdict(
            SchemaState.MIGRATION_REQUIRED, expected_version, observed,
            f"older marked state (v{observed}) must migrate through the "
            f"canonical mechanism to v{expected_version} before normal work",
            user_tables=sorted(tables),
        )

    missing_tables = sorted(EXPECTED_TABLES - tables)
    missing_columns = _missing_columns(con, tables)
    if missing_tables or missing_columns:
        return _verdict(
            SchemaState.PARTIAL, expected_version, observed,
            f"marker records v{observed} but the structural contract is not "
            f"met: {len(missing_tables)} table(s) missing, "
            f"{len(missing_columns)} table(s) missing required columns",
            missing_tables=missing_tables,
            tables_missing_columns=sorted(missing_columns),
            user_tables=sorted(tables),
        )

    return _verdict(
        SchemaState.COMPATIBLE, expected_version, observed,
        f"state matches the expected v{expected_version} contract "
        f"({len(EXPECTED_TABLES)} tables incl. {SCHEMA_META_TABLE}, "
        "required columns verified)",
        user_tables=sorted(tables),
    )


def _missing_columns(
    con: sqlite3.Connection, tables: set[str]
) -> dict[str, list[str]]:
    """Per-table required columns absent from the actual database."""
    missing: dict[str, list[str]] = {}
    for table in sorted(EXPECTED_TABLES & tables):
        actual = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        absent = sorted(EXPECTED_COLUMNS.get(table, frozenset()) - actual)
        if absent:
            missing[table] = absent
    return missing


def inspect_primary_store(db_path: str | Path) -> SchemaStateReport:
    """Read-only inspection of a primary store by path. A missing file is
    FRESH. Opens its own mode=ro handle and never mutates."""
    path = Path(db_path)
    if not path.exists():
        return _verdict(
            SchemaState.FRESH, EXPECTED_SCHEMA_VERSION, None,
            "database file absent; canonical bootstrap may create it",
        )
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return inspect_schema(con)
    finally:
        con.close()
