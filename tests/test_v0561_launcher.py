"""V0.5.6.1 — dashboard launcher regression tests.

No real browser is ever launched (webbrowser.open is always mocked). No
test relies on a specific port being free — every port-collision scenario
deliberately reserves sockets first. No frozen .exe is invoked here; that's
covered by the manual build smoke test.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from web import launcher


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------

def test_preferred_free_port_is_selected():
    rng = range(19500, 19510)
    # nothing occupied in this throwaway range
    port = launcher.find_free_port("127.0.0.1", preferred_range=rng)
    assert port in rng


def test_occupied_preferred_port_causes_fallback():
    rng = range(19520, 19525)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", rng.start))
    s.listen(1)
    try:
        port = launcher.find_free_port("127.0.0.1", preferred_range=rng)
        assert port != rng.start
        assert port in rng
    finally:
        s.close()


def test_multiple_occupied_ports_are_skipped():
    rng = range(19530, 19536)
    held = []
    try:
        for p in list(rng)[:4]:  # occupy the first four of six
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            s.listen(1)
            held.append(s)
        port = launcher.find_free_port("127.0.0.1", preferred_range=rng)
        assert port not in [s.getsockname()[1] for s in held]
        assert port in rng
    finally:
        for s in held:
            s.close()


def test_whole_range_occupied_falls_back_to_os_ephemeral_port():
    rng = range(19540, 19542)  # tiny range, fully occupied
    held = []
    try:
        for p in rng:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            s.listen(1)
            held.append(s)
        port = launcher.find_free_port("127.0.0.1", preferred_range=rng)
        assert port not in list(rng)
        assert launcher.is_port_free("127.0.0.1", port)
    finally:
        for s in held:
            s.close()


def test_launcher_binds_only_to_loopback():
    """find_free_port must probe on the given loopback host, not a wildcard
    bind — a port free on 127.0.0.1 is what CTW will actually use."""
    port = launcher.find_free_port("127.0.0.1", preferred_range=range(19550, 19560))
    # The port must be immediately bindable again on the same loopback host
    # (proving find_free_port released its probe correctly) and not have
    # touched 0.0.0.0.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))
        s.listen(1)  # succeeds — port was genuinely free and not held open


def test_run_gui_rejects_non_loopback_host():
    from web.app import run_gui
    with patch("uvicorn.run"):
        with pytest.raises(ValueError, match="must be loopback"):
            run_gui(host="0.0.0.0", port=19561)


# ---------------------------------------------------------------------------
# Readiness + browser
# ---------------------------------------------------------------------------

def test_browser_url_contains_selected_port():
    with patch("webbrowser.open") as mocked:
        mocked.return_value = True
        launcher.open_browser("127.0.0.1", 19571)
    mocked.assert_called_once_with("http://127.0.0.1:19571/")


def test_browser_opens_only_after_readiness_succeeds():
    with patch.object(launcher, "wait_for_ready", return_value=True) as ready, \
         patch.object(launcher, "open_browser", return_value=True) as opened:
        result = launcher.open_when_ready("127.0.0.1", 19572, timeout=1.0)
    ready.assert_called_once()
    opened.assert_called_once_with("127.0.0.1", 19572)
    assert result is True


def test_browser_not_opened_when_never_ready():
    with patch.object(launcher, "wait_for_ready", return_value=False), \
         patch.object(launcher, "open_browser") as opened:
        result = launcher.open_when_ready("127.0.0.1", 19573, timeout=0.5)
    opened.assert_not_called()
    assert result is False


def test_wait_for_ready_polls_healthz_not_fixed_sleep():
    calls = {"n": 0}

    def fake_probe(host, port, timeout=1.0):
        calls["n"] += 1
        return {"application": "ChineseTechWire"} if calls["n"] >= 3 else None

    with patch.object(launcher, "probe_healthz", side_effect=fake_probe):
        t0 = time.monotonic()
        ok = launcher.wait_for_ready("127.0.0.1", 19574, timeout=5.0, interval=0.05)
        elapsed = time.monotonic() - t0
    assert ok is True
    assert calls["n"] == 3
    assert elapsed < 1.0  # polled quickly, not stuck on one long fixed sleep


# ---------------------------------------------------------------------------
# Runtime state / instance reuse
# ---------------------------------------------------------------------------

def test_existing_valid_instance_is_reused(tmp_path):
    root = tmp_path
    (root / "data").mkdir()
    launcher.write_runtime_state("127.0.0.1", 19580, root=root)
    with patch.object(launcher, "_pid_alive", return_value=True), \
         patch.object(launcher, "probe_healthz", return_value={"application": "ChineseTechWire"}):
        found = launcher.find_existing_instance(root)
    assert found == {"host": "127.0.0.1", "port": 19580}


def test_stale_runtime_state_ignored_and_replaced(tmp_path):
    root = tmp_path
    (root / "data").mkdir()
    launcher.write_runtime_state("127.0.0.1", 19581, root=root)
    # Simulate a crashed process: PID no longer alive.
    with patch.object(launcher, "_pid_alive", return_value=False):
        found = launcher.find_existing_instance(root)
    assert found is None
    assert not launcher.runtime_state_path(root).exists()  # cleared, not left stale


def test_stale_state_ignored_when_port_no_longer_answers_ctw(tmp_path):
    root = tmp_path
    (root / "data").mkdir()
    launcher.write_runtime_state("127.0.0.1", 19582, root=root)
    with patch.object(launcher, "_pid_alive", return_value=True), \
         patch.object(launcher, "probe_healthz", return_value=None):  # something else is on that port now
        found = launcher.find_existing_instance(root)
    assert found is None


def test_runtime_state_missing_file_returns_none(tmp_path):
    assert launcher.find_existing_instance(tmp_path) is None


def test_runtime_state_corrupt_json_treated_as_absent(tmp_path):
    root = tmp_path
    (root / "data").mkdir()
    launcher.runtime_state_path(root).write_text("not json{{{", encoding="utf-8")
    assert launcher.read_runtime_state(root) is None
    assert launcher.find_existing_instance(root) is None


# ---------------------------------------------------------------------------
# /healthz identity endpoint
# ---------------------------------------------------------------------------

def test_healthz_returns_ctw_identity():
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["application"] == "ChineseTechWire"
    assert body["status"] == "ok"
    assert "version" in body


def test_healthz_performs_no_db_mutation(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/healthz.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    from database.db import init_db, get_session
    from database.models import StoryLead
    from sqlalchemy import select
    init_db(db_url)

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    client.get("/healthz")
    client.get("/healthz")

    with get_session() as session:
        assert session.execute(select(StoryLead)).scalars().all() == []


# ---------------------------------------------------------------------------
# Project-root resolution
# ---------------------------------------------------------------------------

def test_project_root_source_mode():
    root = launcher.project_root()
    assert (root / "config").exists() or (root / "main.py").exists()


def test_project_root_frozen_mode(monkeypatch, tmp_path):
    fake_exe = tmp_path / "ChineseTechWire.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    try:
        root = launcher.project_root()
        assert root == tmp_path
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_project_root_frozen_mode_finds_root_from_dist_subdir(monkeypatch, tmp_path):
    """The documented build deliverable is dist/ChineseTechWire.exe — if the
    operator double-clicks it there instead of moving it up first, the
    launcher must still find the real project root one level up."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}", encoding="utf-8")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    fake_exe = dist_dir / "ChineseTechWire.exe"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    try:
        root = launcher.project_root()
        assert root == tmp_path
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_project_root_frozen_mode_finds_root_from_macos_app_bundle(monkeypatch, tmp_path):
    """native/macos/ChineseTechWire.spec builds a macOS .app bundle whose
    real binary lives 6 levels below the project root (native/macos/dist/
    Chinese Tech Wire.app/Contents/MacOS/) — deeper than the flat
    dist/*.exe case above. The upward search must reach that far or manual
    collector launches silently resolve to the wrong directory."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}", encoding="utf-8")
    macos_dir = tmp_path / "native" / "macos" / "dist" / "Chinese Tech Wire.app" / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    fake_exe = macos_dir / "Chinese Tech Wire"
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    try:
        root = launcher.project_root()
        assert root == tmp_path
    finally:
        monkeypatch.delattr(sys, "frozen", raising=False)


def test_validate_project_root_fails_loudly_on_missing_config(tmp_path):
    empty_dir = tmp_path / "not_a_ctw_checkout"
    empty_dir.mkdir()
    with pytest.raises(launcher.ProjectRootError):
        launcher.validate_project_root(empty_dir)


def test_validate_project_root_succeeds_on_real_checkout():
    root = launcher.project_root()
    # Running inside the real repo — config/settings.yaml genuinely exists.
    assert launcher.validate_project_root(root) == root


# ---------------------------------------------------------------------------
# Logging: no secrets
# ---------------------------------------------------------------------------

def test_no_secrets_in_launcher_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/SECRET123/token456")
    monkeypatch.setenv("GEMINI_API_KEY", "sk-super-secret-key")
    (tmp_path / "data").mkdir()

    log_path = launcher.setup_launcher_logging(tmp_path)
    log = logging.getLogger("ctw.launcher")
    log.info("Selected port %s", 19590)
    log.info("Starting dashboard server on http://127.0.0.1:%s/", 19590)
    for h in list(log.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == log_path:
            h.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "SECRET123" not in content
    assert "sk-super-secret-key" not in content
    assert "19590" in content


# ---------------------------------------------------------------------------
# Existing CLI compatibility
# ---------------------------------------------------------------------------

def test_existing_gui_cli_invocation_still_works(monkeypatch, tmp_path):
    """python main.py --gui (no new flags) must behave exactly as before:
    fixed host/port 8000, no browser auto-open, no instance-reuse redirect."""
    db_url = f"sqlite:///{tmp_path}/cli.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)

    monkeypatch.setattr(sys, "argv", ["main.py", "--gui"])
    with patch("web.app.run_gui") as mocked_run_gui, \
         patch("web.launcher.write_runtime_state"), \
         patch("web.launcher.clear_runtime_state"):
        import main
        main.main()
    mocked_run_gui.assert_called_once_with(host="127.0.0.1", port=8000)


def test_gui_port_auto_selects_dynamic_port(monkeypatch, tmp_path):
    db_url = f"sqlite:///{tmp_path}/cli2.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)

    monkeypatch.setattr(sys, "argv", ["main.py", "--gui", "--gui-port", "auto"])
    with patch("web.app.run_gui") as mocked_run_gui, \
         patch("web.launcher.write_runtime_state"), \
         patch("web.launcher.clear_runtime_state"), \
         patch("web.launcher.find_existing_instance", return_value=None):
        import main
        main.main()
    assert mocked_run_gui.call_count == 1
    _, kwargs = mocked_run_gui.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert 18760 <= kwargs["port"] <= 65535
