# Staging release runbook — Chinese Tech Wire on the Synology NAS

Tier B: staging/soak only, no production tier exists for this clank in this phase.
Repeatable procedure for every future candidate build. Nothing here has been executed
against the real NAS yet (no access until 2026-08-15).

## Per-release identity (fill in every time)

| Field | Value |
|---|---|
| development branch | `cloud/chinese-tech-soak` (or a future feature branch merged into it) |
| candidate tag | the reviewed commit SHA |
| candidate image | `chinese-tech-wire:<sha>` |
| staging state path | `/volume1/docker-data/chinese-tech-wire-staging/` — never the operator's real `data/ctw.db` |
| staging schedule | disabled by default; enable only for a deliberate soak run |
| staging notification target | none, or a distinct test webhook |
| rollback image | previous candidate's image, kept loaded |

## Procedure

1. **Build off-NAS**: `docker build --platform linux/amd64 -t chinese-tech-wire:<sha> .`
2. **Local validation**: non-root, `--identity`/`--health` in-container, a real
   `--full-once` run against a throwaway volume, persistence-across-recreation,
   backup/restore drill — the same checks already proven this session.
3. **Transfer**: `docker save | gzip` → copy to NAS → `docker load`.
4. **Staging run, isolated volume only**:
   `IMAGE_TAG=<sha> docker compose -f docker-compose.staging.yml run --rm chinese-tech-wire --full-once`
5. **Soak restarts on material change** — same rule as every Tier B clank: a build
   that changes scraper/pipeline/scoring logic starts its soak clock at zero,
   regardless of how long the previous build had been running cleanly.
6. **No production promotion** in the current maturity policy — this stays
   staging/soak until the user explicitly reclassifies it.
7. **Record which candidate is soaking and since when** — don't let "how long has
   this been running" become a guess.

## Rollback

Previous image stays loaded on the NAS; rollback is swapping the tag back, not a
rebuild.
