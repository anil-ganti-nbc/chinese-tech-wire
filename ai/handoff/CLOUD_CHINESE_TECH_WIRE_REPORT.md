```yaml
project: chinese-tech-wire
stage: cloud-migration-phase-1 (portability + local soak verification)
baseline_commit: c424bbe
branch: cloud/chinese-tech-soak
tier: B (staging/soak only — no production path exists for this clank)
target_environment: Linux AMD64 Docker (host TBD — no cloud host provisioned yet)
image_digest: not pushed anywhere; local verification used tag chinese-tech-wire:soak-local (disposable)
release_channel: soaking
operational_state: healthy (verified locally against isolated test state)
docker_build_verified: true
container_contracts_verified: true
persistent_state_verified: true
scheduler_verified: false
notifications_verified: false
backup_verified: true
restore_verified: true
tests_passed: 350
tests_failed: 0
contracts_changed: false
schema_changed: false
architecture_deviations: none
known_product_defects: see KNOWN_ISSUES.md (pre-existing, not touched)
known_portability_defects: none found
review_required: true
```

## What this phase covered

Portability and local Docker verification only, for a Tier B (staging/soak-only) clank.
No cloud host has been provisioned. No production compose file was created — none is in
scope for this clank in this phase. This is explicitly **not** a production deployment and
must not be treated, labeled, or promoted as one.

## What was verified, and how

All verification ran locally against Docker Desktop (Windows host, linux/amd64 target),
using disposable named volumes and a locally-tagged image (`chinese-tech-wire:soak-local`,
removed after testing — never pushed anywhere).

| Check | Result |
|---|---|
| Existing test suite before changes | 350 passed, 0 failed (204.6s) |
| Existing test suite after changes | 350 passed, 0 failed (205.6s) |
| `docker build --platform linux/amd64` | succeeds reproducibly |
| Non-root execution | `id` inside container (via `--entrypoint id`): `uid=10001(ctw) gid=10001(ctw)` |
| `python main.py --identity` in container | `release_channel: "soaking"` (not hard-coded, never "production") |
| `python main.py --health` on a fresh/empty volume | `operational_state: "unknown"`, honest `status_reasons: ["no ingestion_runs recorded yet"]` — not fabricated "healthy" |
| Real one-shot run (`--full-once`) against isolated named volume | exit 0, real sources fetched (ithome/mydrivers/expreview/zol/jiwei/benchlife/hkepc/technews/xfastest + community), 298 new articles + 40 community threads persisted, `status=SUCCESS` |
| `python main.py --health` after the run | `operational_state: "healthy"`, `total_runs: 1`, truthful `last_successful_run` timestamp from `ingestion_runs` |
| Persistence across container recreation | fresh container against the same named volume reads back identical articles via `--show-recent` |
| `docker compose -f docker-compose.staging.yml config` | validates; no ports published, no Docker socket mounted, `restart: "no"` present |
| Backup (`scripts/backup.py`) | produced a consistent DB snapshot (sqlite3 online backup API) inside the volume |
| Isolated restore (`scripts/restore.py`) | restored into `/app/data/restore-test` (isolated subdirectory, never the live `ctw.db` path), `PRAGMA integrity_check` = ok (21 tables intact) |
| Restored-state verification | a separate container pointed at the restored copy (`DATABASE_URL` override) reported the identical `healthy` state and `total_runs: 1` as the live copy; the live copy was re-checked afterward and remained unchanged |
| Discord safety | `DISCORD_WEBHOOK_URL` left empty throughout — the real run evaluated 100 alert candidates, 2 eligible, 0 sent (no network POST attempted, by design) |

## Notable observation, not a defect

Real network fetches from inside the Linux container succeeded against ithome, mydrivers,
technews, and community sources without incident. Two of the documented anti-bot/fragile
sources (hkepc, xfastest, JD.com, forum sources) are already known to soft-fail per the
prior audit — this is intentional, pre-existing product behavior and was not touched or
"fixed" here. The dry run's 100-lead alert-eligibility check (`--full-once` with no
webhook configured) exercised the full alert decision pipeline (100 evaluated, 2 eligible,
0 attempted since no webhook was set) without ever attempting a real POST, confirming the
staging-safety design holds under a real run.

## What still blocks anything beyond soak

1. **This clank has no production tier by design (Tier B).** No production compose,
   no production release channel wiring, and no promotion path were built — none was
   requested and none should be inferred from this phase's passing verification.
2. **Cloud host decision** — no provider/size/cost has been approved. Needed before any
   external-schedule-over-time or reboot-recovery verification could even be attempted.
3. **Real webhook test** — verification here used no Discord webhook (empty value only),
   by design, to avoid posting anywhere from test runs.
4. **Explicit promotion approval** — per the brief, passing tests never self-authorize
   anything beyond soak. `CTW_RELEASE_CHANNEL` stays `soaking` until an operator says
   otherwise, and even then this clank has no defined channel beyond that in this phase.
