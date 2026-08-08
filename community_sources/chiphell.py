"""Chiphell — https://www.chiphell.com/ (Discuz)

Discovery: board listing HTML for hardware forums.
URL: forum.php?mod=viewthread&tid=ID  or thread-ID-1-1.html
Region: CN | zh-CN
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from community_sources.base import BaseCommunitySource, RawThread

logger = logging.getLogger(__name__)

# Hardware-focused board listing pages (Discuz fids commonly used)
BOARD_URLS = [
    "https://www.chiphell.com/forum.php?mod=forumdisplay&fid=99",   # 电脑讨论 often
    "https://www.chiphell.com/forum-99-1.html",
    "https://www.chiphell.com/forum.php?mod=guide&view=newthread",
]
TID_RE = re.compile(
    r"(?:tid=(\d+)|thread-(\d+)-)",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)")


class ChiphellSource(BaseCommunitySource):
    name = "chiphell"
    base_url = "https://www.chiphell.com"
    region = "CN"
    language_variant = "zh-CN"

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
                    tid = m.group(1) or m.group(2)
                    if not tid or tid in seen:
                        continue
                    title = (a.get_text() or "").strip()
                    if len(title) < 6:
                        continue
                    # skip nav
                    if any(x in title for x in ("登录", "注册", "管理", "首页", "发帖")):
                        continue
                    seen.add(tid)
                    if href.startswith("/"):
                        href = self.make_absolute(href)
                    elif not href.startswith("http"):
                        href = self.make_absolute("/" + href)

                    created = None
                    parent = a.parent
                    for _ in range(6):
                        if parent is None:
                            break
                        blob = parent.get_text(" ", strip=True)
                        tm = TIME_RE.search(blob)
                        if tm:
                            created = self.localize_naive(
                                datetime.strptime(
                                    tm.group(1).replace("/", "-")[:16],
                                    "%Y-%m-%d %H:%M" if " " in tm.group(1) else "%Y-%m-%d",
                                )
                            ) if " " in tm.group(1) else None
                            if created:
                                break
                        parent = getattr(parent, "parent", None)

                    # reply/view heuristics from nearby text
                    reply_count = 0
                    view_count = 0
                    parent = a.parent
                    for _ in range(4):
                        if parent is None:
                            break
                        nums = re.findall(r"\b(\d+)\b", parent.get_text(" ", strip=True))
                        if len(nums) >= 2:
                            try:
                                reply_count = int(nums[-2])
                                view_count = int(nums[-1])
                            except Exception:
                                pass
                        parent = getattr(parent, "parent", None)

                    threads.append(
                        RawThread(
                            platform=self.name,
                            thread_id=tid,
                            title_original=title,
                            url=href.split("#")[0],
                            canonical_url=f"https://www.chiphell.com/forum.php?mod=viewthread&tid={tid}",
                            region=self.region,
                            language_variant=self.language_variant,
                            created_at=created,
                            board="hardware",
                            reply_count=reply_count,
                            view_count=view_count,
                            raw_metadata={"board_url": board_url},
                        )
                    )
            except Exception as e:
                logger.warning("[%s] parse error on %s: %s", self.name, board_url, e)
        logger.info("[%s] discovered %d threads", self.name, len(threads))
        return threads[:40]

    def fetch_thread(self, thread_id: str, url: Optional[str] = None) -> Optional[RawThread]:
        url = url or f"https://www.chiphell.com/forum.php?mod=viewthread&tid={thread_id}"
        html = self.soft_fetch_html(url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        title_el = soup.find("span", id="thread_subject") or soup.find("h1")
        title = (title_el.get_text(strip=True) if title_el else "").strip() or f"thread-{thread_id}"
        # OP post
        op_div = soup.select_one("div.pcb") or soup.select_one("td.t_f") or soup.select_one("div#postlist div")
        op_text = op_div.get_text("\n", strip=True)[:3000] if op_div else None
        images = len(soup.select("div.pcb img, td.t_f img")) if soup else 0
        author_el = soup.select_one("div.authi a") or soup.select_one("a.xw1")
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
