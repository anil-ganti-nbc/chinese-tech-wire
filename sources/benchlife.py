"""BenchLife (Taiwan) — https://benchlife.info/

Discovery: WordPress RSS at /feed/ (preferred), HTML listing fallback.
Region: TW | language_variant: zh-TW | timezone: Asia/Taipei (UTC+8)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from xml.etree import ElementTree as ET
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

RSS_URL = "https://benchlife.info/feed/"
LIST_URL = "https://benchlife.info/"
# slug-based IDs from path
SLUG_RE = re.compile(r"benchlife\.info/([^/?#]+)/?", re.I)


def _parse_rfc2822(text: str) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"([+-])(\d{1,2})$", lambda m: f"{m.group(1)}{int(m.group(2)):02d}00", text)
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class BenchLifeSource(BaseSource):
    name = "benchlife"
    base_url = "https://benchlife.info"
    region = "TW"
    language_variant = "zh-TW"

    def fetch_latest(self) -> List[RawArticle]:
        articles = self._from_rss()
        if not articles:
            articles = self._from_html()
        return articles[:50]

    def _meta(self) -> dict:
        return {
            "region": self.region,
            "language_variant": self.language_variant,
            "timezone": "Asia/Taipei",
        }

    def _from_rss(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        try:
            raw = self.fetch_bytes(RSS_URL)
            text = raw.decode("utf-8", errors="replace")
            root = ET.fromstring(text)
            items = root.findall("./channel/item") or root.findall(".//item")
            seen: set[str] = set()
            for item in items:
                try:
                    title_el = item.find("title")
                    link_el = item.find("link")
                    if title_el is None or link_el is None:
                        continue
                    title = (title_el.text or "").strip()
                    link = (link_el.text or "").strip().split("?")[0].rstrip("/")
                    if not title or not link:
                        continue
                    if link.startswith("http://"):
                        link = "https://" + link[7:]
                    m = SLUG_RE.search(link)
                    art_id = m.group(1) if m else link.rsplit("/", 1)[-1]
                    if art_id in seen:
                        continue
                    seen.add(art_id)

                    pub_el = item.find("pubDate")
                    published = _parse_rfc2822(pub_el.text if pub_el is not None else "")

                    cat_el = item.find("category")
                    category = (cat_el.text or "").strip() or None

                    desc_el = item.find("description")
                    summary = None
                    if desc_el is not None and desc_el.text:
                        summary = BeautifulSoup(desc_el.text, "lxml").get_text(" ", strip=True)[:500]

                    articles.append(
                        RawArticle(
                            source=self.name,
                            source_article_id=art_id,
                            title_original=title,  # Traditional Chinese preserved
                            url=link,
                            published_at=published,
                            summary_original=summary,
                            category=category,
                            canonical_url=link,
                            raw_metadata={**self._meta(), "discovery": "rss"},
                        )
                    )
                except Exception as e:
                    logger.debug("[%s] skip item: %s", self.name, e)
            logger.info("[%s] Parsed %d articles from RSS", self.name, len(articles))
        except Exception as e:
            logger.warning("[%s] RSS failed: %s — trying HTML", self.name, e)
        return articles

    def _from_html(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        try:
            html = self.soft_fetch_html(LIST_URL)
            if not html:
                return articles
            soup = BeautifulSoup(html, "lxml")
            seen: set[str] = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/"):
                    href = self.make_absolute(href)
                m = SLUG_RE.search(href)
                if not m:
                    continue
                art_id = m.group(1)
                if art_id in seen or art_id in ("category", "author", "tag", "page"):
                    continue
                title = (a.get_text() or "").strip()
                if len(title) < 8:
                    continue
                seen.add(art_id)
                clean = href.split("?")[0].rstrip("/")
                articles.append(
                    RawArticle(
                        source=self.name,
                        source_article_id=art_id,
                        title_original=title,
                        url=clean,
                        canonical_url=clean,
                        raw_metadata={**self._meta(), "discovery": "html"},
                    )
                )
            logger.info("[%s] Parsed %d articles from HTML", self.name, len(articles))
        except Exception as e:
            logger.error("[%s] HTML fetch failed: %s", self.name, e)
        return articles
