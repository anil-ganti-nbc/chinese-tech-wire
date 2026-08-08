"""V0.5.7 — best-effort, read-only Windows Task Scheduler status.

Reuses the exact task name scripts/install_scheduler.ps1 creates
("ChineseTechWire"). Never modifies the task — query only, via schtasks.exe
(no PowerShell dependency, no pywin32). Safe on non-Windows: reports
UNAVAILABLE with a clear reason rather than pretending to know anything.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger("ctw.scheduler_status")

DEFAULT_TASK_NAME = "ChineseTechWire"
_QUERY_TIMEOUT_SECONDS = 5.0


def get_scheduler_status(task_name: str = DEFAULT_TASK_NAME) -> Dict[str, Any]:
    """Returns a dict with at least {"available": bool, "reason": str|None}.
    On success also includes status/next_run/last_run/last_result/state.
    Never raises — any failure degrades to available=False with a reason.
    """
    if sys.platform != "win32":
        return {
            "available": False,
            "reason": "not running on Windows",
            "task_name": task_name,
        }

    try:
        proc = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "schtasks.exe not found", "task_name": task_name}
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "schtasks query timed out", "task_name": task_name}
    except Exception as e:
        return {"available": False, "reason": f"schtasks query failed: {e}", "task_name": task_name}

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        reason = "task not found" if "cannot find" in stderr.lower() or "does not exist" in stderr.lower() else (
            stderr[:200] or f"schtasks exited {proc.returncode}"
        )
        return {"available": False, "reason": reason, "task_name": task_name}

    fields = _parse_schtasks_list(proc.stdout)
    if not fields:
        return {"available": False, "reason": "could not parse schtasks output", "task_name": task_name}

    return {
        "available": True,
        "reason": None,
        "task_name": task_name,
        "status": fields.get("Status") or fields.get("Scheduled Task State"),
        "next_run": fields.get("Next Run Time"),
        "last_run": fields.get("Last Run Time"),
        "last_result": fields.get("Last Result"),
    }


def _parse_schtasks_list(output: str) -> Optional[Dict[str, str]]:
    """Parses `schtasks /FO LIST /V` output (simple 'Key:    Value' lines)."""
    fields: Dict[str, str] = {}
    for line in (output or "").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            fields[key] = value
    return fields or None
