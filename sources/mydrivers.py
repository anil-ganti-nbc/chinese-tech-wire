"""MyDrivers (快科技) source adapter.

Discovery: parse the latest listing on https://news.mydrivers.com/
URL pattern: https://news.mydrivers.com/1/{channel}/{id}.htm
(Alternative RSS exists at https://rss.mydrivers.com/Rss.aspx?Tid=1 but listing is more reliable for V0.1.)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

LIST_URL = "https://news.mydrivers.com/"
# Matches both absolute and protocol-relative / relative forms
ARTICLE_ID_RE = re.compile(
    r"(?:https?://(?:news\.)?mydrivers\.com)?/1/\d+/(\d+)\.htm",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2})")


class MyDriversSource(BaseSource):
    name = "mydrivers"
    region = "CN"
    language_variant = "zh-CN"
    base_url = "https://news.mydrivers.com"

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
                # Normalize
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
                # Skip empty, too-short, or pure navigation
                if not title or len(title) < 8:
                    continue
                # Filter obvious non-news (ads, login, etc.)
                if any(x in title.lower() for x in ("登录", "注册", "下载客户端", "广告")):
                    continue

                seen_ids.add(art_id)

                # Canonical clean URL
                clean_url = f"https://news.mydrivers.com/1/1138/{art_id}.htm"
                # Prefer the original matched URL if it already looks good
                if "mydrivers.com/1/" in href:
                    clean_url = href.split("?")[0].rstrip("/")

                # Nearby timestamp
                published: Optional[datetime] = None
                parent = a.parent
                for _ in range(5):
                    if parent is None:
                        break
                    # Search text nodes or spans for time pattern
                    text_blob = parent.get_text(" ", strip=True)
                    tm = TIME_RE.search(text_blob)
                    if tm:
                        published = self.parse_datetime(tm.group(1))
                        break
                    # Also check class-based time elements
                    time_el = parent.find(class_=re.compile(r"time|date|pub", re.I))
                    if time_el:
                        published = self.parse_datetime(time_el.get_text(strip=True))
                        if published:
                            break
                    parent = getattr(parent, "parent", None)

                # Optional short summary from sibling text (best-effort)
                summary = None
                if a.parent:
                    sib_text = a.parent.get_text(" ", strip=True)
                    # Remove the title itself and the time
                    if title in sib_text:
                        rest = sib_text.replace(title, "", 1)
                        rest = TIME_RE.sub("", rest).strip(" -–—|")
                        if 20 < len(rest) < 300:
                            summary = rest[:250]

                articles.append(
                    RawArticle(
                        source=self.name,
                        source_article_id=art_id,
                        title_original=title,
                        url=clean_url,
                        published_at=published,
                        summary_original=summary,
                        canonical_url=clean_url,
                        raw_metadata={"list_url": LIST_URL},
                    )
                )

            logger.info("[%s] Parsed %d candidate articles from list", self.name, len(articles))
        except Exception as e:
            logger.error("[%s] fetch_latest failed: %s", self.name, e, exc_info=True)
        return articles[:60]


def get_source() -> MyDriversSource:
    return MyDriversSource()
