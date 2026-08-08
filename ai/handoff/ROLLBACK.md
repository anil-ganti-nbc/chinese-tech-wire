# Rollback — Chinese Tech Wire cloud migration

## Code rollback

Everything in this phase lives on `cloud/chinese-tech-soak`, ahead of the `master`
baseline commit `c424bbe` ("Baseline commit: current state of Chinese Tech Wire before
cloud migration"). To fully roll back the code: `git checkout master` (or reset the
branch to `c424bbe`). Nothing on `master` was modified — this phase only ever committed
to `cloud/chinese-tech-soak`, and nothing was pushed anywhere.

## Image rollback

No image was pushed to any registry — verification used a disposable local tag
(`chinese-tech-wire:soak-local`), removed as part of test cleanup. There is nothing to
roll back on the image side because nothing was deployed anywhere; a future soak
deployment should tag images by commit SHA before that becomes a concern.

## State rollback

No schema changes were made (`schema_changed: false`), so there is no schema-compatible
or schema-incompatible rollback concern for this phase. If a soak container's volume ever
needs to be reset:
1. Stop the container (`docker compose -f docker-compose.staging.yml down`, or just don't
   re-run it — `restart: "no"` means nothing auto-restarts it).
2. Run `python scripts/backup.py` against the volume if a snapshot isn't already current.
3. Remove the named volume (`docker volume rm ctw_staging_data`) if a clean slate is
   wanted, or leave it in place to keep soak history.
4. Re-run `python main.py --health` after any volume change before trusting the container
   again.

## Restore drill (proven this phase)

`scripts/restore.py --backup <path> --target-dir <isolated dir>` restores into a
directory that is never the live `ctw.db` path (verified using `/app/data/restore-test/`,
a subdirectory of the data volume distinct from the live file), then runs
`PRAGMA integrity_check`. Verified end-to-end locally: backup → restore into the isolated
subdirectory → integrity check passed (21 tables) → a separate `DATABASE_URL`-overridden
process read back the identical `healthy` state and `total_runs: 1` as the live copy →
the live copy was re-checked afterward and confirmed unchanged.

## Config/flag rollback

`--identity` and `--health` are additive CLI flags; removing them (reverting `main.py`
and `runtime_bridge.py`) has no effect on any existing flag, and no other code path calls
into `runtime_bridge`. `config.py`'s new `release_channel` field defaults to `"soaking"`
and is `extra = "ignore"` in `Settings.model_config`, so an unset `CTW_RELEASE_CHANNEL`
env var never breaks existing `.env` files that predate this change.
