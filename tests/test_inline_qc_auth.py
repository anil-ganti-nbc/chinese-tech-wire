"""Inline QC auth transport — regression coverage for the release blocker.

Forensic contract pinned here:

- The dashboard's mutation contract (Phase 0 containment middleware,
  web.app.authenticated_mutations_only) requires
  `Authorization: Bearer $CTW_DASHBOARD_AUTH_TOKEN` on every non-GET
  request. The GUI's `run_gui()` generates one token per process and the
  pages render it into their own loopback-served HTML so the page's own
  JavaScript fetch() can present it back — that is the same transport the
  Health page's "Run collector now" button has always used.
- The original inline QC bar shipped as a *plain browser form POST*, which
  cannot attach that header: every click rendered the middleware's raw
  403 JSON ("Authenticated dashboard profile required for mutations.")
  even though `can_qc` had rendered the buttons. These tests reproduce
  that exact browser-style request and prove both halves:
    * an unauthenticated browser-style POST is still rejected (no auth
      weakening), and
    * the same request through the authenticated fetch transport reaches
      the durable QC archive, clears the queue, and returns the exact
      filter context.
- The QC endpoint additionally serves the fetch transport with
  Accept: application/json results carrying the same validated `next`
  target; plain form posts keep their original 303 semantics.

These tests deliberately exercise the REAL env-token path
(`app.state.mutation_authorizer = None` + CTW_DASHBOARD_AUTH_TOKEN), not
only the always-true test authorizer from conftest — the conftest
authorizer is exactly why the original regression was invisible to the
first inline-QC suite.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from database.db import get_session
from database.models import StoryLead
from pipeline.qc import already_qcd

from tests.test_gui import client  # noqa: F401 (pytest fixture, reused)


def _active_lead_id():
    with get_session() as session:
        lead = session.query(StoryLead).filter(
            StoryLead.lead_status.in_(["NEW", "WATCHING", "ACTIONABLE", "ESCALATED"])
        ).first()
        return lead.id if lead else None


def _seed_active_lead() -> int:
    """A minimal active lead, for tests that need more than the fixture's one."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    with get_session() as session:
        lead = StoryLead(
            lead_status="NEW",
            lead_type="LEAK",
            headline_hint="Auth transport test lead",
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
        session.add(lead)
        session.flush()
        return lead.id


def _raw_app_client(monkeypatch, token: str | None):
    """TestClient against the REAL auth configuration: no injected
    authorizer, exactly the env token run_gui() would have set."""
    from web.app import app

    app.state.mutation_authorizer = None
    if token is None:
        monkeypatch.delenv("CTW_DASHBOARD_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CTW_DASHBOARD_AUTH_TOKEN", token)
    yield TestClient(app)
    app.state.mutation_authorizer = None


@pytest.fixture()
def raw_env_token_client(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/auth.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    from database.db import init_db
    init_db(db_url)
    yield from _raw_app_client(monkeypatch, token="sentinel-op-token")


@pytest.fixture()
def raw_no_token_client(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/noauth.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod
    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg
    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    from database.db import init_db
    init_db(db_url)
    yield from _raw_app_client(monkeypatch, token=None)


# -- the exact regression: browser-style form POST has no Bearer header --------


def test_browser_style_form_post_without_bearer_is_still_rejected(raw_no_token_client):
    """The original regression, reproduced honestly: a plain browser form
    POST (no Authorization header possible) must be rejected by the
    middleware — the fix is the GUI transport, never a weaker contract."""
    lead_id = _seed_active_lead()
    r = raw_no_token_client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "/newsroom"},
        headers={"Accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Authenticated dashboard profile required for mutations."
    assert not already_qcd(lead_id)  # nothing leaked past the middleware


def test_json_accept_does_not_bypass_authentication(raw_no_token_client):
    lead_id = _seed_active_lead()
    r = raw_no_token_client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "/newsroom"},
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert not already_qcd(lead_id)


def test_wrong_token_is_rejected(raw_env_token_client):
    lead_id = _seed_active_lead()
    r = raw_env_token_client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "/newsroom"},
        headers={"Authorization": "Bearer wrong-token", "Accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert not already_qcd(lead_id)


# -- the real production mechanism: env token + Bearer header -------------------


def test_env_token_authenticated_qc_reaches_archive_and_clears_queue(raw_env_token_client):
    """Exactly what the fixed GUI does: fetch POST with
    Authorization: Bearer $CTW_DASHBOARD_AUTH_TOKEN and
    Accept: application/json. Must archive durably and clear the queue."""
    lead_id = _seed_active_lead()
    context = "/newsroom?status=NEW&hours=6&q=auth"
    r = raw_env_token_client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": context},
        headers={"Authorization": "Bearer sentinel-op-token", "Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["outcome"] == "archived"
    # same filter context restored, with the feedback flag appended
    assert body["next"].startswith("/newsroom?")
    assert "status=NEW" in body["next"] and "hours=6" in body["next"] and "fb=1" in body["next"]

    # reached the existing durable QC archive
    assert already_qcd(lead_id) == "USEFUL"

    # the item left the default queue
    with get_session() as session:
        status = session.get(StoryLead, lead_id).lead_status
    assert status not in ("NEW", "WATCHING", "ACTIONABLE", "ESCALATED")
    html = raw_env_token_client.get("/newsroom").text
    assert f"/leads/{lead_id}\"" not in html


def test_all_four_fleet_decisions_work_through_authenticated_transport(client):
    decisions = ["USEFUL", "NOT_USEFUL", "FALSE_POSITIVE", "DUPLICATE"]
    for decision in decisions:
        lead_id = _seed_active_lead()
        r = client.post(
            f"/leads/{lead_id}/feedback",
            data={"feedback": decision, "next": "/newsroom?hours=3"},
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 200, decision
        assert r.json()["ok"] is True, decision
        assert already_qcd(lead_id) == decision, decision


def test_already_qcd_via_json_is_non_mutating_success(client):
    lead_id = _seed_active_lead()
    first = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "DUPLICATE", "next": "/newsroom"},
        headers={"Accept": "application/json"},
    )
    assert first.status_code == 200 and first.json()["outcome"] == "archived"
    second = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "/newsroom"},
        headers={"Accept": "application/json"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["ok"] is True and body["outcome"] == "already_qcd"
    assert "fb=already" in body["next"]
    assert already_qcd(lead_id) == "DUPLICATE"  # the archive still holds one decision


def test_json_next_is_validated_against_off_site_redirects(client):
    lead_id = _seed_active_lead()
    r = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "USEFUL", "next": "https://evil.example/newsroom"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["next"].startswith(f"/leads/{lead_id}")


def test_invalid_feedback_json_is_a_400_not_an_archive(client):
    lead_id = _seed_active_lead()
    r = client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "NOT_A_DECISION", "next": "/newsroom"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert not already_qcd(lead_id)


# -- plain form posts keep their original 303 semantics -------------------------


def test_plain_form_post_with_bearer_keeps_303_redirect_semantics(raw_env_token_client):
    lead_id = _seed_active_lead()
    r = raw_env_token_client.post(
        f"/leads/{lead_id}/feedback",
        data={"feedback": "NOT_USEFUL", "next": "/newsroom?status=NEW"},
        headers={"Authorization": "Bearer sentinel-op-token"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/newsroom?status=NEW")
    assert "fb=1" in r.headers["location"]
    assert already_qcd(lead_id) == "NOT_USEFUL"


# -- the fix does not weaken other mutation endpoints ---------------------------


def test_other_mutation_endpoints_still_require_authentication(raw_no_token_client):
    assert raw_no_token_client.post("/operations/run-now").status_code == 403
    lead_id = _seed_active_lead()
    assert (
        raw_no_token_client.post(
            f"/leads/{lead_id}/outcome", data={"outcome": "WRITTEN_UP"}
        ).status_code
        == 403
    )


# -- the GUI pages carry the authenticated transport ---------------------------


def test_newsroom_renders_token_for_inline_qc_when_configured(raw_env_token_client):
    _seed_active_lead()
    html = raw_env_token_client.get("/newsroom").text
    assert "CTW_DASHBOARD_AUTH_TOKEN" in html
    assert "sentinel-op-token" in html  # loopback-served page, same as Health
    assert "qc.js" in html
    assert 'class="qc-inline"' in html


def test_newsroom_without_auth_profile_stays_read_only(raw_no_token_client):
    _seed_active_lead()
    html = raw_no_token_client.get("/newsroom").text
    assert "sentinel-op-token" not in html
    assert "QC off" in html  # honest read-only hint, no dead buttons
    assert 'class="qc-inline"' not in html


def test_written_stays_out_of_the_inline_bar(client):
    html = client.get("/newsroom").text
    forms = html.split('<form class="qc-inline"')[1:]
    assert forms
    for form in forms:
        assert 'value="WRITTEN"' not in form.split("</form>")[0]
