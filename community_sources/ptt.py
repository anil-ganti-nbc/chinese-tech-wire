"""PTT PC_Shopping — https://www.ptt.cc/bbs/PC_Shopping/

Discovery: board index HTML. May require over18 cookie.
URL: /bbs/PC_Shopping/M.TIMESTAMP.A.XXX.html
Region: TW | zh-TW
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from community_sources.base import BaseCommunitySource, RawThread

logger = logging.getLogger(__name__)

BOARD_URL = "https://www.ptt.cc/bbs/PC_Shopping/index.html"
ARTICLE_RE = re.compile(
    r"/bbs/PC_Shopping/(M\.\d+\.A\.[A-Za-z0-9]+)\.html",
    re.I,
)


class PTTSource(BaseCommunitySource):
    name = "ptt"
    base_url = "https://www.ptt.cc"
    region = "TW"
    language_variant = "zh-TW"

    def __init__(self):
        super().__init__()
        # PTT age gate
        self._client = None  # force recreate with cookie

    @property
    def client(self):
        if self._client is None or self._client.is_closed:
            import httpx
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
                cookies={"over18": "1"},
            )
        return self._client

    def fetch_recent_threads(self) -> List[RawThread]:
        threads: List[RawThread] = []
        html = self.soft_fetch_html(BOARD_URL)
        if not html:
            logger.warning("[%s] board unavailable (age-gate or block)", self.name)
            return threads
        try:
            soup = BeautifulSoup(html, "lxml")
            seen: set[str] = set()
            for div in soup.select("div.r-ent"):
                a = div.select_one("div.title a")
                if not a or not a.get("href"):
                    continue
                href = a["href"]
                m = ARTICLE_RE.search(href)
                if not m:
                    continue
                tid = m.group(1)
                if tid in seen:
                    continue
                title = (a.get_text() or "").strip()
                if not title or title.startswith("(本文已被刪除)"):
                    continue
                seen.add(tid)
                url = self.make_absolute(href)

                author_el = div.select_one("div.author")
                author = author_el.get_text(strip=True) if author_el else None
                date_el = div.select_one("div.date")
                created = None
                if date_el:
                    # PTT shows " 7/24" style — year assumed current
                    raw_d = date_el.get_text(strip=True)
                    try:
                        # MM/DD
                        parts = raw_d.replace(" ", "").split("/")
                        if len(parts) == 2:
                            mo, d = int(parts[0]), int(parts[1])
                            y = datetime.now(self.source_zone()).year
                            created = self.localize_naive(datetime(y, mo, d, 12, 0))
                    except Exception:
                        pass

                nrec = div.select_one("div.nrec")
                reply_count = 0
                if nrec:
                    t = nrec.get_text(strip=True)
                    if t.isdigit():
                        reply_count = int(t)
                    elif t == "爆":
                        reply_count = 100

                threads.append(
                    RawThread(
                        platform=self.name,
                        thread_id=tid,
                        title_original=title,
                        url=url,
                        canonical_url=url,
                        region=self.region,
                        language_variant=self.language_variant,
                        author_name=author,
                        created_at=created,
                        board="PC_Shopping",
                        reply_count=reply_count,
                    )
                )
        except Exception as e:
            logger.warning("[%s] parse error: %s", self.name, e)
        logger.info("[%s] discovered %d threads", self.name, len(threads))
        return threads[:40]

    def fetch_thread(self, thread_id: str, url: Optional[str] = None) -> Optional[RawThread]:
        url = url or f"https://www.ptt.cc/bbs/PC_Shopping/{thread_id}.html"
        html = self.soft_fetch_html(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        # meta spans
        metas = {m.get_text(strip=True): None for m in soup.select("span.article-meta-tag")}
        for tag in soup.select("div.article-metaline, div.article-metaline-right"):
            t = tag.select_one("span.article-meta-tag")
            v = tag.select_one("span.article-meta-value")
            if t and v:
                metas[t.get_text(strip=True)] = v.get_text(strip=True)
        title = metas.get("標題") or metas.get("标题") or thread_id
        author = metas.get("作者")
        main = soup.select_one("#main-content")
        op_text = None
        if main:
            # clone text without pushes
            op_text = main.get_text("\n", strip=True)[:3000]
        images = len(soup.select("#main-content img"))
        return RawThread(
            platform=self.name,
            thread_id=thread_id,
            title_original=title,
            url=url,
            canonical_url=url,
            region=self.region,
            language_variant=self.language_variant,
            author_name=author,
            op_text_original=op_text,
            image_count=images,
            board="PC_Shopping",
            posts=[{
                "post_id": f"{thread_id}-op",
                "author_name": author,
                "text_original": op_text,
                "is_op": True,
                "image_count": images,
            }],
        )
