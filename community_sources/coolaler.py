"""Coolaler / 滄者極限 — https://www.coolaler.com/ (XenForo)

Discovery: forum listing / latest threads.
URL: /forums/threads/{slug}.{id}/
Region: TW | zh-TW
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from community_sources.base import BaseCommunitySource, RawThread
from pipeline.community_score import extract_urls

logger = logging.getLogger(__name__)

BOARD_URLS = [
    "https://www.coolaler.com/forums/",
    "https://www.coolaler.com/",
    "https://www.coolaler.com/forums/forums/nvidia-xian-shi-ka.127/",
]
# Capture full path so we keep slug: /forums/threads/slug.406234/
TID_RE = re.compile(
    r"(/forums/threads/[^/?#]*?\.(\d+))/?",
    re.I,
)
TIME_RE = re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}:\d{2})?)")


class CoolalerSource(BaseCommunitySource):
    name = "coolaler"
    base_url = "https://www.coolaler.com"
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
                    path, tid = m.group(1), m.group(2)
                    if tid in seen:
                        continue
                    title = (a.get_text() or "").strip()
                    if len(title) < 6:
                        continue
                    if any(x in title for x in ("登入", "註冊", "首頁", "搜尋", "二手", "售")):
                        # marketplace noise is common on Coolaler home; skip obvious resale
                        if any(x in title for x in ("售", "二手", "板橋", "台中", "全省售", "雙北")):
                            continue
                    if any(x in title for x in ("登入", "註冊", "首頁", "搜尋")):
                        continue
                    seen.add(tid)

                    if href.startswith("http"):
                        clean = href.split("?")[0].split("#")[0].rstrip("/") + "/"
                    else:
                        clean = self.make_absolute(path.rstrip("/") + "/")

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
                                created = self.localize_naive(
                                    datetime.strptime(raw_t[:16], fmt)
                                )
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
                            url=clean,
                            canonical_url=clean,
                            region=self.region,
                            language_variant=self.language_variant,
                            created_at=created,
                            board="hardware",
                            raw_metadata={"board_url": board_url, "path": path},
                        )
                    )
            except Exception as e:
                logger.warning("[%s] parse %s: %s", self.name, board_url, e)
        logger.info("[%s] discovered %d threads", self.name, len(threads))
        return threads[:40]

    def fetch_thread(
        self, thread_id: str, url: Optional[str] = None
    ) -> Optional[RawThread]:
        """Fetch full thread using the discovered canonical URL (slug required).

        Do NOT invent URLs from bare thread IDs — XenForo needs the slug.
        """
        if not url:
            logger.warning(
                "[%s] fetch_thread(%s) called without URL — cannot invent XenForo slug",
                self.name,
                thread_id,
            )
            return None
        html = self.soft_fetch_html(url)
        if not html:
            return None
        return self._parse_thread_html(html, thread_id, url)

    def _parse_thread_html(
        self, html: str, thread_id: str, url: str
    ) -> Optional[RawThread]:
        soup = BeautifulSoup(html, "lxml")

        # Title
        title_el = (
            soup.select_one("h1.p-title-value")
            or soup.select_one("h1")
            or soup.select_one(".thread-title")
        )
        title = (title_el.get_text(" ", strip=True) if title_el else "").strip()
        if not title:
            title = f"coolaler-{thread_id}"

        # OP article body (XenForo: first article.message / .bbWrapper)
        articles = soup.select("article.message") or soup.select("div.message")
        op_el = None
        author = None
        author_id = None
        created = None
        if articles:
            op_el = articles[0]
            # author
            a_el = (
                op_el.select_one("a.username")
                or op_el.select_one(".message-userDetails a")
                or op_el.select_one(".message-name a")
            )
            if a_el:
                author = a_el.get_text(strip=True)
                href = a_el.get("href") or ""
                m = re.search(r"\.(\d+)/?$", href.rstrip("/"))
                if m:
                    author_id = m.group(1)
            # time
            t_el = op_el.select_one("time") or op_el.select_one(".message-date time")
            if t_el:
                dt_attr = t_el.get("datetime") or t_el.get("data-timestamp")
                if dt_attr and "T" in str(dt_attr):
                    try:
                        created = datetime.fromisoformat(
                            str(dt_attr).replace("Z", "+00:00")
                        )
                        if created.tzinfo is None:
                            created = self.localize_naive(created)
                        else:
                            from datetime import timezone

                            created = created.astimezone(
                                __import__("datetime").timezone.utc
                            )
                    except Exception:
                        pass
                elif t_el.get_text(strip=True):
                    tm = TIME_RE.search(t_el.get_text(strip=True))
                    if tm:
                        try:
                            raw_t = tm.group(1).replace("/", "-")
                            fmt = "%Y-%m-%d %H:%M" if " " in raw_t else "%Y-%m-%d"
                            created = self.localize_naive(
                                datetime.strptime(raw_t[:16], fmt)
                            )
                        except Exception:
                            pass

        body = None
        if op_el:
            body_el = (
                op_el.select_one(".bbWrapper")
                or op_el.select_one(".message-content")
                or op_el.select_one("article")
            )
            if body_el:
                body = body_el.get_text("\n", strip=True)[:5000]

        # Images / attachments in OP
        img_count = 0
        att_count = 0
        if op_el:
            imgs = op_el.select("img")
            # filter avatars/smilies
            for img in imgs:
                src = (img.get("src") or "") + (img.get("data-src") or "")
                if any(x in src for x in ("avatar", "smilie", "emoji", "sprite")):
                    continue
                img_count += 1
            att_count = len(op_el.select("a.attachment, .message-attachments a, .js-attachment"))

        # External URLs from OP text
        urls = extract_urls(body or "")

        # Reply count from page meta if present
        reply_count = max(0, len(articles) - 1) if articles else 0

        posts = [
            {
                "post_id": f"{thread_id}-op",
                "author_name": author,
                "author_id": author_id,
                "text_original": body,
                "is_op": True,
                "image_count": img_count,
                "attachment_count": att_count,
                "external_urls": urls,
            }
        ]

        # Selective non-OP replies (up to max) with evidence signals
        max_replies = int(
            __import__("config").yaml_config.get("community", {}).get(
                "max_selected_replies", 20
            )
        )
        selected = 0
        for art in (articles or [])[1:]:
            if selected >= max_replies:
                break
            body_el = art.select_one(".bbWrapper") or art.select_one(".message-content")
            text = body_el.get_text("\n", strip=True)[:2000] if body_el else ""
            imgs = [
                i
                for i in art.select("img")
                if not any(
                    x in ((i.get("src") or "") + (i.get("data-src") or ""))
                    for x in ("avatar", "smilie", "emoji")
                )
            ]
            art_urls = extract_urls(text)
            has_evidence = bool(imgs) or bool(art_urls) or any(
                k in text
                for k in ("跑分", "截圖", "截图", "實拍", "实拍", "RTX", "Ryzen", "規格", "规格")
            )
            if not has_evidence and selected > 5:
                continue
            a_el = art.select_one("a.username")
            posts.append(
                {
                    "post_id": f"{thread_id}-r{selected+1}",
                    "author_name": a_el.get_text(strip=True) if a_el else None,
                    "text_original": text,
                    "is_op": False,
                    "is_selected_reply": True,
                    "image_count": len(imgs),
                    "external_urls": art_urls,
                }
            )
            selected += 1

        return RawThread(
            platform=self.name,
            thread_id=thread_id,
            title_original=title,
            url=url,
            canonical_url=url,
            region=self.region,
            language_variant=self.language_variant,
            author_name=author,
            author_id=author_id,
            created_at=created,
            board="hardware",
            op_text_original=body,
            reply_count=reply_count,
            image_count=img_count,
            attachment_count=att_count,
            external_urls=urls,
            posts=posts,
        )
