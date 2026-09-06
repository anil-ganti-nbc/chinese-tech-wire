"""Observation continuity for the CTW primary store (STD-DATA-COM-001).

CTW derives editorial state from comparison against its own prior local
history: StoryLead lifecycle transitions (NEW/WATCHING/ACTIONABLE/...),
novelty decay on repeated observation, first-seen provenance on
articles/clusters/records, and the alert backlog gate. All of those facts
are only trustworthy while the dataset's observations belong to one
unbroken observational history. A restore from an older backup, a
re-baseline, or a deliberate authoritative-state replacement breaks that
history without changing a single row: a restored database would let its
stored first-seen/novelty/lead-status facts silently acquire the semantics
of current, continuously-observed records.

STD-DATA-COM-001 (frozen at standards-clank 7c821ea) requires that a
discontinuity in that history MUST be represented as an explicit, queryable
fact about the dataset's timeline — distinct from the records themselves
and from any novelty judgement — and that baseline/bootstrap observations
MUST be distinguishable, at read time, from ordinary post-continuity
observations.

This module is that fact, kept deliberately minimal and deliberately
orthogonal to everything it must not be confused with:

- NOT schema compatibility. ``schema_meta`` (STD-DEPLOY-COM-002) records
  which software contract this store is shaped for. This table records
  which observational regime its rows belong to. A compatibility migration
  that changes the schema does not by itself claim anything about the
  trustworthiness of prior observations, and a continuity break does not
  change the schema contract.
- NOT run identity. ``ingestion_runs.id`` identifies one execution.
- NOT the alert policy activation timestamp. ``policy_activated_at``
  (config, alert backlog gate) records when a policy became active — an
  operations decision — not whether observations share one trustworthy
  history.
- NOT ``first_seen_at``. A first-seen timestamp is a per-record fact; this
  is the timeline fact that says which regime that fact belongs to.

Model: an append-only set of epoch rows, one per observation-continuity
regime. The active regime is the highest ``epoch_number``. An observation
instant ``t`` classifies deterministically at read time:

- no recorded epoch at all            -> UNKNOWN_CONTINUITY (degraded,
  honest — never silently treated as trustworthy history)
- before the first recorded regime    -> PRE_EPOCH: the instant predates
  every continuity claim this store can make; legacy rows live here after
  an adoption/migration boundary, and that is honest — no fabricated
  continuity is asserted for them
- within a regime window              -> that regime (half-open windows:
  [epoch.started_at, next.started_at))

Row writes happen ONLY through the canonical admission actions in
``database/db.py`` — canonical fresh bootstrap, explicit legacy adoption,
the explicit marked migration, and the explicit operator discontinuity
command — never implicitly at normal startup, and never by rewriting
existing rows. History is never rewritten: a new regime is appended, the
previous regime's row remains, and the boundary between them is as durable
as the records on either side of it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

#: The authoritative name of the observation-continuity table. Kept in one
#: place; schema_state.py imports it so the structural manifest and this
#: module can never disagree about what the table is called.
OBSERVATION_CONTINUITY_TABLE = "observation_continuity"

#: Columns the canonical DDL creates. schema_state verifies this exact set
#: as part of the structural contract (a marker claiming current state over
#: a wrong-shaped table is PARTIAL, never silently accepted).
OBSERVATION_CONTINUITY_COLUMNS: tuple[str, ...] = (
    "id",
    "epoch_number",
    "epoch_uid",
    "started_at",
    "established_by",
    "previous_epoch_uid",
    "reason",
    "created_at",
)

#: How a regime came into being. Values are fixed vocabulary so downstream
#: readers can branch on them honestly:
#:   fresh-bootstrap      the canonical initial store; observations begin here
#:   legacy-adoption      explicit operator adoption of pre-authority state;
#:                        continuity is claimed only from this instant on
#:   migration-boundary   explicit marked migration carrying an older marked
#:                        store to the current contract; same honesty rule
#:   operator-discontinuity  an operator declared the prior history
#:                        discontinuous (data loss / restore / re-baseline)
ESTABLISHED_BY_FRESH = "fresh-bootstrap"
ESTABLISHED_BY_ADOPTION = "legacy-adoption"
ESTABLISHED_BY_MIGRATION = "migration-boundary"
ESTABLISHED_BY_OPERATOR = "operator-discontinuity"

#: Read-time classification verdicts for one observation instant.
IN_EPOCH = "IN_EPOCH"
PRE_EPOCH = "PRE_EPOCH"
UNKNOWN_CONTINUITY = "UNKNOWN_CONTINUITY"


def create_table_sql() -> str:
    """The canonical DDL for the observation-continuity authority.

    Raw DDL, exactly like ``schema_meta``: the table is part of the
    persistent-state contract verified by schema_state, but it is not an
    ORM application model — it is written only by the explicit canonical
    admission actions, never by create_all and never by ordinary sessions.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {OBSERVATION_CONTINUITY_TABLE} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "epoch_number INTEGER NOT NULL, "
        "epoch_uid TEXT NOT NULL UNIQUE, "
        "started_at TEXT NOT NULL, "
        "established_by TEXT NOT NULL, "
        "previous_epoch_uid TEXT, "
        "reason TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    """Tolerant ISO parser for timestamps written by this module or by
    anything honest enough to use ISO 8601. Returns None when unparseable
    rather than guessing."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seed_epoch(
    conn: sqlite3.Connection,
    *,
    established_by: str,
    reason: str | None = None,
    previous_epoch_uid: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    """Append one epoch row inside the CALLER'S transaction.

    Never commits, never creates the table, never runs implicitly: every
    caller is one of the canonical admission actions in database/db.py, all
    of which own the transaction and the compatibility verification.
    """
    next_number = conn.execute(
        f"SELECT COALESCE(MAX(epoch_number), 0) + 1 FROM {OBSERVATION_CONTINUITY_TABLE}"
    ).fetchone()[0]
    epoch_uid = f"ctw-obs-{uuid4().hex[:16]}"
    started = started_at or _utcnow_iso()
    conn.execute(
        f"INSERT INTO {OBSERVATION_CONTINUITY_TABLE} "
        "(epoch_number, epoch_uid, started_at, established_by, "
        "previous_epoch_uid, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (next_number, epoch_uid, started, established_by, previous_epoch_uid, reason),
    )
    return {
        "epoch_number": next_number,
        "epoch_uid": epoch_uid,
        "started_at": started,
        "established_by": established_by,
        "previous_epoch_uid": previous_epoch_uid,
        "reason": reason,
    }


def read_active_epoch(con: sqlite3.Connection) -> dict[str, Any] | None:
    """The current observation-continuity regime, or None when the store
    records none (honest UNKNOWN — never silently trusted). Read-only."""
    try:
        row = con.execute(
            f"SELECT epoch_number, epoch_uid, started_at, established_by, "
            f"previous_epoch_uid, reason FROM {OBSERVATION_CONTINUITY_TABLE} "
            "ORDER BY epoch_number DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "epoch_number": row[0],
        "epoch_uid": row[1],
        "started_at": row[2],
        "established_by": row[3],
        "previous_epoch_uid": row[4],
        "reason": row[5],
    }


def read_epoch_history(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every recorded regime boundary, oldest first. Boundaries remain as
    durable as the records on either side of them."""
    try:
        rows = con.execute(
            f"SELECT epoch_number, epoch_uid, started_at, established_by, "
            f"previous_epoch_uid, reason FROM {OBSERVATION_CONTINUITY_TABLE} "
            "ORDER BY epoch_number ASC"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "epoch_number": r[0], "epoch_uid": r[1], "started_at": r[2],
            "established_by": r[3], "previous_epoch_uid": r[4], "reason": r[5],
        }
        for r in rows
    ]


def classify_observed_at(
    con: sqlite3.Connection, observed_at: str | datetime
) -> dict[str, Any]:
    """Classify one observation instant against the recorded regime timeline.

    Answers, for any persisted first-seen/novelty fact keyed by an
    observation instant, whether it falls inside a recognized continuity
    regime or predates every claim this store makes — without out-of-band
    knowledge and without rewriting anything:

    - UNKNOWN_CONTINUITY: the store records no regime (or is unreadable);
      its history must not be treated as trustworthy continuity.
    - PRE_EPOCH: the instant predates the first recorded regime boundary;
      the records' prior continuity is unrecorded and must not be read as
      current-regime history.
    - IN_EPOCH: the instant falls inside a recognized regime window, named
      by epoch_number/epoch_uid (half-open: a regime runs from its
      started_at until the next regime's started_at).
    """
    if isinstance(observed_at, datetime):
        moment = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    else:
        moment = _parse_iso(str(observed_at))
        if moment is None:
            return {
                "status": UNKNOWN_CONTINUITY,
                "epoch_number": None, "epoch_uid": None,
                "reason": "observation instant is not a parseable timestamp",
            }

    history = read_epoch_history(con)
    if not history:
        return {
            "status": UNKNOWN_CONTINUITY,
            "epoch_number": None, "epoch_uid": None,
            "reason": (
                "no observation-continuity regime is recorded in this store; "
                "its historical facts cannot be treated as continuity-verified"
            ),
        }

    starts: list[tuple[dict[str, Any], datetime]] = []
    for entry in history:
        boundary = _parse_iso(entry["started_at"])
        if boundary is None:
            return {
                "status": UNKNOWN_CONTINUITY,
                "epoch_number": None, "epoch_uid": None,
                "reason": (
                    f"epoch {entry.get('epoch_number')} records an unreadable "
                    "started_at; the regime timeline cannot be adjudicated"
                ),
            }
        starts.append((entry, boundary))

    first_boundary = starts[0][1]
    if moment < first_boundary:
        return {
            "status": PRE_EPOCH,
            "epoch_number": None, "epoch_uid": None,
            "reason": (
                "observation predates the first recorded continuity boundary "
                f"({starts[0][0]['started_at']}); its prior continuity is "
                "unrecorded and it must not silently read as current-regime history"
            ),
        }

    current = starts[-1][0]
    for (entry, boundary), (_, next_boundary) in zip(starts, starts[1:]):
        if boundary <= moment < next_boundary:
            current = entry
            break
    return {
        "status": IN_EPOCH,
        "epoch_number": current["epoch_number"],
        "epoch_uid": current["epoch_uid"],
        "reason": "observation falls inside a recorded continuity regime",
    }
