"""V0.5.6 — Editorial Validation and Outcome Intelligence.

Read-only analytics reusable by CLI and GUI. Answers whether CTW's
intelligence is editorially useful — it never changes scoring, thresholds,
alert policy, or lifecycle status. Every number here is evidence for a
future human decision, not a decision itself.

Sample-size discipline: every rate is returned with its numerator and
denominator, and flagged low_sample=True below MIN_SAMPLE so small samples
are never silently presented as trends.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from config import yaml_config
from database.db import get_session
from database.models import (
    LeadEvent,
    LeadNotification,
    LeadOutcome,
    StoryCluster,
    StoryLead,
)
from pipeline.outcomes import FEEDBACK_TO_OUTCOME
from pipeline.source_health import _registries

MIN_SAMPLE_DEFAULT = 10

LIFECYCLE_STATUSES = [
    "NEW", "WATCHING", "ACTIONABLE", "ESCALATED", "STALE", "RESOLVED", "DISMISSED",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _min_sample() -> int:
    return int((yaml_config.get("editorial_validation") or {}).get("min_sample", MIN_SAMPLE_DEFAULT))


def _rate(numerator: int, denominator: int, min_sample: Optional[int] = None) -> Dict[str, Any]:
    min_sample = min_sample if min_sample is not None else _min_sample()
    if denominator == 0:
        return {
            "numerator": 0, "denominator": 0, "pct": None,
            "label": "0 / 0 (n/a)", "low_sample": True, "warning": "no denominator",
        }
    pct = round(100.0 * numerator / denominator, 1)
    low = denominator < min_sample
    return {
        "numerator": numerator,
        "denominator": denominator,
        "pct": pct,
        "label": f"{numerator} / {denominator} = {pct}%",
        "low_sample": low,
        "warning": "LOW_SAMPLE" if low else None,
    }


def _duration_stats(minutes: List[float]) -> Dict[str, Any]:
    if not minutes:
        return {"count": 0, "status": "INSUFFICIENT_TIMESTAMP_DATA"}
    vals = sorted(minutes)
    n = len(vals)

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return vals[idx]

    return {
        "count": n,
        "median_minutes": round(statistics.median(vals), 1),
        "mean_minutes": round(sum(vals) / n, 1),
        "p25_minutes": round(pct(0.25), 1),
        "p75_minutes": round(pct(0.75), 1),
        "min_minutes": round(vals[0], 1),
        "max_minutes": round(vals[-1], 1),
    }


def _bulk_effective_outcomes(session, lead_ids: Optional[List[int]] = None) -> Dict[int, str]:
    """Batched version of pipeline.outcomes.effective_outcome — avoids N+1
    queries when scoring hundreds/thousands of leads at once."""
    from database.models import LeadFeedback

    oq = select(LeadOutcome.lead_id, LeadOutcome.outcome, LeadOutcome.recorded_at)
    fq = select(LeadFeedback.lead_id, LeadFeedback.feedback, LeadFeedback.created_at)
    if lead_ids is not None:
        if not lead_ids:
            return {}
        oq = oq.where(LeadOutcome.lead_id.in_(lead_ids))
        fq = fq.where(LeadFeedback.lead_id.in_(lead_ids))

    latest_outcome: Dict[int, tuple] = {}
    for lid, val, ts in session.execute(oq).all():
        ts = _aware(ts)
        if lid not in latest_outcome or ts > latest_outcome[lid][1]:
            latest_outcome[lid] = (val, ts)

    latest_fb: Dict[int, tuple] = {}
    for lid, val, ts in session.execute(fq).all():
        ts = _aware(ts)
        if lid not in latest_fb or ts > latest_fb[lid][1]:
            latest_fb[lid] = (val, ts)

    result: Dict[int, str] = {}
    all_ids = set(latest_outcome) | set(latest_fb) | set(lead_ids or [])
    for lid in all_ids:
        if lid in latest_outcome:
            result[lid] = latest_outcome[lid][0]
        elif lid in latest_fb:
            result[lid] = FEEDBACK_TO_OUTCOME.get(latest_fb[lid][0], "UNKNOWN")
        else:
            result[lid] = "UNLABELED"
    return result


def _alerted_lead_ids(session, lead_ids: Optional[List[int]] = None) -> set:
    q = select(LeadNotification.lead_id).where(LeadNotification.outcome == "SENT")
    if lead_ids is not None:
        if not lead_ids:
            return set()
        q = q.where(LeadNotification.lead_id.in_(lead_ids))
    return {lid for lid in session.execute(q).scalars().all() if lid is not None}


# ---------------------------------------------------------------------------
# Phase 5 — editorial conversion funnel
# ---------------------------------------------------------------------------

def editorial_funnel(since_hours: Optional[float] = None) -> Dict[str, Any]:
    with get_session() as session:
        q = select(StoryLead.id)
        if since_hours is not None:
            cutoff = _now() - timedelta(hours=since_hours)
            q = q.where(StoryLead.created_at >= cutoff)
        lead_ids = list(session.execute(q).scalars().all())
        total = len(lead_ids)

        alerted_ids = _alerted_lead_ids(session, lead_ids)
        outcomes = _bulk_effective_outcomes(session, lead_ids)

        useful = written = false_ = dup = ignored = unresolved = 0
        labelled = 0
        for lid in lead_ids:
            o = outcomes.get(lid, "UNLABELED")
            if o != "UNLABELED":
                labelled += 1
            if o == "USEFUL":
                useful += 1
            elif o == "WRITTEN":
                written += 1
            elif o == "FALSE":
                false_ += 1
            elif o == "DUPLICATE":
                dup += 1
            elif o == "IGNORED":
                ignored += 1
            elif o in ("UNLABELED", "UNKNOWN", "STALLED"):
                unresolved += 1

        useful_alerted = sum(1 for lid in alerted_ids if outcomes.get(lid) == "USEFUL")
        written_alerted = sum(1 for lid in alerted_ids if outcomes.get(lid) == "WRITTEN")
        ignored_alerted = sum(1 for lid in alerted_ids if outcomes.get(lid) == "IGNORED")
        written_among_useful = written  # approximation: "current" outcome model,
        # a lead currently WRITTEN was very likely useful along the way

        return {
            "since_hours": since_hours,
            "counts": {
                "total_leads": total,
                "alerted_leads": len(alerted_ids),
                "feedback_labelled_leads": labelled,
                "useful_leads": useful,
                "written_leads": written,
                "false_leads": false_,
                "duplicate_leads": dup,
                "ignored_leads": ignored,
                "still_unresolved": unresolved,
            },
            "rates": {
                "feedback_rate": _rate(labelled, total),
                "alert_to_useful_rate": _rate(useful_alerted, len(alerted_ids)),
                "alert_to_written_rate": _rate(written_alerted, len(alerted_ids)),
                "useful_to_written_rate": _rate(written_among_useful, useful + written) if (useful + written) else _rate(0, 0),
                "false_positive_rate": _rate(false_, labelled),
                "duplicate_rate": _rate(dup, labelled),
                "ignored_alert_rate": _rate(ignored_alerted, len(alerted_ids)),
            },
            "min_sample": _min_sample(),
        }


def format_funnel_text(f: Dict[str, Any]) -> str:
    c = f["counts"]
    r = f["rates"]
    lines = [
        "EDITORIAL CONVERSION FUNNEL" + (f" (last {f['since_hours']}h)" if f["since_hours"] else ""),
        "-" * 60,
        f"Total leads:            {c['total_leads']}",
        f"Alerted leads:          {c['alerted_leads']}",
        f"Feedback-labelled:      {c['feedback_labelled_leads']}",
        f"Useful:                 {c['useful_leads']}",
        f"Written:                {c['written_leads']}",
        f"False:                  {c['false_leads']}",
        f"Duplicate:              {c['duplicate_leads']}",
        f"Ignored:                {c['ignored_leads']}",
        f"Still unresolved:       {c['still_unresolved']}",
        "",
        "RATES",
    ]
    for name, d in r.items():
        warn = f"  [{d['warning']}]" if d.get("warning") else ""
        lines.append(f"  {name:<24} {d['label']}{warn}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 6 — source editorial performance (separate from operational health)
# ---------------------------------------------------------------------------

def source_performance() -> List[Dict[str, Any]]:
    """Cluster-based source attribution (V0.5.6.1).

    "Contributed" means database provenance only: an actual Article /
    CommunityThread / DocumentaryRecord row from that source belongs to the
    StoryLead's cluster. Never inferred from entity mentions, URLs in text,
    or headline text. This fixes the earlier version, which only credited a
    source if it won one of the first_signal/first_media/first_documentary
    fields — that undercounted high-volume contributing sources that were
    never "first" (e.g. a NEWS source with hundreds of articles feeding
    FOLLOW_UP leads could show leads_touched=0).

    Uses three grouped (source, cluster_id) queries — not one query per
    cluster or per source — to stay O(1) roundtrips regardless of DB size.
    """
    from database.models import Article, CommunityThread, DocumentaryRecord
    from sqlalchemy import func

    with get_session() as session:
        # First-attribution fields live directly on StoryLead and don't
        # require a cluster join — pull them for every lead so a lead
        # somehow missing cluster_id (shouldn't normally happen, but no
        # reason to silently drop it) still counts here.
        lead_rows = session.execute(
            select(
                StoryLead.id, StoryLead.cluster_id,
                StoryLead.first_signal_source, StoryLead.first_media_source,
                StoryLead.first_documentary_source, StoryLead.lead_time_minutes,
            )
        ).all()

        # Only leads WITH a cluster can be matched against actual stored
        # Article/CommunityThread/DocumentaryRecord provenance.
        cluster_to_lead: Dict[int, int] = {
            r.cluster_id: r.id for r in lead_rows if r.cluster_id is not None
        }
        lead_ids = [r.id for r in lead_rows]
        first_signal_by_lead = {r.id: r.first_signal_source for r in lead_rows}
        first_media_by_lead = {r.id: r.first_media_source for r in lead_rows}
        first_doc_by_lead = {r.id: r.first_documentary_source for r in lead_rows}
        lead_time_by_lead = {r.id: r.lead_time_minutes for r in lead_rows}

        outcomes = _bulk_effective_outcomes(session, lead_ids)
        alerted_ids = _alerted_lead_ids(session, lead_ids)

        # source -> {cluster_id: record_count} — only for clusters that
        # actually have a StoryLead (unattributed clusters can't be "touched").
        contributions: Dict[str, Dict[int, int]] = {}

        def _accumulate(rows):
            for src, cid, cnt in rows:
                if cid not in cluster_to_lead:
                    continue
                bucket = contributions.setdefault(src, {})
                bucket[cid] = bucket.get(cid, 0) + int(cnt)

        cluster_ids = list(cluster_to_lead.keys())
        if cluster_ids:
            _accumulate(session.execute(
                select(Article.source, Article.duplicate_group_id, func.count())
                .where(Article.duplicate_group_id.in_(cluster_ids))
                .group_by(Article.source, Article.duplicate_group_id)
            ).all())
            _accumulate(session.execute(
                select(CommunityThread.platform, CommunityThread.story_cluster_id, func.count())
                .where(CommunityThread.story_cluster_id.in_(cluster_ids))
                .group_by(CommunityThread.platform, CommunityThread.story_cluster_id)
            ).all())
            _accumulate(session.execute(
                select(DocumentaryRecord.source, DocumentaryRecord.story_cluster_id, func.count())
                .where(DocumentaryRecord.story_cluster_id.in_(cluster_ids))
                .group_by(DocumentaryRecord.source, DocumentaryRecord.story_cluster_id)
            ).all())

        results: List[Dict[str, Any]] = []
        for meta in _registries():
            name, layer = meta["name"], meta["layer"]
            per_cluster = contributions.get(name, {})
            records_contributed = sum(per_cluster.values())
            # dedup: a source touches a lead once no matter how many of its
            # records land in that lead's cluster.
            touched_ids = {cluster_to_lead[cid] for cid in per_cluster}

            first_signal = sum(1 for lid in lead_ids if first_signal_by_lead.get(lid) == name)
            first_media = sum(1 for lid in lead_ids if first_media_by_lead.get(lid) == name)
            first_doc = sum(1 for lid in lead_ids if first_doc_by_lead.get(lid) == name)

            alerted = len(touched_ids & alerted_ids)
            useful = sum(1 for lid in touched_ids if outcomes.get(lid) == "USEFUL")
            written = sum(1 for lid in touched_ids if outcomes.get(lid) == "WRITTEN")
            false_ = sum(1 for lid in touched_ids if outcomes.get(lid) == "FALSE")
            dup = sum(1 for lid in touched_ids if outcomes.get(lid) == "DUPLICATE")

            lt_values = [lead_time_by_lead[lid] for lid in touched_ids if lead_time_by_lead.get(lid) is not None]
            lead_time_stats = _duration_stats(lt_values)

            results.append({
                "source": name,
                "layer": layer,
                "records_contributed": records_contributed,
                "leads_touched": len(touched_ids),
                "first_signal_count": first_signal,
                "first_media_count": first_media,
                "first_documentary_count": first_doc,
                "alerted_leads_touched": alerted,
                "useful_leads_touched": useful,
                "written_leads_touched": written,
                "false_leads_touched": false_,
                "duplicate_leads_touched": dup,
                "lead_time_minutes": lead_time_stats,
                "useful_conversion_rate": _rate(useful, len(touched_ids)),
                "written_conversion_rate": _rate(written, len(touched_ids)),
                "false_positive_rate": _rate(false_, len(touched_ids)),
                # Intentionally no composite score — raw metrics only (V0.5.6 spec).
            })
        results.sort(key=lambda r: (-r["records_contributed"], -r["written_leads_touched"]))
        return results


# ---------------------------------------------------------------------------
# Phase 7 — lead-time analytics (real timestamps only)
# ---------------------------------------------------------------------------

def lead_time_analytics(since_hours: Optional[float] = None) -> Dict[str, Any]:
    with get_session() as session:
        cq = select(StoryLead, StoryCluster).join(
            StoryCluster, StoryLead.cluster_id == StoryCluster.id, isouter=True
        )
        if since_hours is not None:
            cutoff = _now() - timedelta(hours=since_hours)
            cq = cq.where(StoryLead.created_at >= cutoff)
        rows = session.execute(cq).all()

        lead_ids = [l.id for l, _ in rows]
        # earliest successful send per lead
        sent_at: Dict[int, datetime] = {}
        for lid, ts in session.execute(
            select(LeadNotification.lead_id, LeadNotification.sent_at)
            .where(LeadNotification.outcome == "SENT")
            .where(LeadNotification.lead_id.in_(lead_ids) if lead_ids else False)
        ).all() if lead_ids else []:
            ts = _aware(ts)
            if ts and (lid not in sent_at or ts < sent_at[lid]):
                sent_at[lid] = ts

        # earliest WRITTEN outcome per lead — explicitly the operator-recorded
        # time, not a verified publish timestamp (see Phase 7 spec).
        written_at: Dict[int, datetime] = {}
        for lid, ts, outc in session.execute(
            select(LeadOutcome.lead_id, LeadOutcome.recorded_at, LeadOutcome.outcome)
            .where(LeadOutcome.lead_id.in_(lead_ids) if lead_ids else False)
        ).all() if lead_ids else []:
            if outc != "WRITTEN":
                continue
            ts = _aware(ts)
            if ts and (lid not in written_at or ts < written_at[lid]):
                written_at[lid] = ts

        buckets: Dict[str, List[float]] = {
            "signal_to_media": [], "documentary_to_media": [], "signal_to_alert": [],
            "signal_to_written": [], "media_to_written": [], "alert_to_written": [],
        }
        rejected_inverted = 0

        def _add(bucket: str, start: Optional[datetime], end: Optional[datetime]):
            nonlocal rejected_inverted
            if not start or not end:
                return
            start, end = _aware(start), _aware(end)
            delta_min = (end - start).total_seconds() / 60.0
            if delta_min < 0:
                rejected_inverted += 1
                return
            buckets[bucket].append(delta_min)

        for lead, cluster in rows:
            fs = cluster.first_signal_at if cluster else lead.first_signal_at
            fm = cluster.first_media_at if cluster else None
            fd = cluster.first_documentary_at if cluster else None
            al = sent_at.get(lead.id)
            wr = written_at.get(lead.id)
            _add("signal_to_media", fs, fm)
            _add("documentary_to_media", fd, fm)
            _add("signal_to_alert", fs, al)
            _add("signal_to_written", fs, wr)
            _add("media_to_written", fm, wr)
            _add("alert_to_written", al, wr)

        return {
            "since_hours": since_hours,
            "rejected_inverted_intervals": rejected_inverted,
            "intervals": {
                "FIRST_SIGNAL_TO_FIRST_MEDIA_PICKUP": _duration_stats(buckets["signal_to_media"]),
                "FIRST_DOCUMENTARY_TO_FIRST_MEDIA_PICKUP": _duration_stats(buckets["documentary_to_media"]),
                "FIRST_SIGNAL_TO_ALERT_SENT": _duration_stats(buckets["signal_to_alert"]),
                "FIRST_SIGNAL_TO_WRITTEN": {
                    **_duration_stats(buckets["signal_to_written"]),
                    "basis": "LeadOutcome.recorded_at (operator-entered, not a verified publish timestamp)",
                },
                "FIRST_MEDIA_PICKUP_TO_WRITTEN": {
                    **_duration_stats(buckets["media_to_written"]),
                    "basis": "LeadOutcome.recorded_at (operator-entered, not a verified publish timestamp)",
                },
                "ALERT_SENT_TO_WRITTEN": {
                    **_duration_stats(buckets["alert_to_written"]),
                    "basis": "LeadOutcome.recorded_at (operator-entered, not a verified publish timestamp)",
                },
            },
        }


# ---------------------------------------------------------------------------
# Phase 8 — lead-type performance
# ---------------------------------------------------------------------------

def lead_type_performance() -> List[Dict[str, Any]]:
    with get_session() as session:
        leads = list(session.execute(select(StoryLead)).scalars().all())
        lead_ids = [l.id for l in leads]
        outcomes = _bulk_effective_outcomes(session, lead_ids)
        alerted_ids = _alerted_lead_ids(session, lead_ids)

        by_type: Dict[str, List[StoryLead]] = {}
        for l in leads:
            by_type.setdefault(l.lead_type or "UNKNOWN", []).append(l)

        results = []
        for lt, group in by_type.items():
            ids = {l.id for l in group}
            alerted = len(ids & alerted_ids)
            useful = sum(1 for l in group if outcomes.get(l.id) == "USEFUL")
            written = sum(1 for l in group if outcomes.get(l.id) == "WRITTEN")
            false_ = sum(1 for l in group if outcomes.get(l.id) == "FALSE")
            dup = sum(1 for l in group if outcomes.get(l.id) == "DUPLICATE")
            results.append({
                "lead_type": lt,
                "total": len(group),
                "alerted": alerted,
                "useful": useful,
                "written": written,
                "false": false_,
                "duplicate": dup,
                "median_priority": round(statistics.median([l.priority_score for l in group]), 1) if group else None,
                "median_evidence": round(statistics.median([l.evidence_score for l in group]), 1) if group else None,
                "median_confidence": round(statistics.median([l.confidence_score for l in group]), 1) if group else None,
            })
        results.sort(key=lambda r: -r["total"])
        return results


# ---------------------------------------------------------------------------
# Phase 9 — alert usefulness
# ---------------------------------------------------------------------------

def alert_performance() -> Dict[str, Any]:
    with get_session() as session:
        sent = list(
            session.execute(
                select(LeadNotification).where(LeadNotification.outcome == "SENT")
            ).scalars().all()
        )
        sent_lead_ids = list({n.lead_id for n in sent if n.lead_id is not None})
        outcomes = _bulk_effective_outcomes(session, sent_lead_ids)

        useful = written = ignored = false_ = dup = unresolved = 0
        for lid in sent_lead_ids:
            o = outcomes.get(lid, "UNLABELED")
            if o == "USEFUL":
                useful += 1
            elif o == "WRITTEN":
                written += 1
            elif o == "IGNORED":
                ignored += 1
            elif o == "FALSE":
                false_ += 1
            elif o == "DUPLICATE":
                dup += 1
            else:
                unresolved += 1

        by_reason: Dict[str, Dict[str, Any]] = {}
        reason_leads: Dict[str, set] = {}
        for n in sent:
            if not n.lead_id:
                continue
            reason = n.alert_reason or "UNKNOWN"
            reason_leads.setdefault(reason, set()).add(n.lead_id)
        for reason, ids in reason_leads.items():
            w = sum(1 for lid in ids if outcomes.get(lid) == "WRITTEN")
            u = sum(1 for lid in ids if outcomes.get(lid) == "USEFUL")
            by_reason[reason] = {
                "sent": len(ids),
                "useful": u,
                "written": w,
                "written_rate": _rate(w, len(ids)),
            }

        return {
            "total_sent_leads": len(sent_lead_ids),
            "total_sent_events": len(sent),
            "useful_after_alert": useful,
            "written_after_alert": written,
            "ignored_after_alert": ignored,
            "false_after_alert": false_,
            "duplicate_after_alert": dup,
            "still_unresolved_after_alert": unresolved,
            "written_rate": _rate(written, len(sent_lead_ids)),
            "useful_rate": _rate(useful, len(sent_lead_ids)),
            "ignored_rate": _rate(ignored, len(sent_lead_ids)),
            "by_alert_reason": by_reason,
        }


# ---------------------------------------------------------------------------
# Phase 10 — lifecycle attrition
# ---------------------------------------------------------------------------

def lifecycle_report() -> Dict[str, Any]:
    with get_session() as session:
        leads = list(session.execute(select(StoryLead)).scalars().all())
        lead_ids = [l.id for l in leads]
        outcomes = _bulk_effective_outcomes(session, lead_ids)

        status_counts: Dict[str, int] = {}
        for l in leads:
            status_counts[l.lead_status] = status_counts.get(l.lead_status, 0) + 1

        events = list(
            session.execute(
                select(LeadEvent)
                .where(LeadEvent.event_type.in_([
                    "STATUS_CHANGE", "BECAME_ACTIONABLE", "BECAME_STALE", "RESURGED",
                ]))
                .where(LeadEvent.lead_id.in_(lead_ids) if lead_ids else False)
                .order_by(LeadEvent.lead_id, LeadEvent.observed_at)
            ).scalars().all()
        ) if lead_ids else []

        transitions: Dict[str, int] = {}
        ever_reached: Dict[int, set] = {l.id: {"NEW"} for l in leads}
        segments_by_lead: Dict[int, List[tuple]] = {l.id: [("NEW", l.created_at)] for l in leads}

        for e in events:
            if " → " not in (e.summary or ""):
                continue
            old, new = [s.strip() for s in e.summary.split("→", 1)]
            key = f"{old} → {new}"
            transitions[key] = transitions.get(key, 0) + 1
            ever_reached.setdefault(e.lead_id, {"NEW"}).add(new)
            segments_by_lead.setdefault(e.lead_id, []).append((new, e.observed_at))

        # time-in-status: duration of each segment until the next one (or now)
        time_in_status: Dict[str, List[float]] = {}
        for lid, segs in segments_by_lead.items():
            segs = sorted(segs, key=lambda s: s[1])
            for i, (status, ts) in enumerate(segs):
                end = segs[i + 1][1] if i + 1 < len(segs) else _now()
                minutes = (_aware(end) - _aware(ts)).total_seconds() / 60.0
                if minutes >= 0:
                    time_in_status.setdefault(status, []).append(minutes)

        median_time_in_status = {
            status: _duration_stats(vals) for status, vals in time_in_status.items()
        }

        # cross-reference with outcomes
        written_never_watching = sum(
            1 for l in leads
            if outcomes.get(l.id) == "WRITTEN" and "WATCHING" not in ever_reached.get(l.id, set())
        )
        useful_remained_new = sum(
            1 for l in leads
            if outcomes.get(l.id) == "USEFUL" and ever_reached.get(l.id, {"NEW"}) == {"NEW"}
        )
        false_reached_watching = sum(
            1 for l in leads
            if outcomes.get(l.id) == "FALSE" and "WATCHING" in ever_reached.get(l.id, set())
        )
        alerted_ids = _alerted_lead_ids(session, lead_ids)
        alerted_ignored = sum(1 for lid in alerted_ids if outcomes.get(lid) == "IGNORED")

        return {
            "status_counts": status_counts,
            "transitions": transitions,
            "median_time_in_status_minutes": median_time_in_status,
            "cross_reference": {
                "written_leads_never_reached_watching": written_never_watching,
                "useful_leads_remained_new": useful_remained_new,
                "false_leads_reached_watching": false_reached_watching,
                "alerted_leads_ultimately_ignored": alerted_ignored,
            },
        }


# ---------------------------------------------------------------------------
# CLI text formatters
# ---------------------------------------------------------------------------

def format_source_performance_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No sources found."
    lines = ["SOURCE EDITORIAL PERFORMANCE (cluster-based attribution)", "-" * 90]
    header = (
        f"{'Source':<12} {'Layer':<11} {'Records':>7} {'Touched':>7} "
        f"{'1stSig':>6} {'1stMedia':>8} {'1stDoc':>6} {'Alerted':>7} "
        f"{'Useful':>6} {'Written':>7} {'False':>5} {'Dup':>3}"
    )
    lines.append(header)
    for r in rows:
        lines.append(
            f"{r['source']:<12} {r['layer']:<11} {r['records_contributed']:>7} {r['leads_touched']:>7} "
            f"{r['first_signal_count']:>6} {r['first_media_count']:>8} {r['first_documentary_count']:>6} "
            f"{r['alerted_leads_touched']:>7} {r['useful_leads_touched']:>6} {r['written_leads_touched']:>7} "
            f"{r['false_leads_touched']:>5} {r['duplicate_leads_touched']:>3}"
        )
    lines.append("")
    lines.append("(no composite score — raw metrics only; volume != usefulness)")
    lines.append("'Touched' = actual stored Article/CommunityThread/DocumentaryRecord in the lead's cluster, not just first-attribution.")
    return "\n".join(lines)


def format_lead_time_text(d: Dict[str, Any]) -> str:
    lines = ["LEAD-TIME ANALYTICS" + (f" (last {d['since_hours']}h)" if d.get("since_hours") else ""), "-" * 78]
    if d.get("rejected_inverted_intervals"):
        lines.append(f"Rejected (end before start): {d['rejected_inverted_intervals']}")
    for name, stats in d["intervals"].items():
        if stats.get("status") == "INSUFFICIENT_TIMESTAMP_DATA":
            lines.append(f"{name}: INSUFFICIENT_TIMESTAMP_DATA")
            continue
        basis = f" [{stats['basis']}]" if "basis" in stats else ""
        lines.append(
            f"{name}: n={stats['count']} median={stats['median_minutes']}m "
            f"mean={stats['mean_minutes']}m p25={stats['p25_minutes']}m p75={stats['p75_minutes']}m "
            f"min={stats['min_minutes']}m max={stats['max_minutes']}m{basis}"
        )
    return "\n".join(lines)


def format_lead_type_text(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No leads found."
    lines = ["LEAD-TYPE PERFORMANCE", "-" * 78]
    for r in rows:
        lines.append(
            f"{r['lead_type']:<20} total={r['total']:<5} alerted={r['alerted']:<4} "
            f"useful={r['useful']:<3} written={r['written']:<3} false={r['false']:<3} "
            f"dup={r['duplicate']:<3} medP={r['median_priority']} medEv={r['median_evidence']} "
            f"medConf={r['median_confidence']}"
        )
    return "\n".join(lines)


def format_alert_performance_text(d: Dict[str, Any]) -> str:
    lines = [
        "ALERT USEFULNESS", "-" * 78,
        f"Total leads with a SENT alert: {d['total_sent_leads']} ({d['total_sent_events']} send events)",
        f"Useful after alert:    {d['useful_after_alert']}",
        f"Written after alert:   {d['written_after_alert']}",
        f"Ignored after alert:   {d['ignored_after_alert']}",
        f"False after alert:     {d['false_after_alert']}",
        f"Duplicate after alert: {d['duplicate_after_alert']}",
        f"Still unresolved:      {d['still_unresolved_after_alert']}",
        f"Written rate:  {d['written_rate']['label']}" + (f" [{d['written_rate']['warning']}]" if d['written_rate'].get('warning') else ""),
        "",
        "By alert reason:",
    ]
    for reason, s in sorted(d["by_alert_reason"].items(), key=lambda kv: -kv[1]["sent"]):
        lines.append(f"  {reason:<28} sent={s['sent']:<4} useful={s['useful']:<3} written={s['written']:<3} written_rate={s['written_rate']['label']}")
    return "\n".join(lines)


def format_lifecycle_text(d: Dict[str, Any]) -> str:
    lines = ["LIFECYCLE ATTRITION", "-" * 78, "Status distribution:"]
    for status in LIFECYCLE_STATUSES:
        if status in d["status_counts"]:
            lines.append(f"  {status:<12} {d['status_counts'][status]}")
    lines.append("")
    lines.append("Transitions observed:")
    for k, v in sorted(d["transitions"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k:<24} {v}")
    lines.append("")
    lines.append("Median time in status:")
    for status, stats in d["median_time_in_status_minutes"].items():
        if stats.get("status") == "INSUFFICIENT_TIMESTAMP_DATA":
            continue
        hrs = stats["median_minutes"] / 60.0
        lines.append(f"  {status:<12} median={stats['median_minutes']}m ({hrs:.1f}h) n={stats['count']}")
    lines.append("")
    cr = d["cross_reference"]
    lines.append("Cross-reference with outcomes:")
    lines.append(f"  WRITTEN leads that never reached WATCHING: {cr['written_leads_never_reached_watching']}")
    lines.append(f"  USEFUL leads that remained NEW:            {cr['useful_leads_remained_new']}")
    lines.append(f"  FALSE leads that reached WATCHING:         {cr['false_leads_reached_watching']}")
    lines.append(f"  Alerted leads ultimately IGNORED:          {cr['alerted_leads_ultimately_ignored']}")
    return "\n".join(lines)
