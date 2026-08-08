#!/usr/bin/env python3
"""Chinese Tech Wire V0.1 — main entrypoint.

Usage:
  python main.py                  # continuous polling
  python main.py --once           # single news-source cycle
  python main.py --full-once      # news+community+documentary+leads
  python main.py --full-once --scheduled  # hourly Task Scheduler entry
  python main.py --source ithome  # single source
  python main.py --dry-run
  python main.py --show-recent
  python main.py --show-high-priority
  python main.py --show-clusters
  python main.py --test-discord
  python main.py --preview-alerts --since-hours 24 --limit 25
  python main.py --diagnose-alerts
  python main.py --test-storylead-discord ID
  python main.py --show-notifications
  python main.py --explain-lead ID
  python main.py --explain-lead-json ID
  python main.py --lead-timeline ID
  python main.py --lead-audit ID
  python main.py --source-health
  python main.py --source-health --source benchlife
  python main.py --identity
  python main.py --health
  python main.py --test-translation
  python main.py --translate "中文标题"

  # V0.5.6 — editorial validation
  python main.py --lead-outcome 3001 WRITTEN --article-url "https://..."
  python main.py --show-outcomes --limit 50
  python main.py --outcome-report
  python main.py --editorial-report
  python main.py --source-performance
  python main.py --lead-type-performance
  python main.py --alert-performance
  python main.py --lifecycle-report
  python main.py --record-miss --miss-title "..." --miss-source ithome --miss-failure-stage SOURCE_BLOCKED
  python main.py --missed-story-report
  python main.py --show-missed-stories
  python main.py --reconstruct-miss ID
  python main.py --freshness-report
  python main.py --freshness-report --json

  # V0.5.6.1 — dashboard
  python main.py --gui
  python main.py --gui --gui-port auto --gui-open-browser
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Type

# Ensure project root on path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# CJK headlines (this is a Chinese-tech feed) crash on Windows' default
# cp1252 console encoding. Force UTF-8 stdout/stderr so --explain-lead,
# --lead-timeline, --lead-audit etc. don't UnicodeEncodeError on a stock
# Windows terminal. Safe no-op on platforms/streams that don't support it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from config import settings, yaml_config
from database.db import get_session, init_db
from database.models import Article, SourceRun, StoryCluster
from pipeline.deduplicate import get_or_create_cluster
from pipeline.normalize import normalize_raw
from pipeline.notify import format_message, send_discord, should_notify
from pipeline.score import score_article
from pipeline.translate import get_translator, log_translation_stats
from pipeline.community_ingest import run_community_source
from pipeline.documentary_ingest import run_documentary_source
from pipeline.newsroom import (
    rebuild_leads, list_leads, format_brief, explain_lead, add_feedback,
)
from documentary_sources import DOCUMENTARY_REGISTRY
from community_sources import COMMUNITY_REGISTRY
from pipeline.upstream import detect_upstream
from sources.base import BaseSource, RawArticle
from sources.ithome import ITHomeSource
from sources.mydrivers import MyDriversSource
from sources.expreview import ExpreviewSource
from sources.zol import ZOLSource
from sources.jiwei import JiweiSource
from sources.benchlife import BenchLifeSource
from sources.hkepc import HKEPCSource
from sources.technews import TechNewsSource
from sources.xfastest import XFastestSource

# V0.1 CN + V0.2 TW/HK
SOURCE_REGISTRY: Dict[str, Type[BaseSource]] = {
    "ithome": ITHomeSource,
    "mydrivers": MyDriversSource,
    "expreview": ExpreviewSource,
    "zol": ZOLSource,
    "jiwei": JiweiSource,
    "benchlife": BenchLifeSource,
    "hkepc": HKEPCSource,
    "technews": TechNewsSource,
    "xfastest": XFastestSource,
}

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ctw")


def run_source(source_name: str, dry_run: bool = False) -> int:
    """Run one source adapter fully. Returns number of new articles."""
    cls = SOURCE_REGISTRY.get(source_name)
    if not cls:
        logger.error("Unknown source: %s", source_name)
        return 0

    src = cls()
    started = datetime.now(timezone.utc)
    new_count = 0
    found = 0
    parse_err = 0
    req_err = 0
    t0 = time.monotonic()

    try:
        logger.info("[%s] Fetching latest", source_name)
        raws: List[RawArticle] = src.fetch_latest()
        found = len(raws)
        logger.info("[%s] %d articles found", source_name, found)
    except Exception as e:
        logger.error("[%s] Request/parser failure: %s", source_name, e)
        req_err = 1
        with get_session() as session:
            run = SourceRun(
                source=source_name,
                layer="NEWS",
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                success=False,
                articles_found=0,
                articles_new=0,
                parse_errors=0,
                request_errors=1,
                response_time_ms=int((time.monotonic() - t0) * 1000),
                error_message=str(e)[:500],
            )
            session.add(run)
        src.close()
        return 0

    translator = get_translator()
    with get_session() as session:
        for raw in raws:
            try:
                # Already seen?
                existing = (
                    session.query(Article)
                    .filter_by(source=raw.source, source_article_id=str(raw.source_article_id))
                    .first()
                )
                if existing:
                    continue

                data = normalize_raw(raw)
                # Translate (non-blocking on failure)
                try:
                    en = translator.translate(data["title_original"])
                    if en:
                        data["title_english"] = en
                except Exception:
                    pass

                # Upstream + score
                stype, up_src, up_url = detect_upstream(data["title_original"], data.get("summary_original"))
                data["source_type"] = stype
                data["upstream_source"] = up_src
                data["upstream_url"] = up_url

                scores = score_article(
                    data["title_original"],
                    data.get("summary_original"),
                    source=raw.source,
                    published_at=data.get("published_at"),
                    is_first=True,
                )
                data.update({
                    "relevance_score": scores["relevance_score"],
                    "novelty_score": scores["novelty_score"],
                    "priority_score": scores["priority_score"],
                    "rumor_flag": scores["rumor_flag"],
                    "rumor_confidence": scores["rumor_confidence"],
                })

                art = Article(**data)
                session.add(art)
                session.flush()  # get id

                # Cluster (pass entities for better matching)
                ents_objs = scores.get("entities") or []
                cluster = get_or_create_cluster(session, art, entities=ents_objs)

                new_count += 1
                logger.info(
                    "[SCORE] %s = %.0f  |  %s",
                    raw.title_original[:40],
                    art.priority_score,
                    raw.source,
                )

                # Notify? (Discord failure must never block ingestion)
                notify, is_high = should_notify(art)
                if notify:
                    try:
                        ents = [e.name for e in ents_objs]
                        cl_rows = (
                            session.query(Article.source, Article.url)
                            .filter_by(duplicate_group_id=cluster.id)
                            .all()
                        )
                        cl_sources = list(dict.fromkeys(r[0] for r in cl_rows))
                        cl_urls = list(dict.fromkeys(r[1] for r in cl_rows if r[1]))
                        payload = format_message(
                            art, ents, cl_sources, cl_urls, is_high
                        )
                        if send_discord(payload, dry_run=dry_run):
                            art.notified = True
                    except Exception as ne:
                        logger.error("[DISCORD] notify path error (ignored): %s", ne)

            except Exception as e:
                parse_err += 1
                logger.warning("[%s] Failed to process article: %s", source_name, e)

        # Health record. soft_fetch_html() swallows individual URL failures
        # (HTTP blocks, anti-bot interstitials) so fetch_latest() can still
        # return normally with an empty list — that must not be recorded as
        # an indistinguishable clean "zero results" run, or a genuinely
        # blocked source (e.g. hkepc/xfastest returning 402/403) looks
        # identical to a source that simply has nothing new right now.
        soft_errors = list(getattr(src, "fetch_error_log", None) or [])
        soft_blocked = bool(soft_errors)
        # Only escalate to a hard failure when NOTHING was retrieved and
        # fetches were failing — if some URLs succeeded (found > 0) this was
        # a partial degradation, not a failed run.
        success = (req_err == 0) and not (found == 0 and soft_blocked)
        error_message = "; ".join(soft_errors[:3])[:500] if soft_blocked else None
        run = SourceRun(
            source=source_name,
            layer="NEWS",
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            success=success,
            articles_found=found,
            articles_new=new_count,
            parse_errors=parse_err,
            request_errors=req_err + len(soft_errors),
            response_time_ms=int((time.monotonic() - t0) * 1000),
            error_message=error_message,
            soft_blocked=soft_blocked,
        )
        session.add(run)

    src.close()
    logger.info("[%s] %d new stories ingested", source_name, new_count)
    log_translation_stats()
    return new_count


def show_recent(limit: int = 15) -> None:
    with get_session() as session:
        arts = (
            session.query(Article)
            .order_by(Article.discovered_at.desc())
            .limit(limit)
            .all()
        )
        if not arts:
            print("No articles yet.")
            return
        for a in arts:
            print(
                f"{a.discovered_at.strftime('%H:%M')} [{a.source:10}] "
                f"{a.priority_score:5.1f} | {a.title_original[:70]}"
            )


def show_high_priority(limit: int = 10) -> None:
    thresh = yaml_config.get("notification", {}).get("priority_threshold", 70)
    with get_session() as session:
        arts = (
            session.query(Article)
            .filter(Article.priority_score >= thresh)
            .order_by(Article.priority_score.desc())
            .limit(limit)
            .all()
        )
        if not arts:
            print(f"No articles >= {thresh}")
            return
        for a in arts:
            print(
                f"{a.priority_score:5.1f} [{a.source}] {a.title_original[:80]}\n"
                f"         {a.url}"
            )


def show_clusters(limit: int = 15) -> None:
    """Inspect recent story clusters and their member articles."""
    with get_session() as session:
        clusters = (
            session.query(StoryCluster)
            .order_by(StoryCluster.updated_at.desc())
            .limit(limit)
            .all()
        )
        if not clusters:
            print("No clusters yet.")
            return
        for cl in clusters:
            members = (
                session.query(Article)
                .filter_by(duplicate_group_id=cl.id)
                .order_by(Article.discovered_at.asc())
                .all()
            )
            sources = list(dict.fromkeys(m.source for m in members))
            print("=" * 72)
            print(
                f"Cluster #{cl.id}  |  first: {cl.first_seen_source} @ "
                f"{cl.first_seen_at.strftime('%Y-%m-%d %H:%M') if cl.first_seen_at else '?'}  |  "
                f"members: {len(members)}  sources: {', '.join(sources)}"
            )
            print(f"  title: {(cl.representative_title or '')[:90]}")
            for m in members:
                en = f"  |  EN: {m.title_english[:50]}" if m.title_english else ""
                print(
                    f"  - [{m.source:10}] P={m.priority_score:5.1f} "
                    f"R={m.relevance_score:4.0f} N={m.novelty_score:4.0f}  "
                    f"{m.title_original[:55]}{en}"
                )
                print(f"    {m.url}")


def _run_translation_test() -> None:
    """Fixed headline test — second run should be a cache HIT."""
    import time as _time
    from pipeline.translate import get_translator, translation_stats, cache_lookup

    sample = "英伟达 RTX 5090 工程样机跑分曝光，性能大幅提升"
    translator = get_translator()
    provider = translator.provider_name
    model = translator.model_name or "—"

    # Detect cache state before call
    pre = cache_lookup(sample, "zh", "en", provider, model)
    t0 = _time.monotonic()
    result = translator.translate(sample)
    latency_ms = int((_time.monotonic() - t0) * 1000)
    cache_status = "HIT" if pre else ("MISS" if result else "FAIL")

    print(f"Provider:    {provider}")
    print(f"Model:       {model}")
    print(f"Original:    {sample}")
    print(f"Translation: {result or '(none — check API key / provider)'}")
    print(f"Cache:       {cache_status}")
    print(f"Latency:     {latency_ms} ms")
    stats = translation_stats()
    print(f"Stats:       requests={stats['requests']} cache_hits={stats['cache_hits']} failures={stats['failures']}")


def _run_translate_one(text: str) -> None:
    import time as _time
    from pipeline.translate import get_translator, cache_lookup

    translator = get_translator()
    pre = cache_lookup(text, "zh", "en", translator.provider_name, translator.model_name)
    t0 = _time.monotonic()
    result = translator.translate(text)
    latency_ms = int((_time.monotonic() - t0) * 1000)
    print(f"Provider:    {translator.provider_name}")
    print(f"Model:       {translator.model_name or '—'}")
    print(f"Original:    {text}")
    print(f"Translation: {result or '(none)'}")
    print(f"Cache:       {'HIT' if pre else ('MISS' if result else 'FAIL')}")
    print(f"Latency:     {latency_ms} ms")




def show_documentary(limit: int = 15) -> None:
    from database.models import DocumentaryRecord
    with get_session() as session:
        rows = (
            session.query(DocumentaryRecord)
            .order_by(DocumentaryRecord.priority_score.desc())
            .limit(limit)
            .all()
        )
        if not rows:
            print("No documentary records yet.")
            return
        for r in rows:
            print(
                f"{r.priority_score:5.1f} [{r.source:10}] {r.record_type:18} "
                f"{r.record_status:10} | {(r.title or r.source_record_id)[:55]}"
            )


def show_documentary_events(limit: int = 20) -> None:
    from database.models import DocumentaryEvent, DocumentaryRecord
    with get_session() as session:
        rows = (
            session.query(DocumentaryEvent)
            .order_by(DocumentaryEvent.observed_at.desc())
            .limit(limit)
            .all()
        )
        if not rows:
            print("No documentary events yet.")
            return
        for e in rows:
            print(f"{e.observed_at}  {e.event_type:22}  record={e.record_id}  {e.summary or ''}")


def show_community(limit: int = 15) -> None:
    """Recent high-value community signals."""
    from database.models import CommunityThread
    with get_session() as session:
        rows = (
            session.query(CommunityThread)
            .order_by(CommunityThread.priority_score.desc())
            .limit(limit)
            .all()
        )
        if not rows:
            print("No community threads yet.")
            return
        for t in rows:
            print(
                f"{t.priority_score:5.1f} [{t.platform:10}] {t.signal_type:18} "
                f"R={t.reply_count:<4} E={t.evidence_score:4.0f} | {t.title_original[:55]}"
            )
            print(f"         {t.url}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chinese Tech Wire V0.1")
    parser.add_argument("--once", action="store_true", help="Run one news-source cycle then exit")
    parser.add_argument("--full-once", action="store_true",
                        help="One complete cycle: news+community+documentary+leads")
    parser.add_argument("--scheduled", action="store_true",
                        help="Mark full-once as SCHEDULED trigger (Task Scheduler)")
    parser.add_argument("--source", type=str, help="Run only this source")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Discord")
    parser.add_argument("--show-recent", action="store_true")
    parser.add_argument("--show-high-priority", action="store_true")
    parser.add_argument("--show-clusters", action="store_true")
    parser.add_argument("--test-discord", action="store_true", help="Send a test webhook message")
    parser.add_argument("--preview-alerts", action="store_true",
                        help="Preview StoryLead Discord eligibility (no POST)")
    parser.add_argument("--diagnose-alerts", action="store_true",
                        help="Print soak diagnostics for alert policy")
    parser.add_argument("--test-storylead-discord", type=int, metavar="LEAD_ID",
                        help="Format a StoryLead payload; add --send to POST")
    parser.add_argument("--send", action="store_true",
                        help="With --test-storylead-discord, actually POST to Discord")
    parser.add_argument("--show-notifications", action="store_true",
                        help="Show recent LeadNotification ledger rows")
    parser.add_argument("--ignore-backlog-gate", action="store_true",
                        help="With --preview-alerts, ignore policy_activated_at gate")
    parser.add_argument("--source-health", action="store_true",
                        help="Print per-source health classification")
    parser.add_argument("--identity", action="store_true",
                        help="Print runtime identity (clank_id, version, release_channel) as JSON")
    parser.add_argument("--health", action="store_true",
                        help="Print truthful runtime health as JSON (exits non-zero only when failed)")
    parser.add_argument("--feedback-report", action="store_true",
                        help="Analyze stored lead feedback (read-only)")
    parser.add_argument("--test-translation", action="store_true", help="Translate a fixed headline and show cache status")
    parser.add_argument("--translate", type=str, metavar="TEXT", help="Translate arbitrary Chinese text")
    parser.add_argument("--show-community", action="store_true")
    parser.add_argument("--community-source", type=str, help="Run one community adapter")
    parser.add_argument("--community-once", action="store_true", help="Run all community sources once")
    parser.add_argument("--show-documentary", action="store_true")
    parser.add_argument("--show-documentary-events", action="store_true")
    parser.add_argument("--documentary-source", type=str)
    parser.add_argument("--documentary-once", action="store_true")
    parser.add_argument("--newsroom-brief", action="store_true")
    parser.add_argument("--rebuild-leads", action="store_true")
    parser.add_argument("--explain-lead", type=int, metavar="ID",
                        help="Human-readable StoryLead explanation")
    parser.add_argument("--explain-lead-json", type=int, metavar="ID",
                        help="Structured JSON explanation")
    parser.add_argument("--lead-timeline", type=int, metavar="ID",
                        help="Provenance timeline for a lead")
    parser.add_argument("--lead-audit", type=int, metavar="ID",
                        help="Material changes + alert audit")
    parser.add_argument("--lead-feedback", nargs=2, metavar=("ID", "FEEDBACK"))
    parser.add_argument("--since-hours", type=float, default=None)
    parser.add_argument("--since-days", type=float, default=None,
                        help="Convenience alias for --since-hours (multiplied by 24)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text (editorial reports)")
    parser.add_argument("--gui", action="store_true", help="Start local newsroom GUI")
    parser.add_argument("--gui-host", type=str, default="127.0.0.1")
    parser.add_argument("--gui-port", type=str, default="8000",
                        help="Port for --gui, or 'auto' to pick a free port dynamically "
                             "(tries 18760-18799, then an OS-assigned port)")
    parser.add_argument("--gui-open-browser", action="store_true",
                        help="With --gui, open the dashboard in the default browser once ready")

    # V0.5.6 — editorial outcomes
    parser.add_argument("--lead-outcome", nargs=2, metavar=("ID", "OUTCOME"),
                        help="Record an editorial outcome for a lead, e.g. --lead-outcome 3001 WRITTEN")
    parser.add_argument("--note", type=str, default=None, help="With --lead-outcome / --record-miss")
    parser.add_argument("--article-url", type=str, default=None, help="With --lead-outcome")
    parser.add_argument("--article-title", type=str, default=None, help="With --lead-outcome")
    parser.add_argument("--confirmation-url", type=str, default=None, help="With --lead-outcome")
    parser.add_argument("--show-outcomes", action="store_true", help="Show recent LeadOutcome ledger rows")
    parser.add_argument("--outcome-report", action="store_true", help="Outcome coverage summary")

    # V0.5.6 — editorial validation reports
    parser.add_argument("--editorial-report", action="store_true", help="Editorial conversion funnel")
    parser.add_argument("--source-performance", action="store_true", help="Source editorial usefulness (separate from operational health)")
    parser.add_argument("--lead-type-performance", action="store_true", help="Outcome breakdown by lead_type")
    parser.add_argument("--alert-performance", action="store_true", help="Discord alert usefulness")
    parser.add_argument("--lifecycle-report", action="store_true", help="Lifecycle attrition + outcome cross-reference")

    # V0.5.6 — missed stories
    parser.add_argument("--record-miss", action="store_true", help="Manually record a story CTW missed")
    parser.add_argument("--miss-title", type=str, default=None)
    parser.add_argument("--miss-source", type=str, default=None)
    parser.add_argument("--miss-article-url", type=str, default=None)
    parser.add_argument("--miss-entities", type=str, default=None)
    parser.add_argument("--miss-scope", type=str, default=None)
    parser.add_argument("--miss-failure-stage", type=str, default="UNKNOWN")
    parser.add_argument("--miss-notes", type=str, default=None)
    parser.add_argument("--miss-lead-id", type=int, default=None)
    parser.add_argument("--miss-cluster-id", type=int, default=None)
    parser.add_argument("--miss-reported-at", type=str, default=None, help="ISO timestamp, optional")
    parser.add_argument("--missed-story-report", action="store_true")
    parser.add_argument("--freshness-report", action="store_true",
                        help="StoryLead age-bucket / active-vs-archived report (read-only)")
    parser.add_argument("--show-missed-stories", action="store_true")
    parser.add_argument("--reconstruct-miss", type=int, metavar="ID",
                        help="Deterministic reconstruction of why a recorded miss happened")

    args = parser.parse_args()

    # Init DB
    init_db(settings.database_url)
    Path("data").mkdir(exist_ok=True)

    if args.identity:
        import json
        from runtime_bridge import get_identity
        print(json.dumps(get_identity(), indent=2, default=str))
        return

    if args.health:
        import json
        from runtime_bridge import get_health
        payload = get_health()
        print(json.dumps(payload, indent=2, default=str))
        if payload.get("operational_state") == "failed":
            sys.exit(1)
        return

    if args.test_discord:
        from pipeline.notify import test_webhook
        result = test_webhook()
        print(f"webhook_configured: {result.reason != 'NO_WEBHOOK'}")
        print(f"attempted: {result.attempted}")
        print(f"sent: {result.sent}")
        print(f"dry_run: {result.dry_run}")
        print(f"status_code: {result.status_code}")
        print(f"reason: {result.reason}")
        if result.error:
            print(f"error: {result.error}")
        return


    if args.test_translation:
        _run_translation_test()
        return
    if args.translate:
        _run_translate_one(args.translate)
        return
    if args.show_recent:
        show_recent()
        return
    if args.show_high_priority:
        show_high_priority()
        return
    if args.show_clusters:
        show_clusters()
        return
    if args.show_community:
        show_community()
        return
    if args.community_source:
        run_community_source(args.community_source, dry_run=args.dry_run)
        return
    if args.community_once:
        for name in COMMUNITY_REGISTRY:
            run_community_source(name, dry_run=args.dry_run)
        return
    if args.show_documentary:
        show_documentary()
        return
    if args.show_documentary_events:
        show_documentary_events()
        return
    if args.documentary_source:
        run_documentary_source(args.documentary_source, dry_run=args.dry_run)
        return
    if args.documentary_once:
        for name in DOCUMENTARY_REGISTRY:
            run_documentary_source(name, dry_run=args.dry_run)
        return

    if args.diagnose_alerts:
        from pipeline.alerts import diagnose_alert_policy
        init_db()
        d = diagnose_alert_policy()
        print("=== LEAD POPULATION ===")
        print(f"total StoryLeads: {d.get('n_leads')}")
        print(f"status: {d.get('status_counts')}")
        print(f"types: {d.get('type_counts')}")
        print(f"age: {d.get('age_buckets')}")
        print("\n=== SCORE DISTRIBUTIONS ===")
        for field, dist in (d.get("distributions") or {}).items():
            print(f"{field}: {dist}")
        print("\n=== ALERT FUNNEL (primary reason) ===")
        for k, v in (d.get("funnel") or {}).items():
            print(f"  {k}: {v}")
        print(f"  accounted: {d.get('funnel_accounted')}")
        print("\n=== PREDICTED VOLUME (eligible, not yet notified) ===")
        print(f"  last 24h: {d.get('predicted_alerts_24h')}")
        print(f"  last 48h: {d.get('predicted_alerts_48h')}")
        print(f"  last 7d:  {d.get('predicted_alerts_7d')}")
        print(f"  daily avg (7d): {d.get('predicted_daily_avg_7d')}")
        print(f"\npolicy_activated_at: {d.get('policy_activated_at')}")
        print(f"ledger SENT={d.get('ledger_sent')} FAILED={d.get('ledger_failed')}")
        print("\n=== CONFIG ===")
        for ck, cv in (d.get("config") or {}).items():
            print(f"  {ck}: {cv}")
        return


    if args.source_health:
        from pipeline.source_health import compute_source_health, format_source_health_text
        init_db()
        rows = compute_source_health(source_filter=args.source)
        if args.source and not rows:
            print(f"Unknown source: {args.source}")
        else:
            print(format_source_health_text(rows, detailed=bool(args.source)))
        return

    if args.feedback_report:
        from pipeline.alerts import feedback_report
        init_db()
        r = feedback_report()
        for k, v in r.items():
            print(f"{k}: {v}")
        return

    if args.preview_alerts:
        from pipeline.alerts import preview_alerts
        init_db()
        hours = args.since_hours if args.since_hours is not None else 24.0
        rows = preview_alerts(
            since_hours=hours,
            limit=args.limit or 25,
            ignore_backlog_gate=args.ignore_backlog_gate,
        )
        eligible = [r for r in rows if r["eligible"]]
        print(f"preview since_hours={hours} limit={args.limit or 25}")
        print(f"rows={len(rows)} eligible={len(eligible)} (no Discord POST)")
        for r in rows:
            flag = "SEND" if r["would_send"] else r["reason"]
            print(
                f"  #{r['id']:>5} P={r['score']:>5.1f} {r['status']:<12} "
                f"age={r['age_hours']}h evid={r['evidence']} rel={r['relevance']} conf={r['confidence']} "
                f"| {flag} | {(r['headline'] or '')[:60]}"
            )
        return

    if args.test_storylead_discord is not None:
        from database.models import StoryLead
        from pipeline.notify import build_storylead_payload, send_discord_result
        init_db()
        lid = args.test_storylead_discord
        with get_session() as session:
            lead = session.get(StoryLead, lid)
            if not lead:
                print(f"Lead #{lid} not found")
                return
            payload = build_storylead_payload(lead, high=float(lead.priority_score or 0) >= 55)
            print(f"lead #{lid} P={lead.priority_score} status={lead.lead_status}")
            print(f"headline: {lead.headline_hint}")
            if not args.send:
                print("preview only (pass --send to POST). notified state NOT mutated.")
                print(payload.get("content"))
                return
            result = send_discord_result(payload, dry_run=False)
            print(f"sent={result.sent} reason={result.reason} status={result.status_code}")
            if result.error:
                print(f"error: {result.error}")
            if result.sent:
                print("Note: test send does not set lead.notified (use production path for that).")
        return

    if args.show_notifications:
        from database.models import LeadNotification
        from sqlalchemy import select, desc
        init_db()
        with get_session() as session:
            rows = list(
                session.execute(
                    select(LeadNotification).order_by(desc(LeadNotification.attempted_at)).limit(args.limit or 20)
                ).scalars().all()
            )
            if not rows:
                print("No lead_notifications rows yet.")
                return
            for r in rows:
                print(
                    f"  {r.attempted_at} lead={r.lead_id} outcome={r.outcome} "
                    f"reason={r.reason_code} alert_reason={r.alert_reason} "
                    f"P={r.priority_score} HTTP={r.webhook_http_status} err={r.error_text}"
                )
        return



    if args.explain_lead is not None:
        from pipeline.explain import explain_lead_structured
        init_db()
        exp = explain_lead_structured(args.explain_lead)
        if not exp:
            print(f"Lead #{args.explain_lead} not found")
            return
        print(exp.human_text)
        return

    if args.explain_lead_json is not None:
        import json
        from pipeline.explain import explain_lead_structured
        init_db()
        exp = explain_lead_structured(args.explain_lead_json)
        if not exp:
            print(json.dumps({"error": "not_found", "lead_id": args.explain_lead_json}))
            return
        print(json.dumps(exp.to_dict(), ensure_ascii=False, indent=2, default=str))
        return

    if args.lead_timeline is not None:
        from pipeline.explain import build_lead_timeline, format_timeline_human
        init_db()
        events = build_lead_timeline(args.lead_timeline)
        print(format_timeline_human(events))
        return

    if args.lead_audit is not None:
        import json
        from pipeline.explain import lead_audit
        init_db()
        audit = lead_audit(args.lead_audit)
        if not audit:
            print(f"Lead #{args.lead_audit} not found")
            return
        print(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
        return

    if args.rebuild_leads:
        n = rebuild_leads(dry_run=args.dry_run)
        print(f"Rebuilt {n} leads")
        return
    if args.newsroom_brief:
        leads = list_leads(limit=args.limit, since_hours=args.since_hours)
        print(format_brief(leads))
        return
    if args.lead_feedback:
        lid, fb = args.lead_feedback
        ok = add_feedback(int(lid), fb)
        print("ok" if ok else "failed")
        return

    since_hours = args.since_hours
    if since_hours is None and args.since_days is not None:
        since_hours = args.since_days * 24.0

    if args.lead_outcome:
        from pipeline.outcomes import record_outcome
        lid, outcome = args.lead_outcome
        row = record_outcome(
            int(lid), outcome,
            note=args.note, article_url=args.article_url,
            article_title=args.article_title, confirmation_url=args.confirmation_url,
            outcome_source="CLI",
        )
        if not row:
            print(f"Failed to record outcome — invalid outcome value or lead #{lid} not found.")
            return
        print(f"Recorded outcome for lead #{lid}: {row.outcome} (is_final={row.is_final})")
        if row.related_article_url:
            print(f"  article: {row.related_article_url}")
        return

    if args.show_outcomes:
        from pipeline.outcomes import list_outcomes, format_outcomes_text
        rows = list_outcomes(limit=args.limit, since_hours=since_hours)
        if args.json:
            import json as _json
            print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_outcomes_text(rows))
        return

    if args.outcome_report:
        import json as _json
        from pipeline.outcomes import outcome_report
        r = outcome_report()
        if args.json:
            print(_json.dumps(r, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"Total leads:          {r['total_leads']}")
            print(f"Leads with outcome:   {r['leads_with_outcome']} ({r['outcome_coverage_pct']}%)")
            print("By outcome:")
            for k, v in sorted(r["counts"].items(), key=lambda kv: -kv[1]):
                print(f"  {k:<22} {v}")
        return

    if args.editorial_report:
        import json as _json
        from pipeline.editorial_validation import editorial_funnel, format_funnel_text
        r = editorial_funnel(since_hours=since_hours)
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_funnel_text(r))
        return

    if args.source_performance:
        import json as _json
        from pipeline.editorial_validation import source_performance, format_source_performance_text
        r = source_performance()
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_source_performance_text(r))
        return

    if args.lead_type_performance:
        import json as _json
        from pipeline.editorial_validation import lead_type_performance, format_lead_type_text
        r = lead_type_performance()
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_lead_type_text(r))
        return

    if args.alert_performance:
        import json as _json
        from pipeline.editorial_validation import alert_performance, format_alert_performance_text
        r = alert_performance()
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_alert_performance_text(r))
        return

    if args.lifecycle_report:
        import json as _json
        from pipeline.editorial_validation import lifecycle_report, format_lifecycle_text
        r = lifecycle_report()
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_lifecycle_text(r))
        return

    if args.record_miss:
        from pipeline.missed_stories import record_miss
        if not args.miss_title:
            print("--record-miss requires --miss-title")
            return
        reported_at = None
        if args.miss_reported_at:
            from datetime import datetime as _dt
            try:
                reported_at = _dt.fromisoformat(args.miss_reported_at)
            except ValueError:
                print(f"Could not parse --miss-reported-at {args.miss_reported_at!r} as ISO timestamp")
                return
        row = record_miss(
            title=args.miss_title, article_url=args.miss_article_url, source=args.miss_source,
            related_entities=args.miss_entities, expected_scope=args.miss_scope,
            failure_stage=args.miss_failure_stage, notes=args.miss_notes,
            matched_lead_id=args.miss_lead_id, matched_cluster_id=args.miss_cluster_id,
            reported_at=reported_at,
        )
        if not row:
            print("Failed to record missed story — check --miss-failure-stage value and any matched lead/cluster id.")
            return
        print(f"Recorded missed story #{row.id}: {row.title!r} [{row.failure_stage}]")
        return

    if args.show_missed_stories:
        from pipeline.missed_stories import list_missed_stories, format_missed_stories_text
        rows = list_missed_stories(limit=args.limit)
        if args.json:
            import json as _json
            print(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        else:
            print(format_missed_stories_text(rows))
        return

    if args.missed_story_report:
        import json as _json
        from pipeline.missed_stories import missed_story_report
        r = missed_story_report()
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else
              "\n".join([f"Total missed stories: {r['total']}",
                         f"Linked to lead:       {r['linked_to_lead']}",
                         f"Linked to cluster:    {r['linked_to_cluster']}",
                         f"Unlinked:             {r['unlinked']}",
                         "By failure stage:"] +
                        [f"  {k:<22} {v}" for k, v in sorted(r["by_failure_stage"].items(), key=lambda kv: -kv[1])]))
        return

    if args.freshness_report:
        import json as _json
        from pipeline.freshness import freshness_report, format_freshness_report_text
        r = freshness_report()
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_freshness_report_text(r))
        return

    if args.reconstruct_miss is not None:
        import json as _json
        from pipeline.missed_stories import reconstruct_miss, format_reconstruction_text
        r = reconstruct_miss(args.reconstruct_miss)
        if not r:
            print(f"Missed story #{args.reconstruct_miss} not found")
            return
        print(_json.dumps(r, ensure_ascii=False, indent=2, default=str) if args.json else format_reconstruction_text(r))
        return

    if args.gui:
        from web.app import run_gui
        from web.launcher import (
            find_existing_instance, find_free_port, open_browser,
            open_when_ready, write_runtime_state, clear_runtime_state,
        )
        host = args.gui_host
        auto = args.gui_port.strip().lower() == "auto"

        if auto:
            existing = find_existing_instance()
            if existing:
                print(f"CTW dashboard already running → http://{existing['host']}:{existing['port']}/")
                if args.gui_open_browser:
                    open_browser(existing["host"], existing["port"])
                return
            port = find_free_port(host)
        else:
            port = int(args.gui_port)

        if args.gui_open_browser:
            import threading
            threading.Thread(target=open_when_ready, args=(host, port), daemon=True).start()

        write_runtime_state(host, port)
        try:
            run_gui(host=host, port=port)
        finally:
            clear_runtime_state()
        return

    if args.full_once:
        from pipeline.full_cycle import run_full_cycle
        from pipeline.scheduled_log import setup_scheduled_logging
        trigger = "SCHEDULED" if args.scheduled else "MANUAL"
        if args.scheduled:
            setup_scheduled_logging()
        stats = run_full_cycle(trigger=trigger, dry_run=args.dry_run)
        print(stats.get("summary", stats))
        return

    sources = [args.source] if args.source else list(SOURCE_REGISTRY.keys())
    dry = args.dry_run

    if args.once or args.source:
        for s in sources:
            run_source(s, dry_run=dry)
        return

    # Continuous mode with simple interval (APScheduler can be added later)
    logger.info("Starting continuous monitoring. Sources: %s", sources)
    intervals = yaml_config.get("polling", {}).get("sources", {})
    default_iv = yaml_config.get("polling", {}).get("default_interval_minutes", 5) * 60
    last_run: Dict[str, float] = {s: 0.0 for s in sources}

    while True:
        now = time.monotonic()
        for s in sources:
            iv = intervals.get(s, default_iv / 60) * 60
            if now - last_run[s] >= iv:
                try:
                    run_source(s, dry_run=dry)
                except Exception as e:
                    logger.exception("Unhandled error in %s: %s", s, e)
                last_run[s] = time.monotonic()
        time.sleep(15)  # check every 15s


if __name__ == "__main__":
    main()
