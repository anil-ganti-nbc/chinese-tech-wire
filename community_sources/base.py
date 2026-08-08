"""Base community adapter — forums are NOT news outlets."""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import yaml_config
from sources.base import REGION_TIMEZONES

logger = logging.getLogger(__name__)
_last_request: Dict[str, float] = {}


@dataclass
class RawThread:
    """Raw community thread before scoring/storage."""

    platform: str
    thread_id: str
    title_original: str
    url: str
    region: str = "CN"
    language_variant: str = "zh-CN"
    author_name: Optional[str] = None
    author_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    category: Optional[str] = None
    board: Optional[str] = None
    op_text_original: Optional[str] = None
    reply_count: int = 0
    view_count: int = 0
    image_count: int = 0
    attachment_count: int = 0
    external_urls: List[Dict[str, str]] = field(default_factory=list)
    # selected replies: list of dicts with post_id, author, text, images, urls, is_op
    posts: List[Dict[str, Any]] = field(default_factory=list)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    canonical_url: Optional[str] = None


class BaseCommunitySource(ABC):
    name: str = "base"
    base_url: str = ""
    region: str = "CN"
    language_variant: str = "zh-CN"

    def __init__(self):
        http_cfg = yaml_config.get("http", {})
        self.timeout = http_cfg.get("timeout_seconds", 20)
        self.user_agent = http_cfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 ChineseTechWire/0.3",
        )
        self.rate_limit = max(
            float(http_cfg.get("rate_limit_per_domain_seconds", 2.0)),
            float(yaml_config.get("community", {}).get("min_rate_limit_seconds", 3.0)),
        )
        self.delay_min = http_cfg.get("random_delay_min", 0.5)
        self.delay_max = http_cfg.get("random_delay_max", 1.5)
        self._client: Optional[httpx.Client] = None
        # Populated by soft_fetch_html on failure — lets callers (run_community_source
        # etc) tell a genuinely-empty listing apart from a swallowed block.
        self.fetch_error_log: List[str] = []

    @property
    def client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client and not self._client.is_closed:
            self._client.close()

    def source_zone(self) -> ZoneInfo:
        return ZoneInfo(REGION_TIMEZONES.get(self.region, "Asia/Shanghai"))

    def localize_naive(self, dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=self.source_zone()).astimezone(timezone.utc)

    def _rate_limit(self, url: str) -> None:
        domain = urlparse(url).netloc
        now = time.monotonic()
        last = _last_request.get(domain, 0.0)
        wait = self.rate_limit - (now - last)
        if wait > 0:
            time.sleep(wait)
        time.sleep(random.uniform(self.delay_min, self.delay_max))
        _last_request[domain] = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def fetch_html(self, url: str) -> str:
        self._rate_limit(url)
        resp = self.client.get(url)
        if resp.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} for {url}",
                request=resp.request,
                response=resp,
            )
        resp.encoding = resp.charset_encoding or "utf-8"
        return resp.text

    def soft_fetch_html(self, url: str) -> Optional[str]:
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

    def make_absolute(self, href: str) -> str:
        return urljoin(self.base_url, href)

    @abstractmethod
    def fetch_recent_threads(self) -> List[RawThread]:
        """Discover recent threads from prioritized boards. Must not raise for empty."""
        ...

    def fetch_thread(self, thread_id: str) -> Optional[RawThread]:
        """Optional: fetch full thread (OP + selective replies). Default None."""
        return None
