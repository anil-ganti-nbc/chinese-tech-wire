"""V0.3 Community Intelligence tests — fixtures + synthetic signal cases."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from community_sources.base import RawThread
from community_sources.chiphell import ChiphellSource
from community_sources.coolaler import CoolalerSource
from community_sources.mobile01 import Mobile01Source
from community_sources.ptt import PTTSource
from pipeline.community_score import (
    classify_signal,
    compute_priority,
    score_thread,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Adapter fixture parsing
# ---------------------------------------------------------------------------

def test_chiphell_fixture():
    html = (FIXTURES / "chiphell_list_sample.html").read_text(encoding="utf-8")
    src = ChiphellSource()
    with patch.object(src, "soft_fetch_html", return_value=html):
        # soft_fetch is called per board URL — return same fixture
        threads = src.fetch_recent_threads()
    assert len(threads) >= 2
    ids = {t.thread_id for t in threads}
    assert "2681983" in ids or "2682001" in ids
    assert src.region == "CN"


def test_ptt_fixture():
    html = (FIXTURES / "ptt_list_sample.html").read_text(encoding="utf-8")
    src = PTTSource()
    with patch.object(src, "soft_fetch_html", return_value=html):
        threads = src.fetch_recent_threads()
    assert len(threads) >= 2
    assert any("RTX 5090" in t.title_original for t in threads)
    assert src.region == "TW"
    assert src.language_variant == "zh-TW"


def test_coolaler_fixture():
    html = (FIXTURES / "coolaler_list_sample.html").read_text(encoding="utf-8")
    src = CoolalerSource()
    with patch.object(src, "soft_fetch_html", return_value=html):
        threads = src.fetch_recent_threads()
    assert len(threads) >= 1
    assert any(t.thread_id == "406234" for t in threads)


def test_mobile01_fixture():
    html = (FIXTURES / "mobile01_list_sample.html").read_text(encoding="utf-8")
    src = Mobile01Source()
    with patch.object(src, "soft_fetch_html", return_value=html):
        threads = src.fetch_recent_threads()
    assert len(threads) >= 1
    assert any("7106510" == t.thread_id for t in threads)


# ---------------------------------------------------------------------------
# Case A — pure speculation must be capped
# ---------------------------------------------------------------------------

def test_case_a_speculation_capped():
    raw = RawThread(
        platform="ptt",
        thread_id="spec1",
        title_original="我覺得 RTX 6090 會有 48GB",
        url="https://www.ptt.cc/bbs/PC_Shopping/spec1.html",
        op_text_original="個人認為下一代旗艦大概是 48GB，應該很強。",
        reply_count=300,
        view_count=8000,
        region="TW",
        language_variant="zh-TW",
    )
    scores = score_thread(raw, hours_old=1.0)
    assert scores["signal_type"] == "SPECULATION"
    assert scores["evidence_score"] < 40
    assert scores["firsthand_score"] < 40
    assert scores["priority_score"] <= 60  # speculation cap


# ---------------------------------------------------------------------------
# Case B — original photo evidence
# ---------------------------------------------------------------------------

def test_case_b_photo_evidence():
    raw = RawThread(
        platform="chiphell",
        thread_id="photo1",
        title_original="我拿到了RTX 5090工程样品实拍",
        url="https://www.chiphell.com/forum.php?mod=viewthread&tid=photo1",
        op_text_original="今天经销商送来工程样品，实拍三张如图，已上机。",
        reply_count=45,
        view_count=1200,
        image_count=3,
        region="CN",
    )
    scores = score_thread(raw)
    assert scores["signal_type"] in ("PHOTO_EVIDENCE", "FIRSTHAND_CLAIM")
    assert scores["evidence_score"] >= 50
    assert scores["firsthand_score"] >= 50
    assert scores["relevance_score"] >= 50
    assert scores["priority_score"] >= 60


# ---------------------------------------------------------------------------
# Case C — repost of news
# ---------------------------------------------------------------------------

def test_case_c_repost():
    raw = RawThread(
        platform="mobile01",
        thread_id="repost1",
        title_original="轉載 ITHome：AMD 正式發布 Ryzen 9000",
        url="https://www.mobile01.com/topicdetail.php?t=repost1",
        op_text_original="原文：https://www.ithome.com/0/981/069.htm 大家怎麼看？",
        reply_count=20,
        region="TW",
        language_variant="zh-TW",
    )
    scores = score_thread(raw)
    assert scores["signal_type"] == "REPOST"
    assert scores["novelty_score"] < 30
    assert scores["priority_score"] <= 45


# ---------------------------------------------------------------------------
# Case D — chronology helpers (unit-level)
# ---------------------------------------------------------------------------

def test_case_d_chronology_ordering():
    """UTC instants: Chiphell 08:52 local CN, BenchLife 09:31 local TW."""
    from sources.ithome import ITHomeSource
    from sources.benchlife import BenchLifeSource
    from community_sources.chiphell import ChiphellSource

    ch = ChiphellSource()
    bl = BenchLifeSource()
    # 08:52 Asia/Shanghai = 00:52 UTC
    t_ch = ch.localize_naive(datetime(2026, 7, 24, 8, 52))
    # 09:31 Asia/Taipei = 01:31 UTC
    t_bl = bl.localize_naive(datetime(2026, 7, 24, 9, 31))
    assert t_ch < t_bl
    lead_minutes = (t_bl - t_ch).total_seconds() / 60
    assert abs(lead_minutes - 39) < 0.1


# ---------------------------------------------------------------------------
# Case E — fake corroboration (repost link)
# ---------------------------------------------------------------------------

def test_case_e_fake_corroboration_is_repost():
    raw = RawThread(
        platform="ptt",
        thread_id="fake_cor",
        title_original="Chiphell 有人拍到 5090",
        url="https://www.ptt.cc/bbs/PC_Shopping/fake.html",
        op_text_original="原文在這 https://www.chiphell.com/forum.php?mod=viewthread&tid=999",
        region="TW",
    )
    scores = score_thread(raw)
    # linking another forum is not independent evidence
    assert scores["corroboration_score"] == 0.0


# ---------------------------------------------------------------------------
# Case F — independent-ish signal classification
# ---------------------------------------------------------------------------

def test_case_f_independent_photo_not_repost():
    raw = RawThread(
        platform="mobile01",
        thread_id="ind1",
        title_original="RTX 5090 PCB 實拍分享",
        url="https://www.mobile01.com/topicdetail.php?t=ind1",
        op_text_original="朋友從通路拿到的板子，實拍如下，沒有轉載。",
        image_count=2,
        region="TW",
        language_variant="zh-TW",
    )
    scores = score_thread(raw)
    assert scores["signal_type"] != "REPOST"
    assert scores["signal_type"] in ("PHOTO_EVIDENCE", "FIRSTHAND_CLAIM", "RUMOR")


def test_priority_weights_sum_sensible():
    p = compute_priority(80, 80, 80, 80, 80, 40, 0, "FIRSTHAND_CLAIM")
    assert 50 <= p <= 100
    p_spec = compute_priority(90, 90, 10, 10, 90, 40, 0, "SPECULATION")
    assert p_spec <= 60


# ---------------------------------------------------------------------------
# Coolaler full-thread parse
# ---------------------------------------------------------------------------

def test_coolaler_full_thread_parse():
    html = (FIXTURES / "coolaler_thread_sample.html").read_text(encoding="utf-8")
    src = CoolalerSource()
    url = "https://www.coolaler.com/forums/threads/rtx-5090-pcb.406999/"
    raw = src._parse_thread_html(html, "406999", url)
    assert raw is not None
    assert "5090" in raw.title_original
    assert raw.author_name == "leakerTW"
    assert raw.op_text_original and "工程樣" in raw.op_text_original
    assert raw.image_count >= 2
    assert raw.attachment_count >= 1
    assert raw.posts and raw.posts[0]["is_op"] is True
    # selective replies: evidence reply kept, +1 may be skipped after quota logic
    non_op = [p for p in raw.posts if not p.get("is_op")]
    assert len(non_op) >= 1
    assert any("跑分" in (p.get("text_original") or "") or p.get("image_count", 0) > 0 for p in non_op)


def test_coolaler_fetch_thread_requires_url():
    src = CoolalerSource()
    assert src.fetch_thread("406999") is None  # no URL → refuse to invent slug


# ---------------------------------------------------------------------------
# Corroboration: independent vs repost
# ---------------------------------------------------------------------------

from pipeline.community_score import compute_corroboration


def test_independent_corroboration_boosts():
    score = compute_corroboration(
        this_platform="chiphell",
        this_title="RTX 6090 PCB 实拍工程样品",
        this_urls=[],
        this_signal_type="PHOTO_EVIDENCE",
        peers=[
            {
                "platform": "mobile01",
                "title": "RTX 6090 PCB 實拍工程樣品分享",
                "urls": [],
                "signal_type": "PHOTO_EVIDENCE",
            }
        ],
    )
    assert score >= 25


def test_fake_corroboration_link_no_boost():
    score = compute_corroboration(
        this_platform="chiphell",
        this_title="RTX 6090 PCB 实拍工程样品",
        this_urls=[],
        this_signal_type="PHOTO_EVIDENCE",
        peers=[
            {
                "platform": "ptt",
                "title": "Chiphell 有人拍到 6090",
                "urls": ["https://www.chiphell.com/forum.php?mod=viewthread&tid=999"],
                "signal_type": "DISCUSSION",
            }
        ],
    )
    assert score == 0.0


# ---------------------------------------------------------------------------
# Longitudinal velocity
# ---------------------------------------------------------------------------

from pipeline.community_score import score_velocity_longitudinal
from datetime import timedelta


def test_velocity_burst_vs_stale():
    t0 = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    # Burst: 20 → 200 replies in 15 minutes
    burst = [
        (t0, 20, 100),
        (t0 + timedelta(minutes=15), 200, 5000),
    ]
    # Stale: same 200 replies but accumulated over 3 days
    stale = [
        (t0 - timedelta(days=3), 10, 50),
        (t0, 200, 5000),
    ]
    v_burst = score_velocity_longitudinal(burst)
    v_stale = score_velocity_longitudinal(stale)
    assert v_burst > v_stale
    assert v_burst >= 50


def test_velocity_single_snapshot_conservative():
    t0 = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
    assert score_velocity_longitudinal([(t0, 50, 1000)]) == 20.0
    assert score_velocity_longitudinal([]) == 0.0


# ---------------------------------------------------------------------------
# Chronology integration (DB-backed)
# ---------------------------------------------------------------------------

def test_chronology_db_integration(tmp_path, monkeypatch):
    """Chiphell 08:52 → PTT 09:07 → BenchLife 09:31 → ITHome 10:02."""
    from database.db import init_db, get_session
    from database.models import Article, CommunityThread, StoryCluster
    from datetime import timezone as tz

    db_url = f"sqlite:///{tmp_path}/chrono.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    # Local CN/TW times → UTC
    # 08:52 Asia/Shanghai = 00:52 UTC
    t_ch = datetime(2026, 7, 24, 0, 52, tzinfo=tz.utc)
    # 09:07 Asia/Taipei = 01:07 UTC
    t_ptt = datetime(2026, 7, 24, 1, 7, tzinfo=tz.utc)
    # 09:31 Asia/Taipei = 01:31 UTC
    t_bl = datetime(2026, 7, 24, 1, 31, tzinfo=tz.utc)
    # 10:02 Asia/Shanghai = 02:02 UTC
    t_ith = datetime(2026, 7, 24, 2, 2, tzinfo=tz.utc)

    with get_session(db_url) as session:
        cluster = StoryCluster(
            first_seen_source="chiphell",
            first_seen_at=t_ch,
            representative_title="RTX 6090 PCB 实拍",
            created_at=t_ch,
            updated_at=t_ch,
        )
        session.add(cluster)
        session.flush()

        session.add(
            CommunityThread(
                platform="chiphell",
                region="CN",
                language_variant="zh-CN",
                thread_id="ch1",
                url="https://www.chiphell.com/forum.php?mod=viewthread&tid=ch1",
                title_original="RTX 6090 PCB 实拍工程样品",
                discovered_at=t_ch,
                first_signal_at=t_ch,
                created_at=t_ch,
                signal_type="PHOTO_EVIDENCE",
                story_cluster_id=cluster.id,
            )
        )
        session.add(
            CommunityThread(
                platform="ptt",
                region="TW",
                language_variant="zh-TW",
                thread_id="ptt1",
                url="https://www.ptt.cc/bbs/PC_Shopping/M.1.html",
                title_original="RTX 6090 PCB 實拍工程樣品討論",
                discovered_at=t_ptt,
                first_signal_at=t_ptt,
                created_at=t_ptt,
                signal_type="DISCUSSION",
                story_cluster_id=cluster.id,
            )
        )
        session.add(
            Article(
                source="benchlife",
                source_article_id="bl1",
                title_original="RTX 6090 PCB 工程樣曝光",
                url="https://www.benchlife.info/bl1",
                discovered_at=t_bl,
                published_at=t_bl,
                duplicate_group_id=cluster.id,
            )
        )
        session.add(
            Article(
                source="ithome",
                source_article_id="ith1",
                title_original="RTX 6090 PCB 工程样品曝光",
                url="https://www.ithome.com/0/1.htm",
                discovered_at=t_ith,
                published_at=t_ith,
                duplicate_group_id=cluster.id,
            )
        )
        session.flush()

        from pipeline.community_ingest import _update_cluster_chronology
        _update_cluster_chronology(session, cluster)
        session.flush()

        assert cluster.first_signal_source == "chiphell"
        assert cluster.first_media_source == "benchlife"
        # SQLite may strip tzinfo on round-trip — compare UTC instants by replace
        def _utc(dt):
            return dt.replace(tzinfo=tz.utc) if dt.tzinfo is None else dt.astimezone(tz.utc)
        assert _utc(cluster.first_signal_at) == t_ch
        assert _utc(cluster.first_media_at) == t_bl
        lead = (_utc(cluster.first_media_at) - _utc(cluster.first_signal_at)).total_seconds() / 60
        assert abs(lead - 39) < 0.1
