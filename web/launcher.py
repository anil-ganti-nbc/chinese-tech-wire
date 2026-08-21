"""V0.5.6.1 — dashboard launcher support.

Pure support code shared by `python main.py --gui` and the packaged
ChineseTechWire.exe: dynamic localhost port selection, a small runtime
state file so a second launch can find (and reuse) an already-running
instance instead of spawning a duplicate, and a readiness poll used before
opening the browser. No new GUI, no bundled browser, no caching
infrastructure — this is process/socket bookkeeping only.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from security.redaction import protect_handler

logger = logging.getLogger("ctw.launcher")

# Dedicated CTW range, tried before falling back to an OS-assigned ephemeral
# port. Keeps CTW's port choice predictable/discoverable across launches
# without hard-coding one number that another local tracker might already hold.
PREFERRED_PORT_RANGE = range(18760, 18800)
RUNTIME_STATE_FILENAME = "dashboard_runtime.json"


_ROOT_MARKER = Path("config") / "settings.yaml"
_ROOT_SEARCH_LEVELS = 4


def _find_root_upward(start: Path, max_levels: int = _ROOT_SEARCH_LEVELS) -> Optional[Path]:
    """Walk up from `start` looking for config/settings.yaml. Handles the
    build deliverable (dist/ChineseTechWire.exe) being double-clicked in
    place, one level below the real project root, without the operator
    having to move it first."""
    candidate = start
    for _ in range(max_levels + 1):
        if (candidate / _ROOT_MARKER).exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def project_root() -> Path:
    """Resolve the CTW project root whether running from source or from a
    PyInstaller-frozen executable.

    Frozen: sys.executable points at the real .exe location (not the
    PyInstaller temp extraction dir). The operator is expected to keep
    ChineseTechWire.exe somewhere under the project root — either directly
    in it, or in dist/ (the build output location) — so this searches
    upward for config/settings.yaml rather than assuming the exe's
    immediate directory. Falls back to the immediate directory if nothing
    is found, so validate_project_root() can still report a clear error.
    """
    if getattr(sys, "frozen", False):
        start = Path(sys.executable).resolve().parent
    else:
        start = Path(__file__).resolve().parent.parent
    return _find_root_upward(start) or start


def bundled_resource_root() -> Path:
    """Where bundled code assets (templates/, static/) live: the PyInstaller
    extraction dir when frozen, otherwise the same as project_root()."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return project_root()


def runtime_state_path(root: Optional[Path] = None) -> Path:
    root = root or project_root()
    return root / "data" / RUNTIME_STATE_FILENAME


class ProjectRootError(RuntimeError):
    """Raised when the resolved project root doesn't look like a real CTW
    checkout — surfaced loudly instead of letting init_db() silently create
    a brand-new, empty database in the wrong place."""


def validate_project_root(root: Optional[Path] = None) -> Path:
    root = root or project_root()
    marker = root / "config" / "settings.yaml"
    if not marker.exists():
        raise ProjectRootError(
            f"Could not find CTW project files at {root} (expected {marker} to exist, "
            f"checked it and up to {_ROOT_SEARCH_LEVELS} parent director{'y' if _ROOT_SEARCH_LEVELS == 1 else 'ies'}). "
            "Keep ChineseTechWire.exe somewhere under the CTW project root — directly in "
            "it, or in dist/ — alongside data/, config/, .env, and logs/; it must not be "
            "run from a folder with no relation to the project."
        )
    return root


def setup_launcher_logging(root: Optional[Path] = None) -> Path:
    """Configure a dedicated launcher log file. Only ever logs port/status/
    lifecycle events — never env vars, .env contents, or any secret value."""
    root = root or project_root()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "dashboard-launcher.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    protect_handler(handler)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return log_path


def open_when_ready(host: str, port: int, timeout: float = 15.0) -> bool:
    """Block until CTW is ready (or timeout), then open the browser.
    Returns True iff the browser was actually opened. Separated from the
    background-thread wiring in main.py so it's directly unit-testable."""
    if not wait_for_ready(host, port, timeout=timeout):
        logger.warning("Dashboard did not become ready within %.1fs — not opening browser", timeout)
        return False
    opened = open_browser(host, port)
    logger.info("Browser open result for http://%s:%s/: %s", host, port, opened)
    return opened


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------

def is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_port(host: str = "127.0.0.1", preferred_range=PREFERRED_PORT_RANGE) -> int:
    """First genuinely-free port in the preferred CTW range. Falls back to
    an OS-assigned ephemeral port if the whole range is occupied (e.g. by
    the operator's other local trackers) — never assumes a port is free
    just because no other CTW process is using it.
    """
    for port in preferred_range:
        if is_port_free(host, port):
            return port
    logger.warning("Preferred CTW port range %s-%s fully occupied — using an OS-assigned port",
                    preferred_range.start, preferred_range.stop - 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Readiness + identity
# ---------------------------------------------------------------------------

def probe_healthz(host: str, port: int, timeout: float = 1.0) -> Optional[dict]:
    """One-shot /healthz check. Returns the parsed body if it identifies as
    CTW, else None. Never raises."""
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
            if body.get("application") == "ChineseTechWire":
                return body
            return None
    except Exception:
        return None


def wait_for_ready(host: str, port: int, timeout: float = 15.0, interval: float = 0.2) -> bool:
    """Poll /healthz until CTW responds or timeout. No fixed sleep — the
    browser must only open once the server is actually accepting connections."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_healthz(host, port, timeout=1.0):
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Runtime state (single-instance discovery)
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True  # can't tell — don't falsely declare it dead
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def write_runtime_state(host: str, port: int, root: Optional[Path] = None) -> Path:
    path = runtime_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def read_runtime_state(root: Optional[Path] = None) -> Optional[dict]:
    path = runtime_state_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_runtime_state(root: Optional[Path] = None) -> None:
    path = runtime_state_path(root)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def find_existing_instance(root: Optional[Path] = None) -> Optional[dict]:
    """Return {'host', 'port'} of a genuinely-live CTW instance, or None.

    A saved state file is trusted only after two independent checks: the
    recorded PID is still alive, AND the recorded host:port answers
    /healthz as CTW right now. Either failing means the state is stale
    (crash, port reused by something else) — it's cleared, not trusted.
    """
    state = read_runtime_state(root)
    if not state:
        return None
    pid, host, port = state.get("pid"), state.get("host"), state.get("port")
    if not (pid and host and port):
        clear_runtime_state(root)
        return None
    if not _pid_alive(int(pid)):
        clear_runtime_state(root)
        return None
    if not probe_healthz(host, int(port), timeout=1.5):
        clear_runtime_state(root)
        return None
    return {"host": host, "port": int(port)}


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def open_browser(host: str, port: int) -> bool:
    import webbrowser
    url = f"http://{host}:{port}/"
    try:
        return bool(webbrowser.open(url))
    except Exception:
        logger.exception("Failed to open browser for %s", url)
        return False
