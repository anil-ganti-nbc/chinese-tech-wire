"""Finder launcher for an isolated Chinese Tech Wire field test."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path


APP_NAME = "Chinese Tech Wire"


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parents[1] / "Resources"
    return Path(__file__).resolve().parents[2]


def default_state_root() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def configure_field_test_runtime() -> tuple[Path, Path]:
    state = Path(os.environ.get("CTW_DATA_DIR", default_state_root())).expanduser().resolve()
    resources = resource_root()
    state.mkdir(parents=True, exist_ok=True)
    (state / "logs").mkdir(parents=True, exist_ok=True)

    # These are deliberate assignments, not defaults: a Finder field test
    # must never inherit a production DB or source-checkout .env by accident.
    os.environ["CTW_DATA_DIR"] = str(state)
    os.environ["CTW_CONFIG_DIR"] = str(resources / "config")
    os.environ["CTW_ENV_FILE"] = str(state / "field-test.env")
    os.environ["DATABASE_URL"] = f"sqlite:///{state / 'ctw.db'}"
    # Field test permits real local collection (isolated DB, no production
    # delivery/secrets) — do not force CTW_DISABLE_COLLECTOR_LAUNCH here.
    # The kill switch itself stays in pipeline.operations.launch_manual_run
    # for anyone who explicitly wants to disable it.
    for secret in ("DISCORD_WEBHOOK_URL", "TRANSLATION_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(secret, None)
    return state, resources


def main() -> int:
    state, resources = configure_field_test_runtime()
    revision = resources / "metadata" / "revision.txt"
    os.environ.setdefault(
        "CTW_SOURCE_REVISION",
        revision.read_text(encoding="utf-8").strip() if revision.exists() else "local-development",
    )

    import uvicorn
    from database.db import init_db
    from config import settings
    from web.app import app
    from web.launcher import (
        clear_runtime_state,
        find_existing_instance,
        find_free_port,
        open_browser,
        open_when_ready,
        setup_launcher_logging,
        write_runtime_state,
    )

    setup_launcher_logging(state)
    log = logging.getLogger("ctw.launcher")
    init_db(settings.database_url)

    existing = find_existing_instance(state)
    if existing:
        open_browser(existing["host"], existing["port"])
        return 0

    host = "127.0.0.1"
    port = find_free_port(host)
    write_runtime_state(host, port, state)
    threading.Thread(target=open_when_ready, args=(host, port), daemon=True).start()

    # See security.redaction.uvicorn_log_config: the redaction record factory
    # clears record.args, which uvicorn's AccessFormatter unpacks structurally.
    from security.redaction import uvicorn_log_config

    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="info",
        log_config=uvicorn_log_config("info"),
    ))
    server_thread = threading.Thread(target=server.run, name="ctw-loopback", daemon=False)

    def stop(_signum: int, _frame: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        log.info("Starting isolated CTW dashboard on http://%s:%s/", host, port)
        server_thread.start()
        server_thread.join()
        return 0 if server.started else 1
    finally:
        server.should_exit = True
        if server_thread.is_alive():
            server_thread.join(timeout=10)
        clear_runtime_state(state)


if __name__ == "__main__":
    raise SystemExit(main())
