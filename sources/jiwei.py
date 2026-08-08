"""Jiwei / 集微网 (laoyaoba.com) source adapter.

Discovery: homepage https://www.laoyaoba.com/ (and focus lists).
URL pattern: https://www.laoyaoba.com/n/{id}
Timestamps frequently relative (“41分钟前”, “5小时前”) or “07-23 09:00”.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

LIST_URL = "https://www.laoyaoba.com/"
ARTICLE_ID_RE = re.compile(r"(?:https?://(?:www\.)?laoyaoba\.com)?/n/(\d+)", re.IGNORECASE)

REL_MIN_RE = re.compile(r"(\d+)\s*分钟前")
REL_HOUR_RE = re.compile(r"(\d+)\s*小时前")
REL_DAY_RE = re.compile(r"(\d+)\s*天前")
ABS_MD_RE = re.compile(r"(0?\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})")
ABS_FULL_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})")


def _parse_relative_or_abs(
    text: str,
    now: Optional[datetime] = None,
    local_tz: Optional[timezone] = None,
) -> Optional[datetime]:
    """Convert relative Chinese timestamps or short absolute forms to UTC.

    Relative strings (``41分钟前``) are elapsed wall-clock time — subtract from
    an aware ``now`` (UTC is fine).

    Absolute naive strings (``07-23 15:51``, ``2026-07-23 15:51``) are local
    China time (Asia/Shanghai) and must be localized before conversion to UTC.
    """
    from zoneinfo import ZoneInfo

    if not text:
        return None
    text = text.strip()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    zone = local_tz or ZoneInfo("Asia/Shanghai")

    m = REL_MIN_RE.search(text)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).astimezone(timezone.utc)
    m = REL_HOUR_RE.search(text)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).astimezone(timezone.utc)
    m = REL_DAY_RE.search(text)
    if m:
        return (now - timedelta(days=int(m.group(1)))).astimezone(timezone.utc)

    m = ABS_FULL_RE.search(text)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            local = datetime(y, mo, d, h, mi, tzinfo=zone)
            return local.astimezone(timezone.utc)
        except ValueError:
            return None

    m = ABS_MD_RE.search(text)
    if m:
        mo, d, h, mi = map(int, m.groups())
        local_now = now.astimezone(zone)
        y = local_now.year
        try:
            local = datetime(y, mo, d, h, mi, tzinfo=zone)
            if local > local_now + timedelta(days=1):
                local = local.replace(year=y - 1)
            return local.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


class JiweiSource(BaseSource):
    name = "jiwei"
    region = "CN"
    language_variant = "zh-CN"
    base_url = "https://www.laoyaoba.com"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        try:
            html = self.fetch_html(LIST_URL)
            soup = BeautifulSoup(html, "lxml")
            seen_ids: set[str] = set()
            now = datetime.now(timezone.utc)

            for a in soup.find_all("a", href=True):
                href = a.get("href") or ""
                if not href:
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = self.make_absolute(href)

                m = ARTICLE_ID_RE.search(href)
                if not m:
                    continue
                art_id = m.group(1)
                if art_id in seen_ids:
                    continue

                title = (a.get_text() or "").strip()
                if not title or len(title) < 6:
                    continue
                if any(x in title for x in ("登录", "注册", "更多", "首页", "下载")):
                    continue

                seen_ids.add(art_id)

                clean = href.split("?")[0].split("#")[0]
                if not clean.startswith("http"):
                    clean = f"https://www.laoyaoba.com/n/{art_id}"
                if clean.startswith("http://"):
                    clean = "https://" + clean[7:]

                published: Optional[datetime] = None
                parent = a.parent
                for _ in range(6):
                    if parent is None:
                        break
                    blob = parent.get_text(" ", strip=True)
                    published = _parse_relative_or_abs(blob, now)
                    if published:
                        break
                    time_el = parent.find(
                        class_=re.compile(r"time|date|pub|ago|minute|hour", re.I)
                    )
                    if time_el:
                        published = _parse_relative_or_abs(time_el.get_text(strip=True), now)
                        if published:
                            break
                    parent = getattr(parent, "parent", None)

                articles.append(
                    RawArticle(
                        source=self.name,
                        source_article_id=art_id,
                        title_original=title,
                        url=clean,
                        published_at=published,
                        canonical_url=clean,
                        raw_metadata={"list_url": LIST_URL},
                    )
                )

            logger.info("[%s] Parsed %d candidate articles from list", self.name, len(articles))
        except Exception as e:
            logger.error("[%s] fetch_latest failed: %s", self.name, e, exc_info=True)
        return articles[:60]


def get_source() -> JiweiSource:
    return JiweiSource()
