"""TechNews 科技新報 (Taiwan) — https://technews.tw/

Discovery: WordPress RSS at https://technews.tw/feed/
URL pattern: https://technews.tw/YYYY/MM/DD/slug/  (also finance.technews.tw etc.)
Region: TW | language_variant: zh-TW | timezone: Asia/Taipei (UTC+8)
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

RSS_URL = "https://technews.tw/feed/"
# Prefer numeric guid id from ?p=NNNN, else slug from path
GUID_RE = re.compile(r"[?&]p=(\d+)")
PATH_ID_RE = re.compile(r"technews\.tw/\d{4}/\d{2}/\d{2}/([^/?#]+)", re.I)


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


class TechNewsSource(BaseSource):
    name = "technews"
    base_url = "https://technews.tw"
    region = "TW"
    language_variant = "zh-TW"

    def fetch_latest(self) -> List[RawArticle]:
        articles: List[RawArticle] = []
        meta = {
            "region": self.region,
            "language_variant": self.language_variant,
            "timezone": "Asia/Taipei",
            "discovery": "rss",
        }
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
                    link = (link_el.text or "").strip().split("?")[0].rstrip("/") + "/"
                    # normalize
                    if link.startswith("http://"):
                        link = "https://" + link[7:]
                    if not title or not link:
                        continue

                    # ID: guid ?p= first, else path slug
                    guid_el = item.find("guid")
                    art_id = None
                    if guid_el is not None and guid_el.text:
                        gm = GUID_RE.search(guid_el.text)
                        if gm:
                            art_id = gm.group(1)
                    if not art_id:
                        pm = PATH_ID_RE.search(link)
                        art_id = pm.group(1) if pm else link.rstrip("/").rsplit("/", 1)[-1]
                    if art_id in seen:
                        continue
                    seen.add(art_id)

                    pub_el = item.find("pubDate")
                    published = _parse_rfc2822(pub_el.text if pub_el is not None else "")

                    cat_el = item.find("category")
                    category = (cat_el.text or "").strip() or None

                    desc_el = item.find("description")
                    summary = None
                    if desc_el is not None and (desc_el.text or ""):
                        summary = BeautifulSoup(desc_el.text, "lxml").get_text(" ", strip=True)[:500]

                    articles.append(
                        RawArticle(
                            source=self.name,
                            source_article_id=str(art_id),
                            title_original=title,  # Traditional Chinese preserved
                            url=link.rstrip("/"),
                            published_at=published,
                            summary_original=summary,
                            category=category,
                            canonical_url=link.rstrip("/"),
                            raw_metadata=meta,
                        )
                    )
                except Exception as e:
                    logger.debug("[%s] skip item: %s", self.name, e)
            logger.info("[%s] Parsed %d articles from RSS", self.name, len(articles))
        except Exception as e:
            logger.error("[%s] RSS failed: %s", self.name, e, exc_info=True)
        return articles[:50]
