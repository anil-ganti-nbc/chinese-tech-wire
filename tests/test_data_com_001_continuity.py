"""STD-DATA-COM-001 observation-continuity tests for CTW.

The frozen standard (standards-clank 7c821ea, data-ontology/STD-DATA-COM-001)
requires, for a Clank that derives novelty/alerting/editorial state from
comparison against its own prior local history:

1. an explicit, queryable representation of continuity boundaries;
2. baseline/bootstrap observations distinguishable, at read time, from
   ordinary post-continuity observations, without out-of-band knowledge;
3. after a data-loss/restore/re-baseline discontinuity, the discontinuity
   itself recorded as durably as the records on either side of it;
4. a downstream novelty/alerting consumer can determine, for any record,
   whether it falls inside a recognized continuity gap or baseline window.

Every test uses disposable SQLite files or :memory:. The one test that
touches the operator's real data/ctw.db operates on an explicit COPY and
asserts row-count preservation; the real database is never written here.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from database.db import (
    adopt_current_schema,
    get_session,
    init_db,
    inspect_primary,
    migrate_current_schema,
    record_observation_discontinuity,
)
from database.observation_continuity import (
    ESTABLISHED_BY_ADOPTION,
    ESTABLISHED_BY_FRESH,
    ESTABLISHED_BY_MIGRATION,
    ESTABLISHED_BY_OPERATOR,
    IN_EPOCH,
    OBSERVATION_CONTINUITY_TABLE,
    PRE_EPOCH,
    UNKNOWN_CONTINUITY,
    classify_observed_at,
    read_active_epoch,
    read_epoch_history,
)
from database.schema_state import (
    SchemaState,
    SchemaStateError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _u(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def _con(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _continuityless_v1_db(tmp_path: Path, name: str = "v1.db") -> Path:
    """The operator's real pre-remediation shape: a v1-marked store with no
    continuity authority — exactly what data/ctw.db looked like before
    this change."""
    db = tmp_path / name
    init_db(_u(db))
    con = _con(db)
    con.execute(f"DROP TABLE {OBSERVATION_CONTINUITY_TABLE}")
    con.execute("UPDATE schema_meta SET version = 1, source = 'legacy-adoption'")
    con.commit()
    con.close()
    return db


def _pre_authority_db(tmp_path: Path, name: str = "legacy.db") -> Path:
    """Structurally complete application schema, no authorities — the
    LEGACY_UNADOPTED shape a real pre-upgrade database has."""
    db = tmp_path / name
    init_db(_u(db))
    con = _con(db)
    con.execute("DROP TABLE schema_meta")
    con.execute(f"DROP TABLE {OBSERVATION_CONTINUITY_TABLE}")
    con.commit()
    con.close()
    return db


# -- A: fresh state establishes explicit continuity semantics -----------------


def test_fresh_state_establishes_explicit_initial_regime(tmp_path):
    db = tmp_path / "fresh.db"
    report = init_db(_u(db))
    assert report.state is SchemaState.COMPATIBLE

    con = _con(db)
    try:
        epoch = read_active_epoch(con)
        assert epoch is not None
        assert epoch["epoch_number"] == 1
        assert epoch["established_by"] == ESTABLISHED_BY_FRESH
        started = datetime.fromisoformat(epoch["started_at"])
        assert started.tzinfo is not None
        # a fresh store claims no pre-existing history
        assert "no prior history" in epoch["reason"]
        # an observation made now belongs to it, readable without
        # out-of-band knowledge
        verdict = classify_observed_at(con, datetime.now(timezone.utc))
        assert verdict["status"] == IN_EPOCH
        assert verdict["epoch_number"] == 1
        assert verdict["epoch_uid"] == epoch["epoch_uid"]
    finally:
        con.close()


# -- B: existing adopted state has an explicit, non-fabricated boundary -------


def test_adopted_state_boundary_is_explicit_and_pre_history_honest(tmp_path):
    db = _pre_authority_db(tmp_path, "adopted.db")
    result = adopt_current_schema(_u(db))
    assert result["adopted"] is True

    con = _con(db)
    try:
        epoch = read_active_epoch(con)
        assert epoch["epoch_number"] == 1
        assert epoch["established_by"] == ESTABLISHED_BY_ADOPTION
        started = datetime.fromisoformat(epoch["started_at"])
        # pre-adoption instants are NOT claimed as regime history
        verdict = classify_observed_at(con, started - timedelta(days=1))
        assert verdict["status"] == PRE_EPOCH
        assert "unrecorded" in verdict["reason"]
        # post-adoption instants are in-regime
        verdict = classify_observed_at(con, datetime.now(timezone.utc))
        assert verdict["status"] == IN_EPOCH
        assert verdict["epoch_number"] == 1
    finally:
        con.close()


# -- C: the persisted fact is queryable by code --------------------------------


def test_continuity_fact_is_queryable_from_the_health_surface(tmp_path, monkeypatch):
    import runtime_bridge

    db = tmp_path / "health.db"
    init_db(_u(db))
    monkeypatch.setattr(runtime_bridge, "_db_path", lambda: db)
    payload = runtime_bridge.get_health()
    fact = payload["observation_continuity"]
    assert fact["epoch_number"] == 1
    assert fact["established_by"] == ESTABLISHED_BY_FRESH
    assert fact["epoch_uid"]
    assert not any(
        "observation continuity not recorded" in r for r in payload["status_reasons"]
    )

    con = _con(db)
    try:
        history = read_epoch_history(con)
        assert [e["epoch_number"] for e in history] == [1]
        assert set(history[0]) == {
            "epoch_number", "epoch_uid", "started_at", "established_by",
            "previous_epoch_uid", "reason",
        }
    finally:
        con.close()


# -- D: normal subsequent work preserves the same continuity identity ----------


def test_normal_work_preserves_the_regime_identity(tmp_path):
    db = tmp_path / "steady.db"
    init_db(_u(db))
    con = _con(db)
    try:
        before = read_active_epoch(con)
    finally:
        con.close()

    assert init_db(_u(db)).state is SchemaState.COMPATIBLE
    with get_session(_u(db)) as session:
        from sqlalchemy import text

        assert session.execute(text("SELECT 1")).scalar() == 1

    con = _con(db)
    try:
        after = read_active_epoch(con)
        assert after == before
        assert len(read_epoch_history(con)) == 1
    finally:
        con.close()


# -- E: an intentional re-baseline changes identity; the timeline is queryable --


def test_operator_discontinuity_begins_a_new_regime(tmp_path):
    db = tmp_path / "rebaseline.db"
    init_db(_u(db))
    con = _con(db)
    try:
        first = read_active_epoch(con)
        first_started = datetime.fromisoformat(first["started_at"])
    finally:
        con.close()

    result = record_observation_discontinuity(
        "operator restored data/ctw.db from the 2026-09-01 backup", _u(db),
    )
    assert result["recorded"] is True
    assert result["epoch_number"] == 2
    assert result["previous_epoch_uid"] == first["epoch_uid"]
    assert result["established_by"] == ESTABLISHED_BY_OPERATOR
    assert result["reason"] == "operator restored data/ctw.db from the 2026-09-01 backup"

    con = _con(db)
    try:
        assert read_active_epoch(con)["epoch_number"] == 2
        history = read_epoch_history(con)
        assert [e["epoch_number"] for e in history] == [1, 2]

        # acceptance 4: any consumer can place an instant in the timeline —
        # inside regime 1's window, inside regime 2, or before every claim
        second_started = datetime.fromisoformat(history[1]["started_at"])
        between = first_started + (second_started - first_started) / 2
        assert classify_observed_at(con, between)["epoch_number"] == 1
        assert classify_observed_at(con, datetime.now(timezone.utc))["epoch_number"] == 2
        assert classify_observed_at(
            con, first_started - timedelta(days=30)
        )["status"] == PRE_EPOCH
    finally:
        con.close()

    # the first regime's row was never rewritten
    con = _con(db)
    try:
        rows = con.execute(
            f"SELECT epoch_number, established_by FROM {OBSERVATION_CONTINUITY_TABLE} "
            "ORDER BY epoch_number"
        ).fetchall()
        assert rows == [(1, ESTABLISHED_BY_FRESH), (2, ESTABLISHED_BY_OPERATOR)]
    finally:
        con.close()


def test_discontinuity_never_happens_implicitly(tmp_path):
    """Startup, repeated opens, and ordinary sessions must never rotate the
    regime; only the explicit operator action can."""
    db = tmp_path / "implicit.db"
    init_db(_u(db))
    con = _con(db)
    try:
        before = read_active_epoch(con)
    finally:
        con.close()

    for _ in range(3):
        init_db(_u(db))
    with get_session(_u(db)) as session:
        from sqlalchemy import text

        session.execute(text("SELECT 1"))

    con = _con(db)
    try:
        assert read_active_epoch(con) == before
        assert len(read_epoch_history(con)) == 1
    finally:
        con.close()


def test_discontinuity_refuses_a_store_not_in_a_state_to_record(tmp_path):
    db = tmp_path / "guarded.db"
    init_db(_u(db))
    assert record_observation_discontinuity("   ", _u(db))["recorded"] is False

    legacy = _pre_authority_db(tmp_path, "legacy.db")
    result = record_observation_discontinuity("restore", _u(legacy))
    assert result["recorded"] is False
    assert result["report"]["compatibility_state"] == "LEGACY_UNADOPTED"
    con = _con(legacy)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert OBSERVATION_CONTINUITY_TABLE not in tables
    finally:
        con.close()


def test_discontinuity_refuses_to_split_a_run_in_flight(tmp_path):
    db = tmp_path / "running.db"
    init_db(_u(db))
    with get_session(_u(db)) as session:
        from database.models import IngestionRun

        session.add(IngestionRun(
            started_at=datetime.now(timezone.utc),
            status="RUNNING",
            summary="simulated in-flight cycle",
        ))

    result = record_observation_discontinuity("mid-run restore", _u(db))
    assert result["recorded"] is False
    assert "RUNNING" in result["reason"]
    con = _con(db)
    try:
        assert con.execute(
            f"SELECT COUNT(*) FROM {OBSERVATION_CONTINUITY_TABLE}"
        ).fetchone()[0] == 1
    finally:
        con.close()


# -- F: a compatibility change alone does not rotate or imply continuity -------


def test_marker_tampering_is_refused_and_touches_no_regime(tmp_path):
    db = tmp_path / "tampered.db"
    init_db(_u(db))
    con = _con(db)
    con.execute("UPDATE schema_meta SET version = 3")
    con.commit()
    con.close()
    before = _sha(db)
    with pytest.raises(SchemaStateError) as excinfo:
        init_db(_u(db))
    assert excinfo.value.report.state is SchemaState.INCOMPATIBLE_NEWER
    assert _sha(db) == before
    con = _con(db)
    try:
        assert len(read_epoch_history(con)) == 1
    finally:
        con.close()


def test_marked_migration_records_the_boundary_explicitly(tmp_path):
    """v1 -> v2 is exactly the mechanism that touches both facts, and it
    does so explicitly: the migration REPORTS the boundary it wrote, and a
    version bump alone (normal startup) creates nothing."""
    db = _continuityless_v1_db(tmp_path, "migrating.db")
    before = _sha(db)
    with pytest.raises(SchemaStateError) as excinfo:
        init_db(_u(db))
    assert excinfo.value.report.state is SchemaState.MIGRATION_REQUIRED
    assert _sha(db) == before  # the bump alone created nothing

    result = migrate_current_schema(_u(db))
    assert result["migrated"] is True
    assert result["observed_schema_version"] == 1
    assert result["state"] == "COMPATIBLE"
    assert result["observation_continuity"]["established_by"] == ESTABLISHED_BY_MIGRATION
    assert init_db(_u(db)).state is SchemaState.COMPATIBLE


def test_migration_refuses_current_and_damaged_state(tmp_path):
    db = tmp_path / "current.db"
    init_db(_u(db))
    result = migrate_current_schema(_u(db))
    assert result["migrated"] is False
    assert "MIGRATION_REQUIRED" in result["reason"]

    damaged = tmp_path / "damaged.db"
    init_db(_u(damaged))
    con = _con(damaged)
    con.execute("DROP TABLE translation_cache")
    con.execute(f"DROP TABLE {OBSERVATION_CONTINUITY_TABLE}")
    con.execute("UPDATE schema_meta SET version = 1, source = 'legacy-adoption'")
    con.commit()
    con.close()
    before = _sha(damaged)
    result = migrate_current_schema(_u(damaged))
    assert result["migrated"] is False
    assert "not structurally complete" in result["reason"]
    assert _sha(damaged) == before


# -- G/H: policy activation and run ids are not continuity ---------------------


@pytest.fixture()
def alert_cfg_enabled(monkeypatch):
    import config as cfgmod

    nr = dict(cfgmod.yaml_config.get("newsroom") or {})
    nr["alerts"] = {
        "enabled": True,
        "min_priority": 50,
        "high_priority": 55,
        "min_evidence": 0,
        "min_relevance": 0,
        "min_confidence": 0,
        "allowed_statuses": ["WATCHING", "ACTIONABLE", "ESCALATED"],
        "max_age_hours": 48,
        "material_score_delta": 5,
        "max_alerts_per_cycle": 5,
        "cooldown_hours": 12,
        "blocked_lead_types": [],
        "allowed_lead_types": [],
        "suppress_before_activation": True,
        "policy_activated_at": None,
    }
    monkeypatch.setitem(cfgmod.yaml_config, "newsroom", nr)
    return nr["alerts"]


def test_policy_activation_change_does_not_masquerade_as_continuity(
    tmp_path, alert_cfg_enabled, monkeypatch,
):
    from pipeline.alerts import evaluate_alert_eligibility

    db = tmp_path / "policy.db"
    init_db(_u(db))
    con = _con(db)
    try:
        before = read_active_epoch(con)
        before_rows = con.execute(
            f"SELECT COUNT(*) FROM {OBSERVATION_CONTINUITY_TABLE}"
        ).fetchone()[0]
    finally:
        con.close()

    now = datetime.now(timezone.utc)
    lead = _lead(last_activity_at=now - timedelta(hours=20))
    # the same lead against two different policy-activation instants: the
    # DECISION is a policy fact and differs; the continuity registry must
    # not move at all
    d_old = evaluate_alert_eligibility(
        lead, prev_status=None, policy_activated_at=(now - timedelta(hours=40)).isoformat(),
    )
    d_new = evaluate_alert_eligibility(
        lead, prev_status=None, policy_activated_at=(now - timedelta(hours=10)).isoformat(),
    )
    assert d_old.eligible is True
    assert d_new.eligible is False and d_new.reason_code == "SUPPRESSED_BACKLOG"

    con = _con(db)
    try:
        after = read_active_epoch(con)
        assert after == before
        assert con.execute(
            f"SELECT COUNT(*) FROM {OBSERVATION_CONTINUITY_TABLE}"
        ).fetchone()[0] == before_rows
    finally:
        con.close()


def _lead(**kwargs):
    from database.models import StoryLead

    now = datetime.now(timezone.utc)
    defaults = dict(
        lead_status="WATCHING",
        lead_type="EARLY_SIGNAL",
        headline_hint="RTX 6090 PCB photo on Chiphell",
        created_at=now,
        updated_at=now,
        last_activity_at=now - timedelta(hours=2),
        priority_score=54.0,
        evidence_score=40.0,
        relevance_score=70.0,
        confidence_score=45.0,
        exclusivity_score=60.0,
        media_saturation_score=10.0,
        novelty_score=70.0,
        momentum_score=40.0,
        source_diversity_score=30.0,
        editorial_value_score=55.0,
        notified=False,
        why_now="Community photo evidence",
        why_it_matters="Possible unreleased GPU",
        evidence_summary="Chiphell PHOTO_EVIDENCE",
        uncertainty_summary="Unconfirmed",
        first_signal_source="chiphell",
        first_signal_at=now - timedelta(hours=3),
    )
    defaults.update(kwargs)
    return StoryLead(**defaults)


def test_new_run_id_does_not_masquerade_as_continuity(tmp_path):
    db = tmp_path / "runs.db"
    init_db(_u(db))
    con = _con(db)
    try:
        before = read_active_epoch(con)
    finally:
        con.close()

    with get_session(_u(db)) as session:
        from database.models import IngestionRun

        session.add(IngestionRun(
            started_at=datetime.now(timezone.utc),
            status="SUCCESS",
            summary="an ordinary run",
        ))
        session.add(IngestionRun(
            started_at=datetime.now(timezone.utc),
            status="SUCCESS",
            summary="another ordinary run with a different id",
        ))

    con = _con(db)
    try:
        after = read_active_epoch(con)
        assert after == before
        assert len(read_epoch_history(con)) == 1
    finally:
        con.close()


# -- I: unknown/legacy continuity cannot silently claim trustworthiness --------


def test_unknown_continuity_is_reported_never_claimed(tmp_path):
    db = tmp_path / "noregime.db"
    init_db(_u(db))
    con = _con(db)
    con.execute(f"DELETE FROM {OBSERVATION_CONTINUITY_TABLE}")
    con.commit()
    con.close()

    con = _con(db)
    try:
        verdict = classify_observed_at(con, datetime.now(timezone.utc))
        assert verdict["status"] == UNKNOWN_CONTINUITY
        assert "cannot be treated as continuity-verified" in verdict["reason"]
        assert read_active_epoch(con) is None
    finally:
        con.close()

    # a store that predates the authority entirely answers the same way
    con = _con(db)
    con.execute(f"DROP TABLE {OBSERVATION_CONTINUITY_TABLE}")
    con.commit()
    con.close()
    con = _con(db)
    try:
        assert read_active_epoch(con) is None
        assert classify_observed_at(
            con, datetime.now(timezone.utc)
        )["status"] == UNKNOWN_CONTINUITY
        # an unparseable instant degrades honestly rather than guessing
        verdict = classify_observed_at(con, "not-a-timestamp")
        assert verdict["status"] == UNKNOWN_CONTINUITY
    finally:
        con.close()

    # the compatibility verdict is PARTIAL, not laundered into compatible
    assert inspect_primary(_u(db)).state is SchemaState.PARTIAL


def test_health_flags_a_regime_less_compatible_store(tmp_path, monkeypatch):
    import runtime_bridge

    db = tmp_path / "noregime.db"
    init_db(_u(db))
    con = _con(db)
    con.execute(f"DELETE FROM {OBSERVATION_CONTINUITY_TABLE}")
    con.commit()
    con.close()
    monkeypatch.setattr(runtime_bridge, "_db_path", lambda: db)
    payload = runtime_bridge.get_health()
    assert payload["observation_continuity"] == {"status": UNKNOWN_CONTINUITY}
    assert any(
        "observation continuity not recorded" in r for r in payload["status_reasons"]
    )


# -- J: within one regime, existing novelty/backlog behavior is unchanged ------


def test_backlog_gate_still_governs_within_one_regime(
    tmp_path, alert_cfg_enabled, monkeypatch,
):
    from pipeline.alerts import evaluate_alert_eligibility

    db = tmp_path / "alerts.db"
    init_db(_u(db))
    con = _con(db)
    try:
        epoch = read_active_epoch(con)
    finally:
        con.close()

    now = datetime.now(timezone.utc)
    monkeypatch.setitem(
        alert_cfg_enabled, "policy_activated_at",
        (now - timedelta(hours=2)).isoformat(),
    )
    stale_lead = _lead(
        priority_score=56.8,
        last_activity_at=now - timedelta(hours=40),
    )
    decision = evaluate_alert_eligibility(stale_lead, prev_status=None)
    assert decision.eligible is False
    assert decision.reason_code == "SUPPRESSED_BACKLOG"

    con = _con(db)
    try:
        verdict = classify_observed_at(con, now)
        assert verdict["status"] == IN_EPOCH
        assert verdict["epoch_number"] == epoch["epoch_number"]
    finally:
        con.close()


# -- data preservation over a copy of the real operator database ----------------


def _table_row_counts(path: Path) -> dict[str, int]:
    con = _con(path)
    try:
        return {
            r[0]: con.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        con.close()


def test_real_database_migration_preserves_every_row(tmp_path):
    """Scratch COPY of the operator's real database: the marked migration
    must destroy nothing and fabricate nothing."""
    real = REPO_ROOT / "data" / "ctw.db"
    if not real.exists():
        pytest.skip("no real local database present")
    copy = tmp_path / "ctw-copy.db"
    shutil.copy(real, copy)

    before = _table_row_counts(copy)
    assert inspect_primary(_u(copy)).state is SchemaState.MIGRATION_REQUIRED

    result = migrate_current_schema(_u(copy))
    assert result["migrated"] is True
    assert result["observation_continuity"]["established_by"] == ESTABLISHED_BY_MIGRATION

    after = _table_row_counts(copy)
    assert not set(before) - set(after)
    # every application table keeps every row exactly; the two authority
    # tables legitimately change: observation_continuity appears, and
    # schema_meta GROWS by one marker row — the old marker row is preserved,
    # never rewritten
    application_before = {t: c for t, c in before.items() if t != "schema_meta"}
    for table, count in application_before.items():
        assert after[table] == count, f"{table} row count changed"
    assert set(after) - set(before) == {OBSERVATION_CONTINUITY_TABLE}
    assert after["schema_meta"] == before["schema_meta"] + 1

    con = _con(copy)
    try:
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        marker = con.execute(
            "SELECT version, source FROM schema_meta ORDER BY adopted_at"
        ).fetchall()
        assert marker == [(1, "legacy-adoption"), (2, "marked-migration")]
        epoch = read_active_epoch(con)
        assert epoch["established_by"] == ESTABLISHED_BY_MIGRATION
        # pre-migration rows classify PRE_EPOCH — honest, not fabricated
        oldest = con.execute(
            "SELECT MIN(started_at) FROM ingestion_runs"
        ).fetchone()[0]
        if oldest is not None:
            assert classify_observed_at(con, oldest)["status"] == PRE_EPOCH
        verdict = classify_observed_at(con, datetime.now(timezone.utc))
        assert verdict["status"] == IN_EPOCH
    finally:
        con.close()


# -- CLI: the explicit actions are reachable and refuse what they must ----------


def _run_main(db: Path, *args: str):
    env = dict(os.environ)
    env["DATABASE_URL"] = _u(db)
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
    )


def test_migrate_schema_flag_end_to_end(tmp_path):
    db = _continuityless_v1_db(tmp_path, "cli.db")
    out = _run_main(db, "--migrate-schema")
    assert out.returncode == 0, out.stdout + out.stderr
    assert inspect_primary(_u(db)).state is SchemaState.COMPATIBLE
    con = _con(db)
    try:
        assert read_active_epoch(con)["established_by"] == ESTABLISHED_BY_MIGRATION
    finally:
        con.close()

    out2 = _run_main(db, "--migrate-schema")
    assert out2.returncode == 1
    assert "MIGRATION_REQUIRED" in out2.stdout


def test_new_observation_epoch_flag_end_to_end(tmp_path):
    db = tmp_path / "cli2.db"
    init_db(_u(db))
    out = _run_main(db, "--new-observation-epoch", "operator rebuilt the store")
    assert out.returncode == 0, out.stdout + out.stderr
    con = _con(db)
    try:
        history = read_epoch_history(con)
        assert [e["epoch_number"] for e in history] == [1, 2]
        assert history[1]["reason"] == "operator rebuilt the store"
    finally:
        con.close()

    out2 = _run_main(db, "--new-observation-epoch", "   ")
    assert out2.returncode == 1
    assert "stated reason" in out2.stdout


def test_new_observation_epoch_on_refused_store_writes_nothing(tmp_path):
    db = _pre_authority_db(tmp_path, "cli3.db")
    before = _sha(db)
    out = _run_main(db, "--new-observation-epoch", "attempted break")
    assert out.returncode == 3
    assert _sha(db) == before
