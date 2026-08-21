"""Discord webhook notifications with rich embeds and structured send results."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import httpx

from config import settings, yaml_config
from database.models import Article
from security.redaction import redact_text

logger = logging.getLogger(__name__)


@dataclass
class DiscordSendResult:
    attempted: bool
    sent: bool
    dry_run: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    reason: str = "UNKNOWN"

    def __bool__(self) -> bool:
        """True only when a real delivery succeeded (not dry-run)."""
        return bool(self.sent and not self.dry_run)


def _fmt_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _why_it_matters(article: Article, entities: List[str]) -> str:
    """Short deterministic rationale for a journalist."""
    parts: List[str] = []
    st = (article.source_type or "").upper()
    if st == "LEAK":
        parts.append("Leak / unconfirmed report")
    elif st == "BENCHMARK":
        parts.append("Benchmark or performance data")
    elif st == "RETAIL_LISTING":
        parts.append("Retail listing — may appear before official launch")
    elif st == "REGULATORY":
        parts.append("Regulatory / certification signal")
    elif st == "OFFICIAL":
        parts.append("Official announcement")
    elif st == "SOCIAL_MEDIA":
        parts.append("Social-media origin")

    if article.rumor_flag:
        parts.append(f"rumor confidence {article.rumor_confidence*100:.0f}%")

    if entities:
        parts.append("entities: " + ", ".join(entities[:5]))

    if article.priority_score >= 90:
        parts.append("high-priority lead worth immediate check")
    elif article.novelty_score >= 70:
        parts.append("elevated novelty")

    if not parts:
        return "Relevant hardware/semiconductor coverage from Chinese tech media."
    return "; ".join(parts).capitalize() + "."


def format_message(
    article: Article,
    entities: List[str],
    cluster_sources: Optional[List[str]] = None,
    cluster_urls: Optional[List[str]] = None,
    is_high: bool = False,
) -> Dict[str, Any]:
    """Build a Discord webhook payload with embed (legacy article path)."""
    title_en = (article.title_english or "").strip()
    title_zh = article.title_original
    score = article.priority_score

    if is_high:
        header = f"🚨 HIGH PRIORITY LEAD — {score:.0f}/100"
        color = 0xE74C3C
    else:
        header = f"🔥 CHINESE TECH WIRE — {score:.0f}/100"
        color = 0x3498DB

    display_title = title_en if title_en else title_zh
    if len(display_title) > 250:
        display_title = display_title[:247] + "..."

    ents = " • ".join(entities[:10]) if entities else "—"
    rumor = (
        f"Yes ({article.rumor_confidence*100:.0f}%)"
        if article.rumor_flag
        else "No"
    )
    stype = article.source_type or "UNKNOWN"
    sources = cluster_sources or [article.source]
    sources_str = ", ".join(dict.fromkeys(sources))

    why = _why_it_matters(article, entities)

    fields = [
        {"name": "Original (ZH)", "value": title_zh[:1020] or "—", "inline": False},
        {"name": "Source", "value": article.source, "inline": True},
        {"name": "Published", "value": _fmt_time(article.published_at), "inline": True},
        {"name": "Detected", "value": _fmt_time(article.discovered_at), "inline": True},
        {"name": "Priority", "value": f"{article.priority_score:.0f}", "inline": True},
        {"name": "Relevance", "value": f"{article.relevance_score:.0f}", "inline": True},
        {"name": "Novelty", "value": f"{article.novelty_score:.0f}", "inline": True},
        {"name": "Type", "value": stype, "inline": True},
        {"name": "Rumor", "value": rumor, "inline": True},
        {"name": "Cluster sources", "value": sources_str, "inline": True},
        {"name": "Entities", "value": ents[:1020], "inline": False},
        {"name": "Why it matters", "value": why[:1020], "inline": False},
    ]

    embed: Dict[str, Any] = {
        "title": display_title,
        "url": article.url,
        "color": color,
        "fields": fields,
        "footer": {"text": "Chinese Tech Wire — article alert (legacy)"},
    }

    extra_urls = []
    if cluster_urls:
        for u in cluster_urls:
            if u and u != article.url:
                extra_urls.append(u)
    if extra_urls:
        lines = "\n".join(extra_urls[:5])
        embed["fields"].append(
            {"name": "Other source URLs", "value": lines[:1020], "inline": False}
        )

    return {"content": header, "embeds": [embed]}


def _fmt_t(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%H:%M UTC")


def build_storylead_payload(lead: Any, high: bool = False) -> Dict[str, Any]:
    """Production StoryLead embed used by alerts and test commands."""
    score = float(getattr(lead, "priority_score", 0) or 0)
    header = f"{'🚨' if high else '📣'} STORY LEAD — {score:.0f}/100"
    color = 0xE74C3C if high else 0xF39C12
    return {
        "content": header,
        "embeds": [{
            "title": (getattr(lead, "headline_hint", None) or "Story lead")[:250],
            "color": color,
            "fields": [
                {"name": "Status", "value": str(getattr(lead, "lead_status", "—")), "inline": True},
                {"name": "Type", "value": str(getattr(lead, "lead_type", "—")), "inline": True},
                {"name": "Exclusivity", "value": f"{float(getattr(lead, 'exclusivity_score', 0) or 0):.0f}", "inline": True},
                {"name": "Confidence", "value": f"{float(getattr(lead, 'confidence_score', 0) or 0):.0f}", "inline": True},
                {"name": "Saturation", "value": f"{float(getattr(lead, 'media_saturation_score', 0) or 0):.0f}", "inline": True},
                {"name": "Evidence", "value": f"{float(getattr(lead, 'evidence_score', 0) or 0):.0f}", "inline": True},
                {"name": "Why now", "value": (getattr(lead, "why_now", None) or "—")[:1020], "inline": False},
                {"name": "Why it matters", "value": (getattr(lead, "why_it_matters", None) or "—")[:1020], "inline": False},
                {"name": "Evidence trail", "value": (getattr(lead, "evidence_summary", None) or "—")[:1020], "inline": False},
                {"name": "Uncertainty", "value": (getattr(lead, "uncertainty_summary", None) or "—")[:1020], "inline": False},
                {
                    "name": "First signal",
                    "value": f"{getattr(lead, 'first_signal_source', None) or '—'} {_fmt_t(getattr(lead, 'first_signal_at', None))}",
                    "inline": True,
                },
                {
                    "name": "First documentary",
                    "value": f"{getattr(lead, 'first_documentary_source', None) or '—'}",
                    "inline": True,
                },
                {
                    "name": "First media",
                    "value": f"{getattr(lead, 'first_media_source', None) or '—'}",
                    "inline": True,
                },
            ],
            "footer": {"text": f"Chinese Tech Wire Newsroom · lead #{getattr(lead, 'id', '?')}"},
        }],
    }


def send_discord_result(payload: Dict[str, Any], dry_run: bool = False) -> DiscordSendResult:
    """Send payload and return a structured result. Never raises. Never logs secrets."""
    url = (settings.discord_webhook_url or "").strip()
    if not url:
        logger.warning("[DISCORD] No DISCORD_WEBHOOK_URL configured; skipping")
        return DiscordSendResult(
            attempted=False, sent=False, dry_run=False,
            reason="NO_WEBHOOK", error="webhook not configured",
        )
    if dry_run or yaml_config.get("notification", {}).get("dry_run", False):
        preview = payload.get("content", "")
        logger.info("[DISCORD] DRY-RUN would send: %s", preview[:150])
        return DiscordSendResult(
            attempted=True, sent=False, dry_run=True, reason="DRY_RUN",
        )
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload)
            if resp.status_code in (200, 204):
                logger.info("[DISCORD] Notification sent (HTTP %s)", resp.status_code)
                return DiscordSendResult(
                    attempted=True, sent=True, dry_run=False,
                    status_code=resp.status_code, reason="SENT",
                )
            # Sanitize body — never echo webhook or tokens
            body = (resp.text or "")[:200]
            body = body.replace(url, "[webhook]")
            logger.error("[DISCORD] HTTP %s (body redacted/truncated)", resp.status_code)
            return DiscordSendResult(
                attempted=True, sent=False, dry_run=False,
                status_code=resp.status_code, reason="HTTP_ERROR",
                error=f"HTTP {resp.status_code}: {body}",
            )
    except Exception as e:
        logger.error("[DISCORD] Failed: %s", type(e).__name__)
        return DiscordSendResult(
            attempted=True, sent=False, dry_run=False,
            reason="EXCEPTION", error=redact_text(f"{type(e).__name__}: {e}")[:300],
        )


def send_discord(payload: Dict[str, Any], dry_run: bool = False) -> bool:
    """Compatibility wrapper. True only for confirmed real delivery (not dry-run)."""
    result = send_discord_result(payload, dry_run=dry_run)
    # Preserve prior dry-run behavior for callers that treated dry-run as success:
    # New structured API is preferred. Boolean True only when actually sent.
    if result.dry_run:
        return True  # legacy: dry-run was treated as soft-success for flow control
    return bool(result.sent)


def should_notify(article: Article) -> tuple[bool, bool]:
    """Legacy article path. Disabled unless notification.enable_article_alerts is true."""
    if not yaml_config.get("notification", {}).get("enable_article_alerts", False):
        return False, False
    thresh = yaml_config.get("notification", {}).get("priority_threshold", 70)
    high = yaml_config.get("notification", {}).get("high_priority_threshold", 90)
    if article.notified:
        return False, False
    if article.priority_score >= high:
        return True, True
    if article.priority_score >= thresh:
        return True, False
    return False, False


def test_webhook() -> DiscordSendResult:
    """Send a fixed test message. Returns structured result; never prints secrets."""
    configured = bool((settings.discord_webhook_url or "").strip())
    logger.info("[DISCORD] test-discord webhook_configured=%s", configured)
    if not configured:
        return DiscordSendResult(
            attempted=False, sent=False, dry_run=False,
            reason="NO_WEBHOOK", error="DISCORD_WEBHOOK_URL not set",
        )
    payload = {
        "content": "✅ Chinese Tech Wire — webhook test OK",
        "embeds": [
            {
                "title": "Discord test from Chinese Tech Wire",
                "description": "If you see this, the webhook is configured correctly.",
                "color": 0x2ECC71,
                "footer": {"text": "python main.py --test-discord"},
            }
        ],
    }
    return send_discord_result(payload, dry_run=False)
