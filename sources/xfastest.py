"""XFastest (Taiwan) — https://news.xfastest.com/

Discovery: news listing pages (multiple fallbacks).
URL pattern: https://news.xfastest.com/{category}/{numeric_id}/{slug}/
Region: TW | language_variant: zh-TW | timezone: Asia/Taipei (UTC+8)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

# Try several entry points — news subdomain can be flaky / geo-sensitive
LIST_URLS = [
    "https://news.xfastest.com/",
    "https://news.xfastest.com/category/industry/",
    "https://www.xfastest.com/",
]

# /intel/163803/intel-18a-high-na-euv/  or absolute
ARTICLE_ID_RE = re.compile(
    r"(?:https?://)?(?:news\.)?xfastest\.com/[^/]+/(\d{4,})/([^/?#]+)",
    re.IGNORECASE,
)
# forum-style threads sometimes appear on www
THREAD_RE = re.compile(
    r"(?:https?://)?(?:www\.)?xfastest\.com/thread-(\d+)-",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)")


class XFastestSource(BaseSource):
    name = "xfastest"
    base_url = "https://news.xfastest.com"
    region = "TW"
    language_variant = "zh-TW"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        seen: set[str] = set()
        meta = {
            "region": self.region,
            "language_variant": self.language_variant,
            "timezone": "Asia/Taipei",
        }

        for list_url in LIST_URLS:
            html = self.soft_fetch_html(list_url)
            if not html:
                continue
            try:
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a.get("href") or ""
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        # resolve against the list_url host
                        if "www.xfastest.com" in list_url:
                            href = "https://www.xfastest.com" + href
                        else:
                            href = self.make_absolute(href)

                    art_id: Optional[str] = None
                    m = ARTICLE_ID_RE.search(href)
                    if m:
                        art_id = m.group(1)
                    else:
                        tm = THREAD_RE.search(href)
                        if tm:
                            art_id = "thread-" + tm.group(1)
                    if not art_id or art_id in seen:
                        continue

                    title = (a.get_text() or "").strip()
                    if len(title) < 8:
                        continue
                    if any(
                        x in title
                        for x in ("登入", "註冊", "更多", "首頁", "論壇", "會員", "搜尋")
                    ):
                        continue

                    seen.add(art_id)
                    clean = href.split("?")[0].split("#")[0].rstrip("/")
                    if clean.startswith("http://"):
                        clean = "https://" + clean[7:]

                    published: Optional[datetime] = None
                    parent = a.parent
                    for _ in range(5):
                        if parent is None:
                            break
                        blob = parent.get_text(" ", strip=True)
                        tmatch = TIME_RE.search(blob)
                        if tmatch:
                            published = self.parse_datetime(
                                tmatch.group(1).replace("/", "-")
                            )
                            if published:
                                break
                        parent = getattr(parent, "parent", None)

                    articles.append(
                        RawArticle(
                            source=self.name,
                            source_article_id=art_id,
                            title_original=title,  # Traditional Chinese preserved
                            url=clean,
                            published_at=published,
                            canonical_url=clean,
                            raw_metadata={**meta, "list_url": list_url},
                        )
                    )
            except Exception as e:
                logger.warning("[%s] parse error on %s: %s", self.name, list_url, e)

        logger.info("[%s] Parsed %d candidate articles", self.name, len(articles))
        return articles[:50]
