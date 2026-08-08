"""Per-source health derivation from SourceRun history + registries.

Classifications are conservative — success with zero records is not HEALTHY.

V0.5.5.1: SourceRun is now layer-aware (NEWS/COMMUNITY/DOCUMENTARY all write
it), and a zero-result run distinguishes two very different situations that
used to look identical:

  - articles_found > 0, articles_new == 0: the source was reached fine and
    returned its normal listing, it just has nothing new right now. That's
    QUIET, not a problem.
  - articles_found == 0 despite a "successful" fetch, OR the run was marked
    soft_blocked (a caught HTTP block/anti-bot interstitial that
    soft_fetch_html swallowed): that's a real signal something is wrong —
    DEGRADED, or BLOCKED for sources with a documented access limitation.

This distinction is what separates a genuinely idle source (e.g. benchlife
between posts) from a genuinely inaccessible one (e.g. hkepc/xfastest
returning HTTP 402/403 from this network).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select

from config import yaml_config
from database.db import get_session
from database.models import Article, CommunityThread, DocumentaryRecord, SourceRun

logger = logging.getLogger(__name__)

# Documented adapter reliability (not live probes). BLOCKED = consistently
# refused at the network/anti-bot layer (verified via direct probe: hkepc
# returns HTTP 402, xfastest returns HTTP 403 / connection reset from this
# environment). PARTIAL = genuinely intermittent (jd sometimes gets through).
# DISABLED = intentionally excluded from the active registry by product
# decision (see community_sources/documentary_sources DISABLED_* sets) —
# not a live probe result, a policy statement.
KNOWN_STATUS = {
    "geekbench": "DISABLED",
    "jd": "PARTIAL",
    "hkepc": "BLOCKED",
    "xfastest": "BLOCKED",
    # ptt: PTT's own web gateway returned HTTP 500 "Server Too Busy" on the
    # board index, hotboards, another board, and 10/10 sampled stored
    # article URLs — confirmed via direct probe, not a CTW-side bug or
    # block. Disabled from active production pending PTT-side recovery;
    # historical data is preserved and still readable everywhere.
    "ptt": "DISABLED",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _health_cfg() -> Dict[str, Any]:
    h = (yaml_config.get("source_health") or {})
    return {
        "fail_streak_failing": int(h.get("fail_streak_failing", 3)),
        "fail_streak_degraded": int(h.get("fail_streak_degraded", 2)),
        "zero_streak_quiet": int(h.get("zero_streak_quiet", 5)),
        "zero_streak_degraded": int(h.get("zero_streak_degraded", 12)),
        "stale_hours": float(h.get("stale_hours", 36)),
        "quiet_hours": float(h.get("quiet_hours", 24)),
        "lookback_runs": int(h.get("lookback_runs", 30)),
    }


def _registries() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    try:
        from sources import SOURCE_REGISTRY
        for name in SOURCE_REGISTRY:
            rows.append({"name": name, "layer": "NEWS"})
    except Exception:
        pass
    try:
        from community_sources import COMMUNITY_REGISTRY, DISABLED_COMMUNITY_SOURCES
        for name in COMMUNITY_REGISTRY:
            rows.append({"name": name, "layer": "COMMUNITY"})
        for name in DISABLED_COMMUNITY_SOURCES:
            rows.append({"name": name, "layer": "COMMUNITY"})
    except Exception:
        pass
    try:
        from documentary_sources import DOCUMENTARY_REGISTRY, DISABLED_DOCUMENTARY_SOURCES
        for name in DOCUMENTARY_REGISTRY:
            rows.append({"name": name, "layer": "DOCUMENTARY"})
        for name in DISABLED_DOCUMENTARY_SOURCES:
            rows.append({"name": name, "layer": "DOCUMENTARY"})
    except Exception:
        pass
    return rows


def classify_source(
    name: str,
    layer: str,
    runs: List[SourceRun],
    counts_24h: int,
    counts_7d: int,
    lifetime_records: int = 0,
) -> Dict[str, Any]:
    cfg = _health_cfg()
    now = _now()

    known = KNOWN_STATUS.get(name)
    if known == "DISABLED":
        return _row(name, layer, "DISABLED", runs, counts_24h, counts_7d, "disabled by product policy")

    if not runs:
        note = "no SourceRun rows"
        if lifetime_records:
            note += (
                f"; {lifetime_records} historical record(s) exist pre-dating telemetry "
                "(no fabricated run history — resolves once this source runs again)"
            )
        return _row(name, layer, "NEVER_PROVEN", runs, counts_24h, counts_7d, note)

    last = runs[0]
    last_attempt = _aware(last.started_at or last.finished_at)
    last_success = next((r for r in runs if r.success), None)
    last_nonzero = next((r for r in runs if (r.articles_new or 0) > 0 and r.success), None)

    fail_streak = 0
    for r in runs:
        if not r.success or (r.request_errors or 0) > 0:
            fail_streak += 1
        else:
            break

    # "new" zero-streak: successful runs with nothing NEW (site reached fine,
    # nothing changed) — this alone is QUIET, not a problem.
    new_zero_streak = 0
    for r in runs:
        if r.success and (r.articles_new or 0) == 0:
            new_zero_streak += 1
        elif r.success:
            break
        else:
            break

    # "found" zero-streak: successful, error-free runs that discovered
    # literally nothing — much stronger signal (possible parser drift /
    # silent structural change on the source's page).
    found_zero_streak = 0
    for r in runs:
        if r.success and not r.soft_blocked and (r.articles_found or 0) == 0:
            found_zero_streak += 1
        elif r.success and not r.soft_blocked:
            break
        else:
            break

    soft_blocked_streak = 0
    for r in runs:
        if r.soft_blocked:
            soft_blocked_streak += 1
        else:
            break

    ages_h = ((now - last_attempt).total_seconds() / 3600.0) if last_attempt else 9999.0
    err = (last.error_message or "")[:200] if last else None

    avg_ms = None
    ms_vals = [r.response_time_ms for r in runs if r.response_time_ms]
    if ms_vals:
        avg_ms = int(sum(ms_vals) / len(ms_vals))

    # Self-healing BLOCKED: only force the documented-blocked label when
    # there is no recent evidence of the block having lifted. If a real
    # non-zero success shows up in the lookback window, trust the evidence
    # over the documented limitation.
    recent_real_success = any(r.success and (r.articles_found or 0) > 0 for r in runs[: cfg["lookback_runs"]])
    if known == "BLOCKED" and not recent_real_success and (fail_streak > 0 or soft_blocked_streak > 0):
        status = "BLOCKED"
        note = err or "consistently refused at network/anti-bot layer (documented)"
        return {
            "source": name,
            "layer": layer,
            "status": status,
            "known_limitation": known,
            "last_attempt": last_attempt,
            "last_success": _aware(last_success.finished_at or last_success.started_at) if last_success else None,
            "last_nonzero": _aware(last_nonzero.finished_at or last_nonzero.started_at) if last_nonzero else None,
            "fail_streak": fail_streak,
            "zero_streak": new_zero_streak,
            "records_24h": counts_24h,
            "records_7d": counts_7d,
            "avg_response_ms": avg_ms,
            "last_error": err,
            "note": note,
        }

    status = "HEALTHY"
    note = ""

    if fail_streak >= cfg["fail_streak_failing"]:
        status = "FAILING"
        note = f"consecutive failures={fail_streak}"
    elif fail_streak >= cfg["fail_streak_degraded"]:
        status = "DEGRADED"
        note = f"consecutive failures={fail_streak}"
    elif ages_h >= cfg["stale_hours"]:
        status = "STALE"
        note = f"last attempt {ages_h:.0f}h ago"
    elif found_zero_streak >= cfg["zero_streak_quiet"]:
        # Clean fetch (no caught errors) that discovered literally nothing,
        # repeatedly — real "something's off" signal (e.g. structural drift
        # on the source's page), unlike a merely-quiet publishing cadence.
        status = "DEGRADED"
        note = f"found=0 on {found_zero_streak} consecutive clean fetches — possible parser drift"
    elif new_zero_streak >= cfg["zero_streak_quiet"] or (counts_24h == 0 and ages_h >= cfg["quiet_hours"]):
        # found > 0 every time (adapter/parser demonstrably working), just
        # nothing NEW — this is a publishing-cadence fact about the source,
        # not a defect, no matter how long it persists.
        status = "QUIET"
        note = f"zero-result streak={new_zero_streak}, 24h_new={counts_24h}"
    elif last.success and (last.articles_new or 0) > 0:
        status = "HEALTHY"
        note = "recent non-zero success"
    elif last.success:
        status = "QUIET"
        note = "last run succeeded with zero new records"
    else:
        status = "DEGRADED"
        note = err or "last run unsuccessful"

    if known == "PARTIAL" and status in ("HEALTHY", "QUIET"):
        # Keep honesty about partial adapters
        note = (note + "; documented PARTIAL").strip("; ")

    return {
        "source": name,
        "layer": layer,
        "status": status,
        "known_limitation": known,
        "last_attempt": last_attempt,
        "last_success": _aware(last_success.finished_at or last_success.started_at) if last_success else None,
        "last_nonzero": _aware(last_nonzero.finished_at or last_nonzero.started_at) if last_nonzero else None,
        "fail_streak": fail_streak,
        "zero_streak": new_zero_streak,
        "records_24h": counts_24h,
        "records_7d": counts_7d,
        "avg_response_ms": avg_ms,
        "last_error": err,
        "note": note,
    }


def _row(name, layer, status, runs, c24, c7, note):
    last = runs[0] if runs else None
    return {
        "source": name,
        "layer": layer,
        "status": status,
        "known_limitation": KNOWN_STATUS.get(name),
        "last_attempt": _aware(last.started_at) if last else None,
        "last_success": None,
        "last_nonzero": None,
        "fail_streak": 0,
        "zero_streak": 0,
        "records_24h": c24,
        "records_7d": c7,
        "avg_response_ms": None,
        "last_error": (last.error_message or "")[:200] if last else None,
        "note": note,
    }


def compute_source_health(source_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    cfg = _health_cfg()
    now = _now()
    cut24 = now - timedelta(hours=24)
    cut7 = now - timedelta(days=7)
    results: List[Dict[str, Any]] = []

    with get_session() as session:
        art_24 = dict(
            session.execute(
                select(Article.source, func.count())
                .where(Article.discovered_at >= cut24)
                .group_by(Article.source)
            ).all()
        )
        art_7 = dict(
            session.execute(
                select(Article.source, func.count())
                .where(Article.discovered_at >= cut7)
                .group_by(Article.source)
            ).all()
        )
        art_life = dict(
            session.execute(select(Article.source, func.count()).group_by(Article.source)).all()
        )
        ct_24 = dict(
            session.execute(
                select(CommunityThread.platform, func.count())
                .where(CommunityThread.discovered_at >= cut24)
                .group_by(CommunityThread.platform)
            ).all()
        )
        ct_7 = dict(
            session.execute(
                select(CommunityThread.platform, func.count())
                .where(CommunityThread.discovered_at >= cut7)
                .group_by(CommunityThread.platform)
            ).all()
        )
        ct_life = dict(
            session.execute(
                select(CommunityThread.platform, func.count()).group_by(CommunityThread.platform)
            ).all()
        )
        try:
            doc_24 = dict(
                session.execute(
                    select(DocumentaryRecord.source, func.count())
                    .where(DocumentaryRecord.first_seen_at >= cut24)
                    .group_by(DocumentaryRecord.source)
                ).all()
            )
            doc_7 = dict(
                session.execute(
                    select(DocumentaryRecord.source, func.count())
                    .where(DocumentaryRecord.first_seen_at >= cut7)
                    .group_by(DocumentaryRecord.source)
                ).all()
            )
            doc_life = dict(
                session.execute(
                    select(DocumentaryRecord.source, func.count()).group_by(DocumentaryRecord.source)
                ).all()
            )
        except Exception:
            doc_24, doc_7, doc_life = {}, {}, {}

        for meta in _registries():
            name, layer = meta["name"], meta["layer"]
            if source_filter and name != source_filter:
                continue
            runs = list(
                session.execute(
                    select(SourceRun)
                    .where(SourceRun.source == name)
                    .order_by(desc(SourceRun.started_at))
                    .limit(cfg["lookback_runs"])
                ).scalars().all()
            )
            if layer == "NEWS":
                c24, c7, clife = art_24.get(name, 0), art_7.get(name, 0), art_life.get(name, 0)
            elif layer == "COMMUNITY":
                c24, c7, clife = ct_24.get(name, 0), ct_7.get(name, 0), ct_life.get(name, 0)
            else:
                c24, c7, clife = doc_24.get(name, 0), doc_7.get(name, 0), doc_life.get(name, 0)
            results.append(classify_source(name, layer, runs, int(c24), int(c7), int(clife)))

    order = {"FAILING": 0, "DEGRADED": 1, "BLOCKED": 2, "STALE": 3, "QUIET": 4, "NEVER_PROVEN": 5, "PARTIAL": 6, "HEALTHY": 7, "DISABLED": 8}
    results.sort(key=lambda r: (order.get(r["status"], 9), r["layer"], r["source"]))
    return results


def format_source_health_text(rows: Optional[List[Dict[str, Any]]] = None, detailed: bool = False) -> str:
    rows = rows or compute_source_health()
    if detailed and len(rows) == 1:
        r = rows[0]
        la = r["last_attempt"].strftime("%Y-%m-%d %H:%M UTC") if r["last_attempt"] else "—"
        ls = r["last_success"].strftime("%Y-%m-%d %H:%M UTC") if r["last_success"] else "—"
        ln = r["last_nonzero"].strftime("%Y-%m-%d %H:%M UTC") if r["last_nonzero"] else "—"
        lines = [
            f"SOURCE HEALTH — {r['source']}",
            "-" * 72,
            f"layer:              {r['layer']}",
            f"status:             {r['status']}",
            f"known_limitation:   {r['known_limitation'] or '—'}",
            f"last_attempt:       {la}",
            f"last_success:       {ls}",
            f"last_nonzero:       {ln}",
            f"zero_streak:        {r['zero_streak']}",
            f"fail_streak:        {r['fail_streak']}",
            f"records_24h:        {r['records_24h']}",
            f"records_7d:         {r['records_7d']}",
            f"avg_response_ms:    {r['avg_response_ms'] if r['avg_response_ms'] is not None else '—'}",
            f"last_error:         {r['last_error'] or '—'}",
            f"note:               {r['note']}",
        ]
        return "\n".join(lines)

    lines = ["SOURCE HEALTH", "-" * 72]
    for r in rows:
        la = r["last_attempt"].strftime("%Y-%m-%d %H:%M") if r["last_attempt"] else "—"
        lines.append(
            f"{r['source']:<12} {r['layer']:<11} {r['status']:<12} "
            f"24h={r['records_24h']:<4} 7d={r['records_7d']:<5} "
            f"fail={r['fail_streak']} zero={r['zero_streak']} "
            f"last={la} {r['note']}"
        )
    return "\n".join(lines)
