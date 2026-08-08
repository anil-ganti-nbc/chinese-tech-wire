"""V0.4 Documentary Intelligence tests.

Geekbench automated monitoring is DISABLED — no tests may import GeekbenchSource
or make requests to browser.geekbench.com.
"""

from __future__ import annotations

from datetime import datetime, timezone

from documentary_sources import DOCUMENTARY_REGISTRY, DISABLED_DOCUMENTARY_SOURCES
from documentary_sources.base import RawDocumentary
from pipeline.documentary_change import detect_changes
from pipeline.documentary_score import score_documentary


def test_no_geekbench_in_registry():
    assert "geekbench" not in DOCUMENTARY_REGISTRY
    assert "jd" in DOCUMENTARY_REGISTRY
    assert "geekbench" in DISABLED_DOCUMENTARY_SOURCES


def test_import_documentary_package_no_geekbench_adapter():
    import documentary_sources as ds
    assert "GeekbenchSource" not in getattr(ds, "__all__", [])
    assert "geekbench" not in ds.DOCUMENTARY_REGISTRY


def test_new_record_event():
    events = detect_changes(None, {"cpu": "Nova Lake"}, is_new=True)
    assert events[0][0] == "NEW_RECORD"


def test_price_added():
    types = [e[0] for e in detect_changes({"price": None}, {"price": 14999})]
    assert "PRICE_ADDED" in types


def test_price_changed():
    types = [e[0] for e in detect_changes({"price": 14999}, {"price": 13999})]
    assert "PRICE_CHANGED" in types


def test_spec_added_gpu():
    types = [e[0] for e in detect_changes({"cpu": "i9"}, {"cpu": "i9", "gpu": "RTX 6070"})]
    assert "SPEC_ADDED" in types


def test_trivial_change_ignored():
    assert detect_changes({"cpu": "i9", "marketing": "a!!!"}, {"cpu": "i9", "marketing": "a."}) == []


def test_model_revealed():
    types = [e[0] for e in detect_changes({}, {"model_number": "83XX"})]
    assert "MODEL_REVEALED" in types


def test_availability_changed():
    types = [e[0] for e in detect_changes({"availability": "preorder"}, {"availability": "in_stock"})]
    assert "AVAILABILITY_CHANGED" in types


def test_generic_benchmark_record_scoring():
    raw = RawDocumentary(
        record_type="BENCHMARK_RECORD",
        source="example_benchmark",
        source_record_id="syn-1",
        url="https://example.invalid/result/syn-1",
        title="Lenovo 83XX — Intel Nova Lake-HX ES",
        product="Lenovo 83XX",
        model_number="83XX",
        structured={
            "benchmark": "ExampleBench",
            "cpu_name": "Nova Lake-HX ES",
            "cpu_cores": 24,
            "single_core": 3500,
            "multi_core": 22000,
            "system": "Lenovo 83XX",
        },
    )
    s = score_documentary(raw)
    assert s["evidence_score"] >= 70
    assert s["novelty_score"] >= 60


def test_content_hash_stable_for_same_structured():
    r1 = RawDocumentary(
        record_type="BENCHMARK_RECORD", source="example_benchmark",
        source_record_id="1", url="http://x", title="CPU",
        structured={"cpu_name": "Nova Lake", "single_core": 3000},
    )
    r2 = RawDocumentary(
        record_type="BENCHMARK_RECORD", source="example_benchmark",
        source_record_id="1", url="http://x", title="CPU",
        structured={"single_core": 3000, "cpu_name": "Nova Lake"},
    )
    assert r1.content_hash() == r2.content_hash()


def test_hash_changes_on_spec():
    r1 = RawDocumentary(record_type="RETAIL_LISTING", source="jd", source_record_id="1",
                        url="http://x", structured={"price": None, "gpu": None})
    r2 = RawDocumentary(record_type="RETAIL_LISTING", source="jd", source_record_id="1",
                        url="http://x", structured={"price": None, "gpu": "RTX 6070"})
    assert r1.content_hash() != r2.content_hash()


def test_documentary_lifecycle(tmp_path, monkeypatch):
    from database.db import init_db, get_session
    from database.models import DocumentaryEvent, DocumentaryRecord, DocumentarySnapshot
    from pipeline.documentary_ingest import ingest_raw_documentary, mark_missing_records

    db_url = f"sqlite:///{tmp_path}/doc.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    raw = RawDocumentary(
        record_type="RETAIL_LISTING", source="jd", source_record_id="sku1",
        url="https://item.jd.com/sku1.html", title="联想 拯救者 Y9000P 未发布",
        structured={"price": None, "gpu": None, "cpu": "i9"},
    )
    assert ingest_raw_documentary(raw, dry_run=True) is not None

    with get_session(db_url) as session:
        row = session.query(DocumentaryRecord).filter_by(source_record_id="sku1").one()
        events = session.query(DocumentaryEvent).filter_by(record_id=row.id).all()
        assert any(e.event_type == "NEW_RECORD" for e in events)
        assert session.query(DocumentarySnapshot).filter_by(record_id=row.id).count() == 1

    ingest_raw_documentary(RawDocumentary(
        record_type="RETAIL_LISTING", source="jd", source_record_id="sku1",
        url="https://item.jd.com/sku1.html", title="联想 拯救者 Y9000P 未发布",
        structured={"price": None, "gpu": "RTX 6070", "cpu": "i9"},
    ), dry_run=True)
    ingest_raw_documentary(RawDocumentary(
        record_type="RETAIL_LISTING", source="jd", source_record_id="sku1",
        url="https://item.jd.com/sku1.html", title="联想 拯救者 Y9000P 未发布",
        structured={"price": 14999, "gpu": "RTX 6070", "cpu": "i9"},
    ), dry_run=True)

    with get_session(db_url) as session:
        types = [e.event_type for e in session.query(DocumentaryEvent).all()]
        assert "SPEC_ADDED" in types and "PRICE_ADDED" in types

    mark_missing_records("jd", seen_ids=set(), dry_run=True)
    with get_session(db_url) as session:
        rec = session.query(DocumentaryRecord).filter_by(source_record_id="sku1").one()
        assert rec.record_status == "MISSING"
        assert not any(e.event_type == "RECORD_REMOVED" for e in session.query(DocumentaryEvent).all())

    mark_missing_records("jd", seen_ids=set(), dry_run=True)
    with get_session(db_url) as session:
        rec = session.query(DocumentaryRecord).filter_by(source_record_id="sku1").one()
        assert rec.record_status == "REMOVED"

    ingest_raw_documentary(RawDocumentary(
        record_type="RETAIL_LISTING", source="jd", source_record_id="sku1",
        url="https://item.jd.com/sku1.html", title="联想 拯救者 Y9000P 未发布",
        structured={"price": 14999, "gpu": "RTX 6070", "cpu": "i9"},
    ), dry_run=True)
    with get_session(db_url) as session:
        rec = session.query(DocumentaryRecord).filter_by(source_record_id="sku1").one()
        assert rec.record_status == "REAPPEARED"
        assert any(e.event_type == "RECORD_REAPPEARED" for e in session.query(DocumentaryEvent).all())


def test_benchmark_repeat_same_id(tmp_path, monkeypatch):
    from database.db import init_db, get_session
    from database.models import DocumentaryRecord
    from pipeline.documentary_ingest import ingest_raw_documentary

    db_url = f"sqlite:///{tmp_path}/gb.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    def make():
        return RawDocumentary(
            record_type="BENCHMARK_RECORD", source="example_benchmark",
            source_record_id="100", url="https://example.invalid/100",
            title="AMD Ryzen 9 9950X",
            structured={"cpu_name": "AMD Ryzen 9 9950X", "single_core": 3400, "multi_core": 20000},
        )

    assert ingest_raw_documentary(make(), dry_run=True) is not None
    assert ingest_raw_documentary(make(), dry_run=True) is not None
    with get_session(db_url) as session:
        assert session.query(DocumentaryRecord).filter_by(
            source="example_benchmark", source_record_id="100"
        ).count() == 1


def test_cross_layer_chronology_and_corroboration(tmp_path, monkeypatch):
    from database.db import init_db, get_session
    from database.models import Article, CommunityThread, DocumentaryRecord, StoryCluster
    from pipeline.community_ingest import _update_cluster_chronology as community_chrono
    from pipeline.documentary_ingest import _update_cluster_chronology as doc_chrono
    from pipeline.documentary_ingest import _cross_corroborate
    from datetime import timezone as tz

    db_url = f"sqlite:///{tmp_path}/chrono.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    t_ch = datetime(2026, 7, 24, 0, 52, tzinfo=tz.utc)
    t_doc = datetime(2026, 7, 24, 1, 13, tzinfo=tz.utc)
    t_ith = datetime(2026, 7, 24, 3, 22, tzinfo=tz.utc)

    with get_session(db_url) as session:
        cluster = StoryCluster(
            first_seen_source="chiphell", first_seen_at=t_ch,
            representative_title="联想 拯救者 83ABC 未发布笔记本",
            created_at=t_ch, updated_at=t_ch,
        )
        session.add(cluster)
        session.flush()

        ct = CommunityThread(
            platform="chiphell", region="CN", language_variant="zh-CN",
            thread_id="ch1",
            url="https://www.chiphell.com/forum.php?mod=viewthread&tid=ch1",
            title_original="联想 拯救者 83ABC 未发布笔记本实拍 RTX 6070",
            discovered_at=t_ch, first_signal_at=t_ch, created_at=t_ch,
            signal_type="PHOTO_EVIDENCE", corroboration_score=0.0, priority_score=70.0,
            relevance_score=80.0, novelty_score=80.0, evidence_score=70.0,
            firsthand_score=70.0, velocity_score=20.0, community_score=40.0,
            story_cluster_id=cluster.id,
        )
        session.add(ct)
        doc = DocumentaryRecord(
            record_type="RETAIL_LISTING", source="jd", source_record_id="sku-83abc",
            url="https://item.jd.com/sku-83abc.html",
            title="联想 拯救者 83ABC RTX 6070", model_number="83ABC",
            first_seen_at=t_doc, last_seen_at=t_doc, observed_at=t_doc,
            record_status="ACTIVE", evidence_type="RETAIL_LISTING",
            evidence_score=80.0, novelty_score=85.0, relevance_score=80.0,
            priority_score=85.0, story_cluster_id=cluster.id,
        )
        session.add(doc)
        session.add(Article(
            source="ithome", source_article_id="ith1",
            title_original="联想 拯救者 83ABC 未发布机型曝光",
            url="https://www.ithome.com/0/1.htm",
            discovered_at=t_ith, published_at=t_ith, duplicate_group_id=cluster.id,
        ))
        session.flush()
        community_chrono(session, cluster)
        doc_chrono(session, cluster)
        _cross_corroborate(session, doc)
        session.flush()

        assert cluster.first_signal_source == "chiphell"
        assert cluster.first_documentary_source == "jd"
        assert cluster.first_media_source == "ithome"

        def _utc(dt):
            return dt.replace(tzinfo=tz.utc) if dt.tzinfo is None else dt.astimezone(tz.utc)

        assert abs((_utc(cluster.first_documentary_at) - _utc(cluster.first_signal_at)).total_seconds() / 60 - 21) < 0.1
        assert abs((_utc(cluster.first_media_at) - _utc(cluster.first_signal_at)).total_seconds() / 60 - 150) < 0.1
        session.refresh(ct)
        assert (ct.corroboration_score or 0) >= 25


def test_external_geekbench_url_classified_not_fetched():
    from pipeline.community_score import classify_external_url, extract_urls
    assert classify_external_url("https://browser.geekbench.com/v6/cpu/12345") == "geekbench"
    urls = extract_urls("跑分见 https://browser.geekbench.com/v6/cpu/999")
    assert any(u["source_type"] == "geekbench" for u in urls)
