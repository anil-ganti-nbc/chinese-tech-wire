"""HKEPC (Hong Kong) — https://www.hkepc.com/

Discovery: news listing / homepage article links.
URL pattern: https://www.hkepc.com/{numeric_id}/{slug}
Region: HK | language_variant: zh-HK | timezone: Asia/Hong_Kong (UTC+8)

Note: Some automated crawlers receive HTTP 402 from this host; live user runs
from HK/TW/CN typically succeed. Adapter isolates failures cleanly.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

LIST_URLS = [
    "https://www.hkepc.com/",
    "https://www.hkepc.com/news",
]
# /25993/Some_Slug
ARTICLE_ID_RE = re.compile(
    r"(?:https?://)?(?:www\.)?hkepc\.com/(\d{3,})/([^/?#]+)",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)")


class HKEPCSource(BaseSource):
    name = "hkepc"
    base_url = "https://www.hkepc.com"
    region = "HK"
    language_variant = "zh-HK"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        seen: set[str] = set()
        meta = {
            "region": self.region,
            "language_variant": self.language_variant,
            "timezone": "Asia/Hong_Kong",
        }
        for list_url in LIST_URLS:
            try:
                html = self.soft_fetch_html(list_url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a.get("href") or ""
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        href = self.make_absolute(href)
                    m = ARTICLE_ID_RE.search(href)
                    if not m:
                        continue
                    art_id = m.group(1)
                    if art_id in seen:
                        continue
                    title = (a.get_text() or "").strip()
                    if len(title) < 8:
                        continue
                    # skip nav
                    if any(x in title for x in ("登入", "註冊", "論壇", "更多", "首頁")):
                        continue
                    seen.add(art_id)
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
                            published = self.parse_datetime(tm.group(1).replace("/", "-"))
                            if published:
                                break
                        parent = getattr(parent, "parent", None)

                    articles.append(
                        RawArticle(
                            source=self.name,
                            source_article_id=art_id,
                            title_original=title,  # zh-HK preserved
                            url=clean,
                            published_at=published,
                            canonical_url=clean,
                            raw_metadata={**meta, "list_url": list_url},
                        )
                    )
                logger.info("[%s] Parsed candidates from %s → total %d", self.name, list_url, len(articles))
            except Exception as e:
                logger.warning("[%s] parse/fetch %s: %s", self.name, list_url, e)
                # continue other list URLs
        return articles[:50]
