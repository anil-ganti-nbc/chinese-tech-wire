"""PTT audit regression tests (post-outage disablement).

Root cause established by live probe (see HANDOFF/response for the full
audit): PTT's own web gateway (ptt.cc) is returning HTTP 500 "Server Too
Busy" on the board index, another board, hotboards.html, and 10/10 sampled
stored article URLs — a PTT-side reliability issue, not a CTW URL
construction bug and not an anti-bot block. PTT was removed from the
active COMMUNITY_REGISTRY (mirroring the existing geekbench precedent) and
marked DISABLED in source-health. All historical PTT data is preserved.

No live network access in these tests — parser fixtures and DB fixtures only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

from community_sources import COMMUNITY_REGISTRY, DISABLED_COMMUNITY_SOURCES, PTTSource
from database.db import get_session, init_db
from database.models import CommunityThread, StoryCluster, StoryLead
from pipeline.explain import primary_source_url


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/ptt_disable.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


# ---------------------------------------------------------------------------
# URL construction — confirms the root cause is NOT a CTW-side bug
# ---------------------------------------------------------------------------

def test_ptt_canonical_url_construction_is_correct():
    """The parser itself builds well-formed, real PTT article URLs — the
    HTTP 500s are PTT-side, not caused by malformed CTW URL construction."""
    html = (FIXTURES / "ptt_list_sample.html").read_text(encoding="utf-8")
    src = PTTSource()
    with patch.object(src, "soft_fetch_html", return_value=html):
        threads = src.fetch_recent_threads()
    assert threads
    for t in threads:
        assert t.url.startswith("https://www.ptt.cc/bbs/PC_Shopping/M.")
        assert t.url.endswith(".html")
        assert t.canonical_url == t.url


# ---------------------------------------------------------------------------
# Source disabled state
# ---------------------------------------------------------------------------

def test_ptt_not_in_active_community_registry():
    assert "ptt" not in COMMUNITY_REGISTRY


def test_ptt_in_disabled_community_sources_with_reason():
    assert "ptt" in DISABLED_COMMUNITY_SOURCES
    assert DISABLED_COMMUNITY_SOURCES["ptt"]  # non-empty reason string


def test_ptt_adapter_class_still_importable():
    """PTTSource itself is kept for parser compatibility / easy re-enable —
    only the active registry entry was removed."""
    assert PTTSource.name == "ptt"


# ---------------------------------------------------------------------------
# Historical data preserved
# ---------------------------------------------------------------------------

def test_historical_ptt_records_remain_readable(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        session.add(CommunityThread(
            platform="ptt", region="TW", language_variant="zh-TW",
            thread_id="hist-1", url="https://www.ptt.cc/bbs/PC_Shopping/M.1.html",
            title_original="historical ptt post",
            created_at=now, discovered_at=now,
            signal_type="DISCUSSION", priority_score=10,
        ))
    with get_session() as session:
        rows = session.execute(select(CommunityThread).where(CommunityThread.platform == "ptt")).scalars().all()
    assert len(rows) == 1
    assert rows[0].url == "https://www.ptt.cc/bbs/PC_Shopping/M.1.html"


def test_historical_ptt_leads_and_clusters_not_deleted(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(
            first_seen_source="ptt", first_seen_at=now, created_at=now, updated_at=now,
        )
        session.add(c)
        session.flush()
        lead = StoryLead(
            cluster_id=c.id, lead_status="WATCHING", lead_type="EARLY_SIGNAL",
            created_at=now, updated_at=now,
            priority_score=40, evidence_score=40, relevance_score=50, confidence_score=40,
            first_signal_source="ptt",
        )
        session.add(lead)
        cid, lid = c.id, None
    with get_session() as session:
        cluster = session.get(StoryCluster, cid)
        assert cluster is not None
        lead = session.execute(select(StoryLead).where(StoryLead.cluster_id == cid)).scalar_one_or_none()
        assert lead is not None
        assert lead.first_signal_source == "ptt"


# ---------------------------------------------------------------------------
# full-cycle ignores the disabled source without error
# ---------------------------------------------------------------------------

def test_full_cycle_skips_disabled_ptt_without_error(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    seen_community_sources = []

    def fake_run_community_source(name, dry_run=False):
        seen_community_sources.append(name)
        return 0

    with patch("main.run_source", return_value=0), \
         patch("pipeline.community_ingest.run_community_source", side_effect=fake_run_community_source), \
         patch("pipeline.documentary_ingest.run_documentary_source", return_value=0), \
         patch("pipeline.newsroom.rebuild_leads", return_value=0), \
         patch("sources.SOURCE_REGISTRY", {}), \
         patch("community_sources.COMMUNITY_REGISTRY", COMMUNITY_REGISTRY), \
         patch("documentary_sources.DOCUMENTARY_REGISTRY", {}):
        from pipeline.full_cycle import run_full_cycle
        stats = run_full_cycle(trigger="MANUAL", dry_run=True)
    assert stats["error_count"] == 0
    assert stats["status"] == "SUCCESS"
    assert "ptt" not in seen_community_sources  # disabled source never iterated
    assert set(seen_community_sources) == set(COMMUNITY_REGISTRY.keys())


# ---------------------------------------------------------------------------
# source-health reports the intended status
# ---------------------------------------------------------------------------

def test_source_health_reports_ptt_disabled(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from pipeline.source_health import compute_source_health
    rows = compute_source_health()
    ptt = next(r for r in rows if r["source"] == "ptt")
    assert ptt["status"] == "DISABLED"
    assert ptt["layer"] == "COMMUNITY"


def test_source_health_ptt_disabled_even_with_stray_runs(tmp_path, monkeypatch):
    """Matches the existing geekbench precedent: DISABLED must not be
    overridden by leftover SourceRun rows."""
    _db(tmp_path, monkeypatch)
    from database.models import SourceRun
    now = _now()
    with get_session() as session:
        session.add(SourceRun(
            source="ptt", layer="COMMUNITY", started_at=now, finished_at=now,
            success=True, articles_found=5, articles_new=5,
        ))
    from pipeline.source_health import compute_source_health
    rows = compute_source_health()
    ptt = next(r for r in rows if r["source"] == "ptt")
    assert ptt["status"] == "DISABLED"


# ---------------------------------------------------------------------------
# primary_source_url: fallback + no fabricated replacement
# ---------------------------------------------------------------------------

def test_primary_source_falls_back_when_ptt_is_first_signal(tmp_path, monkeypatch):
    """A lead whose first_signal_source is ptt, but whose cluster also has
    a real Article, must link to that Article — not the dead PTT page."""
    _db(tmp_path, monkeypatch)
    from database.models import Article
    now = _now()
    with get_session() as session:
        c = StoryCluster(first_seen_source="ptt", first_seen_at=now, created_at=now, updated_at=now)
        session.add(c)
        session.flush()
        session.add(CommunityThread(
            platform="ptt", region="TW", language_variant="zh-TW",
            thread_id="t1", url="https://www.ptt.cc/bbs/PC_Shopping/M.1.html",
            title_original="ptt thread", created_at=now, discovered_at=now,
            signal_type="DISCUSSION", priority_score=10, story_cluster_id=c.id,
        ))
        session.add(Article(
            source="ithome", source_article_id="a1", title_original="real article",
            url="https://www.ithome.com/a1", discovered_at=now, duplicate_group_id=c.id,
        ))
        lead = StoryLead(
            cluster_id=c.id, lead_status="WATCHING", lead_type="EARLY_SIGNAL",
            created_at=now, updated_at=now,
            priority_score=40, evidence_score=40, relevance_score=50, confidence_score=40,
            first_signal_source="ptt",
        )
        session.add(lead)
        session.flush()
        lid = lead.id

    with get_session() as session:
        lead = session.get(StoryLead, lid)
        url = primary_source_url(lead, session=session)
    assert url == "https://www.ithome.com/a1"  # fell back, not the dead ptt link


def test_primary_source_returns_none_when_only_ptt_available(tmp_path, monkeypatch):
    """A cluster with only PTT records (like the real live #3001 case) must
    render nothing usable — never a fabricated or known-dead URL."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(first_seen_source="ptt", first_seen_at=now, created_at=now, updated_at=now)
        session.add(c)
        session.flush()
        session.add(CommunityThread(
            platform="ptt", region="TW", language_variant="zh-TW",
            thread_id="t1", url="https://www.ptt.cc/bbs/PC_Shopping/M.1.html",
            title_original="ptt thread", created_at=now, discovered_at=now,
            signal_type="DISCUSSION", priority_score=10, story_cluster_id=c.id,
        ))
        lead = StoryLead(
            cluster_id=c.id, lead_status="WATCHING", lead_type="EARLY_SIGNAL",
            created_at=now, updated_at=now,
            priority_score=40, evidence_score=40, relevance_score=50, confidence_score=40,
            first_signal_source="ptt",
        )
        session.add(lead)
        session.flush()
        lid = lead.id

    with get_session() as session:
        lead = session.get(StoryLead, lid)
        url = primary_source_url(lead, session=session)
    assert url is None  # caller renders "—", never a guessed/dead link


def test_newsroom_renders_dash_for_ptt_only_lead(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(first_seen_source="ptt", first_seen_at=now, created_at=now, updated_at=now)
        session.add(c)
        session.flush()
        session.add(CommunityThread(
            platform="ptt", region="TW", language_variant="zh-TW",
            thread_id="t1", url="https://www.ptt.cc/bbs/PC_Shopping/M.1.html",
            title_original="ptt thread", created_at=now, discovered_at=now,
            signal_type="DISCUSSION", priority_score=10, story_cluster_id=c.id,
        ))
        session.add(StoryLead(
            cluster_id=c.id, lead_status="WATCHING", lead_type="EARLY_SIGNAL",
            created_at=now, updated_at=now,
            priority_score=40, evidence_score=40, relevance_score=50, confidence_score=40,
            first_signal_source="ptt",
        ))

    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert "https://www.ptt.cc" not in r.text  # never linked
    assert "—" in r.text
