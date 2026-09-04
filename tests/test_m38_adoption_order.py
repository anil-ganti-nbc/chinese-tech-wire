"""--adopt-current-schema must be reachable, without weakening the gate.

The flag existed but could never run: main() called init_db() first, which
raises SchemaStateError on LEGACY_UNADOPTED -- precisely the state adoption
exists to resolve. The branch also imported adopt_current_schema from
database.schema_state, where it is not defined.

Every test here builds its own scratch database. None of them touch the
operator's real data/ctw.db.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _legacy_unadopted_db(tmp_path: Path) -> Path:
    """Structurally complete, no schema_meta -- i.e. LEGACY_UNADOPTED."""
    import sqlite3

    from database.db import init_db
    from database.schema_state import SCHEMA_META_TABLE

    db = tmp_path / "legacy.db"
    init_db(f"sqlite:///{db.as_posix()}")          # fresh bootstrap
    con = sqlite3.connect(db)                       # then strip the authority
    con.execute(f"DROP TABLE IF EXISTS {SCHEMA_META_TABLE}")
    con.commit()
    con.close()
    return db


def _run_main(db: Path, *args: str):
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=str(REPO), capture_output=True, text=True, env=env,
    )


def test_legacy_unadopted_is_classified_as_such(tmp_path):
    from database.schema_state import SchemaState, inspect_primary_store

    db = _legacy_unadopted_db(tmp_path)
    assert inspect_primary_store(db).state is SchemaState.LEGACY_UNADOPTED


def test_without_the_flag_legacy_unadopted_still_fails_closed(tmp_path):
    """STD-DEPLOY-COM-002 must not be weakened by making the flag reachable."""
    from database.schema_state import SchemaState, inspect_primary_store

    db = _legacy_unadopted_db(tmp_path)
    out = _run_main(db, "--freshness-report")
    assert out.returncode != 0, out.stdout
    assert "LEGACY_UNADOPTED" in (out.stdout + out.stderr)
    # And it was refused untouched -- still unadopted afterwards.
    assert inspect_primary_store(db).state is SchemaState.LEGACY_UNADOPTED


def test_with_the_explicit_flag_adoption_is_reached_and_succeeds(tmp_path):
    """The regression: this used to be unreachable."""
    from database.schema_state import SchemaState, inspect_primary_store

    db = _legacy_unadopted_db(tmp_path)
    out = _run_main(db, "--adopt-current-schema")
    assert out.returncode == 0, out.stdout + out.stderr
    payload = json.loads(out.stdout[out.stdout.index("{"):out.stdout.rindex("}") + 1])
    assert payload["adopted"] is True
    assert payload["state"] == "COMPATIBLE"
    assert inspect_primary_store(db).state is SchemaState.COMPATIBLE


def test_adoption_writes_only_the_schema_authority(tmp_path):
    """It must record authority, not repair or rewrite operator data."""
    import sqlite3

    db = _legacy_unadopted_db(tmp_path)
    con = sqlite3.connect(db)
    before = {
        r[0]: con.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    con.close()

    assert _run_main(db, "--adopt-current-schema").returncode == 0

    con = sqlite3.connect(db)
    after = {
        r[0]: con.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    meta = list(con.execute("SELECT * FROM schema_meta"))
    con.close()

    assert set(after) - set(before) == {"schema_meta"}
    assert not set(before) - set(after)
    for table, count in before.items():
        assert after[table] == count, f"{table} row count changed"
    assert len(meta) == 1


def test_adoption_refuses_a_database_that_is_not_legacy_unadopted(tmp_path):
    """A COMPATIBLE store is not silently re-adopted."""
    from database.db import adopt_current_schema, init_db

    db = tmp_path / "compatible.db"
    init_db(f"sqlite:///{db.as_posix()}")
    result = adopt_current_schema(f"sqlite:///{db.as_posix()}")
    assert result["adopted"] is False
    assert "LEGACY_UNADOPTED" in result["reason"]


def test_normal_compatible_startup_is_unchanged(tmp_path):
    from database.db import init_db
    from database.schema_state import SchemaState, inspect_primary_store

    db = tmp_path / "ok.db"
    init_db(f"sqlite:///{db.as_posix()}")
    assert inspect_primary_store(db).state is SchemaState.COMPATIBLE
    out = _run_main(db, "--freshness-report")
    assert out.returncode == 0, out.stdout + out.stderr
    assert inspect_primary_store(db).state is SchemaState.COMPATIBLE


def test_adoption_flag_imports_from_the_module_that_defines_it():
    """The branch imported from database.schema_state, where it is absent."""
    with pytest.raises(ImportError):
        from database.schema_state import adopt_current_schema  # noqa: F401
    from database.db import adopt_current_schema  # noqa: F401

    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "from database.db import adopt_current_schema" in src
