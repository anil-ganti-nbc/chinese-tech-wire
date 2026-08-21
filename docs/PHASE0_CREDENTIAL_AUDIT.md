# Phase 0 credential audit

Status: **OPEN — operator evidence and any required rotation are not complete.**

Run this only in a restricted workspace. Disable shell tracing; never print an
environment dump, credential, authorization header, or webhook. Findings use
provider key IDs or one-way fingerprints, never values.

## Required surfaces

1. Scan the exact default-branch and PR heads and all tracked files.
2. Mirror the repository and scan all reachable history, deleted files,
   branches, tags, and available pull-request refs.
3. Scan application, launcher, scheduler, Uvicorn, proxy, container, system,
   rotated, and compressed logs on each evidenced host.
4. Scan crash reports, tracebacks, minidumps, diagnostic/support bundles,
   database fields containing errors, archives, and backups in scope.
5. Enumerate CI runs for every ref; inspect logs, annotations, caches where
   accessible, artifacts, test reports, and build output. Expired or
   inaccessible artifacts remain `UNKNOWN`.
6. Enumerate image tags and digests. Inspect image config/history, environment,
   build metadata, every filesystem layer (including files deleted in later
   layers), stopped-container writable layers, provenance, and SBOM.
7. Scan approved host working/config/evidence/log/crash paths, service
   environment files, scheduler definitions, temp paths, and mounted volumes.
   Do not traverse unrelated user data.

Use a pinned history-aware scanner plus provider-specific or one-way
fingerprint matching. Record tool versions, rule/config digests, exact refs,
image digests, host paths, timestamps, and inaccessible surfaces. Header-based
transport prevents new query-string leakage but does not clean old history,
logs, proxies, or artifacts.

Any active or possibly active match requires operator rotation unless the
provider proves it is revoked/non-secret. Rotation is not authorized by this
document. Map every consumer, create a least-privilege replacement in the
approved secret manager, verify a staging consumer, update approved instances
one at a time, revoke the old provider key, prove the old key fails, and rescan.

Use `credential-finding-register.template.csv`. No credential currently has a
completed exposure decision, so required rotations remain `UNKNOWN` pending
the audit.
