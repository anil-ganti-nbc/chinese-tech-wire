# Files changed — cloud/chinese-tech-soak

## Added
- `Dockerfile` — Linux AMD64, non-root (uid 10001), one-shot entrypoint, HEALTHCHECK.
  Labeled staging/soak only; no production image is built from this Dockerfile in this
  phase (there is no `docker-compose.yml`/production overlay for this clank).
- `.dockerignore` — did not previously exist in this repo.
- `docker-compose.staging.yml` — the *only* compose file added. Distinct volume
  (`ctw_staging_data`), `restart: "no"`, no ports, no Docker socket, default command
  `--full-once` (never `--gui`). All secret-shaped env vars use `${VAR:-}` interpolation,
  never literals.
- `scripts/entrypoint.sh` — routes CLI args straight to `python main.py`, defaults to
  `--full-once` with no args. No browser launch, no in-process scheduling.
- `scripts/backup.py` — new backup mechanism (none existed before). Stdlib-only,
  sqlite3 online backup API (WAL-safe), backs up `data/ctw.db`.
- `scripts/restore.py` — restores into an isolated target directory only (verified using
  a subdirectory of the data volume, `data/restore-test/`, distinct from the live
  `data/ctw.db` file), runs `PRAGMA integrity_check` after restoring.
- `runtime_bridge.py` — new top-level module (this repo has no `newsroom/`-style package,
  so it sits alongside `main.py`/`config.py`). Builds `identity`/`health`/`version_info`
  payloads.
- `ai/handoff/` (this directory).

## Modified
- `main.py` — added `--identity` and `--health` CLI flags (additive; all existing flags
  and output formats unchanged). Dispatched right after `init_db()`, before every other
  flag branch, mirroring the priority given to `--source-health`/`--diagnose-alerts`.
  Usage docstring at the top of the file updated to list the two new flags.
- `config.py` — added `release_channel: str` field (alias `CTW_RELEASE_CHANNEL`, default
  `"soaking"`) to the `Settings` class. No other fields changed.

## Explicitly left unchanged
- All source/collector/pipeline/scoring/clustering logic (`sources/*`, `community_sources/*`,
  `documentary_sources/*`, `pipeline/*` other than being read by `runtime_bridge.py`).
- Database schema and `database/models.py` — no columns, tables, or migrations added.
- `database/db.py` — untouched; `runtime_bridge.py` calls its existing `init_db()`/
  `get_session()` exactly as `main.py` already does.
- `web/app.py`, `web/launcher.py`, `web/templates/*` — untouched. `CTW_VERSION` in
  `web/app.py` was read, not imported, into `runtime_bridge.py` (see DECISIONS.md) to
  avoid a heavy FastAPI import for a headless health check.
- PyInstaller packaging (`ChineseTechWire.spec`, `launcher_main.py`, `requirements-build.txt`,
  `build/`, `dist/`) — untouched and excluded from the Docker build context.
- `.env`, `.env.example`, `.gitignore` — untouched (already correctly handled per the
  prior baseline audit).
