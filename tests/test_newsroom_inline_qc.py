"""Newsroom inline QC bar (fleet-standard, beside Source) + English-first
headline preference + deliberate translation backfill.

Fleet contract pinned here:

- The newsroom main page carries a QC column immediately next to Source,
  with exactly the four fleet-standard decisions — CTW's own fifth value
  (WRITTEN) stays on the lead-detail page, never in the inline fleet bar.
- One click posts to the EXISTING /leads/{id}/feedback endpoint, which
  delegates to pipeline.qc.record_qc_decision: transactional archive +
  immediate queue removal + race guard. Nothing new is invented.
- The redirect returns to the same filtered newsroom view.
- English titles are preferred for display when a translation exists;
  originals stay stored and reachable; translation failure never blocks
  ingestion; page loads never call translation APIs or mutate data.
"""

from __future__ import annotations

import pytest

from database.db import get_session
from database.models import Article, StoryCluster, StoryLead
from pipeline.newsroom import build_headline_hint, upsert_lead_for_cluster
from pipeline.translate_backfill import (
    BackfillCounts,
    _translate_one,
    translate_missing,
)


def _active_lead_id():
    with get_session() as session:
        lead = session.query(StoryLead).filter(
            StoryLead.lead_status.in_(["NEW", "WATCHING", "ACTIONABLE", "ESCALATED"])
        ).first()
        return lead.id if lead else None


def _article_by_source(source):
    with get_session() as session:
        return session.query(Article).filter(Article.source == source).one()


# Reuse test_gui.py's seeded-app `client` fixture so the inline-QC tests run
# against exactly the same TestClient + fixture data the newsroom suite pins.
from tests.test_gui import client  # noqa: E402,F401 (pytest fixture, imported for reuse)


# -- 1-3: inline QC bar next to Source ------------------------------------------


def test_newsroom_has_inline_qc_bar_beside_source_column(client):
    r = client.get("/newsroom")
    assert r.status_code == 200
    html = r.text
    assert "<th>Source</th>" in html and "<th>QC</th>" in html
    assert html.index("<th>Source</th>") < html.index("<th>QC</th>") < html.index("<th>Why now</th>")
    # the inline form is the durable QC path, not a decorative widget
    assert 'class="qc-inline"' in html and "/feedback" in html


def test_newsroom_inline_bar_offers_all_four_fleet_decisions(client):
    html = client.get("/newsroom").text
    for decision in ("USEFUL", "NOT_USEFUL", "FALSE_POSITIVE", "DUPLICATE"):
        assert f'value="{decision}"' in html, f"missing inline decision {decision}"


def test_written_is_ctw_specific_and_not_in_the_inline_fleet_bar(client):
    html = client.get("/newsroom").text
    forms = html.split('<form class="qc-inline"')[1:]
    assert forms, "inline QC forms must be rendered"
    for form in forms:
        body = form.split("</form>")[0]
        assert 'value="WRITTEN"' not in body
        assert 'value="USEFUL"' in body and 'value="DUPLICATE"' in body


def test_lead_detail_keeps_the_ctw_written_decision(client):
    r = client.get("/leads/1")
    if r.status_code != 200:
        pytest.skip("no lead detail available in this fixture")
    assert 'value="WRITTEN"' in r.text


# -- 4: one click archives + clears the queue + returns to context ---------------


def test_inline_qc_post_archives_and_returns_to_the_filtered_newsroom(client):
    lead_id = _active_lead_id()
    context = "/newsroom?status=WATCHING&hours=6"
    r = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": context},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/newsroom")
    assert "status=WATCHING" in r.headers["location"]

    # archived into the separate QC ledger
    from pipeline.qc import already_qcd
    assert already_qcd(lead_id) == "USEFUL"

    # removed from the default queue immediately
    html = client.get("/newsroom").text
    assert f"/leads/{lead_id}\"" not in html


def test_inline_qc_race_is_a_refusal_not_a_second_archive(client):
    from pipeline.qc import AlreadyQcdError, record_qc_decision
    lead_id = _active_lead_id()
    record_qc_decision(lead_id, "NOT_USEFUL")
    r = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "/newsroom"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "fb=already" in r.headers["location"]
    with pytest.raises(AlreadyQcdError):
        record_qc_decision(lead_id, "USEFUL")  # the archive still holds one row


def test_off_site_next_is_refused(client):
    lead_id = _active_lead_id()
    r = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "https://evil.example/newsroom"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/leads/{lead_id}")


# -- 5-6: English-first headline, original preserved -----------------------------


def test_newsroom_headline_prefers_stored_english_title(client):
    html = client.get("/newsroom").text
    # the seeded cluster's founding article carries an English translation
    assert "RTX 6070 spotted" in html
    assert "RTX 6070 曝光" not in html  # the original is not the main-page headline


def test_headline_prefers_the_representative_records_own_english():
    from types import SimpleNamespace

    ctx = {
        "cluster": __import__("types").SimpleNamespace(representative_title="代表 原文"),
        "articles": [__import__("types").SimpleNamespace(
            title_original="代表 原文", title_english="Representative article")],
        "community": [],
        "docs": [],
    }
    hint = build_headline_hint(ctx, "LEAK")
    assert "Representative article" in hint
    assert "代表 原文" not in hint


def test_headline_falls_back_to_original_when_no_translation_exists():
    from types import SimpleNamespace
    ctx = {
        "cluster": SimpleNamespace(representative_title="纯中文标题样例"),
        "articles": [],
        "community": [],
        "docs": [],
    }
    hint = build_headline_hint(ctx, "LEAK")
    assert "纯中文标题样例" in hint  # fallback keeps the lead visible, never hidden


def test_original_title_stays_stored_and_rebuild_only_touches_display(client, monkeypatch):
    with get_session() as session:
        article = session.query(Article).filter(Article.source == "ithome").one()
        cluster_id = article.duplicate_group_id
        assert article.title_original == "RTX 6070 曝光"
        assert article.title_english == "RTX 6070 spotted"
    upsert_lead_for_cluster(cluster_id, dry_run=True)
    with get_session() as session:
        article = session.query(Article).filter(Article.source == "ithome").one()
        assert article.title_original == "RTX 6070 曝光"   # stored original untouched
        assert article.title_english == "RTX 6070 spotted"  # translation untouched


# -- 7: provider failure never blocks ingestion ---------------------------------


from pipeline.translate import Translator as _BaseTranslator


class FailingTranslator(_BaseTranslator):
    """A real wrapper whose provider call explodes - like an unreachable
    OpenRouter endpoint. The cache-aware base translate() must convert the
    failure into None, never a pass-aborting exception."""

    provider_name = "failing"

    def translate_raw(self, text, source_language="zh", target_language="en"):
        raise RuntimeError("provider exploded")


def test_translation_failure_does_not_prevent_ingestion(tmp_path, monkeypatch):
    """The enrichment pass is exactly the ingestion coupling surface: a
    provider failure leaves the record stored, untranslated (pending)."""
    from database.db import init_db
    import config as cfg
    import database.db as dbmod

    db_url = f"sqlite:///{tmp_path}/ingest.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    from datetime import datetime, timezone

    with get_session(db_url) as session:
        session.add(Article(
            source="ithome", source_article_id="x1",
            title_original="失败测试标题",
            url="https://www.ithome.com/x1",
            discovered_at=datetime.now(timezone.utc), priority_score=60,
        ))
    counts = translate_missing(FailingTranslator(), article_limit=10, community_limit=10)
    assert counts["failed"] == 1 and counts["translated"] == 0
    with get_session(db_url) as session:
        row = session.query(Article).filter(Article.source == "ithome").one()
        assert row.title_original == "失败测试标题"  # record intact
        assert row.title_english is None  # left pending, honestly


# -- 8: GET /newsroom never calls translation APIs or mutates data ---------------


def test_newsroom_get_calls_no_translation_and_mutates_nothing(client, monkeypatch):
    calls = []

    def forbidden(self, *args, **kwargs):
        calls.append("called")
        raise AssertionError("GET /newsroom must not call the translation provider")

    monkeypatch.setattr("pipeline.translate.Translator.translate", forbidden)

    with get_session() as session:
        before = session.query(StoryLead).count()
    r = client.get("/newsroom")
    assert r.status_code == 200
    assert calls == []
    with get_session() as session:
        assert session.query(StoryLead).count() == before  # no data churn


# -- backfill: cache prevents paying twice --------------------------------------


class CountingTranslator(_BaseTranslator):
    """Real-shaped fake: provider work happens in translate_raw, so the
    base class's cache-aware translate() classifies calls exactly the way
    the production translator does (raw call = paid request)."""

    provider_name = "counting"
    model_name = "test"

    def __init__(self):
        self.calls = []

    def translate_raw(self, text, source_language="zh", target_language="en"):
        self.calls.append(text)
        return f"EN: {text}"


def test_backfill_is_cache_aware_and_idempotent(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from database.db import init_db
    import config as cfg
    import database.db as dbmod

    db_url = f"sqlite:///{tmp_path}/bf.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)

    with get_session(db_url) as session:
        # two articles sharing one identical headline: the same text, twice
        session.add(Article(
            source="ithome", source_article_id="t1", title_original="同一标题 翻译测试",
            url="https://example.com/t1",
            discovered_at=datetime.now(timezone.utc), priority_score=60,
        ))
        session.add(Article(
            source="mydrivers", source_article_id="t2", title_original="同一标题 翻译测试",
            url="https://example.com/t2",
            discovered_at=datetime.now(timezone.utc), priority_score=60,
        ))

    first = CountingTranslator()
    counts = translate_missing(first, article_limit=10, community_limit=10)
    assert counts["attempted"] == 2
    assert counts["translated"] == 1          # the first occurrence paid once
    assert counts["cached"] == 1              # the second was served by the cache
    assert len(first.calls) == 1              # exactly ONE provider call for identical text

    # a later pass over unchanged data: everything already has translations,
    # so nothing is attempted - idempotent, no repeated payment either way
    counts2 = translate_missing(CountingTranslator(), article_limit=10, community_limit=10)
    assert counts2["attempted"] == 0 and counts2["translated"] == 0 and counts2["cached"] == 0
