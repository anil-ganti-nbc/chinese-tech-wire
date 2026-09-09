"""Translation spend telemetry + budget circuit breaker.

A physically separate SQLite store (data/translation_cost.db) — the live
fleet database's schema is governed and must never be casually mutated for
telemetry. Only aggregate usage numbers are persisted here: token counts
and billed cost per provider call. No prompt text, no translation content,
no API keys ever enter this file.

The budget breaker makes accidental paid inference impossible to do
quietly: before any paid call the caller asks `ensure_within_budget()`,
which compares the current run's and the UTC day's actual billed cost
(from OpenRouter's usage.cost, never an estimate against max_tokens)
against small ceilings (defaults: $0.05/run, $0.25/day, env-overridable).
Past a ceiling, every further translation request is refused with a single
COST_BUDGET_EXCEEDED warning until the window resets; ingestion continues
and records simply stay pending.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_RUN_BUDGET_USD = 0.05
DEFAULT_DAILY_BUDGET_USD = 0.25

DB_PATH = Path(DATA_DIR) / "translation_cost.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL
);
CREATE INDEX IF NOT EXISTS usage_events_ts_idx ON usage_events(ts DESC);
"""

# Process-local run window (a "run" = one process invocation, e.g. one
# --translate-backfill pass or one scheduled cycle).
_RUN_STARTED = datetime.now(timezone.utc).astimezone(timezone.utc).isoformat()

_warned_budget = {"run": False, "day": False}


def _iso() -> str:
    return datetime.now(timezone.utc).astimezone(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _run_budget() -> float:
    try:
        return float(os.environ.get("CTW_TRANSLATION_RUN_BUDGET_USD", DEFAULT_RUN_BUDGET_USD))
    except ValueError:
        return DEFAULT_RUN_BUDGET_USD


def _daily_budget() -> float:
    try:
        return float(os.environ.get("CTW_TRANSLATION_DAILY_BUDGET_USD", DEFAULT_DAILY_BUDGET_USD))
    except ValueError:
        return DEFAULT_DAILY_BUDGET_USD


def record_usage(provider: str, model: str, usage: dict) -> None:
    """Persist one call's aggregate usage numbers. Fail-open: telemetry
    must never break a translation (or an ingestion run)."""
    try:
        details_prompt = (usage.get("prompt_tokens_details") or {}) if isinstance(usage, dict) else {}
        details_completion = (usage.get("completion_tokens_details") or {}) if isinstance(usage, dict) else {}
        cost = usage.get("cost") if isinstance(usage, dict) else None
        with _connect() as con:
            con.execute(
                "INSERT INTO usage_events(ts, provider, model, prompt_tokens, completion_tokens,"
                " reasoning_tokens, cached_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                (
                    _iso(), provider, model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(details_completion.get("reasoning_tokens") or 0),
                    int(details_prompt.get("cached_tokens") or 0),
                    float(cost) if cost is not None else None,
                ),
            )
    except Exception as e:  # noqa: BLE001 — telemetry never fails a call
        logger.debug("[TRANSLATE] usage telemetry store failed: %s", e)


def totals_since(iso_since: str) -> dict:
    """Aggregate billed usage since a UTC ISO timestamp. Cost falls back to
    0.0 for any row where OpenRouter did not supply usage.cost."""
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT COUNT(*) n,"
                " COALESCE(SUM(prompt_tokens), 0) prompt_tokens,"
                " COALESCE(SUM(completion_tokens), 0) completion_tokens,"
                " COALESCE(SUM(reasoning_tokens), 0) reasoning_tokens,"
                " COALESCE(SUM(cached_tokens), 0) cached_tokens,"
                " COALESCE(SUM(cost_usd), 0.0) cost_usd"
                " FROM usage_events WHERE ts >= ?",
                (iso_since,),
            ).fetchone()
        return {
            "requests": row["n"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "reasoning_tokens": row["reasoning_tokens"],
            "cached_tokens": row["cached_tokens"],
            "cost_usd": round(row["cost_usd"], 6),
        }
    except Exception:  # noqa: BLE001 — reporting must never crash the CLI
        return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "reasoning_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}


def _midnight_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def ensure_within_budget(*, budget_override_usd: float | None = None) -> None:
    """Raise RuntimeError('COST_BUDGET_EXCEEDED …') when this run or this
    UTC day has already spent past its ceiling. One warning per window,
    never one per refused call."""
    run_budget = float(budget_override_usd) if budget_override_usd is not None else _run_budget()
    daily_budget = _daily_budget()

    run = totals_since(_RUN_STARTED)
    if run_budget is not None and run_budget >= 0 and run["cost_usd"] >= run_budget:
        if not _warned_budget["run"]:
            logger.warning(
                "[TRANSLATE] COST_BUDGET_EXCEEDED run spend $%.4f >= $%.2f ceiling; "
                "further translation refused this run (records stay pending)",
                run["cost_usd"], run_budget,
            )
            _warned_budget["run"] = True
        raise RuntimeError(
            f"COST_BUDGET_EXCEEDED: run spend ${run['cost_usd']:.4f} >= ${run_budget:.2f}"
        )

    day = totals_since(_midnight_utc())
    if day["cost_usd"] >= daily_budget:
        if not _warned_budget["day"]:
            logger.warning(
                "[TRANSLATE] COST_BUDGET_EXCEEDED daily spend $%.4f >= $%.2f ceiling; "
                "further translation refused until the UTC day resets",
                day["cost_usd"], daily_budget,
            )
            _warned_budget["day"] = True
        raise RuntimeError(
            f"COST_BUDGET_EXCEEDED: daily spend ${day['cost_usd']:.4f} >= ${daily_budget:.2f}"
        )


def cost_report() -> dict:
    """Operator report: current run, last 24h, and the UTC day so far."""
    now = datetime.now(timezone.utc)
    day = totals_since(_midnight_utc())
    last24 = totals_since((now - timedelta(hours=24)).isoformat())
    return {
        "current_run": totals_since(_RUN_STARTED),
        "last_24h": last24,
        "utc_day_so_far": day,
        "budgets": {"run_usd": _run_budget(), "daily_usd": _daily_budget()},
    }
