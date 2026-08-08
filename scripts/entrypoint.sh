#!/bin/sh
# Container entrypoint for Chinese Tech Wire (Tier B — staging/soak only).
# External scheduler invokes this via `docker compose -f
# docker-compose.staging.yml run --rm chinese-tech-wire --full-once` (or
# another one-shot subcommand). No in-process scheduling, no --gui by
# default, no browser launch.
set -eu
cd /app
mkdir -p /app/data
if [ "$#" -eq 0 ]; then
  exec python main.py --full-once
fi
exec python main.py "$@"
