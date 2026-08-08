# Decisions — Chinese Tech Wire cloud migration

1. **This is a Tier B (staging/soak only) clank — no production compose file was built,
   and none should be inferred from anything in this phase.** Only
   `docker-compose.staging.yml` exists. There is no `docker-compose.yml` for this project.
   Nothing here is labeled, tagged, or defaulted to "production" anywhere.

2. **`release_channel` defaults to `"soaking"`, never anything more trusted.** Added as
   `Settings.release_channel` in `config.py` (alias `CTW_RELEASE_CHANNEL`), read by
   `runtime_bridge.get_identity()`/`get_health()`. Mirrors the sibling Free Game Tracker
   clank's rule (`release_channel` must never be hard-coded to a value the deployment
   hasn't earned) but uses the least-trusted channel name appropriate to this clank's
   tier — "soaking", not "experimental" — since Tier B has no path to anything more
   trusted in this phase.

3. **`--identity`/`--health` are new, additive CLI flags, not replacements.** `main.py`
   uses a flat `argparse` CLI (not subcommands), so the new flags were added alongside
   existing ones like `--source-health`/`--diagnose-alerts` and dispatched at the same
   priority point (immediately after `init_db()`, before every other flag branch).
   Existing flags, their behavior, and their output formats are untouched.

4. **`runtime_bridge.py` lives at the repo root, not inside a package.** Free Game
   Tracker's reference puts it at `newsroom/runtime_bridge.py` because that project is
   organized as a `newsroom` package. Chinese Tech Wire has no such package — `main.py`,
   `config.py`, `database/`, `pipeline/`, `sources/`, etc. all sit directly under the repo
   root — so `runtime_bridge.py` follows that same flat convention.

5. **`runtime_bridge.py` duplicates `web/app.py`'s `CTW_VERSION` constant rather than
   importing it.** Importing `web.app` constructs a `FastAPI` app and mounts a static
   file directory as an import-time side effect — too much weight and too many
   dependencies (fastapi, jinja2, markupsafe) to pull in just for a headless
   `--identity`/`--health` check that must work independent of the GUI surface. The
   constant is documented as needing to stay in sync with `web/app.py:CTW_VERSION`;
   flagged in FILES_CHANGED.md/KNOWN_ISSUES.md rather than silently risking drift.

6. **Health derives `operational_state` from the `ingestion_runs` table, matching what
   the GUI Health page already reads** (`IngestionRun.status`: `SUCCESS` → `healthy`,
   `PARTIAL` → `degraded`, `FAILED` → `failed`), rather than inventing a parallel health
   model. With **zero** `ingestion_runs` rows (a freshly built container that has never
   completed a cycle), health reports `"unknown"`, not `"healthy"` — a soak container
   that has proven nothing yet must not claim it is fine.

7. **Backup/restore use sqlite3's online backup API, not a raw file copy** — same
   rationale as the Free Game Tracker reference: correctness under a WAL-mode write in
   progress, no journal-mode change imposed on the application. `backup.py` is new
   because no backup mechanism existed for this repo before this phase.

8. **`restore.py`'s isolation guarantee was verified using a subdirectory of the same
   data volume (`/app/data/restore-test/`), not a second named volume.** A brand-new,
   never-before-mounted named volume gets `root:root` ownership from the Docker daemon
   unless the mount path already exists (with content or ownership) inside the image —
   `/app/data` does (it's created and `chown`'d in the Dockerfile), so mounting the
   *same* volume and writing to an isolated subdirectory within it is both correctly
   writable by the non-root `ctw` user and never touches the live `ctw.db` file — the
   actual safety property required. This matches the exact approach Free Game Tracker's
   own verified restore drill used (`--target-dir /app/data/restore-test`).

9. **No dashboard (`--gui`) invocation in the default container command.** The default
   `CMD` is `--full-once`; `--gui` still routes correctly through the entrypoint if
   invoked explicitly, but nothing about the staging compose file or Dockerfile ever
   triggers it, so `webbrowser.open()` never fires inside the (headless) container.

10. **Did not provision any cloud host, did not touch the real Discord webhook, and did
    not push any git history.** Per the brief's explicit "no push yet" instruction and
    this clank's staging/soak-only scope — nothing here authorizes or attempts anything
    beyond local verification.
