"""V0.5.3 StoryLead Discord alert stabilization tests.

All HTTP is mocked. No real Discord POSTs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from database.db import get_session, init_db
from database.models import LeadNotification, StoryCluster, StoryLead
from pipeline.alerts import (
    evaluate_alert_eligibility,
    get_alert_stats,
    maybe_alert_lead,
    preview_alerts,
    reset_alert_stats,
)
from pipeline.notify import DiscordSendResult, send_discord_result


def _db(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/alerts.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    import database.db as dbmod

    monkeypatch.setattr(dbmod, "DEFAULT_DB", db_url)
    import config as cfg

    monkeypatch.setattr(cfg.settings, "database_url", db_url)
    init_db(db_url)
    return db_url


def _now():
    return datetime.now(timezone.utc)


def _lead(**kwargs) -> StoryLead:
    now = _now()
    defaults = dict(
        lead_status="WATCHING",
        lead_type="EARLY_SIGNAL",
        headline_hint="RTX 6090 PCB photo on Chiphell",
        created_at=now,
        updated_at=now,
        last_activity_at=now - timedelta(hours=2),
        priority_score=54.0,
        evidence_score=40.0,
        relevance_score=70.0,
        confidence_score=45.0,
        exclusivity_score=60.0,
        media_saturation_score=10.0,
        novelty_score=70.0,
        momentum_score=40.0,
        source_diversity_score=30.0,
        editorial_value_score=55.0,
        notified=False,
        why_now="Community photo evidence",
        why_it_matters="Possible unreleased GPU",
        evidence_summary="Chiphell PHOTO_EVIDENCE",
        uncertainty_summary="Unconfirmed",
        first_signal_source="chiphell",
        first_signal_at=now - timedelta(hours=3),
    )
    defaults.update(kwargs)
    return StoryLead(**defaults)


@pytest.fixture()
def alert_cfg(monkeypatch):
    import config as cfg

    nr = dict(cfg.yaml_config.get("newsroom") or {})
    nr["alerts"] = {
        "enabled": True,
        "min_priority": 52,
        "high_priority": 55,
        "min_evidence": 15,
        "min_relevance": 35,
        "min_confidence": 25,
        "allowed_statuses": ["WATCHING", "ACTIONABLE", "ESCALATED"],
        "max_age_hours": 48,
        "material_score_delta": 8,
        "max_alerts_per_cycle": 5,
        "suppress_before_activation": True,
        "policy_activated_at": (_now() - timedelta(days=1)).isoformat(),
    }
    monkeypatch.setitem(cfg.yaml_config, "newsroom", nr)
    return nr["alerts"]


def test_old_policy_zero_eligible_on_soak_like_scores(alert_cfg):
    """Legacy ACTIONABLE+75 gate cannot fire when max score is ~57."""
    lead = _lead(priority_score=56.8, lead_status="WATCHING")
    # Simulate old gate
    old_eligible = lead.priority_score >= 75 and lead.lead_status in ("ACTIONABLE", "ESCALATED")
    assert old_eligible is False


def test_new_policy_alerts_qualifying_fresh_watching(alert_cfg):
    lead = _lead(priority_score=54.0, lead_status="WATCHING")
    d = evaluate_alert_eligibility(lead, policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.eligible is True
    assert d.would_send is True
    assert d.alert_reason == "FIRST_ELIGIBLE"


def test_stale_qualifying_lead_suppressed(alert_cfg):
    lead = _lead(
        priority_score=55.0,
        lead_status="WATCHING",
        last_activity_at=_now() - timedelta(hours=72),
    )
    d = evaluate_alert_eligibility(lead, policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.eligible is False
    assert d.reason_code == "SUPPRESSED_STALE"


def test_backlog_before_activation_suppressed(alert_cfg):
    activation = _now() - timedelta(hours=1)
    lead = _lead(
        priority_score=55.0,
        last_activity_at=_now() - timedelta(hours=24),
    )
    d = evaluate_alert_eligibility(lead, policy_activated_at=activation.isoformat())
    assert d.eligible is False
    assert d.reason_code == "SUPPRESSED_BACKLOG"


def test_watching_eligible_when_configured(alert_cfg):
    lead = _lead(lead_status="WATCHING", priority_score=53)
    d = evaluate_alert_eligibility(lead, policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.eligible is True


def test_new_status_not_in_allowed(alert_cfg):
    lead = _lead(lead_status="NEW", priority_score=55)
    d = evaluate_alert_eligibility(lead, policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.eligible is False
    assert d.reason_code == "SUPPRESSED_STATUS"


def test_successful_send_marks_notified(tmp_path, monkeypatch, alert_cfg):
    _db(tmp_path, monkeypatch)
    reset_alert_stats()
    lead = _lead()
    with get_session() as session:
        session.add(lead)
        session.flush()
        fake = DiscordSendResult(attempted=True, sent=True, dry_run=False, status_code=204, reason="SENT")
        with patch("pipeline.alerts.send_discord_result", return_value=fake):
            maybe_alert_lead(session, lead, dry_run=False, policy_activated_at=alert_cfg["policy_activated_at"])
        assert lead.notified is True
        assert lead.last_notified_at is not None
        assert lead.last_notified_priority == pytest.approx(54.0)
        n = session.execute(
            __import__("sqlalchemy").select(LeadNotification).where(LeadNotification.outcome == "SENT")
        ).scalars().all()
        assert len(n) >= 1


def test_http_failure_does_not_mark_notified(tmp_path, monkeypatch, alert_cfg):
    _db(tmp_path, monkeypatch)
    reset_alert_stats()
    lead = _lead()
    with get_session() as session:
        session.add(lead)
        session.flush()
        fake = DiscordSendResult(
            attempted=True, sent=False, dry_run=False, status_code=500, reason="HTTP_ERROR", error="HTTP 500"
        )
        with patch("pipeline.alerts.send_discord_result", return_value=fake):
            maybe_alert_lead(session, lead, dry_run=False, policy_activated_at=alert_cfg["policy_activated_at"])
        assert lead.notified is False
        failed = session.execute(
            __import__("sqlalchemy").select(LeadNotification).where(LeadNotification.outcome == "FAILED")
        ).scalars().all()
        assert len(failed) >= 1


def test_missing_webhook_does_not_mark_notified(tmp_path, monkeypatch, alert_cfg):
    _db(tmp_path, monkeypatch)
    reset_alert_stats()
    lead = _lead()
    with get_session() as session:
        session.add(lead)
        session.flush()
        fake = DiscordSendResult(
            attempted=False, sent=False, dry_run=False, reason="NO_WEBHOOK", error="webhook not configured"
        )
        with patch("pipeline.alerts.send_discord_result", return_value=fake):
            maybe_alert_lead(session, lead, dry_run=False, policy_activated_at=alert_cfg["policy_activated_at"])
        assert lead.notified is False


def test_dry_run_does_not_mark_notified(tmp_path, monkeypatch, alert_cfg):
    _db(tmp_path, monkeypatch)
    reset_alert_stats()
    lead = _lead()
    with get_session() as session:
        session.add(lead)
        session.flush()
        with patch(
            "pipeline.alerts.send_discord_result",
            return_value=DiscordSendResult(attempted=True, sent=False, dry_run=True, reason="DRY_RUN"),
        ):
            maybe_alert_lead(session, lead, dry_run=True, policy_activated_at=alert_cfg["policy_activated_at"])
        assert lead.notified is False
        assert get_alert_stats()["alerts_dry_run"] >= 1


def test_failed_delivery_remains_retryable(tmp_path, monkeypatch, alert_cfg):
    _db(tmp_path, monkeypatch)
    reset_alert_stats()
    lead = _lead()
    with get_session() as session:
        session.add(lead)
        session.flush()
        fail = DiscordSendResult(attempted=True, sent=False, dry_run=False, status_code=429, reason="HTTP_ERROR")
        ok = DiscordSendResult(attempted=True, sent=True, dry_run=False, status_code=204, reason="SENT")
        with patch("pipeline.alerts.send_discord_result", side_effect=[fail, ok]):
            maybe_alert_lead(session, lead, dry_run=False, policy_activated_at=alert_cfg["policy_activated_at"])
            assert lead.notified is False
            maybe_alert_lead(session, lead, dry_run=False, policy_activated_at=alert_cfg["policy_activated_at"])
            assert lead.notified is True


def test_already_notified_unchanged_suppressed(alert_cfg):
    lead = _lead(notified=True, last_notified_status="WATCHING", last_notified_priority=54.0)
    d = evaluate_alert_eligibility(lead, prev_status="WATCHING", policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.eligible is False
    assert d.reason_code == "SUPPRESSED_NOTIFIED"


def test_material_score_increase_uses_last_notified_priority(alert_cfg):
    lead = _lead(
        notified=True,
        last_notified_status="WATCHING",
        last_notified_priority=50.0,
        priority_score=59.0,
    )
    d = evaluate_alert_eligibility(lead, prev_status="WATCHING", policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.eligible is True
    assert d.alert_reason == "MATERIAL_SCORE_INCREASE"


def test_status_transition_realert(alert_cfg):
    lead = _lead(
        notified=True,
        lead_status="ACTIONABLE",
        last_notified_status="WATCHING",
        last_notified_priority=54.0,
        priority_score=54.0,
    )
    d = evaluate_alert_eligibility(
        lead, prev_status="WATCHING", policy_activated_at=alert_cfg["policy_activated_at"]
    )
    assert d.eligible is True
    assert d.alert_reason == "BECAME_ACTIONABLE"


def test_send_discord_result_dry_run_not_bool_true_for_delivery(monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg.settings, "discord_webhook_url", "https://example.com/hook")
    r = send_discord_result({"content": "x"}, dry_run=True)
    assert r.dry_run is True
    assert r.sent is False
    assert bool(r) is False  # must not look like confirmed delivery


def test_send_discord_result_no_webhook(monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg.settings, "discord_webhook_url", "")
    r = send_discord_result({"content": "x"}, dry_run=False)
    assert r.reason == "NO_WEBHOOK"
    assert r.sent is False


def test_preview_no_post(tmp_path, monkeypatch, alert_cfg):
    _db(tmp_path, monkeypatch)
    with get_session() as session:
        session.add(_lead())
    with patch("pipeline.alerts.send_discord_result") as mocked:
        rows = preview_alerts(since_hours=24, limit=10, ignore_backlog_gate=True)
        mocked.assert_not_called()
    assert isinstance(rows, list)


def test_alert_stats_reset_between_cycles(alert_cfg):
    reset_alert_stats()
    assert get_alert_stats()["alerts_evaluated"] == 0
    from pipeline.alerts import _bump

    _bump("alerts_evaluated", 3)
    assert get_alert_stats()["alerts_evaluated"] == 3
    reset_alert_stats()
    assert get_alert_stats()["alerts_evaluated"] == 0


def test_quality_gate_suppresses(alert_cfg):
    lead = _lead(priority_score=55, evidence_score=5, relevance_score=70, confidence_score=40)
    d = evaluate_alert_eligibility(lead, policy_activated_at=alert_cfg["policy_activated_at"])
    assert d.reason_code == "SUPPRESSED_QUALITY"
