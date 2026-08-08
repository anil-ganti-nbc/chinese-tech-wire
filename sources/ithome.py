"""ITHome (IT之家) source adapter.

Discovery: parse https://www.ithome.com/list/ for recent articles.
URL pattern: https://www.ithome.com/0/{section}/{id}.htm
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

LIST_URL = "https://www.ithome.com/list/"
ARTICLE_ID_RE = re.compile(r"/0/\d+/(\d+)\.htm")


class ITHomeSource(BaseSource):
    name = "ithome"
    region = "CN"
    language_variant = "zh-CN"
    base_url = "https://www.ithome.com"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        try:
            html = self.fetch_html(LIST_URL)
            soup = BeautifulSoup(html, "lxml")
            # Collect candidate links matching ITHome article pattern
            seen_ids: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not href:
                    continue
                # Normalize protocol-relative
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
                if not title or len(title) < 5:
                    continue
                # Skip non-news (lapin deals etc)
                if "lapin.ithome.com" in href:
                    continue
                seen_ids.add(art_id)

                # Try to find nearby time
                published = None
                parent = a.parent
                for _ in range(4):
                    if parent is None:
                        break
                    time_el = parent.find(string=re.compile(r"\d{4}-\d{2}-\d{2}"))
                    if time_el:
                        published = self.parse_datetime(str(time_el).strip(" *"))
                        break
                    # also look for *time*
                    time_el = parent.find(class_=re.compile(r"time|date", re.I))
                    if time_el:
                        published = self.parse_datetime(time_el.get_text(strip=True))
                        break
                    parent = parent.parent

                articles.append(
                    RawArticle(
                        source=self.name,
                        source_article_id=art_id,
                        title_original=title,
                        url=href.split("?")[0],
                        published_at=published,
                        raw_metadata={"list_url": LIST_URL},
                    )
                )
            logger.info("[%s] Parsed %d candidate articles from list", self.name, len(articles))
        except Exception as e:
            logger.error("[%s] fetch_latest failed: %s", self.name, e, exc_info=True)
            # Do not raise — isolate failure
        return articles[:50]  # safety cap for V0.1


# Convenience for CLI / tests
def get_source() -> ITHomeSource:
    return ITHomeSource()
