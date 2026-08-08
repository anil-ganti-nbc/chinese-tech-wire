"""ZOL (中关村在线) source adapter.

Discovery: parse latest list on https://news.zol.com.cn/
URL pattern examples:
  https://news.zol.com.cn/1220/12205261.html
  https://diy.zol.com.cn/1220/12204512.html
  https://mobile.zol.com.cn/1220/12203902.html
source_article_id = the numeric id before .html
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

LIST_URL = "https://news.zol.com.cn/"
# Matches any *.zol.com.cn/{yymm}/{id}.html (or .shtml)
ARTICLE_ID_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)?zol\.com\.cn/\d+/(\d+)\.(?:html?|shtml)",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2})|(20\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})"
)


class ZOLSource(BaseSource):
    name = "zol"
    region = "CN"
    language_variant = "zh-CN"
    base_url = "https://www.zol.com.cn"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        try:
            html = self.fetch_html(LIST_URL)
            soup = BeautifulSoup(html, "lxml")
            seen_ids: set[str] = set()

            for a in soup.find_all("a", href=True):
                href = a.get("href") or ""
                if not href:
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = "https://news.zol.com.cn" + href

                m = ARTICLE_ID_RE.search(href)
                if not m:
                    continue
                art_id = m.group(1)
                if art_id in seen_ids:
                    continue

                title = (a.get_text() or "").strip()
                if not title or len(title) < 8:
                    continue
                if any(x in title for x in ("更多", "登录", "注册", "首页", "排行榜")):
                    continue

                seen_ids.add(art_id)

                clean = href.split("?")[0].split("#")[0]
                if clean.startswith("http://"):
                    clean = "https://" + clean[7:]

                published: Optional[datetime] = None
                parent = a.parent
                for _ in range(5):
                    if parent is None:
                        break
                    blob = parent.get_text(" ", strip=True)
                    tm = TIME_RE.search(blob)
                    if tm:
                        raw_t = next(g for g in tm.groups() if g)
                        published = self.parse_datetime(raw_t)
                        if published:
                            break
                    time_el = parent.find(class_=re.compile(r"time|date|pub", re.I))
                    if time_el:
                        published = self.parse_datetime(time_el.get_text(strip=True))
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


def get_source() -> ZOLSource:
    return ZOLSource()
