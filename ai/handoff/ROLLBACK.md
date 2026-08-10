# Rollback — Chinese Tech Wire cloud migration

**Updated 2026-08-10** with the real Hetzner deployment record. Everything
below "Current deployment" was written before any real deployment existed
(when the plan was still `cloud/chinese-tech-soak`, unpushed); that plan was
superseded once `main` on GitHub became the actual deployment source. See
`STAGING_RELEASE_RUNBOOK.md` for the general repeatable procedure.

## Current deployment

```
current_deployed_revision:  552ffff (main, includes the Git-revision provenance PR)
previous_revision:           52fdd72 (main, pre-provenance — no image was ever built from
                              this revision on Hetzner, so it is a code rollback target,
                              not an image rollback target)
rollback_image:               none currently retained — 552ffff is the only image built
                              on Hetzner for this clank so far. The next revision change
                              should retain this image before replacing it.
host:                          204.168.142.1 (Hetzner)
persistent volume:             ctw_staging_data (named Docker volume, isolated from every
                              other clank's state)
```

## Code rollback

`git checkout <previous_sha>` on the Hetzner clone (`~/staging/chinese-tech-wire`),
rebuild, and update `.deployed-id` — never hand-edit application source on the
host (see `DECISIONS.md` / this migration's explicit "never hot-patch Hetzner"
rule). The production directory on the original Windows development machine
was never touched by this migration and remains at `52fdd72` intentionally —
it is not a rollback target, it predates the provenance work by design (all
GitHub changes went through a separate working copy, never the local
production checkout).

## Image rollback procedure (the pattern to use going forward)

Same pattern as OEM Radar / Free Game Tracker:

```bash
# On Hetzner:
cd ~/staging/chinese-tech-wire
echo "<previous-short-sha>" > .deployed-id
# Next scheduled cron run (or a manual ./deploy_run.sh) picks it up automatically.
```

The wrapper script (`deploy_run.sh`) reads `.deployed-id` at invocation time,
so rollback is a one-line file edit, not a rebuild or a cron/compose edit.
Previous images are not deleted automatically — retain at least one
known-good image per revision change within disk constraints.

## State rollback

No schema changes have been made at any point (`schema_changed: false`
remains accurate). The persistent volume (`ctw_staging_data`) is independent
of which image tag runs against it — a rollback only changes the running
image, never the data, unless a future schema-incompatible change makes that
unsafe (not yet the case). To reset state deliberately:

1. Stop/skip the container (`restart: "no"` means nothing auto-restarts it —
   simply don't invoke `deploy_run.sh`).
2. Run `python scripts/backup.py` against the volume if a snapshot isn't
   already current.
3. Remove the named volume (`docker volume rm ctw_staging_data`) for a clean
   slate, or leave it in place to preserve soak history (the current
   recommendation — see `STAGING_RELEASE_RUNBOOK.md`'s soak-restart rule).
4. Re-run `--health` after any volume change before trusting the container
   again.

## Restore drill (proven pre-deployment, not re-run against live Hetzner state)

`scripts/restore.py --backup <path> --target-dir <isolated dir>` was verified
end-to-end during the pre-deployment phase (backup → restore into an isolated
subdirectory → integrity check passed → a separate process read back
identical state). This has not been re-exercised against the real Hetzner
volume as of this update — the volume currently holds 13 real, valuable
scheduled-run cycles (see `STAGING_RELEASE_RUNBOOK.md`), so a restore drill
against it should use a copy, not the live volume, to avoid any risk to that
soak history.

## Config/flag rollback

`--identity` and `--health` are additive CLI flags; removing them has no
effect on any existing flag. `config.py`'s `release_channel` field defaults
to `"soaking"` and is `extra = "ignore"`, so an unset `CTW_RELEASE_CHANNEL`
env var never breaks pre-existing `.env` files. The Git-revision provenance
fields (`source_revision`, `source_revision_short` on `runtime_bridge.py`)
are likewise additive — removing the `GIT_REVISION` build arg reverts to
`"unknown"`, never an error.
