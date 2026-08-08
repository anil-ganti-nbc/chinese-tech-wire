# Test results — Chinese Tech Wire cloud migration

## Native (Windows, .venv), before any change
`pytest -q` → **350 passed**, 3 warnings (1 httpx/starlette TestClient deprecation, 2
pre-existing date-parsing deprecation warnings in `sources/base.py`), in 204.61s.

## Native (Windows, .venv), after config.py/main.py/runtime_bridge.py changes
`pytest -q` → **350 passed**, same 3 warnings, 205.61s. No regressions from the additive
`--identity`/`--health` flags or the new `release_channel` settings field.

## In-container (Linux AMD64, Docker Desktop)
Not run via pytest inside the image (no dev/test dependencies pruned from the image —
`requirements.txt` includes `pytest`/`pytest-asyncio` already, so `pytest` *could* run
in-container, but doing so was out of scope here; verification instead used direct CLI
invocation against real and isolated state, per the brief):

- `id` (via `--entrypoint id`) → `uid=10001(ctw) gid=10001(ctw) groups=10001(ctw)`
- `python main.py --identity` → valid JSON, `release_channel: "soaking"`
- `python main.py --health` (empty volume) → `operational_state: "unknown"`, exit 0,
  honest `status_reasons: ["no ingestion_runs recorded yet"]`
- `python main.py --full-once` (isolated named volume `ctw_soak_test_data`) → exit 0,
  real sources fetched, 298 new articles + 40 community threads persisted, `status=SUCCESS`,
  100 alert candidates evaluated / 2 eligible / 0 sent (no webhook configured)
- `python main.py --health` (same volume, post-run) → `operational_state: "healthy"`,
  `total_runs: 1`, truthful timestamps
- `python main.py --show-recent` (fresh container, same volume) → identical data after
  recreation
- `python scripts/backup.py` → DB snapshot produced inside the volume
  (`/app/data/backups/ctw-<stamp>.db`)
- `python scripts/restore.py --target-dir /app/data/restore-test` → isolated restore,
  `PRAGMA integrity_check` = ok (21 tables)
- `python main.py --health` against the restored copy (`DATABASE_URL` override) → same
  `healthy` state and `total_runs: 1` as the live copy; live copy re-checked afterward,
  unchanged
- `docker compose -f docker-compose.staging.yml config` → validates, no ports, no socket,
  `restart: "no"`

## Not run
- Load/soak testing over real elapsed time — this phase is local, point-in-time
  verification only; a real soak (running the container repeatedly over days against a
  real host) needs a provisioned host, out of scope here.
- SIGTERM-during-long-run test — the observed `--full-once` run completed in ~74s against
  real sources in this environment; deferred as low-value for a job this short.
- In-container `pytest` run — dev/test dependencies are present in the image (not stripped),
  but running the suite inside the container was not exercised in this phase; native
  Windows `pytest` results above are the authoritative pass/fail record for this change.
