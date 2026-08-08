"""JD.com retail watchlist monitor.

Public search pages are aggressively anti-bot. This adapter:
1. Attempts public search HTML for configured watch terms
2. Soft-fails cleanly when blocked (HTTP 403/302-to-login/empty)
3. Never bypasses CAPTCHA or authentication

Status expected in many environments: PARTIAL / BLOCKED
Architecture is complete so live verification can succeed when network permits.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from config import yaml_config
from documentary_sources.base import BaseDocumentarySource, RawDocumentary

logger = logging.getLogger(__name__)

SKU_RE = re.compile(r"(?:item\.jd\.com/|/product/)(\d+)\.html", re.I)
PRICE_RE = re.compile(r"[¥￥]\s*([\d,]+(?:\.\d+)?)")


def _watch_terms() -> List[str]:
    cfg = yaml_config.get("documentary", {}).get("watchlists", {})
    terms = cfg.get("retail") or cfg.get("default") or []
    if terms:
        return list(terms)
    return [
        "RTX 6090",
        "RTX 6080",
        "RTX 6070",
        "联想 拯救者",
        "ROG 幻",
        "Steam Deck",
        "Legion Go",
    ]


class JDSource(BaseDocumentarySource):
    name = "jd"
    record_type = "RETAIL_LISTING"
    base_url = "https://search.jd.com"
    region = "CN"

    def fetch_latest(self) -> List[RawDocumentary]:
        records: List[RawDocumentary] = []
        seen: set[str] = set()
        terms = _watch_terms()
        for term in terms[:6]:
            # Keyword search page — often blocked for non-browser clients
            url = f"https://search.jd.com/Search?keyword={term.replace(' ', '%20')}&enc=utf-8"
            html = self.soft_fetch_html(url)
            if not html:
                logger.warning(
                    "[%s] search blocked/unavailable for term=%r — mark PARTIAL",
                    self.name,
                    term,
                )
                # soft_fetch_html already logged this to fetch_error_log
                continue
            # Detect anti-bot interstitial. This is a 200-OK response, so
            # soft_fetch_html sees it as a success — record it explicitly so
            # run_documentary_source doesn't mistake it for a clean "search
            # genuinely returned nothing" result.
            if "jd.com" not in html.lower() or "验证" in html[:2000] or len(html) < 2000:
                logger.warning("[%s] anti-bot or empty response for %r", self.name, term)
                self.fetch_error_log.append(f"anti-bot interstitial for term={term!r}")
                continue
            try:
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    m = SKU_RE.search(href)
                    if not m:
                        continue
                    sku = m.group(1)
                    if sku in seen:
                        continue
                    title = (a.get_text() or "").strip()
                    if len(title) < 8:
                        # try nearby
                        parent = a.parent
                        if parent:
                            title = parent.get_text(" ", strip=True)[:200]
                    if len(title) < 8:
                        continue
                    seen.add(sku)
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        href = "https://item.jd.com" + href
                    price = None
                    parent = a.parent
                    for _ in range(5):
                        if parent is None:
                            break
                        pm = PRICE_RE.search(parent.get_text(" ", strip=True))
                        if pm:
                            try:
                                price = float(pm.group(1).replace(",", ""))
                            except Exception:
                                pass
                            break
                        parent = getattr(parent, "parent", None)

                    # Seller quality unknown from list alone
                    seller_type = "UNKNOWN"
                    structured = {
                        "sku": sku,
                        "price": price,
                        "currency": "CNY" if price is not None else None,
                        "seller_type": seller_type,
                        "search_term": term,
                        "cpu": None,
                        "gpu": None,
                        "ram": None,
                        "storage": None,
                        "availability": None,
                    }
                    # Lightweight spec hints from title
                    if re.search(r"RTX\s*\d+", title, re.I):
                        structured["gpu"] = re.search(r"(RTX\s*\d+\s*\w*)", title, re.I).group(1)
                    if re.search(r"(\d+)\s*G[BＢ].*内存|内存.*(\d+)\s*G", title):
                        structured["ram"] = "present_in_title"

                    records.append(
                        RawDocumentary(
                            record_type=self.record_type,
                            source=self.name,
                            source_record_id=sku,
                            url=f"https://item.jd.com/{sku}.html",
                            canonical_url=f"https://item.jd.com/{sku}.html",
                            title=title[:300],
                            brand=None,
                            product=title[:120],
                            model_number=sku,
                            region="CN",
                            language_variant="zh-CN",
                            structured=structured,
                            raw_metadata={"search_term": term},
                        )
                    )
            except Exception as e:
                logger.warning("[%s] parse error term=%r: %s", self.name, term, e)

        logger.info("[%s] discovered %d listings", self.name, len(records))
        return records[:40]
