# Known issues — Chinese Tech Wire cloud migration

## Found and fixed this phase

None. Unlike Free Game Tracker's `pip install .` packaging path, this repo runs directly
from a copied source tree (`COPY main.py ... ./`, no `pip install .` of the project
itself), so no import-path-under-site-packages class of defect applies here. No
schema-affecting or portability-blocking defect was found.

## Pre-existing, documented, intentionally not touched

- Several source adapters (JD.com, HKEPC, XFastest, forum/community sources such as PTT)
  are documented in `pipeline/source_health.py`'s `KNOWN_STATUS` map as anti-bot/fragile
  or intentionally disabled (`BLOCKED`, `PARTIAL`, `DISABLED`). This is intentional,
  pre-existing product behavior, confirmed unrelated to containerization — the same
  sources that soft-fail natively also soft-fail (or succeed) identically from inside the
  Linux container. Not "fixed" here, per the brief.
- Two overlapping schema-migration mechanisms exist in `database/db.py`:
  `Base.metadata.create_all()` (new tables) plus a hand-rolled `migrate_schema()` doing
  `ALTER TABLE ... ADD COLUMN` for columns added after the original schema. This is
  pre-existing and untouched — no new columns, tables, or migration steps were added in
  this phase.
- `ChineseTechWire.spec` / `dist/ChineseTechWire.exe` / `launcher_main.py` (PyInstaller
  packaging) is a separate distribution path from the container path built here. Left
  completely alone; excluded from the Docker build context via `.dockerignore`.
- The `data/ctw.db` real dev database (~11MB) contains genuine production-shaped data
  accumulated during development. It is never copied into the image (excluded via
  `.dockerignore`) and never touched by any Docker verification in this phase — all
  container tests ran against disposable named volumes, removed after use.

## Resolved since the above was written (2026-08-10 update)

- **External scheduler firing over real elapsed time**: resolved. 13 genuine
  cron-triggered cycles observed on Hetzner (`5 * * * *`), all `SUCCESS`, 0
  errors, 0 warnings, spanning 2026-08-09 19:47 UTC through 2026-08-10 06:00
  UTC. See `STAGING_RELEASE_RUNBOOK.md`.
- **Run-lock / overlap safety**: this clank has no in-application run-lock
  (unlike OEM Radar/SemInt). An external `flock`-based lock was added at the
  cron-wrapper level and **verified under a real deliberate overlap**: a
  second invocation while one was mid-run was refused immediately (exit 1,
  no output), the first continued normally to `SUCCESS`.
- **Git-revision provenance**: implemented (`org.opencontainers.image.revision`
  OCI label + `CTW_SOURCE_REVISION` env var, surfaced through `--identity`/
  `--version`), the second proof case after OEM Radar. All three provenance
  sources (GitHub SHA, image label, identity output) confirmed equal.

## Still deferred (genuinely not yet exercised)

- **Host reboot / container-crash recovery** — not tested for this specific
  clank. The cron entry itself (plain crontab, not a systemd timer) survives
  a host reboot on its own, but this has not been empirically verified with
  an actual reboot for Chinese Tech Wire specifically.
- **Notification delivery from a real target network** — still deliberately
  untested. `DISCORD_WEBHOOK_URL` remains unset in the staging deployment;
  every real cycle so far has evaluated ~100 alerts/run and sent 0, by
  design (staging-safety default), not because delivery was tried and
  failed.
- **Tailscale / private-access model** — not applicable to the current
  Hetzner deployment; deferred to the eventual NAS phase.
- **Any production release channel, compose file, or promotion path** — out
  of scope. This clank remains `NEEDS MORE TESTING` / staging-soak; 13 clean
  scheduled cycles is soak evidence, not a production-readiness
  determination, and no one has reclassified it.
- **Restore drill against the live Hetzner volume** — the backup/restore
  mechanism was proven pre-deployment against a throwaway volume; it has not
  been re-exercised against the real Hetzner `ctw_staging_data` volume
  (which now holds real soak history worth not risking casually).
