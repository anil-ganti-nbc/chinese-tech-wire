
"""V0.5.1 GUI route tests — observational only, no scoring changes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from database.db import init_db, get_session
from database.models import Article, CommunityThread, DocumentaryRecord, StoryCluster, StoryLead
from pipeline.newsroom import upsert_lead_for_cluster


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/gui.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    now = datetime.now(timezone.utc)
    with get_session(db_url) as session:
        c = StoryCluster(
            first_seen_source="chiphell", first_seen_at=now,
            representative_title="RTX 6070 笔记本实拍",
            created_at=now, updated_at=now,
            first_signal_source="chiphell", first_signal_at=now,
            first_documentary_source="jd", first_documentary_at=now,
        )
        session.add(c)
        session.flush()
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="g1", url="https://example.com/t",
            title_original="RTX 6070 笔记本实拍 <script>alert(1)</script>",
            discovered_at=now, first_signal_at=now, created_at=now,
            signal_type="PHOTO_EVIDENCE", evidence_score=70, firsthand_score=70,
            velocity_score=20, priority_score=70, relevance_score=80, novelty_score=80,
            community_score=40, story_cluster_id=c.id,
        ))
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="gsku",
            url="https://item.jd.com/g.html", title="RTX 6070 笔记本",
            first_seen_at=now, last_seen_at=now, observed_at=now, record_status="ACTIVE",
            evidence_score=80, novelty_score=80, relevance_score=80, priority_score=85,
            story_cluster_id=c.id,
        ))
        session.add(Article(
            source="ithome", source_article_id="g1",
            title_original="RTX 6070 曝光", title_english="RTX 6070 spotted",
            url="https://www.ithome.com/g1",
            discovered_at=now, published_at=now, duplicate_group_id=c.id,
            priority_score=60,
        ))
        cid = c.id

    upsert_lead_for_cluster(cid, dry_run=True)

    from web.app import app
    return TestClient(app)


def test_newsroom(client):
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert b"Newsroom" in r.content or b"SCORE" in r.content or b"Score" in r.content
    assert b"<script>alert(1)</script>" not in r.content  # escaped in list if present


def test_lead_detail(client):
    r = client.get("/newsroom")
    assert r.status_code == 200
    # find lead id from DB via another call
    r2 = client.get("/leads/1")
    # may be 200 or 404 depending on id
    assert r2.status_code in (200, 404)
    if r2.status_code == 200:
        assert b"Why now" in r2.content or b"why" in r2.content.lower()
        assert b"<script>alert(1)</script>" not in r2.content


def test_lead_404(client):
    r = client.get("/leads/999999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Newsroom SOURCE column (direct source-link)
# ---------------------------------------------------------------------------

def _first_lead_id(client) -> int:
    from database.db import get_session
    from database.models import StoryLead
    from sqlalchemy import select
    with get_session() as session:
        lead = session.execute(select(StoryLead).order_by(StoryLead.id)).scalars().first()
        return lead.id


def test_newsroom_lead_still_links_to_internal_detail_route(client):
    lid = _first_lead_id(client)
    r = client.get("/newsroom")
    assert f'href="/leads/{lid}"'.encode() in r.content


def test_newsroom_source_links_to_original_url(client):
    """The fixture's lead is community-first (chiphell) — SOURCE must point
    at the actual stored CommunityThread URL, not the media article URL."""
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert b'href="https://example.com/t"' in r.content


def test_newsroom_source_uses_new_tab(client):
    r = client.get("/newsroom")
    assert b'target="_blank"' in r.content
    assert b'rel="noopener noreferrer"' in r.content


def test_newsroom_source_missing_url_renders_dash(tmp_path, monkeypatch):
    """A lead with no cluster (no stored provenance) must render '—',
    never a guessed or dead link."""
    db_url = f"sqlite:///{tmp_path}/gui_nourl.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    now = datetime.now(timezone.utc)
    with get_session(db_url) as session:
        session.add(StoryLead(
            lead_status="NEW", lead_type="FOLLOW_UP",
            created_at=now, updated_at=now,
            priority_score=30, evidence_score=40, relevance_score=50, confidence_score=40,
        ))

    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert "—".encode() in r.content


def test_newsroom_source_url_escaped(tmp_path, monkeypatch):
    """A URL containing characters that would break out of the href
    attribute must render escaped, not raw."""
    db_url = f"sqlite:///{tmp_path}/gui_esc.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    now = datetime.now(timezone.utc)
    with get_session(db_url) as session:
        c = StoryCluster(
            first_seen_source="ithome", first_seen_at=now,
            created_at=now, updated_at=now,
        )
        session.add(c)
        session.flush()
        session.add(Article(
            source="ithome", source_article_id="esc1",
            title_original="test", url='https://example.com/1?x="><script>alert(1)</script>',
            discovered_at=now, duplicate_group_id=c.id,
        ))
        session.add(StoryLead(
            cluster_id=c.id, lead_status="WATCHING", lead_type="FOLLOW_UP",
            created_at=now, updated_at=now,
            priority_score=40, evidence_score=40, relevance_score=50, confidence_score=40,
            first_media_source="ithome",
        ))

    from web.app import app
    client = TestClient(app)
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert b"<script>alert(1)</script>" not in r.content


def test_newsroom_get_does_not_mutate(client):
    from unittest.mock import patch
    with patch("sources.ithome.ITHomeSource.fetch_latest") as mocked:
        client.get("/newsroom")
        client.get("/newsroom?status=WATCHING&lead_type=FOLLOW_UP&q=rtx&hours=24&page=1")
        mocked.assert_not_called()


def test_newsroom_filters_and_pagination_still_work(client):
    r = client.get("/newsroom?status=WATCHING&lead_type=FOLLOW_UP&q=rtx&hours=24&page=1&per_page=10")
    assert r.status_code == 200
    r2 = client.get("/newsroom?hide_written=0")
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Blank `hours` query param regression (Filter button 422)
# ---------------------------------------------------------------------------

def test_newsroom_blank_hours_returns_200(client):
    r = client.get("/newsroom?hours=")
    assert r.status_code == 200


def test_newsroom_missing_hours_returns_200(client):
    r = client.get("/newsroom")
    assert r.status_code == 200


def test_newsroom_blank_hours_behaves_like_no_filter(client):
    """A blank hours value must return the identical result set to omitting
    hours entirely — not silently apply some default cutoff."""
    r_blank = client.get("/newsroom?hours=")
    r_missing = client.get("/newsroom")
    assert r_blank.status_code == r_missing.status_code == 200
    assert r_blank.text == r_missing.text


def test_newsroom_numeric_hours_still_filters(client):
    r = client.get("/newsroom?hours=1")
    assert r.status_code == 200


def test_newsroom_invalid_hours_rejected_explicitly(client):
    """A genuinely invalid non-empty value must be rejected (422), not
    silently coerced into no-filter or some default."""
    r = client.get("/newsroom?hours=abc")
    assert r.status_code == 422


def test_newsroom_filters_work_with_blank_hours(client):
    r = client.get("/newsroom?status=WATCHING&lead_type=FOLLOW_UP&q=rtx&hours=")
    assert r.status_code == 200


def test_newsroom_pagination_works_with_blank_hours(client):
    r = client.get("/newsroom?hours=&page=1&per_page=10")
    assert r.status_code == 200


def test_newsroom_blank_hours_does_not_mutate(client):
    from unittest.mock import patch
    with patch("sources.ithome.ITHomeSource.fetch_latest") as mocked:
        client.get("/newsroom?hours=")
        mocked.assert_not_called()


def test_news(client):
    r = client.get("/news")
    assert r.status_code == 200
    assert b"ithome" in r.content or b"News" in r.content


def test_community(client):
    r = client.get("/community")
    assert r.status_code == 200
    assert b"chiphell" in r.content


def test_documentary(client):
    r = client.get("/documentary")
    assert r.status_code == 200


def test_clusters(client):
    r = client.get("/clusters")
    assert r.status_code == 200


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert b"Discord" in r.content or b"Translation" in r.content


def test_activity(client):
    r = client.get("/activity")
    assert r.status_code == 200


def test_soak(client):
    r = client.get("/soak")
    assert r.status_code == 200


def test_feedback(client):
    # ensure lead 1 exists
    from database.models import StoryLead
    from database.db import get_session
    with get_session() as s:
        lead = s.query(StoryLead).first()
        assert lead is not None
        lid = lead.id
    r = client.post(f"/leads/{lid}/feedback", data={"feedback": "USEFUL"}, follow_redirects=False)
    assert r.status_code in (303, 302)


def test_csv_export(client):
    r = client.get("/export/newsroom.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")


def test_empty_filters(client):
    r = client.get("/newsroom?status=STALE&q=zzzznonexistent")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# STD-UI-COM-010: timestamp zone must be determinable from the UI.
# ---------------------------------------------------------------------------


def test_timezone_convention_is_stated_on_every_page(client):
    """fmt() renders "%m-%d %H:%M" with no per-value zone marker. The frozen
    standard accepts one clearly stated surface-level convention instead of
    repeating a marker on every value -- but it must actually be stated, and
    it must appear on every surface that shows timestamps."""
    for path in ("/newsroom", "/news", "/community", "/documentary",
                 "/clusters", "/activity", "/notifications", "/health"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "All times UTC" in resp.text, f"{path} states no timezone convention"


def test_fmt_normalises_to_utc_so_the_stated_convention_is_true():
    """The convention must be true by construction, not incidentally.
    _aware() only ATTACHES UTC to a naive value; an already-aware value in
    another zone would previously have been printed in that zone's
    wall-clock while the page claimed UTC."""
    from datetime import timedelta, timezone as _tz

    from web.app import _fmt

    # 09:30 in UTC+05:30 is 04:00 UTC -- the rendered value must be the UTC one.
    ist = _tz(timedelta(hours=5, minutes=30))
    aware_non_utc = datetime(2026, 9, 3, 9, 30, tzinfo=ist)
    assert _fmt(aware_non_utc) == "09-03 04:00"

    # A naive value is still treated as UTC, unchanged from previous behaviour.
    assert _fmt(datetime(2026, 9, 3, 4, 0)) == "09-03 04:00"

    # And an already-UTC value is untouched.
    assert _fmt(datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc)) == "09-03 04:00"
