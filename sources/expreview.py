"""Expreview (超能网) source adapter.

Primary discovery: RSS feed at https://www.expreview.com/rss.php
Confirmed fields: title, link, description (HTML), category, author, pubDate (RFC 2822 +08).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from sources.base import BaseSource, RawArticle

logger = logging.getLogger(__name__)

RSS_URL = "https://www.expreview.com/rss.php"
ARTICLE_ID_RE = re.compile(r"/(\d+)\.html?(?:$|\?)")


def _strip_html(html: str) -> str:
    """Extract plain text summary from description CDATA/HTML."""
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "lxml")
        # remove scripts/styles if any
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # collapse whitespace
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html).strip()


def _parse_pubdate(text: str) -> Optional[datetime]:
    """Parse RSS pubDate (RFC 2822) into timezone-aware UTC datetime.

    Expreview uses short offsets like '+08' instead of the canonical '+0800'.
    Normalize those before handing to email.utils.
    """
    if not text:
        return None
    text = text.strip()
    # Fix short timezone offsets: +08 → +0800, -5 → -0500, etc.
    text = re.sub(r"([+-])(\d{1,2})$", lambda m: f"{m.group(1)}{int(m.group(2)):02d}00", text)
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


class ExpreviewSource(BaseSource):
    name = "expreview"
    region = "CN"
    language_variant = "zh-CN"
    base_url = "https://www.expreview.com"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        try:
            # Prefer bytes then decode as UTF-8 (RSS declares UTF-8)
            raw = self.fetch_bytes(RSS_URL)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")

            root = ET.fromstring(text)
            # RSS 2.0: channel/item
            items = root.findall("./channel/item")
            if not items:
                # fallback for namespaced or slight variations
                items = root.findall(".//item")

            seen_ids: set[str] = set()
            for item in items:
                try:
                    title_el = item.find("title")
                    link_el = item.find("link")
                    if title_el is None or link_el is None:
                        continue
                    title = (title_el.text or "").strip()
                    link = (link_el.text or "").strip()
                    if not title or not link:
                        continue

                    # Normalize http → https and strip query
                    if link.startswith("http://"):
                        link = "https://" + link[7:]
                    link = link.split("?")[0].rstrip("/")

                    m = ARTICLE_ID_RE.search(link)
                    if not m:
                        # still accept but use full path as id fallback
                        art_id = link.rsplit("/", 1)[-1]
                    else:
                        art_id = m.group(1)

                    if art_id in seen_ids:
                        continue
                    seen_ids.add(art_id)

                    desc_el = item.find("description")
                    summary = _strip_html(desc_el.text if desc_el is not None else "")[:800] or None

                    cat_el = item.find("category")
                    category = (cat_el.text or "").strip() or None

                    author_el = item.find("author")
                    author = (author_el.text or "").strip() or None

                    pub_el = item.find("pubDate")
                    published = _parse_pubdate(pub_el.text if pub_el is not None else "")

                    articles.append(
                        RawArticle(
                            source=self.name,
                            source_article_id=art_id,
                            title_original=title,
                            url=link,
                            published_at=published,
                            summary_original=summary,
                            category=category,
                            canonical_url=link,
                            raw_metadata={
                                "author": author,
                                "rss_url": RSS_URL,
                            },
                        )
                    )
                except Exception as e:
                    logger.debug("[%s] skipped one item: %s", self.name, e)
                    continue

            logger.info("[%s] Parsed %d articles from RSS", self.name, len(articles))
        except Exception as e:
            logger.error("[%s] fetch_latest failed: %s", self.name, e, exc_info=True)
            # isolate — never raise to caller
        return articles[:80]  # safety cap


def get_source() -> ExpreviewSource:
    return ExpreviewSource()
