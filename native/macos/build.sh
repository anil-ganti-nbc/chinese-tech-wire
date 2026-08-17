#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export CTW_BUILD_REVISION="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf local-development)"
exec "${PYTHON:-python3}" -m PyInstaller --noconfirm --clean \
  --distpath "$ROOT/native/macos/dist" \
  --workpath "$ROOT/native/macos/build" \
  "$ROOT/native/macos/ChineseTechWire.spec"
