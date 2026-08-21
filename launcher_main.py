#!/usr/bin/env python3
"""V0.5.6.1 — PyInstaller entrypoint for ChineseTechWire.exe.

Launches the existing FastAPI/Jinja dashboard (web.app) against the
operator's real CTW project directory — data/ctw.db, config/settings.yaml,
.env, logs/ — never an isolated copy. Picks a free localhost port, waits
for readiness, opens the default browser, and keeps running until closed.

This is NOT a second GUI and does not touch scheduled collection — it just
gives the existing dashboard a reliable double-click entry point.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

# Same fix as main.py: a stock Windows console defaults stdout/stderr to
# cp1252, which crashes on the "→" in run_gui()'s startup banner (and on
# any CJK headline anywhere else in the app). Force UTF-8 before anything
# else prints.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _find_root_upward(start: Path, max_levels: int = 4) -> Path:
    """Walk up from `start` looking for config/settings.yaml — handles the
    build deliverable (dist/ChineseTechWire.exe) being double-clicked in
    place, one level below the real project root. Falls back to `start`
    unchanged if nothing is found, so the caller can still fail loudly."""
    candidate = start
    for _ in range(max_levels + 1):
        if (candidate / "config" / "settings.yaml").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return start


def _resolve_and_enter_project_root() -> Path:
    """Frozen: sys.executable is the real .exe location. Source: this
    file's own directory. Either way, chdir into the resolved root so
    every relative path in the app (sqlite db, etc.) resolves the same way
    it does for `python main.py`."""
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
        root = _find_root_upward(start)
    else:
        root = Path(__file__).resolve().parent
    os.chdir(root)
    return root


def main() -> int:
    root = _resolve_and_enter_project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from security.redaction import install_logging_redaction

    install_logging_redaction()

    from web.launcher import (
        ProjectRootError,
        clear_runtime_state,
        find_existing_instance,
        find_free_port,
        open_browser,
        open_when_ready,
        setup_launcher_logging,
        validate_project_root,
        write_runtime_state,
    )

    setup_launcher_logging(root)
    log = logging.getLogger("ctw.launcher")
    log.info("ChineseTechWire launcher starting — project root=%s", root)

    try:
        validate_project_root(root)
    except ProjectRootError as e:
        log.error("%s", e)
        try:
            print(f"ERROR: {e}", file=sys.stderr)
            input("Press Enter to close...")
        except Exception:
            pass
        return 1

    existing = find_existing_instance(root)
    if existing:
        log.info("Existing CTW dashboard found at %s:%s — reusing it, not starting a second server",
                  existing["host"], existing["port"])
        open_browser(existing["host"], existing["port"])
        return 0

    host = "127.0.0.1"
    port = find_free_port(host)
    log.info("Selected port %s", port)

    write_runtime_state(host, port, root)
    threading.Thread(target=open_when_ready, args=(host, port), daemon=True).start()

    try:
        from web.app import run_gui
        log.info("Starting dashboard server on http://%s:%s/", host, port)
        run_gui(host=host, port=port)
        log.info("Dashboard server stopped cleanly")
        return 0
    except Exception:
        log.exception("Dashboard server crashed")
        return 1
    finally:
        clear_runtime_state(root)


if __name__ == "__main__":
    raise SystemExit(main())
