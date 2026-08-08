"""V0.5.6 — Editorial Validation regression tests.

Covers: conversion funnel, source performance, lead-time analytics,
lead-type performance, alert performance, lifecycle attrition, missed
stories, GUI, and CLI-level JSON/read-only guarantees.

All network access is mocked — no live HTTP calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database.db import get_session, init_db
from database.models import (
    Article,
    CommunityThread,
    DocumentaryRecord,
    IngestionRun,
    LeadEvent,
    LeadNotification,
    SourceRun,
    StoryCluster,
    StoryLead,
)
from pipeline.editorial_validation import (
    alert_performance,
    editorial_funnel,
    lead_time_analytics,
    lead_type_performance,
    lifecycle_report,
    source_performance,
)
from pipeline.missed_stories import (
    list_missed_stories,
    missed_story_report,
    reconstruct_miss,
    record_miss,
)
from pipeline.outcomes import record_outcome


def _now():
    return datetime.now(timezone.utc)


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/v056ev.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _lead(session, **overrides):
    defaults = dict(
        lead_status="WATCHING", lead_type="EARLY_SIGNAL",
        created_at=_now(), updated_at=_now(),
        priority_score=50, evidence_score=40, relevance_score=50, confidence_score=40,
    )
    defaults.update(overrides)
    lead = StoryLead(**defaults)
    session.add(lead)
    session.flush()
    session.refresh(lead)
    return lead


def _cluster(session, **overrides):
    now = _now()
    defaults = dict(first_seen_source="ithome", first_seen_at=now, created_at=now, updated_at=now)
    defaults.update(overrides)
    c = StoryCluster(**defaults)
    session.add(c)
    session.flush()
    session.refresh(c)
    return c


def _article(session, cluster_id, source, source_article_id=None, **overrides):
    now = _now()
    defaults = dict(
        source=source, source_article_id=source_article_id or f"{source}-{now.timestamp()}",
        title_original="test article", url=f"https://{source}.example/1",
        discovered_at=now, duplicate_group_id=cluster_id,
    )
    defaults.update(overrides)
    a = Article(**defaults)
    session.add(a)
    session.flush()
    return a


def _thread(session, cluster_id, platform, thread_id=None, **overrides):
    now = _now()
    defaults = dict(
        platform=platform, thread_id=thread_id or f"{platform}-{now.timestamp()}",
        title_original="test thread", url=f"https://{platform}.example/1",
        created_at=now, updated_at=now, discovered_at=now, story_cluster_id=cluster_id,
        signal_type="DISCUSSION", priority_score=10,
    )
    defaults.update(overrides)
    t = CommunityThread(**defaults)
    session.add(t)
    session.flush()
    return t


def _docrecord(session, cluster_id, source, source_record_id=None, **overrides):
    now = _now()
    defaults = dict(
        record_type="RETAIL_LISTING", source=source,
        source_record_id=source_record_id or f"{source}-{now.timestamp()}",
        url=f"https://{source}.example/1", first_seen_at=now, last_seen_at=now,
        observed_at=now, story_cluster_id=cluster_id,
    )
    defaults.update(overrides)
    d = DocumentaryRecord(**defaults)
    session.add(d)
    session.flush()
    return d


def _sent_notification(session, lead_id, alert_reason="FIRST_ELIGIBLE", sent_at=None):
    n = LeadNotification(
        lead_id=lead_id, attempted_at=_now(), sent_at=(sent_at or _now()),
        outcome="SENT", reason_code="SENT", alert_reason=alert_reason,
        priority_score=50,
    )
    session.add(n)
    return n


# ---------------------------------------------------------------------------
# Editorial funnel
# ---------------------------------------------------------------------------

def test_funnel_denominator_correctness(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        l1 = _lead(session)
        l2 = _lead(session)
        ids = (l1.id, l2.id)
    record_outcome(ids[0], "WRITTEN")
    f = editorial_funnel()
    assert f["counts"]["total_leads"] == 2
    assert f["counts"]["written_leads"] == 1
    assert f["counts"]["still_unresolved"] == 1
    assert f["rates"]["feedback_rate"]["numerator"] == 1
    assert f["rates"]["feedback_rate"]["denominator"] == 2


def test_funnel_zero_sample(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    f = editorial_funnel()
    assert f["counts"]["total_leads"] == 0
    assert f["rates"]["feedback_rate"]["denominator"] == 0
    assert f["rates"]["feedback_rate"]["low_sample"] is True


def test_funnel_low_sample_warning(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        l1 = _lead(session)
        lid = l1.id
    record_outcome(lid, "USEFUL")
    f = editorial_funnel()
    assert f["rates"]["feedback_rate"]["low_sample"] is True
    assert f["rates"]["feedback_rate"]["warning"] == "LOW_SAMPLE"


def test_useful_written_conversion(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        leads = [_lead(session) for _ in range(3)]
        ids = [l.id for l in leads]
    record_outcome(ids[0], "USEFUL")
    record_outcome(ids[1], "WRITTEN")
    f = editorial_funnel()
    assert f["counts"]["useful_leads"] == 1
    assert f["counts"]["written_leads"] == 1


def test_false_positive_rate(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        leads = [_lead(session) for _ in range(2)]
        ids = [l.id for l in leads]
    record_outcome(ids[0], "FALSE")
    f = editorial_funnel()
    assert f["counts"]["false_leads"] == 1
    assert f["rates"]["false_positive_rate"]["numerator"] == 1
    assert f["rates"]["false_positive_rate"]["denominator"] == 1  # only labelled leads


# ---------------------------------------------------------------------------
# Source performance (V0.5.6.1 — cluster-based attribution)
# ---------------------------------------------------------------------------

def test_first_signal_attribution(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session, first_signal_source="chiphell")
        _lead(session, first_signal_source="chiphell")
        _lead(session, first_signal_source="ptt")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["chiphell"]["first_signal_count"] == 2
    assert rows["ptt"]["first_signal_count"] == 1


def test_media_only_source(tmp_path, monkeypatch):
    """A source that only ever picks up stories (never first) should show
    first_signal_count == 0 but first_media_count > 0."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session, first_signal_source="chiphell", first_media_source="ithome")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["first_signal_count"] == 0
    assert rows["ithome"]["first_media_count"] == 1


def test_community_first_source(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session, first_signal_source="ptt")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ptt"]["layer"] == "COMMUNITY"
    assert rows["ptt"]["first_signal_count"] == 1


def test_no_composite_score(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session, first_signal_source="chiphell")
    rows = source_performance()
    for r in rows:
        assert "score" not in r
        assert "composite_score" not in r


# --- V0.5.6.1 regression cases: cluster-based "touched" attribution -------

def test_news_source_contributes_but_not_first_media_still_touched(tmp_path, monkeypatch):
    """Root-cause regression: a NEWS source with a real Article in the
    cluster must register leads_touched, even though it never won
    first_signal/first_media/first_documentary."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        lead = _lead(session, cluster_id=c.id, first_signal_source="chiphell", first_media_source="benchlife")
        _article(session, c.id, "ithome")  # ithome never "won" anything
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["first_signal_count"] == 0
    assert rows["ithome"]["first_media_count"] == 0
    assert rows["ithome"]["leads_touched"] == 1
    assert rows["ithome"]["records_contributed"] == 1


def test_duplicate_source_records_one_lead_touched_two_records(tmp_path, monkeypatch):
    """Two articles from the same source in the same cluster: 1 lead
    touched, but records_contributed reflects both rows."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id)
        _article(session, c.id, "ithome", source_article_id="a1")
        _article(session, c.id, "ithome", source_article_id="a2")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["leads_touched"] == 1
    assert rows["ithome"]["records_contributed"] == 2


def test_multiple_news_sources_in_one_cluster_all_touched(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id)
        _article(session, c.id, "ithome")
        _article(session, c.id, "mydrivers")
        _article(session, c.id, "zol")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["leads_touched"] == 1
    assert rows["mydrivers"]["leads_touched"] == 1
    assert rows["zol"]["leads_touched"] == 1


def test_mixed_layer_cluster_each_source_attributed_correctly(tmp_path, monkeypatch):
    """One cluster with news + community + documentary records — each
    source/layer credited independently."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id, first_signal_source="chiphell",
              first_media_source="ithome", first_documentary_source="jd")
        _article(session, c.id, "ithome")
        _thread(session, c.id, "chiphell")
        _docrecord(session, c.id, "jd")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["layer"] == "NEWS"
    assert rows["ithome"]["leads_touched"] == 1
    assert rows["chiphell"]["layer"] == "COMMUNITY"
    assert rows["chiphell"]["leads_touched"] == 1
    assert rows["chiphell"]["first_signal_count"] == 1
    assert rows["jd"]["layer"] == "DOCUMENTARY"
    assert rows["jd"]["leads_touched"] == 1
    assert rows["jd"]["first_documentary_count"] == 1


def test_first_media_count_exclusive_to_actual_first_source(tmp_path, monkeypatch):
    """Two NEWS sources both contribute Articles to the cluster, but only
    the one recorded as first_media_source counts toward first_media_count."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id, first_media_source="ithome")
        _article(session, c.id, "ithome")
        _article(session, c.id, "zol")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["first_media_count"] == 1
    assert rows["zol"]["first_media_count"] == 0
    # both still legitimately touched
    assert rows["ithome"]["leads_touched"] == 1
    assert rows["zol"]["leads_touched"] == 1


def test_written_outcome_credits_every_contributing_source(tmp_path, monkeypatch):
    """A WRITTEN outcome must credit every source that contributed a real
    record to that lead's cluster, not only whichever was first."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        lead = _lead(session, cluster_id=c.id, first_signal_source="chiphell")
        lid = lead.id
        _thread(session, c.id, "chiphell")
        _article(session, c.id, "ithome")
        _article(session, c.id, "mydrivers")
    record_outcome(lid, "WRITTEN")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["chiphell"]["written_leads_touched"] == 1
    assert rows["ithome"]["written_leads_touched"] == 1
    assert rows["mydrivers"]["written_leads_touched"] == 1


def test_source_not_in_cluster_gets_no_credit(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id)
        _article(session, c.id, "ithome")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["zol"]["leads_touched"] == 0
    assert rows["zol"]["records_contributed"] == 0


def test_repeated_records_do_not_inflate_touched_lead_count(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id)
        for i in range(5):
            _article(session, c.id, "ithome", source_article_id=f"a{i}")
    rows = {r["source"]: r for r in source_performance()}
    assert rows["ithome"]["leads_touched"] == 1
    assert rows["ithome"]["records_contributed"] == 5


def test_empty_cluster_handled_safely(tmp_path, monkeypatch):
    """A lead with a cluster that has zero stored records must not error
    and must attribute nothing."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id)
    rows = source_performance()
    assert all(r["leads_touched"] == 0 for r in rows)


def test_source_performance_json_valid(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        lead = _lead(session, cluster_id=c.id)
        lid = lead.id
        _article(session, c.id, "ithome")
    record_outcome(lid, "USEFUL")
    rows = source_performance()
    text = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    reparsed = json.loads(text)
    assert any(r["source"] == "ithome" and r["leads_touched"] == 1 for r in reparsed)


def test_source_performance_queries_do_not_mutate(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        lead = _lead(session, cluster_id=c.id, priority_score=41)
        lid = lead.id
        _article(session, c.id, "ithome")
    source_performance()
    source_performance()
    with get_session() as session:
        lead = session.get(StoryLead, lid)
        assert lead.priority_score == 41
        assert len(session.execute(select(Article)).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Lead-time analytics
# ---------------------------------------------------------------------------

def test_signal_before_media_latency(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(
            first_seen_source="chiphell", first_seen_at=now - timedelta(hours=2),
            created_at=now, updated_at=now,
            first_signal_at=now - timedelta(hours=2), first_media_at=now - timedelta(hours=1),
        )
        session.add(c)
        session.flush()
        _lead(session, cluster_id=c.id)
    d = lead_time_analytics()
    stats = d["intervals"]["FIRST_SIGNAL_TO_FIRST_MEDIA_PICKUP"]
    assert stats["count"] == 1
    assert stats["median_minutes"] == 60.0


def test_documentary_before_media_latency(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(
            first_seen_source="jd", first_seen_at=now - timedelta(hours=3),
            created_at=now, updated_at=now,
            first_documentary_at=now - timedelta(hours=3), first_media_at=now - timedelta(hours=1),
        )
        session.add(c)
        session.flush()
        _lead(session, cluster_id=c.id)
    d = lead_time_analytics()
    stats = d["intervals"]["FIRST_DOCUMENTARY_TO_FIRST_MEDIA_PICKUP"]
    assert stats["count"] == 1
    assert stats["median_minutes"] == 120.0


def test_alert_before_written_latency(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(session)
        lid = lead.id
        _sent_notification(session, lid, sent_at=now - timedelta(hours=2))
    record_outcome(lid, "WRITTEN")
    d = lead_time_analytics()
    stats = d["intervals"]["ALERT_SENT_TO_WRITTEN"]
    assert stats["count"] == 1
    assert stats["median_minutes"] > 0


def test_missing_timestamps_excluded(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session)  # no cluster, no notification, no outcome
    d = lead_time_analytics()
    assert d["intervals"]["FIRST_SIGNAL_TO_FIRST_MEDIA_PICKUP"]["status"] == "INSUFFICIENT_TIMESTAMP_DATA"
    assert d["intervals"]["ALERT_SENT_TO_WRITTEN"]["status"] == "INSUFFICIENT_TIMESTAMP_DATA"


def test_negative_inverted_timestamps_rejected(tmp_path, monkeypatch):
    """media pickup recorded BEFORE the signal (bad data) must be rejected,
    not silently treated as a negative duration."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        c = StoryCluster(
            first_seen_source="chiphell", first_seen_at=now,
            created_at=now, updated_at=now,
            first_signal_at=now, first_media_at=now - timedelta(hours=1),  # inverted
        )
        session.add(c)
        session.flush()
        _lead(session, cluster_id=c.id)
    d = lead_time_analytics()
    assert d["intervals"]["FIRST_SIGNAL_TO_FIRST_MEDIA_PICKUP"]["status"] == "INSUFFICIENT_TIMESTAMP_DATA"
    assert d["rejected_inverted_intervals"] == 1


def test_percentile_correctness(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        for mins in (10, 20, 30, 40, 100):
            c = StoryCluster(
                first_seen_source="chiphell", first_seen_at=now,
                created_at=now, updated_at=now,
                first_signal_at=now, first_media_at=now + timedelta(minutes=mins),
            )
            session.add(c)
            session.flush()
            _lead(session, cluster_id=c.id)
    d = lead_time_analytics()
    stats = d["intervals"]["FIRST_SIGNAL_TO_FIRST_MEDIA_PICKUP"]
    assert stats["count"] == 5
    assert stats["median_minutes"] == 30.0
    assert stats["min_minutes"] == 10.0
    assert stats["max_minutes"] == 100.0


# ---------------------------------------------------------------------------
# Lead-type performance
# ---------------------------------------------------------------------------

def test_lead_type_counts_and_outcomes(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        l1 = _lead(session, lead_type="EARLY_SIGNAL")
        l2 = _lead(session, lead_type="EARLY_SIGNAL")
        l3 = _lead(session, lead_type="FOLLOW_UP")
        ids = [l1.id, l2.id, l3.id]
    record_outcome(ids[0], "WRITTEN")
    rows = {r["lead_type"]: r for r in lead_type_performance()}
    assert rows["EARLY_SIGNAL"]["total"] == 2
    assert rows["EARLY_SIGNAL"]["written"] == 1
    assert rows["FOLLOW_UP"]["total"] == 1


def test_lead_type_medians(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session, lead_type="LEAK", priority_score=20, evidence_score=10, confidence_score=30)
        _lead(session, lead_type="LEAK", priority_score=40, evidence_score=30, confidence_score=50)
    rows = {r["lead_type"]: r for r in lead_type_performance()}
    assert rows["LEAK"]["median_priority"] == 30.0


def test_lead_type_performance_no_side_effects(tmp_path, monkeypatch):
    """Calling the report must never mutate stored scores."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session, lead_type="LEAK", priority_score=42)
        lid = lead.id
    lead_type_performance()
    lead_type_performance()
    with get_session() as session:
        lead = session.get(StoryLead, lid)
        assert lead.priority_score == 42


# ---------------------------------------------------------------------------
# Alert performance
# ---------------------------------------------------------------------------

def test_successful_sent_notification_counted(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session)
        lid = lead.id
        _sent_notification(session, lid)
    a = alert_performance()
    assert a["total_sent_leads"] == 1


def test_failed_notification_excluded_from_sent_metrics(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session)
        lid = lead.id
        session.add(LeadNotification(
            lead_id=lid, attempted_at=_now(), outcome="FAILED", reason_code="HTTP_ERROR",
        ))
    a = alert_performance()
    assert a["total_sent_leads"] == 0


def test_written_after_alert(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session)
        lid = lead.id
        _sent_notification(session, lid, alert_reason="BECAME_ACTIONABLE")
    record_outcome(lid, "WRITTEN")
    a = alert_performance()
    assert a["written_after_alert"] == 1
    assert a["by_alert_reason"]["BECAME_ACTIONABLE"]["written"] == 1


def test_unresolved_alert(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session)
        lid = lead.id
        _sent_notification(session, lid)
    a = alert_performance()
    assert a["still_unresolved_after_alert"] == 1


# ---------------------------------------------------------------------------
# Lifecycle attrition
# ---------------------------------------------------------------------------

def test_lifecycle_transition_counts(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(session, lead_status="WATCHING")
        session.add(LeadEvent(lead_id=lead.id, event_type="STATUS_CHANGE", observed_at=now, summary="NEW → WATCHING"))
    r = lifecycle_report()
    assert r["transitions"]["NEW → WATCHING"] == 1


def test_useful_lead_remains_new(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session, lead_status="NEW")
        lid = lead.id
    record_outcome(lid, "USEFUL")
    r = lifecycle_report()
    assert r["cross_reference"]["useful_leads_remained_new"] == 1


def test_written_lead_reaches_watching(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(session, lead_status="WATCHING")
        lid = lead.id
        session.add(LeadEvent(lead_id=lid, event_type="STATUS_CHANGE", observed_at=now, summary="NEW → WATCHING"))
    record_outcome(lid, "WRITTEN")
    r = lifecycle_report()
    assert r["cross_reference"]["written_leads_never_reached_watching"] == 0


def test_false_lead_reaches_actionable(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        lead = _lead(session, lead_status="ACTIONABLE")
        lid = lead.id
        session.add(LeadEvent(lead_id=lid, event_type="STATUS_CHANGE", observed_at=now, summary="NEW → WATCHING"))
        session.add(LeadEvent(lead_id=lid, event_type="BECAME_ACTIONABLE", observed_at=now + timedelta(hours=1), summary="WATCHING → ACTIONABLE"))
    record_outcome(lid, "FALSE")
    r = lifecycle_report()
    assert r["cross_reference"]["false_leads_reached_watching"] == 1


# ---------------------------------------------------------------------------
# Missed stories
# ---------------------------------------------------------------------------

def test_missed_story_manual_record(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    row = record_miss(title="Some story CTW missed", source="ithome", failure_stage="LOW_SCORE")
    assert row is not None
    assert row.failure_stage == "LOW_SCORE"
    rows = list_missed_stories()
    assert len(rows) == 1


def test_missed_story_invalid_failure_stage(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    assert record_miss(title="x", failure_stage="NOT_A_STAGE") is None


def test_missed_story_linked_lead_confirmed_alert_suppression(tmp_path, monkeypatch):
    """A miss linked to a lead that was explicitly SUPPRESSED_SCORE gets a
    confirmed reconstruction, not a guess."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        cluster = StoryCluster(first_seen_source="chiphell", first_seen_at=_now(), created_at=_now(), updated_at=_now())
        session.add(cluster)
        session.flush()
        lead = _lead(session, cluster_id=cluster.id, priority_score=30, notified=False)
        lid = lead.id
    miss = record_miss(title="Missed leak", matched_lead_id=lid, failure_stage="LOW_SCORE")
    r = reconstruct_miss(miss.id)
    assert r["story_existed_in_ctw"] == "YES"
    assert r["matched_lead_id"] == lid
    assert r["certainty"] == "confirmed"
    assert r["likely_failure_stage"] == "LOW_SCORE"


def test_missed_story_linked_cluster_no_lead(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        cluster = StoryCluster(first_seen_source="chiphell", first_seen_at=_now(), created_at=_now(), updated_at=_now())
        session.add(cluster)
        session.flush()
        cid = cluster.id
    miss = record_miss(title="Cluster but no lead", matched_cluster_id=cid, failure_stage="CLUSTER_MISSED")
    r = reconstruct_miss(miss.id)
    assert r["story_existed_in_ctw"] == "YES"
    assert r["matched_lead_id"] is None
    assert r["likely_failure_stage"] == "CLUSTER_MISSED"
    assert r["certainty"] == "probable"


def test_missed_story_unknown_failure_stage_when_unlinked_and_healthy(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    miss = record_miss(title="Totally unlinked", source="ithome", failure_stage="UNKNOWN")
    r = reconstruct_miss(miss.id)
    assert r["story_existed_in_ctw"] == "NO"
    assert r["certainty"] == "unknown"


def test_missed_story_blocked_source_reconstruction(tmp_path, monkeypatch):
    """A miss attributed to a source with fresh BLOCKED telemetry gets a
    probable (not confirmed — no point-in-time history) reconstruction."""
    _db(tmp_path, monkeypatch)
    now = _now()
    with get_session() as session:
        for i in range(3):
            session.add(SourceRun(
                source="hkepc", layer="NEWS",
                started_at=now - timedelta(hours=i), finished_at=now - timedelta(hours=i),
                success=False, articles_found=0, articles_new=0,
                soft_blocked=True, error_message="HTTP 402",
            ))
    miss = record_miss(title="Missed HKEPC story", source="hkepc", failure_stage="SOURCE_BLOCKED")
    r = reconstruct_miss(miss.id)
    assert r["source_monitored"] == "YES"
    assert r["source_health_now"] == "BLOCKED"
    assert r["likely_failure_stage"] == "SOURCE_BLOCKED"
    assert r["certainty"] == "probable"


def test_missed_story_source_not_monitored(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    miss = record_miss(title="Weibo story", source="weibo", failure_stage="SOURCE_NOT_MONITORED")
    r = reconstruct_miss(miss.id)
    assert r["source_monitored"] == "NO"
    assert r["likely_failure_stage"] == "SOURCE_NOT_MONITORED"
    assert r["certainty"] == "confirmed"


def test_missed_story_report_counts(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    record_miss(title="a", failure_stage="LOW_SCORE")
    record_miss(title="b", failure_stage="LOW_SCORE")
    record_miss(title="c", failure_stage="SOURCE_NOT_MONITORED")
    r = missed_story_report()
    assert r["total"] == 3
    assert r["by_failure_stage"]["LOW_SCORE"] == 2


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def test_validation_page_loads_empty_db(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/validation")
    assert r.status_code == 200


def test_validation_page_filters_work(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        _lead(session)
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/validation?since_days=7")
    assert r.status_code == 200


def test_validation_page_shows_corrected_source_metrics(tmp_path, monkeypatch):
    """The source-performance section on /validation must reflect
    cluster-based touched counts, not just first-attribution."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        c = _cluster(session)
        _lead(session, cluster_id=c.id, first_signal_source="chiphell")
        _article(session, c.id, "ithome")  # touched, never first
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/validation")
    assert r.status_code == 200
    assert "ithome" in r.text


def test_validation_html_escaped(tmp_path, monkeypatch):
    """A missed-story title with HTML/script content must render escaped."""
    _db(tmp_path, monkeypatch)
    record_miss(title="<script>alert(1)</script>", failure_stage="OTHER")
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get("/validation")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_validation_get_is_read_only(tmp_path, monkeypatch):
    """Loading the page must not create SourceRun/LeadOutcome/IngestionRun rows."""
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        before_runs = len(session.execute(select(SourceRun)).scalars().all())
    from fastapi.testclient import TestClient
    from web.app import app
    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "sources.ithome.ITHomeSource.fetch_latest"
    ) as mocked:
        client = TestClient(app)
        client.get("/validation")
        client.get("/leads/1")  # 404 path — still must not scrape
        mocked.assert_not_called()
    with get_session() as session:
        after_runs = len(session.execute(select(SourceRun)).scalars().all())
    assert before_runs == after_runs == 0


def test_lead_detail_outcome_form_escapes_notes(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session)
        lid = lead.id
    record_outcome(lid, "USEFUL", note="<b>bold note</b>")
    from fastapi.testclient import TestClient
    from web.app import app
    client = TestClient(app)
    r = client.get(f"/leads/{lid}")
    assert "<b>bold note</b>" not in r.text
    assert "&lt;b&gt;" in r.text


# ---------------------------------------------------------------------------
# CLI-level: JSON validity + read-only guarantee
# ---------------------------------------------------------------------------

def test_cli_editorial_report_json_valid(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from pipeline.editorial_validation import editorial_funnel
    payload = editorial_funnel()
    # Simulate what main.py does for --json
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    reparsed = json.loads(text)
    assert reparsed["counts"]["total_leads"] == 0


def test_reports_do_not_mutate_db(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        lead = _lead(session, priority_score=33)
        lid = lead.id
        session.add(IngestionRun(started_at=_now(), trigger="MANUAL", status="SUCCESS"))
    editorial_funnel()
    source_performance()
    lead_time_analytics()
    lead_type_performance()
    alert_performance()
    lifecycle_report()
    missed_story_report()
    with get_session() as session:
        lead = session.get(StoryLead, lid)
        assert lead.priority_score == 33
        assert len(session.execute(select(IngestionRun)).scalars().all()) == 1
