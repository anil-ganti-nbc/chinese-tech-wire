"""FastAPI newsroom GUI — reads existing SQLite; does not rescore/rescrape on page load."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from markupsafe import escape

from database.db import get_session, init_db
from database.models import (
    LeadNotification,
    SourceRun,

    Article,
    CommunityThread,
    DocumentaryEvent,
    DocumentaryRecord,
    IngestionRun,
    LeadEvent,
    LeadFeedback,
    StoryCluster,
    StoryLead,
)
from pipeline.newsroom import add_feedback, explain_lead
from pipeline.explain import explain_lead_structured, build_lead_timeline

logger = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

# Jinja: autoescape is on by default for HTML

app = FastAPI(title="Chinese Tech Wire Newsroom", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_hours(raw: Optional[str]) -> Optional[float]:
    """Blank/missing 'hours' means no filter — FastAPI's own Optional[float]
    still 422s on a present-but-empty query value (e.g. the filter form
    submitting hours=), so this is parsed manually as a raw string instead.
    A genuinely invalid non-empty value is rejected explicitly (422), not
    silently dropped or coerced."""
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid hours value: {raw!r}")


def _age_str(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    dt = _aware(dt)
    secs = max(0, int((_now() - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _fmt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    dt = _aware(dt)
    return dt.strftime("%m-%d %H:%M")


templates.env.globals["age_str"] = _age_str
templates.env.globals["fmt"] = _fmt
templates.env.globals["now"] = _now


STATUS_ORDER = {
    "ACTIONABLE": 0,
    "ESCALATED": 1,
    "WATCHING": 2,
    "NEW": 3,
    "STALE": 4,
    "RESOLVED": 5,
    "DISMISSED": 6,
}


# ---------------------------------------------------------------------------
# Runtime identification (V0.5.6.1 dashboard launcher)
# ---------------------------------------------------------------------------

CTW_VERSION = "0.5.6.1"


@app.get("/healthz")
def healthz():
    """Cheap identity probe for the dashboard launcher — confirms a given
    host:port is genuinely this CTW instance before trusting a saved runtime
    state file. No scraping, no scoring, no DB writes, no secrets."""
    return {"application": "ChineseTechWire", "version": CTW_VERSION, "status": "ok"}


# ---------------------------------------------------------------------------
# Newsroom
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
@app.get("/newsroom", response_class=HTMLResponse)
def newsroom(
    request: Request,
    status: Optional[str] = None,
    lead_type: Optional[str] = None,
    q: Optional[str] = None,
    hours: Optional[str] = None,
    freshness: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=200),
    hide_written: int = Query(1, ge=0, le=1),
):
    from pipeline.freshness import active_cutoff

    hours_val = _parse_hours(hours)
    freshness_val = (freshness or "active").lower()
    if freshness_val not in ("active", "archived", "all"):
        freshness_val = "active"

    with get_session() as session:
        query = select(StoryLead)
        if status:
            query = query.where(StoryLead.lead_status == status.upper())
        else:
            # default: hide resolved/dismissed unless filtered
            query = query.where(StoryLead.lead_status.notin_(["DISMISSED"]))
            if hide_written:
                query = query.where(StoryLead.lead_status != "RESOLVED")
        if lead_type:
            query = query.where(StoryLead.lead_type == lead_type.upper())
        if hours_val:
            # An explicit hours filter is a more specific, independent time
            # window — it fully controls recency, so the freshness
            # active/archived default is not also applied on top of it.
            cutoff = _now() - timedelta(hours=hours_val)
            query = query.where(StoryLead.last_activity_at >= cutoff)
        elif freshness_val == "active":
            # SQL-filtered, not loaded-then-sliced. NULL last_activity_at is
            # never silently hidden — missing data stays visible.
            cutoff = active_cutoff()
            query = query.where(or_(StoryLead.last_activity_at >= cutoff, StoryLead.last_activity_at.is_(None)))
        elif freshness_val == "archived":
            cutoff = active_cutoff()
            query = query.where(StoryLead.last_activity_at < cutoff)
        # freshness_val == "all": no time filter
        if q:
            like = f"%{q}%"
            query = query.where(
                or_(
                    StoryLead.headline_hint.ilike(like),
                    StoryLead.why_now.ilike(like),
                    StoryLead.evidence_summary.ilike(like),
                )
            )
        # fetch then sort in Python for status priority (SQLite limited).
        # Sort primarily by last_activity_at (real new-evidence timestamp,
        # not routine-rebuild updated_at churn) so an old lead can't jump to
        # the top merely because a background rebuild touched its row.
        rows = list(session.execute(query).scalars().all())
        rows.sort(
            key=lambda L: (
                STATUS_ORDER.get(L.lead_status, 9),
                -(_aware(L.last_activity_at) or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
                -(L.priority_score or 0),
            )
        )
        total = len(rows)
        start = (page - 1) * per_page
        page_rows = rows[start : start + per_page]

        # summary counts
        all_leads = session.execute(select(StoryLead)).scalars().all()
        summary = {
            "actionable": sum(1 for x in all_leads if x.lead_status == "ACTIONABLE"),
            "watching": sum(1 for x in all_leads if x.lead_status == "WATCHING"),
            "new_24h": sum(
                1
                for x in all_leads
                if x.created_at and _aware(x.created_at) >= _now() - timedelta(hours=24)
            ),
            "written": sum(1 for x in all_leads if x.lead_status == "RESOLVED"),
            "stale": sum(1 for x in all_leads if x.lead_status == "STALE"),
        }
        fb = session.execute(select(LeadFeedback)).scalars().all()
        summary["false_positives"] = sum(1 for f in fb if f.feedback == "FALSE_POSITIVE")
        summary["useful"] = sum(1 for f in fb if f.feedback == "USEFUL")

        # latest feedback per lead
        fb_map: Dict[int, str] = {}
        for f in sorted(fb, key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc)):
            fb_map[f.lead_id] = f.feedback

        # primary external source URL per lead, for the SOURCE column —
        # page-scoped (<=200 rows), never invents a URL (see pipeline.explain).
        from pipeline.explain import primary_source_url
        source_url_map: Dict[int, Optional[str]] = {
            L.id: primary_source_url(L, session=session) for L in page_rows
        }

    return templates.TemplateResponse(
        request,
        "newsroom.html",
        {
            "leads": page_rows,
            "summary": summary,
            "page": page,
            "per_page": per_page,
            "total": total,
            "status": status or "",
            "lead_type": lead_type or "",
            "q": q or "",
            "hours": hours_val,
            "freshness": freshness_val,
            "hide_written": hide_written,
            "fb_map": fb_map,
            "source_url_map": source_url_map,
            "active": "newsroom",
        },
    )


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int):
    with get_session() as session:
        lead = session.get(StoryLead, lead_id)
        if not lead:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": f"Lead #{lead_id} not found", "active": "newsroom"},
                status_code=404,
            )
        events = list(
            session.execute(
                select(LeadEvent)
                .where(LeadEvent.lead_id == lead_id)
                .order_by(LeadEvent.observed_at.asc())
            ).scalars().all()
        )
        feedbacks = list(
            session.execute(
                select(LeadFeedback)
                .where(LeadFeedback.lead_id == lead_id)
                .order_by(LeadFeedback.created_at.desc())
            ).scalars().all()
        )
        cluster = session.get(StoryCluster, lead.cluster_id) if lead.cluster_id else None
        arts, cts, docs = [], [], []
        if lead.cluster_id:
            arts = list(
                session.execute(
                    select(Article)
                    .where(Article.duplicate_group_id == lead.cluster_id)
                    .order_by(Article.discovered_at.asc())
                ).scalars().all()
            )
            cts = list(
                session.execute(
                    select(CommunityThread)
                    .where(CommunityThread.story_cluster_id == lead.cluster_id)
                    .order_by(CommunityThread.discovered_at.asc())
                ).scalars().all()
            )
            docs = list(
                session.execute(
                    select(DocumentaryRecord)
                    .where(DocumentaryRecord.story_cluster_id == lead.cluster_id)
                    .order_by(DocumentaryRecord.first_seen_at.asc())
                ).scalars().all()
            )

        # provenance timeline
        timeline = []
        for c in cts:
            timeline.append({
                "t": _aware(c.first_signal_at or c.created_at or c.discovered_at),
                "layer": "COMMUNITY",
                "source": c.platform,
                "title": c.title_original,
                "url": c.url,
                "extra": c.signal_type,
            })
        for d in docs:
            timeline.append({
                "t": _aware(d.first_seen_at or d.observed_at),
                "layer": "DOCUMENTARY",
                "source": d.source,
                "title": d.title or d.source_record_id,
                "url": d.url,
                "extra": d.record_type,
            })
        for a in arts:
            timeline.append({
                "t": _aware(a.published_at or a.discovered_at),
                "layer": "NEWS",
                "source": a.source,
                "title": a.title_english or a.title_original,
                "title_orig": a.title_original,
                "url": a.url,
                "extra": "article",
            })
        timeline = [x for x in timeline if x["t"]]
        timeline.sort(key=lambda x: x["t"])

        # lead times
        lead_times = {}
        if cluster:
            fs, fd, fm = (
                _aware(cluster.first_signal_at),
                _aware(cluster.first_documentary_at),
                _aware(cluster.first_media_at),
            )
            if fs and fd:
                lead_times["signal_to_doc"] = int((fd - fs).total_seconds() / 60)
            if fs and fm:
                lead_times["signal_to_media"] = int((fm - fs).total_seconds() / 60)
            if fd and fm:
                lead_times["doc_to_media"] = int((fm - fd).total_seconds() / 60)

        structured = explain_lead_structured(lead_id)
        explanation = structured.human_text if structured else explain_lead(lead_id)
        structured_dict = structured.to_dict() if structured else None
        tl_events = build_lead_timeline(lead_id)
        timeline_structured = [
            {"t": e.timestamp, "layer": e.layer, "source": e.source, "title": e.title,
             "url": e.url, "extra": e.event_type + ((" · " + e.summary) if e.summary else ""),
             "title_orig": None, "event_type": e.event_type, "inferred": e.inferred}
            for e in tl_events
        ]
        breakdown = lead.score_breakdown or {}

        # detach-friendly copies
        lead_id_v = lead.id
        latest_fb = feedbacks[0].feedback if feedbacks else None

        from pipeline.outcomes import outcome_history, OUTCOME_VALUES
        outcomes = outcome_history(lead_id, session=session)
        current_outcome_row = outcomes[0] if outcomes else None

    # Prefer structured timeline when available
    display_timeline = timeline_structured if timeline_structured else [
        {**item, "t": item["t"].isoformat() if hasattr(item.get("t"), "isoformat") else item.get("t")}
        for item in timeline
    ]

    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "lead": lead,
            "events": events,
            "feedbacks": feedbacks,
            "latest_fb": latest_fb,
            "timeline": display_timeline,
            "lead_times": lead_times,
            "cluster": cluster,
            "explanation": explanation,
            "breakdown": breakdown,
            "structured": structured_dict,
            "outcomes": outcomes,
            "current_outcome": current_outcome_row,
            "outcome_values": sorted(OUTCOME_VALUES),
            "active": "newsroom",
        },
    )


@app.post("/leads/{lead_id}/feedback")
def lead_feedback_post(lead_id: int, feedback: str = Form(...)):
    ok = add_feedback(lead_id, feedback.strip())
    if not ok:
        return RedirectResponse(f"/leads/{lead_id}?err=1", status_code=303)
    return RedirectResponse(f"/leads/{lead_id}?fb=1", status_code=303)


# ---------------------------------------------------------------------------
# News / Community / Documentary / Clusters
# ---------------------------------------------------------------------------

@app.get("/news", response_class=HTMLResponse)
def news_view(
    request: Request,
    source: Optional[str] = None,
    q: Optional[str] = None,
    hours: Optional[float] = Query(48),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=10, le=200),
):
    with get_session() as session:
        query = select(Article)
        if source:
            query = query.where(Article.source == source)
        if hours:
            query = query.where(Article.discovered_at >= _now() - timedelta(hours=hours))
        if q:
            like = f"%{q}%"
            query = query.where(
                or_(Article.title_original.ilike(like), Article.title_english.ilike(like))
            )
        query = query.order_by(desc(Article.discovered_at))
        all_rows = list(session.execute(query).scalars().all())
        total = len(all_rows)
        rows = all_rows[(page - 1) * per_page : page * per_page]
        sources = sorted({r for r in session.execute(select(Article.source)).scalars().all() if r})
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "articles": rows,
            "page": page,
            "per_page": per_page,
            "total": total,
            "source": source or "",
            "q": q or "",
            "hours": hours,
            "sources": sources,
            "active": "news",
        },
    )


@app.get("/community", response_class=HTMLResponse)
def community_view(
    request: Request,
    platform: Optional[str] = None,
    signal: Optional[str] = None,
    hours: Optional[float] = Query(72),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=10, le=200),
):
    with get_session() as session:
        query = select(CommunityThread)
        if platform:
            query = query.where(CommunityThread.platform == platform)
        if signal:
            query = query.where(CommunityThread.signal_type == signal)
        if hours:
            query = query.where(
                CommunityThread.discovered_at >= _now() - timedelta(hours=hours)
            )
        query = query.order_by(desc(CommunityThread.priority_score))
        all_rows = list(session.execute(query).scalars().all())
        total = len(all_rows)
        rows = all_rows[(page - 1) * per_page : page * per_page]
    return templates.TemplateResponse(
        request,
        "community.html",
        {
            "threads": rows,
            "page": page,
            "per_page": per_page,
            "total": total,
            "platform": platform or "",
            "signal": signal or "",
            "hours": hours,
            "active": "community",
        },
    )


@app.get("/documentary", response_class=HTMLResponse)
def documentary_view(
    request: Request,
    source: Optional[str] = None,
    hours: Optional[float] = Query(168),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=10, le=200),
):
    with get_session() as session:
        query = select(DocumentaryRecord)
        if source:
            query = query.where(DocumentaryRecord.source == source)
        if hours:
            query = query.where(
                DocumentaryRecord.first_seen_at >= _now() - timedelta(hours=hours)
            )
        query = query.order_by(desc(DocumentaryRecord.priority_score))
        all_rows = list(session.execute(query).scalars().all())
        total = len(all_rows)
        rows = all_rows[(page - 1) * per_page : page * per_page]
        # events for visible records
        ids = [r.id for r in rows]
        events = []
        if ids:
            events = list(
                session.execute(
                    select(DocumentaryEvent)
                    .where(DocumentaryEvent.record_id.in_(ids))
                    .order_by(DocumentaryEvent.observed_at.desc())
                    .limit(50)
                ).scalars().all()
            )
    return templates.TemplateResponse(
        request,
        "documentary.html",
        {
            "records": rows,
            "events": events,
            "page": page,
            "per_page": per_page,
            "total": total,
            "source": source or "",
            "hours": hours,
            "active": "documentary",
        },
    )


@app.get("/clusters", response_class=HTMLResponse)
def clusters_view(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
):
    with get_session() as session:
        all_rows = list(
            session.execute(
                select(StoryCluster).order_by(desc(StoryCluster.updated_at))
            ).scalars().all()
        )
        total = len(all_rows)
        rows = all_rows[(page - 1) * per_page : page * per_page]
        # counts
        counts = {}
        for c in rows:
            n_news = session.execute(
                select(func.count()).select_from(Article).where(
                    Article.duplicate_group_id == c.id
                )
            ).scalar() or 0
            n_comm = session.execute(
                select(func.count()).select_from(CommunityThread).where(
                    CommunityThread.story_cluster_id == c.id
                )
            ).scalar() or 0
            n_doc = session.execute(
                select(func.count()).select_from(DocumentaryRecord).where(
                    DocumentaryRecord.story_cluster_id == c.id
                )
            ).scalar() or 0
            counts[c.id] = {"news": n_news, "community": n_comm, "documentary": n_doc}
    return templates.TemplateResponse(
        request,
        "clusters.html",
        {
            "clusters": rows,
            "counts": counts,
            "page": page,
            "per_page": per_page,
            "total": total,
            "active": "clusters",
        },
    )


@app.get("/clusters/{cluster_id}", response_class=HTMLResponse)
def cluster_detail(request: Request, cluster_id: int):
    # reuse lead if exists
    with get_session() as session:
        lead = session.execute(
            select(StoryLead).where(StoryLead.cluster_id == cluster_id)
        ).scalar_one_or_none()
        if lead:
            return RedirectResponse(f"/leads/{lead.id}", status_code=302)
        cluster = session.get(StoryCluster, cluster_id)
        if not cluster:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": f"Cluster #{cluster_id} not found", "active": "clusters"},
                status_code=404,
            )
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "message": f"Cluster #{cluster_id} has no StoryLead yet. Run: python main.py --rebuild-leads",
            "active": "clusters",
        },
    )


# ---------------------------------------------------------------------------
# Health / Activity / Soak
# ---------------------------------------------------------------------------

@app.get("/health", response_class=HTMLResponse)
def health_view(request: Request, runs_offset: int = Query(0, ge=0)):
    from pipeline.operations import DEFAULT_RECENT_LIMIT
    with get_session() as session:
        n_art = session.execute(select(func.count()).select_from(Article)).scalar() or 0
        n_ct = session.execute(select(func.count()).select_from(CommunityThread)).scalar() or 0
        n_doc = session.execute(select(func.count()).select_from(DocumentaryRecord)).scalar() or 0
        n_lead = session.execute(select(func.count()).select_from(StoryLead)).scalar() or 0
        last_art = session.execute(
            select(Article).order_by(desc(Article.discovered_at)).limit(1)
        ).scalar_one_or_none()
        last_ct = session.execute(
            select(CommunityThread).order_by(desc(CommunityThread.discovered_at)).limit(1)
        ).scalar_one_or_none()
        last_doc = session.execute(
            select(DocumentaryRecord).order_by(desc(DocumentaryRecord.observed_at)).limit(1)
        ).scalar_one_or_none()

        src_counts = {}
        for a in session.execute(select(Article)).scalars().all():
            src_counts[a.source] = src_counts.get(a.source, 0) + 1
        plat_counts = {}
        for c in session.execute(select(CommunityThread)).scalars().all():
            plat_counts[c.platform] = plat_counts.get(c.platform, 0) + 1
        doc_counts = {}
        for d in session.execute(select(DocumentaryRecord)).scalars().all():
            doc_counts[d.source] = doc_counts.get(d.source, 0) + 1

    from pipeline.operations import runtime_snapshot
    snapshot = runtime_snapshot(recent_limit=DEFAULT_RECENT_LIMIT, recent_offset=runs_offset)

    import os
    discord = "CONFIGURED" if os.getenv("DISCORD_WEBHOOK_URL") else "NOT CONFIGURED"
    trans = os.getenv("TRANSLATION_PROVIDER", "noop")
    gemini_key = bool(os.getenv("GEMINI_API_KEY"))
    translation_status = f"{trans}" + (" (key set)" if gemini_key and trans == "gemini" else "")
    if trans == "gemini" and not gemini_key:
        translation_status = "gemini — NO KEY (noop fallback)"

    from pipeline.source_health import compute_source_health
    try:
        source_health_rows = compute_source_health()
    except Exception:
        source_health_rows = []
    attention = [r for r in source_health_rows if r["status"] in ("FAILING", "DEGRADED", "STALE", "NEVER_PROVEN")]

    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "n_art": n_art,
            "n_ct": n_ct,
            "n_doc": n_doc,
            "n_lead": n_lead,
            "last_art": last_art,
            "last_ct": last_ct,
            "last_doc": last_doc,
            "src_counts": src_counts,
            "plat_counts": plat_counts,
            "doc_counts": doc_counts,
            "discord": discord,
            "translation_status": translation_status,
            "snapshot": snapshot,
            "runs_offset": runs_offset,
            "runs_limit": DEFAULT_RECENT_LIMIT,
            "source_health_rows": source_health_rows,
            "attention_count": len(attention),
            "active": "health",
        },
    )


@app.get("/api/health/runtime")
def api_health_runtime(
    manual_run_id: Optional[int] = Query(None),
    runs_offset: int = Query(0, ge=0),
    runs_limit: int = Query(20, ge=1, le=100),
):
    """Read-only JSON for the Health page's live runtime panel. Polled by
    vanilla JS every few seconds — never scrapes, scores, reclusters,
    translates, sends notifications, or mutates the database."""
    from pipeline.operations import runtime_snapshot
    return runtime_snapshot(manual_run_id=manual_run_id, recent_limit=runs_limit, recent_offset=runs_offset)


@app.post("/operations/run-now")
def operations_run_now():
    """Launches the same production full-cycle path the scheduled task
    uses, as a detached background process — this request does not wait
    for collection to finish. Rejects the launch if a run is genuinely
    already active (see pipeline.operations.active_running_run for the
    stale-RUNNING-row safety window)."""
    from pipeline.operations import active_running_run, correlate_new_run, launch_manual_run

    existing = active_running_run()
    if existing is not None:
        return JSONResponse(
            {"started": False, "reason": "ALREADY_RUNNING", "run_id": existing.id},
            status_code=409,
        )

    launch_time = _now()
    try:
        launch_manual_run()
    except Exception as e:
        logger.exception("Failed to launch manual collector run")
        return JSONResponse({"started": False, "reason": f"launch failed: {e}"}, status_code=500)

    run_id = correlate_new_run(launch_time)
    return {"started": True, "run_id": run_id}





@app.get("/notifications", response_class=HTMLResponse)
def notifications_view(
    request: Request,
    outcome: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from database.models import LeadNotification
    from sqlalchemy import select, desc
    q = select(LeadNotification).order_by(desc(LeadNotification.attempted_at))
    if outcome:
        q = q.where(LeadNotification.outcome == outcome.upper())
    if reason:
        q = q.where(LeadNotification.reason_code == reason)
    with get_session() as session:
        rows = list(session.execute(q.offset(offset).limit(limit)).scalars().all())
        lead_ids = [r.lead_id for r in rows if r.lead_id]
        headlines = {}
        if lead_ids:
            for L in session.execute(select(StoryLead).where(StoryLead.id.in_(lead_ids))).scalars().all():
                headlines[L.id] = L.headline_hint
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "rows": rows,
            "headlines": headlines,
            "outcome": outcome or "",
            "reason": reason or "",
            "limit": limit,
            "offset": offset,
            "active": "notifications",
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def ingestion_run_detail(request: Request, run_id: int):
    from database.models import IngestionRun, SourceRun
    from sqlalchemy import select, desc
    with get_session() as session:
        run = session.get(IngestionRun, run_id)
        if not run:
            return templates.TemplateResponse(request, "error.html", {"message": f"Run {run_id} not found", "active": "health"}, status_code=404)
        # Source runs near this window
        src_runs = []
        if run.started_at:
            from datetime import timedelta
            window_end = run.finished_at or (run.started_at + timedelta(hours=2))
            src_runs = list(
                session.execute(
                    select(SourceRun)
                    .where(SourceRun.started_at >= run.started_at)
                    .where(SourceRun.started_at <= window_end)
                    .order_by(SourceRun.started_at)
                ).scalars().all()
            )
        notifs = list(
            session.execute(
                select(LeadNotification)
                .where(LeadNotification.ingestion_run_id == run_id)
                .order_by(desc(LeadNotification.attempted_at))
                .limit(50)
            ).scalars().all()
        ) if hasattr(LeadNotification, "ingestion_run_id") else []
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "src_runs": src_runs, "notifs": notifs, "active": "health"},
    )



@app.get("/activity", response_class=HTMLResponse)
def activity_view(request: Request, hours: float = Query(24)):
    cutoff = _now() - timedelta(hours=hours)
    items = []
    with get_session() as session:
        for e in session.execute(
            select(LeadEvent)
            .where(LeadEvent.observed_at >= cutoff)
            .order_by(desc(LeadEvent.observed_at))
            .limit(80)
        ).scalars().all():
            items.append({
                "t": e.observed_at,
                "kind": "LEAD",
                "text": f"{e.event_type}: {e.summary or ''}",
                "link": f"/leads/{e.lead_id}",
            })
        for e in session.execute(
            select(DocumentaryEvent)
            .where(DocumentaryEvent.observed_at >= cutoff)
            .order_by(desc(DocumentaryEvent.observed_at))
            .limit(40)
        ).scalars().all():
            items.append({
                "t": e.observed_at,
                "kind": "DOC",
                "text": f"{e.event_type}: {e.summary or ''}",
                "link": "/documentary",
            })
        for a in session.execute(
            select(Article)
            .where(Article.discovered_at >= cutoff)
            .order_by(desc(Article.discovered_at))
            .limit(40)
        ).scalars().all():
            items.append({
                "t": a.discovered_at,
                "kind": "NEWS",
                "text": f"{a.source}: {(a.title_english or a.title_original or '')[:80]}",
                "link": a.url,
            })
        for c in session.execute(
            select(CommunityThread)
            .where(CommunityThread.discovered_at >= cutoff)
            .order_by(desc(CommunityThread.discovered_at))
            .limit(40)
        ).scalars().all():
            items.append({
                "t": c.discovered_at,
                "kind": "COMM",
                "text": f"{c.platform}/{c.signal_type}: {(c.title_original or '')[:80]}",
                "link": c.url,
            })
    items.sort(key=lambda x: _aware(x["t"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    items = items[:100]
    return templates.TemplateResponse(
        request,
        "activity.html",
        { "items": items, "hours": hours, "active": "activity"},
    )


@app.get("/soak", response_class=HTMLResponse)
def soak_view(request: Request):
    with get_session() as session:
        n_art = session.execute(select(func.count()).select_from(Article)).scalar() or 0
        n_ct = session.execute(select(func.count()).select_from(CommunityThread)).scalar() or 0
        n_doc = session.execute(select(func.count()).select_from(DocumentaryRecord)).scalar() or 0
        n_lead = session.execute(select(func.count()).select_from(StoryLead)).scalar() or 0
        leads = list(session.execute(select(StoryLead)).scalars().all())
        fb = list(session.execute(select(LeadFeedback)).scalars().all())
        by_status = {}
        by_type = {}
        for L in leads:
            by_status[L.lead_status] = by_status.get(L.lead_status, 0) + 1
            by_type[L.lead_type] = by_type.get(L.lead_type, 0) + 1
        by_fb = {}
        for f in fb:
            by_fb[f.feedback] = by_fb.get(f.feedback, 0) + 1
        first = None
        for model in (Article, CommunityThread, DocumentaryRecord, StoryLead):
            row = session.execute(
                select(model).order_by(model.__table__.c[list(model.__table__.c.keys())[0]].asc()).limit(1)
            ).scalar_one_or_none()
        # observation window from earliest article/lead
        times = []
        for a in session.execute(select(Article.discovered_at).limit(1)).all():
            pass
        earliest = session.execute(
            select(func.min(Article.discovered_at))
        ).scalar()
        latest = session.execute(
            select(func.max(Article.discovered_at))
        ).scalar()
    return templates.TemplateResponse(
        request,
        "soak.html",
        {
            "n_art": n_art,
            "n_ct": n_ct,
            "n_doc": n_doc,
            "n_lead": n_lead,
            "by_status": by_status,
            "by_type": by_type,
            "by_fb": by_fb,
            "earliest": earliest,
            "latest": latest,
            "active": "soak",
        },
    )


@app.get("/export/newsroom.csv")
def export_newsroom_csv():
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "id", "score", "status", "type", "headline", "exclusivity",
            "confidence", "saturation", "why_now", "first_signal", "first_media",
        ]
    )
    with get_session() as session:
        for L in session.execute(
            select(StoryLead).order_by(desc(StoryLead.priority_score)).limit(200)
        ).scalars().all():
            w.writerow([
                L.id,
                f"{L.priority_score:.1f}",
                L.lead_status,
                L.lead_type,
                (L.headline_hint or "").replace("\n", " "),
                f"{L.exclusivity_score:.0f}",
                f"{L.confidence_score:.0f}",
                f"{L.media_saturation_score:.0f}",
                (L.why_now or "").replace("\n", " "),
                L.first_signal_source or "",
                L.first_media_source or "",
            ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=newsroom.csv"},
    )


@app.get("/validation", response_class=HTMLResponse)
def validation_view(request: Request, since_days: Optional[float] = Query(None)):
    """V0.5.6 editorial validation dashboard. Read-only — GET never scores,
    scrapes, or mutates anything; it only reads existing tables."""
    from pipeline.editorial_validation import (
        editorial_funnel, source_performance, lead_time_analytics,
        lead_type_performance, alert_performance, lifecycle_report,
    )
    from pipeline.missed_stories import missed_story_report, list_missed_stories
    from pipeline.outcomes import outcome_report

    since_hours = since_days * 24.0 if since_days else None
    funnel = editorial_funnel(since_hours=since_hours)
    sources = source_performance()
    lead_times = lead_time_analytics(since_hours=since_hours)
    lead_types = lead_type_performance()
    alerts = alert_performance()
    lifecycle = lifecycle_report()
    outcomes = outcome_report()
    misses = missed_story_report()
    recent_misses = list_missed_stories(limit=10)

    return templates.TemplateResponse(
        request,
        "validation.html",
        {
            "since_days": since_days,
            "funnel": funnel,
            "sources": sources[:15],
            "lead_times": lead_times,
            "lead_types": lead_types,
            "alerts": alerts,
            "lifecycle": lifecycle,
            "outcomes": outcomes,
            "misses": misses,
            "recent_misses": recent_misses,
            "active": "validation",
        },
    )


@app.post("/leads/{lead_id}/outcome")
def lead_outcome_post(
    lead_id: int,
    outcome: str = Form(...),
    notes: Optional[str] = Form(None),
    article_url: Optional[str] = Form(None),
    article_title: Optional[str] = Form(None),
    confirmation_url: Optional[str] = Form(None),
):
    from pipeline.outcomes import record_outcome
    row = record_outcome(
        lead_id, outcome.strip(),
        note=(notes or None), article_url=(article_url or None),
        article_title=(article_title or None), confirmation_url=(confirmation_url or None),
        outcome_source="GUI",
    )
    if not row:
        return RedirectResponse(f"/leads/{lead_id}?err=1", status_code=303)
    return RedirectResponse(f"/leads/{lead_id}?outcome=1", status_code=303)


def create_app() -> FastAPI:
    init_db()
    return app


def run_gui(host: str = "127.0.0.1", port: int = 8000) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "GUI binding to non-loopback host %s — this exposes the local newsroom UI",
            host,
        )
    import uvicorn

    init_db()
    try:
        print(f"Chinese Tech Wire Newsroom GUI → http://{host}:{port}")
    except UnicodeEncodeError:
        # Stock Windows console (cp1252) can't encode the arrow — degrade
        # gracefully rather than crashing the whole server before it starts.
        print(f"Chinese Tech Wire Newsroom GUI -> http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
