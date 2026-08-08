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

## Explicitly deferred (needs a cloud host, not yet approved — and this clank has no
## production tier to promote to even once a host exists)

- External scheduler actually firing over real elapsed time.
- Host reboot / container-crash recovery.
- Notification delivery from a real target network (this phase deliberately never
  exercised a real Discord webhook — `DISCORD_WEBHOOK_URL` was empty throughout, and the
  one real `--full-once` run correctly evaluated alert eligibility (100 evaluated, 2
  eligible) while sending 0, since no webhook was configured).
- Tailscale / private-access model.
- Any production release channel, compose file, or promotion path — out of scope for this
  Tier B (staging/soak only) clank in this phase, by design, not by omission.
