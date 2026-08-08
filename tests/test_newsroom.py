
'''V0.5 Newsroom Intelligence tests - synthetic scenarios A-J.'''

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from database.db import init_db, get_session
from database.models import (
    Article,
    CommunityThread,
    DocumentaryRecord,
    LeadFeedback,
    StoryCluster,
    StoryLead,
)
from pipeline.newsroom import (
    add_feedback,
    compute_editorial,
    explain_lead,
    gather_cluster_context,
    score_source_diversity,
    upsert_lead_for_cluster,
)


def _setup(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/nr.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _cluster(session, title="test"):
    now = datetime.now(timezone.utc)
    c = StoryCluster(
        first_seen_source="chiphell",
        first_seen_at=now,
        representative_title=title,
        created_at=now,
        updated_at=now,
    )
    session.add(c)
    session.flush()
    return c


def test_case_a_lone_speculation(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "我觉得 RTX 6090 会有 48GB")
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="a1", url="http://x/a1",
            title_original="我觉得 RTX 6090 会有 48GB",
            discovered_at=now, first_signal_at=now, created_at=now,
            signal_type="SPECULATION", evidence_score=10, firsthand_score=10,
            velocity_score=80, priority_score=40, relevance_score=50,
            novelty_score=40, community_score=40, story_cluster_id=c.id,
        ))
        c.first_signal_source = "chiphell"
        c.first_signal_at = now
        session.flush()
        cid = c.id
    lead = upsert_lead_for_cluster(cid, dry_run=True)
    assert lead is not None
    assert lead.lead_status in ("WATCHING", "NEW")
    assert lead.confidence_score < 50
    assert lead.priority_score < 70


def test_case_b_retail_discovery(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "联想 拯救者 83ABC RTX 6070")
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="b1",
            url="https://item.jd.com/b1.html", title="联想 拯救者 83ABC RTX 6070",
            model_number="83ABC", first_seen_at=now, last_seen_at=now, observed_at=now,
            record_status="ACTIVE", evidence_type="RETAIL_LISTING",
            evidence_score=85, novelty_score=90, relevance_score=85, priority_score=90,
            story_cluster_id=c.id,
        ))
        c.first_documentary_source = "jd"
        c.first_documentary_at = now
        session.flush()
        cid = c.id
    lead = upsert_lead_for_cluster(cid, dry_run=True)
    assert lead.lead_type in ("RETAIL_DISCOVERY", "DOCUMENTARY_DISCOVERY")
    assert lead.exclusivity_score >= 70
    assert lead.evidence_score >= 40


def test_case_c_corroboration_raises(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "联想 83ABC 未发布")
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="c1", url="http://x/c1",
            title_original="联想 83ABC 未发布笔记本实拍",
            discovered_at=now - timedelta(hours=1), first_signal_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
            signal_type="PHOTO_EVIDENCE", evidence_score=70, firsthand_score=70,
            velocity_score=30, priority_score=65, relevance_score=80, novelty_score=75,
            community_score=40, story_cluster_id=c.id,
        ))
        c.first_signal_source = "chiphell"
        c.first_signal_at = now - timedelta(hours=1)
        session.flush()
        cid = c.id
    lead1 = upsert_lead_for_cluster(cid, dry_run=True)
    p1 = lead1.priority_score
    with get_session(db) as session:
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="c-sku",
            url="https://item.jd.com/c.html", title="联想 83ABC RTX 6070",
            model_number="83ABC", first_seen_at=now, last_seen_at=now, observed_at=now,
            record_status="ACTIVE", evidence_type="RETAIL_LISTING",
            evidence_score=80, novelty_score=80, relevance_score=80, priority_score=85,
            story_cluster_id=cid,
        ))
        cl = session.get(StoryCluster, cid)
        cl.first_documentary_source = "jd"
        cl.first_documentary_at = now
        cl.updated_at = now
    lead2 = upsert_lead_for_cluster(cid, dry_run=True)
    assert lead2.evidence_score >= lead1.evidence_score
    assert lead2.priority_score >= p1 - 1


def test_case_d_saturation(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "AMD Ryzen 9000 发布")
        session.add(Article(
            source="benchlife", source_article_id="d1", title_original="AMD Ryzen 9000 发布",
            url="http://bl/d1", discovered_at=now, published_at=now, duplicate_group_id=c.id,
        ))
        c.first_media_source = "benchlife"
        c.first_media_at = now
        session.flush()
        cid = c.id
    lead1 = upsert_lead_for_cluster(cid, dry_run=True)
    excl1 = lead1.exclusivity_score
    with get_session(db) as session:
        for i, src in enumerate(["ithome", "mydrivers", "technews", "zol", "expreview"]):
            session.add(Article(
                source=src, source_article_id=f"d{i+2}", title_original="AMD Ryzen 9000 发布",
                url=f"http://x/{src}", discovered_at=now, published_at=now, duplicate_group_id=cid,
            ))
    lead2 = upsert_lead_for_cluster(cid, dry_run=True)
    assert lead2.media_saturation_score > lead1.media_saturation_score
    assert lead2.exclusivity_score < excl1


def test_case_e_repost_swarm(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "ITHome 原文 RTX")
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="e0", url="http://x/e0", title_original="原创 RTX 照片",
            discovered_at=now, first_signal_at=now, created_at=now,
            signal_type="PHOTO_EVIDENCE", evidence_score=70, story_cluster_id=c.id,
            priority_score=70, relevance_score=70, novelty_score=70, firsthand_score=70,
            velocity_score=20, community_score=40,
        ))
        for i, plat in enumerate(["ptt", "mobile01", "coolaler"]):
            session.add(CommunityThread(
                platform=plat, region="TW", language_variant="zh-TW",
                thread_id=f"er{i}", url=f"http://x/er{i}",
                title_original="转载 ITHome RTX 报道",
                discovered_at=now, first_signal_at=now, created_at=now,
                signal_type="REPOST", evidence_score=10, story_cluster_id=c.id,
                priority_score=30, relevance_score=40, novelty_score=15,
                firsthand_score=5, velocity_score=10, community_score=40,
            ))
        session.flush()
        ctx = gather_cluster_context(session, c.id)
    assert score_source_diversity(ctx) < 80


def test_case_f_independent_layers(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "RTX 6070 笔记本")
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="f1", url="http://x/f1", title_original="RTX 6070 笔记本实拍",
            discovered_at=now, first_signal_at=now, created_at=now,
            signal_type="PHOTO_EVIDENCE", evidence_score=75, story_cluster_id=c.id,
            priority_score=75, relevance_score=80, novelty_score=80, firsthand_score=75,
            velocity_score=20, community_score=40,
        ))
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="fsku",
            url="https://item.jd.com/f.html", title="RTX 6070 笔记本",
            first_seen_at=now, last_seen_at=now, observed_at=now, record_status="ACTIVE",
            evidence_score=80, novelty_score=80, relevance_score=80, priority_score=85,
            story_cluster_id=c.id,
        ))
        session.add(Article(
            source="benchlife", source_article_id="fbl", title_original="RTX 6070 笔记本曝光",
            url="http://bl/f", discovered_at=now, published_at=now, duplicate_group_id=c.id,
        ))
        session.flush()
        ctx = gather_cluster_context(session, c.id)
    assert score_source_diversity(ctx) >= 60
    assert compute_editorial(ctx)["confidence_score"] >= 50


def test_case_g_stale(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    with get_session(db) as session:
        c = _cluster(session, "旧传闻 GPU")
        c.created_at = old
        c.updated_at = old
        c.first_seen_at = old
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="g1", url="http://x/g1", title_original="旧传闻 GPU",
            discovered_at=old, first_signal_at=old, created_at=old,
            signal_type="RUMOR", evidence_score=20, story_cluster_id=c.id,
            priority_score=40, relevance_score=40, novelty_score=30, firsthand_score=10,
            velocity_score=5, community_score=40,
        ))
        session.flush()
        cid = c.id
    lead = upsert_lead_for_cluster(cid, dry_run=True)
    assert lead.lead_status == "STALE" or lead.priority_score < 60


def test_case_h_resurgence(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(days=5)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "旧传闻 GPU 复活")
        c.created_at = old
        c.updated_at = old
        session.add(CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="h1", url="http://x/h1", title_original="旧传闻 GPU",
            discovered_at=old, first_signal_at=old, created_at=old,
            signal_type="RUMOR", evidence_score=20, story_cluster_id=c.id,
            priority_score=40, relevance_score=40, novelty_score=30, firsthand_score=10,
            velocity_score=5, community_score=40,
        ))
        session.flush()
        cid = c.id
    lead1 = upsert_lead_for_cluster(cid, dry_run=True)
    with get_session(db) as session:
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="hsku",
            url="https://item.jd.com/h.html", title="旧传闻 GPU 真机上架",
            first_seen_at=now, last_seen_at=now, observed_at=now, record_status="ACTIVE",
            evidence_score=85, novelty_score=80, relevance_score=80, priority_score=88,
            story_cluster_id=cid,
        ))
        cl = session.get(StoryCluster, cid)
        cl.first_documentary_source = "jd"
        cl.first_documentary_at = now
        cl.updated_at = now
    lead2 = upsert_lead_for_cluster(cid, dry_run=True)
    assert lead2.priority_score >= lead1.priority_score


def test_case_i_feedback(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "feedback test")
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="i1",
            url="https://item.jd.com/i.html", title="feedback test product",
            first_seen_at=now, last_seen_at=now, observed_at=now, record_status="ACTIVE",
            evidence_score=80, novelty_score=80, relevance_score=80, priority_score=85,
            story_cluster_id=c.id,
        ))
        session.flush()
        cid = c.id
    lead = upsert_lead_for_cluster(cid, dry_run=True)
    assert add_feedback(lead.id, "WRITTEN")
    with get_session(db) as session:
        fb = session.query(LeadFeedback).filter_by(lead_id=lead.id).all()
        assert len(fb) == 1 and fb[0].feedback == "WRITTEN"
        assert session.get(StoryLead, lead.id).lead_status == "RESOLVED"
    text = explain_lead(lead.id)
    assert "FINAL" in text or "final" in text.lower() or "Score" in text


def test_case_j_popular_speculation_capped(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with get_session(db) as session:
        c = _cluster(session, "纯猜测 超高热度")
        session.add(CommunityThread(
            platform="ptt", region="TW", language_variant="zh-TW",
            thread_id="j1", url="http://x/j1", title_original="我觉得会出 RTX 6090",
            discovered_at=now, first_signal_at=now, created_at=now,
            signal_type="SPECULATION", evidence_score=5, firsthand_score=5,
            velocity_score=99, reply_count=500, view_count=20000,
            priority_score=55, relevance_score=40, novelty_score=40, community_score=40,
            story_cluster_id=c.id,
        ))
        session.flush()
        cid_spec = c.id
        c2 = _cluster(session, "官方店 RTX 6070 上架")
        session.add(DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="j2",
            url="https://item.jd.com/j2.html", title="官方店 RTX 6070 笔记本",
            first_seen_at=now, last_seen_at=now, observed_at=now, record_status="ACTIVE",
            evidence_score=90, novelty_score=90, relevance_score=90, priority_score=92,
            story_cluster_id=c2.id,
        ))
        session.flush()
        cid_doc = c2.id
    lead_spec = upsert_lead_for_cluster(cid_spec, dry_run=True)
    lead_doc = upsert_lead_for_cluster(cid_doc, dry_run=True)
    assert lead_doc.priority_score > lead_spec.priority_score
