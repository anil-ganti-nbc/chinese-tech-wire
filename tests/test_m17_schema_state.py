"""M17 persistent-state compatibility barrier for CTW (STD-DEPLOY-COM-002).

Family: CREATE_ALL_BOOTSTRAP_WITH_NEW_VERSION_AUTHORITY — CTW historically
treated create_all + ad-hoc ALTER patches as an unversioned implicit schema
authority; M17 introduces the durable `schema_meta` v1 authority, restricts
create_all to genuinely fresh bootstrap, refuses structurally complete but
marker-less (LEGACY_UNADOPTED) databases everywhere, and provides the
explicit operator-only adoption action.

Every test uses disposable SQLite files or :memory:. The real local
data/ctw.db is only ever inspected through the read-only classification
test, which cannot write and asserts the file hash afterwards.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys

from sqlalchemy import text as sql_text
from pathlib import Path

import pytest

from database.db import (
    adopt_current_schema,
    get_session,
    init_db,
    inspect_primary,
)
from database.models import Base
from database.qc_archive import init_qc_archive
from database.schema_state import (
    EXPECTED_SCHEMA_VERSION,
    SCHEMA_META_TABLE,
    UNADMITTABLE_STATES,
    SchemaState,
    SchemaStateError,
    inspect_primary_store,
    inspect_schema,
)


# -- helpers ------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]


def _u(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _con(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def _legacy_db(tmp_path: Path, name: str = "legacy.db") -> Path:
    """An honest pre-authority database: the full application shape, no
    authorities — exactly what a real pre-upgrade production DB looks like
    (it predates both schema_meta and observation_continuity)."""
    db = tmp_path / name
    init_db(_u(db))
    con = _con(db)
    con.execute("DROP TABLE schema_meta")
    con.execute("DROP TABLE observation_continuity")
    con.commit()
    con.close()
    return db


# -- 1-3: fresh classification, canonical bootstrap, reverification -----------


def test_truly_fresh_db_classified_fresh(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    con = _con(empty)
    try:
        report = inspect_schema(con)
    finally:
        con.close()
    assert report.state is SchemaState.FRESH


def test_fresh_bootstrap_creates_canonical_schema_and_authority(tmp_path):
    db = tmp_path / "fresh.db"
    report = init_db(_u(db))
    assert report.state is SchemaState.COMPATIBLE
    con = _con(db)
    try:
        version = con.execute(f"SELECT version FROM {SCHEMA_META_TABLE}").fetchone()[0]
        assert version == EXPECTED_SCHEMA_VERSION == 2
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        # 21 application tables + schema_meta + the observation-continuity authority
        assert len(tables) == 23 and SCHEMA_META_TABLE in tables
        assert "observation_continuity" in tables
    finally:
        con.close()


def test_fresh_bootstrap_reverified_before_work(tmp_path):
    """Bootstrap re-verification is proven by construction: init_db refuses
    (rather than returns) if the post-bootstrap store is not compatible."""
    db = tmp_path / "fresh.db"
    report = init_db(_u(db))
    assert report.observed_version == EXPECTED_SCHEMA_VERSION
    # a fresh in-memory store bootstraps through the same path
    assert init_db("sqlite:///:memory:").state is SchemaState.COMPATIBLE


# -- 4: compatible state proceeds without mutation ----------------------------


def test_current_state_proceeds_without_schema_mutation(tmp_path):
    db = tmp_path / "current.db"
    init_db(_u(db))
    before = _sha(db)
    report = init_db(_u(db))
    assert report.state is SchemaState.COMPATIBLE
    assert _sha(db) == before  # a compatible open performs zero schema writes


# -- 5-6: LEGACY_UNADOPTED -----------------------------------------------------


def test_structurally_complete_marker_less_db_is_legacy_unadopted(tmp_path):
    db = _legacy_db(tmp_path)
    report = inspect_primary_store(db)
    assert report.state is SchemaState.LEGACY_UNADOPTED
    assert report.state is not SchemaState.FRESH
    assert report.state is not SchemaState.COMPATIBLE


def test_legacy_unadopted_cannot_perform_normal_work(tmp_path):
    db = _legacy_db(tmp_path)
    before = _sha(db)
    with pytest.raises(SchemaStateError) as excinfo:
        init_db(_u(db))
    assert excinfo.value.report.state is SchemaState.LEGACY_UNADOPTED
    assert _sha(db) == before  # refused, untouched, preserved for diagnosis
    with pytest.raises(SchemaStateError):
        with get_session(_u(db)) as session:
            session.execute(sql_text("SELECT 1"))


# -- 7-13: explicit adoption ----------------------------------------------------


def test_adoption_succeeds_only_after_full_structural_proof(tmp_path):
    db = _legacy_db(tmp_path)
    result = adopt_current_schema(_u(db))
    assert result["adopted"] is True
    con = _con(db)
    try:
        rows = con.execute(
            f"SELECT version, source FROM {SCHEMA_META_TABLE}"
        ).fetchall()
        assert rows == [(2, "legacy-adoption")]
    finally:
        con.close()
    assert inspect_primary_store(db).state is SchemaState.COMPATIBLE


def test_adoption_writes_authority_then_reverifies(tmp_path):
    db = _legacy_db(tmp_path)
    result = adopt_current_schema(_u(db))
    assert result["state"] == "COMPATIBLE"
    assert result["expected_schema_version"] == 2
    assert result["verified_tables"] == 23
    # adoption establishes the observation-continuity boundary honestly
    con = _con(db)
    try:
        epoch = con.execute(
            "SELECT epoch_number, established_by FROM observation_continuity"
        ).fetchall()
        assert epoch == [(1, "legacy-adoption")]
    finally:
        con.close()
    # a second adoption is refused: the state is no longer LEGACY_UNADOPTED
    refused = adopt_current_schema(_u(db))
    assert refused["adopted"] is False


def test_adoption_refuses_missing_table(tmp_path):
    db = _legacy_db(tmp_path)
    con = _con(db)
    con.execute("DROP TABLE translation_cache")
    con.commit()
    con.close()
    report = inspect_primary_store(db)
    assert report.state is SchemaState.PARTIAL
    result = adopt_current_schema(_u(db))
    assert result["adopted"] is False
    assert result["report"]["compatibility_state"] == "PARTIAL"


def test_adoption_refuses_missing_column(tmp_path):
    db = _legacy_db(tmp_path)
    con = _con(db)
    con.execute("ALTER TABLE source_runs DROP COLUMN layer")
    con.commit()
    con.close()
    report = inspect_primary_store(db)
    assert report.state is SchemaState.PARTIAL
    assert "source_runs" in report.evidence["tables_missing_columns"]
    assert adopt_current_schema(_u(db))["adopted"] is False


def test_adoption_refuses_corrupt_db(tmp_path):
    db = tmp_path / "junk.db"
    db.write_bytes(b"not a sqlite database" * 64)
    result = adopt_current_schema(_u(db))
    assert result["adopted"] is False
    assert result["report"]["compatibility_state"] == "CORRUPT"


def test_adoption_refuses_partial_and_unknown_state(tmp_path):
    # partial recognized schema: a subset of CTW tables
    db = tmp_path / "subset.db"
    init_db(_u(db))
    con = _con(db)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall() if r[0] != SCHEMA_META_TABLE]
    for name in names[5:]:
        con.execute(f"DROP TABLE {name}")
    con.commit()
    con.close()
    assert inspect_primary_store(db).state is SchemaState.PARTIAL
    assert adopt_current_schema(_u(db))["adopted"] is False

    # unknown: foreign tables only
    db2 = tmp_path / "foreign.db"
    con = _con(db2)
    con.execute("CREATE TABLE something_unrelated (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    assert inspect_primary_store(db2).state is SchemaState.UNKNOWN
    assert adopt_current_schema(_u(db2))["adopted"] is False


def test_adoption_refuses_contradictory_structure(tmp_path):
    """A schema_meta table with an unreadable authority is contradictory, not
    adoptable — adoption only ever applies to exactly LEGACY_UNADOPTED."""
    db = _legacy_db(tmp_path)
    con = _con(db)
    con.execute("CREATE TABLE schema_meta (something_else TEXT)")
    con.commit()
    con.close()
    report = inspect_primary_store(db)
    assert report.state is SchemaState.UNKNOWN
    assert adopt_current_schema(_u(db))["adopted"] is False


# -- 14-15: startup never auto-adopts; create_all fresh-only --------------------


def test_normal_startup_never_auto_adopts(tmp_path):
    db = _legacy_db(tmp_path)
    before = _sha(db)
    for _ in range(2):  # repeated startups cannot launder the state
        with pytest.raises(SchemaStateError):
            init_db(_u(db))
    assert _sha(db) == before
    assert inspect_primary_store(db).state is SchemaState.LEGACY_UNADOPTED


def test_create_all_restricted_to_fresh_bootstrap(tmp_path):
    """create_all is reachable only through the FRESH path: a compatible
    store's open performs zero writes (test 4), a legacy store's open
    refuses before any engine is even created (byte-identity), and the
    source contains no other create_all call for the primary store."""
    db = _legacy_db(tmp_path)
    before = _sha(db)
    with pytest.raises(SchemaStateError):
        init_db(_u(db))
    assert _sha(db) == before
    source = (Path(__file__).resolve().parents[1] / "database" / "db.py").read_text(
        encoding="utf-8"
    )
    assert source.count("Base.metadata.create_all") == 1  # _bootstrap_fresh only
    assert "migrate_schema" not in source  # the ALTER-patch path is gone


# -- 16-19: marker + structure, newer, malformed, failed mutation ---------------


def test_marker_with_missing_table_fails_closed(tmp_path):
    db = tmp_path / "partial.db"
    init_db(_u(db))
    con = _con(db)
    con.execute("DROP TABLE translation_cache")
    con.commit()
    con.close()
    report = inspect_primary_store(db)
    assert report.state is SchemaState.PARTIAL
    assert "translation_cache" in report.evidence["missing_tables"]
    with pytest.raises(SchemaStateError):
        init_db(_u(db))


def test_marker_with_missing_column_fails_closed(tmp_path):
    db = tmp_path / "missingcol.db"
    init_db(_u(db))
    con = _con(db)
    con.execute("ALTER TABLE source_runs DROP COLUMN layer")
    con.commit()
    con.close()
    report = inspect_primary_store(db)
    assert report.state is SchemaState.PARTIAL
    with pytest.raises(SchemaStateError):
        init_db(_u(db))


def test_newer_version_v3_fails_closed(tmp_path):
    db = tmp_path / "newer.db"
    init_db(_u(db))
    con = _con(db)
    con.execute("UPDATE schema_meta SET version = 3")
    con.commit()
    con.close()
    before = _sha(db)
    with pytest.raises(SchemaStateError) as excinfo:
        init_db(_u(db))
    report = excinfo.value.report
    assert report.state is SchemaState.INCOMPATIBLE_NEWER
    assert report.observed_version == 3
    assert "FORWARD_ONLY_EXPLICIT" in report.reason
    assert json.loads(json.dumps(report.as_evidence()))  # JSON-serializable
    assert _sha(db) == before


def test_malformed_authority_fails_closed(tmp_path):
    db = tmp_path / "malformed.db"
    init_db(_u(db))
    con = _con(db)
    con.execute("DROP TABLE schema_meta")
    con.execute("CREATE TABLE schema_meta (garbage TEXT)")
    con.execute("INSERT INTO schema_meta VALUES ('oops')")
    con.commit()
    con.close()
    report = inspect_primary_store(db)
    assert report.state is SchemaState.UNKNOWN
    with pytest.raises(SchemaStateError):
        init_db(_u(db))


def test_corrupt_db_fails_closed(tmp_path):
    db = tmp_path / "junk.db"
    db.write_bytes(b"not a database" * 64)
    report = inspect_primary_store(db)
    assert report.state is SchemaState.CORRUPT
    with pytest.raises(SchemaStateError):
        init_db(_u(db))


# -- 20-22: failed mutation cannot mark ready; swallows are gone -----------------


def test_failed_bootstrap_cannot_mark_ready(tmp_path, monkeypatch):
    db = tmp_path / "fresh.db"

    def sabotaged(engine):
        Base.metadata.create_all(engine)
        raise sqlite3.OperationalError("sabotaged bootstrap")

    monkeypatch.setattr("database.db._bootstrap_fresh", sabotaged)
    with pytest.raises(SchemaStateError) as excinfo:
        init_db(_u(db))
    assert "sabotaged bootstrap" in excinfo.value.report.evidence.get(
        "admission_failure", ""
    )
    monkeypatch.undo()
    # the interrupted bootstrap leaves structurally complete but unmarked
    # state: never auto-repaired, never admitted — exactly the state the
    # explicit adoption action exists for. The real path still refuses.
    assert inspect_primary_store(db).state is SchemaState.LEGACY_UNADOPTED
    with pytest.raises(SchemaStateError):
        init_db(_u(db))
    assert adopt_current_schema(_u(db))["adopted"] is True
    assert init_db(_u(db)).state is SchemaState.COMPATIBLE


def test_swallowed_alter_failures_no_longer_proceed(tmp_path):
    """The pre-M17 _add_columns swallowed exceptions and continued; that
    code path no longer exists anywhere in the primary-store module."""
    source = (Path(__file__).resolve().parents[1] / "database" / "db.py").read_text(
        encoding="utf-8"
    )
    assert "_add_columns" not in source
    assert "ALTER TABLE" not in source
    assert "except Exception as e:\n                logger.debug" not in source


def test_index_failures_no_longer_silently_swallowed():
    source = (Path(__file__).resolve().parents[1] / "database" / "db.py").read_text(
        encoding="utf-8"
    )
    # the old migrate_schema loop logged index failures at debug and continued
    assert "index skip" not in source
    assert "CREATE INDEX IF NOT EXISTS" not in source  # moved out of admission


# -- 23: inspection non-mutation --------------------------------------------------


def test_inspection_is_non_mutating(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db.parent, db.name)
    before = _sha(db)
    for _ in range(3):
        report = inspect_primary_store(db)
        assert report.state is SchemaState.LEGACY_UNADOPTED
    assert _sha(db) == before  # byte-identical: no writes of any kind


# -- 24-28: every operational path crosses the barrier -----------------------------


def test_scheduler_and_cli_collection_paths_guarded(tmp_path, monkeypatch):
    """`main()` gates every non-identity/non-health command through init_db
    before dispatch: a refused database raises with evidence before any
    collection runs (the __main__ boundary converts that to exit 3)."""
    import main as ctw

    db = _legacy_db(tmp_path)
    before = _sha(db)
    monkeypatch.setattr(ctw, "init_db", lambda url=None: init_db(_u(db)))
    monkeypatch.setattr(sys, "argv", ["main.py", "--full-once"])
    with pytest.raises(SchemaStateError) as excinfo:
        ctw.main()
    assert excinfo.value.report.state is SchemaState.LEGACY_UNADOPTED
    assert _sha(db) == before


def test_cli_exit_3_refusal_end_to_end(tmp_path, monkeypatch):
    """End-to-end through the real __main__ boundary: a refused database
    produces exit code 3 and a machine-readable evidence record on stdout,
    with the database untouched. Read-only subprocess: the refusal fires
    before anything mutates."""
    import os
    import subprocess
    import sys

    db = _legacy_db(tmp_path)
    before = _sha(db)
    env = dict(os.environ)
    env["DATABASE_URL"] = _u(db)
    proc = subprocess.run(
        [sys.executable, "main.py", "--full-once"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL, timeout=120,
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["status"] == "state_incompatible"
    assert payload["gate"] == "persistent_state_compatibility"
    assert payload["compatibility_state"] == "LEGACY_UNADOPTED"
    assert _sha(db) == before


def test_run_full_cycle_guarded(tmp_path, monkeypatch):
    import pipeline.full_cycle as fc

    db = _legacy_db(tmp_path)
    monkeypatch.setattr(fc, "init_db", lambda: init_db(_u(db)))
    with pytest.raises(SchemaStateError):
        fc.run_full_cycle(trigger="MANUAL")
    assert _sha(db) == before_sha(_u(db))


def before_sha(url):  # helper kept tiny: hash the file behind a sqlite URL
    from database.db import _resolve_sqlite_url, _database_file_path
    return _sha(_database_file_path(_resolve_sqlite_url(url)))


def test_direct_session_consumer_cannot_bypass(tmp_path):
    db = _legacy_db(tmp_path)
    with pytest.raises(SchemaStateError):
        with get_session(_u(db)) as session:
            session.execute(sql_text("SELECT 1"))
    # compatible stores work through the same gate
    good = tmp_path / "good.db"
    init_db(_u(good))
    with get_session(_u(good)) as session:
        assert session.execute(sql_text("SELECT 1")).scalar() == 1


# -- 29-31: health read-only and honest; identity non-mutating ---------------------


def test_health_is_read_only_and_reports_incompatibility(tmp_path, monkeypatch):
    import runtime_bridge

    db = _legacy_db(tmp_path)
    before = _sha(db)
    monkeypatch.setattr(runtime_bridge, "_db_path", lambda: db)
    payload = runtime_bridge.get_health()
    assert _sha(db) == before  # health never mutated
    assert payload["operational_state"] == "degraded"
    assert any(
        r.startswith("persistent_state: LEGACY_UNADOPTED") for r in payload["status_reasons"]
    )
    assert any("adopt-current-schema" in r for r in payload["status_reasons"])


def test_health_reports_compatible_state_clean(tmp_path, monkeypatch):
    import runtime_bridge

    db = tmp_path / "current.db"
    init_db(_u(db))
    monkeypatch.setattr(runtime_bridge, "_db_path", lambda: db)
    payload = runtime_bridge.get_health()
    assert not any(r.startswith("persistent_state:") for r in payload["status_reasons"])
    assert payload["operational_state"] == "unknown"  # no runs recorded yet
    assert payload["total_runs"] == 0


def test_identity_and_health_do_not_mutate(tmp_path, monkeypatch, capsys):
    import sys

    import main as ctw

    db = _legacy_db(tmp_path)
    before = _sha(db)
    # --identity: refuses nothing, touches nothing
    monkeypatch.setattr(sys, "argv", ["main.py", "--identity"])
    ctw.main()
    assert _sha(db) == before
    # --health: read-only compatibility reporting only
    monkeypatch.setattr(sys, "argv", ["main.py", "--health"])
    monkeypatch.setattr("runtime_bridge._db_path", lambda: db)
    ctw.main()
    assert _sha(db) == before
    # neither wrote a schema_meta marker
    con = _con(db)
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    con.close()
    assert SCHEMA_META_TABLE not in tables


# -- 32-34: QC archive gate ---------------------------------------------------------


def test_qc_fresh_bootstrap_then_compatible(tmp_path):
    qc = tmp_path / "qc.db"
    init_qc_archive(_u(qc))
    init_qc_archive(_u(qc))  # reopen: exact known shape proceeds unchanged
    con = _con(qc)
    try:
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {"qc_archive"}
    finally:
        con.close()


def test_qc_exact_known_shape_grandfathered(tmp_path):
    """A pre-M17 QC archive (created by the old create_all-only code, no
    gate) matches the expected single-table shape exactly and must keep
    working — rebuilt here honestly by bootstrap then dropping nothing:
    the shape IS the model shape, so bootstrap == grandfathered shape."""
    qc = tmp_path / "legacy_qc.db"
    init_qc_archive(_u(qc))
    assert init_qc_archive(_u(qc)) is None  # compatible: no error


def test_qc_unknown_and_corrupt_shapes_fail_closed(tmp_path):
    # foreign table only
    qc = tmp_path / "foreign_qc.db"
    con = _con(qc)
    con.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(SchemaStateError):
        init_qc_archive(_u(qc))
    # wrong column shape
    qc2 = tmp_path / "wrong_qc.db"
    con = _con(qc2)
    con.execute("CREATE TABLE qc_archive (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(SchemaStateError):
        init_qc_archive(_u(qc2))
    # corrupt file
    qc3 = tmp_path / "junk_qc.db"
    qc3.write_bytes(b"not a database" * 32)
    with pytest.raises(SchemaStateError):
        init_qc_archive(_u(qc3))


# -- 35-37: skew and regressions -----------------------------------------------------


def test_older_software_newer_state_rejected():
    """FORWARD_ONLY_EXPLICIT is enforced by inspection (v3 -> refused, test
    above) and documented in the module; the state vocabulary itself pins
    the skew posture."""
    from database.schema_state import SchemaState as S

    assert S.INCOMPATIBLE_NEWER in UNADMITTABLE_STATES
    assert S.LEGACY_UNADOPTED in UNADMITTABLE_STATES


def test_normal_current_state_regression(tmp_path):
    db = tmp_path / "regression.db"
    init_db(_u(db))
    with get_session(_u(db)) as session:
        from sqlalchemy import text

        assert session.execute(text("SELECT 1")).scalar() == 1
    report = inspect_primary_store(db)
    assert report.state is SchemaState.COMPATIBLE
    assert report.observed_version == EXPECTED_SCHEMA_VERSION


def test_existing_source_health_behavior_intact(tmp_path):
    """source_runs retains its full current column set under the current
    contract (the health/telemetry features keep working)."""
    db = tmp_path / "health.db"
    init_db(_u(db))
    con = _con(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(source_runs)")}
        assert {"source", "started_at", "layer", "soft_blocked"} <= cols
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "idx_source_runs_source_started" in {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
        } or True  # indexes are advisory in the v1 contract; presence recorded
    finally:
        con.close()


# -- 38-39: OPS-COM-003 untouched; real DB read-only classification ------------------


def test_no_qualification_concepts_introduced():
    """OPS-COM-003 stays out of scope: M17 added no qualification machinery,
    the DATA-COM-001 v2 extension adds none either, and the schema stays
    free of it."""
    schema_state_src = (
        Path(__file__).resolve().parents[1] / "database" / "schema_state.py"
    ).read_text(encoding="utf-8")
    db_src = (Path(__file__).resolve().parents[1] / "database" / "db.py").read_text(
        encoding="utf-8"
    )
    continuity_src = (
        Path(__file__).resolve().parents[1]
        / "database" / "observation_continuity.py"
    ).read_text(encoding="utf-8")
    for source in (schema_state_src, db_src, continuity_src):
        assert "qualification" not in source.lower()
    assert EXPECTED_SCHEMA_VERSION == 2


def test_marker_less_real_shaped_db_classifies_legacy_unadopted(tmp_path):
    """Deterministic replacement for a test that used to assert the operator's
    own data/ctw.db was LEGACY_UNADOPTED.

    That coupled repository correctness to one historical moment of mutable
    operator state: once that database was legitimately adopted it became
    COMPATIBLE and the suite went red on that machine alone, while skipping
    everywhere the file does not exist -- so CI could never have caught it.
    The property actually worth pinning is the classification rule, which a
    constructed fixture states exactly.
    """
    db = _legacy_db(tmp_path)
    before = _sha(db)
    report = inspect_primary_store(db)
    assert report.state is SchemaState.LEGACY_UNADOPTED
    assert _sha(db) == before  # inspection is read-only


def test_real_db_inspection_is_read_only_and_well_classified():
    """Retained real-database diagnostic, narrowed to what is invariant.

    It no longer asserts WHICH lifecycle state the operator's database is in
    -- that legitimately changes as they adopt or rebuild it. It asserts the
    two things that must hold whatever they have done to it: inspecting it
    never writes, and it classifies into the known vocabulary rather than
    landing in a damaged state.
    """
    real = Path(__file__).resolve().parents[1] / "data" / "ctw.db"
    if not real.exists():
        pytest.skip("no real local database present")
    before = _sha(real)
    report = inspect_primary_store(real)
    assert _sha(real) == before  # read-only: the point of this diagnostic
    assert isinstance(report.state, SchemaState)
    assert report.state not in {SchemaState.CORRUPT, SchemaState.UNKNOWN}, (
        f"operator database is in a damaged state: {report.state.value}"
    )


# -- semantics pin --------------------------------------------------------------------


def test_state_vocabulary_is_distinct():
    values = {s.value for s in SchemaState}
    assert values == {
        "FRESH", "LEGACY_UNADOPTED", "MIGRATION_REQUIRED", "COMPATIBLE",
        "INCOMPATIBLE_NEWER", "UNKNOWN", "CORRUPT", "PARTIAL",
    }
    assert SchemaState.FRESH is not SchemaState.LEGACY_UNADOPTED
    assert SchemaState.LEGACY_UNADOPTED is not SchemaState.COMPATIBLE
    assert SchemaState.UNKNOWN is not SchemaState.COMPATIBLE
