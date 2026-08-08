"""Base source adapter interface and shared HTTP helpers."""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import yaml_config

logger = logging.getLogger(__name__)

# Per-domain last request time for simple rate limiting
_last_request: Dict[str, float] = {}

# IANA zones for CN / TW / HK sources (all currently UTC+8 year-round)
REGION_TIMEZONES: Dict[str, str] = {
    "CN": "Asia/Shanghai",
    "TW": "Asia/Taipei",
    "HK": "Asia/Hong_Kong",
}


@dataclass
class RawArticle:
    """Raw article as returned by a source adapter before normalization."""

    source: str
    source_article_id: str
    title_original: str
    url: str
    published_at: Optional[datetime] = None
    summary_original: Optional[str] = None
    category: Optional[str] = None
    canonical_url: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


class BaseSource(ABC):
    """Every source adapter must implement this interface."""

    name: str = "base"
    base_url: str = ""
    # V0.2 region metadata (override in subclasses)
    region: str = "CN"              # CN | TW | HK
    language_variant: str = "zh-CN"  # zh-CN | zh-TW | zh-HK

    def __init__(self):
        http_cfg = yaml_config.get("http", {})
        self.timeout = http_cfg.get("timeout_seconds", 20)
        self.user_agent = http_cfg.get(
            "user_agent",
            "Mozilla/5.0 (compatible; ChineseTechWire/0.1)",
        )
        self.rate_limit = http_cfg.get("rate_limit_per_domain_seconds", 2.0)
        self.delay_min = http_cfg.get("random_delay_min", 0.3)
        self.delay_max = http_cfg.get("random_delay_max", 1.2)
        self._client: Optional[httpx.Client] = None
        # Populated by soft_fetch_html on failure — lets callers (run_source
        # etc) tell a genuinely-empty listing apart from a swallowed block.
        self.fetch_error_log: List[str] = []

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                follow_redirects=True,
                http2=True,
            )
        return self._client

    def close(self) -> None:
        if self._client and not self._client.is_closed:
            self._client.close()

    def _rate_limit(self, url: str) -> None:
        domain = urlparse(url).netloc
        now = time.monotonic()
        last = _last_request.get(domain, 0.0)
        wait = self.rate_limit - (now - last)
        if wait > 0:
            time.sleep(wait)
        # small jitter
        time.sleep(random.uniform(self.delay_min, self.delay_max))
        _last_request[domain] = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def fetch_html(self, url: str) -> str:
        """Fetch URL with rate limiting and retries. Raises on hard failure."""
        self._rate_limit(url)
        logger.debug("[%s] GET %s", self.name, url)
        resp = self.client.get(url)
        if resp.status_code >= 400:
            # Clear one-line error — no stack spam for expected HTTP failures
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} for {url}",
                request=resp.request,
                response=resp,
            )
        resp.encoding = resp.charset_encoding or "utf-8"
        return resp.text

    def soft_fetch_html(self, url: str) -> Optional[str]:
        """Fetch HTML; on any failure return None and log a single warning."""
        try:
            return self.fetch_html(url)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else "?"
            logger.warning("[%s] HTTP %s fetching %s — skipping", self.name, code, url)
            self.fetch_error_log.append(f"HTTP {code} for {url}")
            return None
        except Exception as e:
            logger.warning("[%s] fetch failed for %s: %s", self.name, url, e)
            self.fetch_error_log.append(f"{type(e).__name__}: {e}"[:200])
            return None

    def fetch_bytes(self, url: str) -> bytes:
        self._rate_limit(url)
        resp = self.client.get(url)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} for {url}",
                request=resp.request,
                response=resp,
            )
        return resp.content

    @abstractmethod
    def fetch_latest(self) -> List[RawArticle]:
        """Return newest articles discovered from listing/feed. Must not raise for empty."""
        ...

    def fetch_article(self, url: str) -> Optional[RawArticle]:
        """Optional: fetch full article page. Default returns None (not required for V0.1)."""
        return None

    def make_absolute(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def source_zone(self) -> ZoneInfo:
        """IANA timezone for this source's local wall-clock timestamps."""
        name = REGION_TIMEZONES.get(getattr(self, "region", "CN"), "Asia/Shanghai")
        return ZoneInfo(name)

    def localize_naive(self, dt: datetime) -> datetime:
        """Interpret a naive datetime as local source time, return UTC-aware."""
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=self.source_zone()).astimezone(timezone.utc)

    def parse_datetime(self, text: str) -> Optional[datetime]:
        """Parse listing timestamps into timezone-aware UTC.

        Naive strings (e.g. ``2026-07-24 14:00``) are interpreted in the
        source's local zone (CN→Asia/Shanghai, TW→Asia/Taipei, HK→Asia/Hong_Kong),
        then converted to UTC.  Already-aware values are converted to UTC only.
        """
        if not text:
            return None
        text = text.strip()
        # Common formats used on CN/TW/HK listing pages
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d",
            "%m-%d %H:%M",
            "%Y年%m月%d日 %H:%M",
            "%Y年%m月%d日",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                # month-day without year → assume current local year
                if fmt.startswith("%m"):
                    local_now = datetime.now(self.source_zone())
                    dt = dt.replace(year=local_now.year)
                    # future by >1 day → previous year
                    aware_local = dt.replace(tzinfo=self.source_zone())
                    if aware_local > local_now + timedelta(days=1):
                        dt = dt.replace(year=local_now.year - 1)
                return self.localize_naive(dt)
            except ValueError:
                continue
        return None
