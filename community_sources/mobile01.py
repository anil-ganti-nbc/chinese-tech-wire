"""Mobile01 — https://www.mobile01.com/

Discovery: hardware board listings.
URL: topicdetail.php?f=FID&t=TID
Region: TW | zh-TW
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from community_sources.base import BaseCommunitySource, RawThread

logger = logging.getLogger(__name__)

# Hardware-related board IDs (PC DIY, graphics, etc.)
BOARD_URLS = [
    "https://www.mobile01.com/forumtopic.php?c=17",   # 電腦 DIY
    "https://www.mobile01.com/topiclist.php?f=343",  # often GPU-related
    "https://www.mobile01.com/forumtopic.php?c=16",
]
TID_RE = re.compile(r"topicdetail\.php\?[^\"']*t=(\d+)", re.I)
TIME_RE = re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)")


class Mobile01Source(BaseCommunitySource):
    name = "mobile01"
    base_url = "https://www.mobile01.com"
    region = "TW"
    language_variant = "zh-TW"

    def fetch_recent_threads(self) -> List[RawThread]:
        threads: List[RawThread] = []
        seen: set[str] = set()
        for board_url in BOARD_URLS:
            html = self.soft_fetch_html(board_url)
            if not html:
                continue
            try:
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    m = TID_RE.search(href)
                    if not m:
                        continue
                    tid = m.group(1)
                    if tid in seen:
                        continue
                    title = (a.get_text() or "").strip()
                    if len(title) < 6:
                        continue
                    if any(x in title for x in ("登入", "註冊", "首頁", "發表")):
                        continue
                    seen.add(tid)
                    if not href.startswith("http"):
                        href = self.make_absolute(href if href.startswith("/") else "/" + href)

                    created = None
                    parent = a.parent
                    for _ in range(5):
                        if parent is None:
                            break
                        tm = TIME_RE.search(parent.get_text(" ", strip=True))
                        if tm:
                            try:
                                raw_t = tm.group(1).replace("/", "-")
                                fmt = "%Y-%m-%d %H:%M" if " " in raw_t else "%Y-%m-%d"
                                created = self.localize_naive(datetime.strptime(raw_t[:16], fmt))
                            except Exception:
                                pass
                            if created:
                                break
                        parent = getattr(parent, "parent", None)

                    threads.append(
                        RawThread(
                            platform=self.name,
                            thread_id=tid,
                            title_original=title,
                            url=href.split("#")[0],
                            canonical_url=f"https://www.mobile01.com/topicdetail.php?t={tid}",
                            region=self.region,
                            language_variant=self.language_variant,
                            created_at=created,
                            board="pc-diy",
                            raw_metadata={"board_url": board_url},
                        )
                    )
            except Exception as e:
                logger.warning("[%s] parse %s: %s", self.name, board_url, e)
        logger.info("[%s] discovered %d threads", self.name, len(threads))
        return threads[:40]

    def fetch_thread(self, thread_id: str, url: Optional[str] = None) -> Optional[RawThread]:
        url = url or f"https://www.mobile01.com/topicdetail.php?t={thread_id}"
        html = self.soft_fetch_html(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        title_el = soup.find("h1") or soup.select_one(".topic")
        title = (title_el.get_text(strip=True) if title_el else f"thread-{thread_id}")
        op = soup.select_one(".single-post-content") or soup.select_one(".article")
        op_text = op.get_text("\n", strip=True)[:3000] if op else None
        images = len(soup.select(".single-post-content img, .article img"))
        author_el = soup.select_one(".c-authorInfo__id") or soup.select_one("a.fn")
        author = author_el.get_text(strip=True) if author_el else None
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
            posts=[{
                "post_id": f"{thread_id}-op",
                "author_name": author,
                "text_original": op_text,
                "is_op": True,
                "image_count": images,
            }],
        )
