# Staging release runbook — Chinese Tech Wire

Tier B: staging/soak only, no production tier exists for this clank in this
phase. Repeatable procedure for every future candidate build.

**Updated 2026-08-10**: this runbook was originally written for a direct
Windows→Synology NAS move (no NAS access until 2026-08-15). The actual
topology is now:

```
GitHub (canonical source)
        ↓
Hetzner (temporary remote runtime/soak host — CURRENT, this is where soaking
         is actually happening as of this update)
        ↓
NAS (eventual permanent runtime host — not yet available)
```

Everything below is written against the real Hetzner deployment. Nothing
here is Hetzner-specific in a way that blocks the eventual NAS move — see
"NAS portability" at the bottom.

## Per-release identity (fill in every time)

| Field | Value (current, 2026-08-10) |
|---|---|
| development branch | `main` (this release came via `feature/git-revision-identity`, merged) |
| candidate commit | `552ffff472689470820aecdec3d6dfa6faf478a8` |
| candidate image | `chinese-tech-wire:552ffff` |
| staging state path | Docker named volume `ctw_staging_data` on the Hetzner host, mounted at `/app/data` — never the operator's real local `data/ctw.db` |
| staging schedule | **enabled** — `5 * * * *` (hourly, at :05), staggered against the rest of the Hetzner fleet (Free Game Tracker `:00`, OEM Radar `:20`, Semiconductor Intelligence `:40`) |
| staging notification target | none — `DISCORD_WEBHOOK_URL` deliberately unset; alerts are evaluated (100/cycle) but never sent, matching the staging-safety default |
| rollback image | none yet retained (first deployment) — see `ROLLBACK.md` for the procedure to use on the next revision change |

## Procedure (as actually executed for 552ffff)

1. **Build on Hetzner from the merged GitHub revision** (not off-host and
   transferred — this clank's images are small enough, and the host has
   direct GitHub access):
   `GIT_REVISION=$(git rev-parse HEAD) IMAGE_TAG=$(git rev-parse --short HEAD) docker compose -f docker-compose.staging.yml build`
2. **Provenance verification**: confirmed `org.opencontainers.image.revision`
   (OCI label) == `--identity`'s `source_revision` == the GitHub commit SHA,
   all three equal.
3. **Real collector cycle** (not just tests/mocks): `--full-once` against
   the isolated named volume — 305 articles, 40 community items, 100 leads,
   0 errors on the first real cycle.
4. **Persistence + idempotency**: a second real run against the same volume
   correctly found already-seen articles unchanged (`articles+=0`) while
   still completing successfully — proves state persists across container
   recreation and the pipeline doesn't reprocess/re-alert on unchanged data.
5. **Scheduler**: no in-application run-lock exists for this clank (unlike
   OEM Radar/SemInt) — wrapped the cron invocation in `flock -n
   /tmp/ctw-run.lock` (external, host-level) rather than modifying
   application code. **Verified under real overlap** (2026-08-10):
   deliberately started a second invocation while one was mid-run — the
   second was refused immediately by `flock` (exit 1, no output, container
   never even started) while the first continued to a normal `SUCCESS`
   completion.
6. **Real unattended scheduled operation**: 13 genuine cron-triggered
   cycles observed as of this update (run IDs 1-13, spanning 2026-08-09
   19:47 UTC through 2026-08-10 06:00 UTC), **all SUCCESS, 0 errors, 0
   warnings**. Durations: 68.7s-231.6s (one outlier at 231.6s during a
   deliberate overlap test, still succeeded; typical is ~70s).
7. **Soak restarts on material change** — unchanged rule: a build that
   changes scraper/pipeline/scoring logic starts its soak clock at zero,
   regardless of how long the previous build had been running cleanly. The
   provenance-only change that produced `552ffff` did **not** touch
   scraper/pipeline/scoring logic, so this is treated as a continuation of
   the soak that began with the first real cycle, not a restart.
8. **No production promotion** in the current maturity policy — this stays
   **NEEDS MORE TESTING / staging-soak** regardless of how cleanly Hetzner
   deployment went. Docker working and 13 clean cycles is soak evidence,
   not a production-readiness determination.

## Current soak status (2026-08-10)

```
soak start:              2026-08-09 ~19:47 UTC (first real cycle on Hetzner)
cycles completed:         13, all SUCCESS
errors:                   0
warnings:                 0
database:                 1.5MB, PRAGMA integrity_check: ok
release_channel:          soaking (truthful — never claims more)
```

## Resource profile (measured, not estimated)

```
RAM:              64-69 MiB during a real cycle (very light)
CPU:              near-zero most of the time, brief bursts to ~24%
                  (consistent with I/O-bound-with-light-parsing behavior,
                  not the strongly I/O-bound OEM Radar profile — CTW does
                  meaningfully more in-process work per item, e.g. NLP-ish
                  lead scoring, but is still far from CPU-bound)
image size:        328MB
wall-clock:        ~70s typical, up to ~230s observed under host contention
```

## NAS portability

Nothing in this deployment is Hetzner-specific:
- Persistent state is a named Docker volume (`ctw_staging_data`), not a
  bind-mount to a Hetzner-specific path.
- The `flock`-based run-lock uses a generic `/tmp` path, portable to any
  Linux host.
- No Hetzner IP, API, or provider metadata is referenced anywhere in the
  application or its deployment config.
- The full migration unit (GitHub revision + Dockerfile/compose + named
  volume + cron entry + this documentation) is directly reproducible on a
  Synology NAS once available — clone the repo, build with the same
  `GIT_REVISION` mechanism, create the named volume, install the same cron
  line (or a systemd timer equivalent), done.

## Rollback

See `ROLLBACK.md` for the current, accurate rollback record and procedure.
