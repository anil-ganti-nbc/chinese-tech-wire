"""V0.5.5.1 — Source Health Integrity regression tests.

Covers:
- community/documentary ingest layers now emit SourceRun telemetry
  (previously only NEWS did, which made chiphell/mobile01/jd permanently
  NEVER_PROVEN despite real historical ingestion).
- a caught fetch failure (soft_fetch_html swallowing an HTTP block) no
  longer masquerades as a clean "zero results" success.
- classify_source() honestly distinguishes QUIET (reached fine, nothing
  new) from DEGRADED (found nothing despite a clean fetch) from BLOCKED
  (documented, consistently refused at the network layer).

All network access is mocked — no live HTTP calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select

from database.db import get_session, init_db
from database.models import CommunityThread, SourceRun
from community_sources.base import RawThread
from community_sources.chiphell import ChiphellSource
from documentary_sources.base import RawDocumentary
from documentary_sources.jd import JDSource
from pipeline.community_ingest import run_community_source
from pipeline.documentary_ingest import run_documentary_source
from pipeline.source_health import classify_source, compute_source_health


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/v0551.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _thread(tid="t1", title="測試主題", created=None):
    return RawThread(
        platform="chiphell",
        thread_id=tid,
        title_original=title,
        url=f"https://www.chiphell.com/thread-{tid}.html",
        created_at=created or _now(),
        reply_count=1,
        view_count=10,
    )


def _doc(rid="sku1", title="RTX 6090 上市"):
    return RawDocumentary(
        record_type="RETAIL_LISTING",
        source="jd",
        source_record_id=rid,
        url=f"https://item.jd.com/{rid}.html",
        title=title,
        structured={"price": 9999},
    )


# ---------------------------------------------------------------------------
# Telemetry emission
# ---------------------------------------------------------------------------

def test_community_source_emits_sourcerun(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with patch.object(ChiphellSource, "fetch_recent_threads", return_value=[_thread("a1")]):
        n = run_community_source("chiphell", dry_run=True)
    assert n == 1
    with get_session() as session:
        runs = session.execute(
            select(SourceRun).where(SourceRun.source == "chiphell")
        ).scalars().all()
    assert len(runs) == 1
    assert runs[0].layer == "COMMUNITY"
    assert runs[0].success is True
    assert runs[0].articles_found == 1
    assert runs[0].articles_new == 1


def test_documentary_source_emits_sourcerun(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with patch.object(JDSource, "fetch_latest", return_value=[_doc("sku1")]):
        n = run_documentary_source("jd", dry_run=True)
    assert n == 1
    with get_session() as session:
        runs = session.execute(
            select(SourceRun).where(SourceRun.source == "jd")
        ).scalars().all()
    assert len(runs) == 1
    assert runs[0].layer == "DOCUMENTARY"
    assert runs[0].success is True
    assert runs[0].articles_found == 1


def test_community_zero_result_run_is_clean_success(tmp_path, monkeypatch):
    """Genuinely empty listing (no fetch errors) must record success=True, soft_blocked=False."""
    _db(tmp_path, monkeypatch)
    with patch.object(ChiphellSource, "fetch_recent_threads", return_value=[]):
        n = run_community_source("chiphell", dry_run=True)
    assert n == 0
    with get_session() as session:
        run = session.execute(select(SourceRun).where(SourceRun.source == "chiphell")).scalar_one()
    assert run.success is True
    assert run.articles_found == 0
    assert run.soft_blocked is False


def test_community_nonzero_run_success(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    threads = [_thread("a1"), _thread("a2"), _thread("a3")]
    with patch.object(ChiphellSource, "fetch_recent_threads", return_value=threads):
        n = run_community_source("chiphell", dry_run=True)
    assert n == 3
    with get_session() as session:
        stored = session.execute(select(CommunityThread)).scalars().all()
    assert len(stored) == 3


def test_documentary_soft_failed_run_marks_blocked(tmp_path, monkeypatch):
    """Simulate JD's anti-bot soft-fail: soft_fetch_html swallows the block,
    fetch_latest() returns [] normally, but fetch_error_log records it.
    The run must NOT be recorded as an indistinguishable clean success.
    """
    _db(tmp_path, monkeypatch)

    def blocked_fetch(self):
        self.fetch_error_log.append("HTTP 403 for https://search.jd.com/Search?keyword=x")
        return []

    with patch.object(JDSource, "fetch_latest", blocked_fetch):
        n = run_documentary_source("jd", dry_run=True)
    assert n == 0
    with get_session() as session:
        run = session.execute(select(SourceRun).where(SourceRun.source == "jd")).scalar_one()
    assert run.soft_blocked is True
    assert run.success is False
    assert run.articles_found == 0
    assert "403" in (run.error_message or "")


def test_request_failure_records_hard_error(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with patch.object(ChiphellSource, "fetch_recent_threads", side_effect=RuntimeError("network down")):
        n = run_community_source("chiphell", dry_run=True)
    assert n == 0
    with get_session() as session:
        run = session.execute(select(SourceRun).where(SourceRun.source == "chiphell")).scalar_one()
    assert run.success is False
    assert run.request_errors >= 1


def test_community_partial_soft_block_still_succeeds(tmp_path, monkeypatch):
    """If some content still came through despite a partial fetch error,
    the run is a real (if degraded) success, not a hard failure."""
    _db(tmp_path, monkeypatch)

    def partial_fetch(self):
        self.fetch_error_log.append("HTTP 500 for https://www.chiphell.com/forum-2-1.html")
        return [_thread("a1")]

    with patch.object(ChiphellSource, "fetch_recent_threads", partial_fetch):
        n = run_community_source("chiphell", dry_run=True)
    assert n == 1
    with get_session() as session:
        run = session.execute(select(SourceRun).where(SourceRun.source == "chiphell")).scalar_one()
    assert run.soft_blocked is True
    assert run.success is True  # got real content despite the partial error
    assert run.articles_found == 1


# ---------------------------------------------------------------------------
# Classification honesty
# ---------------------------------------------------------------------------

def test_never_proven_only_when_genuinely_unsupported(tmp_path, monkeypatch):
    """Before any run: NEVER_PROVEN. After one real run (even zero-result):
    the source is proven and must not remain NEVER_PROVEN."""
    _db(tmp_path, monkeypatch)
    rows = compute_source_health()
    chiphell = next(r for r in rows if r["source"] == "chiphell")
    assert chiphell["status"] == "NEVER_PROVEN"

    with patch.object(ChiphellSource, "fetch_recent_threads", return_value=[_thread("a1")]):
        run_community_source("chiphell", dry_run=True)

    rows = compute_source_health()
    chiphell = next(r for r in rows if r["source"] == "chiphell")
    assert chiphell["status"] != "NEVER_PROVEN"


def test_historical_records_without_telemetry_not_fabricated(tmp_path, monkeypatch):
    """A source with real historical records but zero SourceRun rows must
    stay honestly NEVER_PROVEN (telemetry gap), never invented as HEALTHY."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        session.add(CommunityThread(
            platform="mobile01",
            region="TW",
            language_variant="zh-TW",
            thread_id="hist-1",
            url="https://www.mobile01.com/topicdetail.php?f=1&t=1",
            title_original="歷史紀錄",
            created_at=now - timedelta(days=3),
            discovered_at=now - timedelta(days=3),
            signal_type="DISCUSSION",
            priority_score=10,
        ))
    rows = compute_source_health()
    mobile01 = next(r for r in rows if r["source"] == "mobile01")
    assert mobile01["status"] == "NEVER_PROVEN"
    assert "1" in mobile01["note"] or "historical" in mobile01["note"]


def test_partial_jd_semantics(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="jd", layer="DOCUMENTARY",
            started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
            success=True, articles_found=2, articles_new=0,
        )
        for i in range(3)
    ]
    row = classify_source("jd", "DOCUMENTARY", runs, counts_24h=0, counts_7d=0)
    assert row["known_limitation"] == "PARTIAL"
    assert "PARTIAL" in row["note"]


def test_prolonged_zero_result_degraded(tmp_path, monkeypatch):
    """Clean fetches (no errors) that repeatedly find NOTHING is a much
    stronger signal than 'found the same listing, nothing new' — must be
    DEGRADED, not silently HEALTHY/QUIET."""
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="ithome", layer="NEWS",
            started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
            success=True, articles_found=0, articles_new=0, soft_blocked=False,
        )
        for i in range(8)
    ]
    row = classify_source("ithome", "NEWS", runs, counts_24h=0, counts_7d=0)
    assert row["status"] == "DEGRADED"
    assert "parser drift" in row["note"] or "found=0" in row["note"]


def test_quiet_not_degraded_when_found_nonzero(tmp_path, monkeypatch):
    """The benchlife ground-truth case: found>0 every run (adapter/parser
    fine), just nothing NEW for a while — must be QUIET, not DEGRADED."""
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="benchlife", layer="NEWS",
            started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
            success=True, articles_found=8, articles_new=0, soft_blocked=False,
        )
        for i in range(8)
    ]
    row = classify_source("benchlife", "NEWS", runs, counts_24h=0, counts_7d=0)
    assert row["status"] == "QUIET"


def test_live_nonzero_run_resets_zero_streak(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="zol", layer="NEWS",
            started_at=now, finished_at=now,
            success=True, articles_found=5, articles_new=2,
        ),
    ] + [
        SourceRun(
            source="zol", layer="NEWS",
            started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
            success=True, articles_found=5, articles_new=0,
        )
        for i in range(1, 8)
    ]
    row = classify_source("zol", "NEWS", runs, counts_24h=2, counts_7d=2)
    assert row["zero_streak"] == 0
    assert row["status"] == "HEALTHY"


def test_blocked_classification_hkepc(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="hkepc", layer="NEWS",
            started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
            success=False, articles_found=0, articles_new=0,
            soft_blocked=True, error_message="HTTP 402 for https://www.hkepc.com/",
        )
        for i in range(5)
    ]
    row = classify_source("hkepc", "NEWS", runs, counts_24h=0, counts_7d=0)
    assert row["status"] == "BLOCKED"
    assert row["known_limitation"] == "BLOCKED"


def test_blocked_self_heals_on_real_success(tmp_path, monkeypatch):
    """A documented-BLOCKED source that unexpectedly gets real content
    through must not remain stuck labeled BLOCKED forever."""
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="hkepc", layer="NEWS",
            started_at=now, finished_at=now,
            success=True, articles_found=3, articles_new=1,
        ),
    ] + [
        SourceRun(
            source="hkepc", layer="NEWS",
            started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
            success=False, articles_found=0, articles_new=0, soft_blocked=True,
        )
        for i in range(1, 5)
    ]
    row = classify_source("hkepc", "NEWS", runs, counts_24h=1, counts_7d=1)
    assert row["status"] != "BLOCKED"


def test_disabled_source_remains_disabled(tmp_path, monkeypatch):
    """geekbench must stay DISABLED even if stray SourceRun rows exist."""
    _db(tmp_path, monkeypatch)
    now = _now()
    runs = [
        SourceRun(
            source="geekbench", layer="DOCUMENTARY",
            started_at=now, finished_at=now,
            success=True, articles_found=5, articles_new=5,
        )
    ]
    row = classify_source("geekbench", "DOCUMENTARY", runs, counts_24h=5, counts_7d=5)
    assert row["status"] == "DISABLED"


def test_source_health_classification_is_layer_aware(tmp_path, monkeypatch):
    """compute_source_health must tag each registry source with its actual
    layer (NEWS/COMMUNITY/DOCUMENTARY), not lump everything as NEWS."""
    _db(tmp_path, monkeypatch)
    rows = compute_source_health()
    by_name = {r["source"]: r for r in rows}
    assert by_name["ithome"]["layer"] == "NEWS"
    assert by_name["chiphell"]["layer"] == "COMMUNITY"
    assert by_name["jd"]["layer"] == "DOCUMENTARY"
