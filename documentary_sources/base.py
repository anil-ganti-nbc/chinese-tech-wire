"""Base documentary adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import yaml_config

logger = logging.getLogger(__name__)
_last_request: Dict[str, float] = {}


@dataclass
class RawDocumentary:
    record_type: str  # RETAIL_LISTING | BENCHMARK_RECORD | REGULATORY_RECORD | CERTIFICATION_RECORD
    source: str
    source_record_id: str
    url: str
    title: Optional[str] = None
    manufacturer: Optional[str] = None
    brand: Optional[str] = None
    product: Optional[str] = None
    model_number: Optional[str] = None
    region: str = "GLOBAL"
    language_variant: Optional[str] = None
    published_at: Optional[datetime] = None
    structured: Dict[str, Any] = field(default_factory=dict)
    # meaningful fields for change detection: price, cpu, gpu, ram, scores, etc.
    canonical_url: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def content_hash(self) -> str:
        """Hash of meaningful structured fields only (ignore layout noise)."""
        payload = {
            "title": self.title,
            "manufacturer": self.manufacturer,
            "brand": self.brand,
            "product": self.product,
            "model_number": self.model_number,
            "structured": _stable(self.structured),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _stable(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [_stable(x) for x in obj]
    return obj


class BaseDocumentarySource(ABC):
    name: str = "base"
    record_type: str = "BENCHMARK_RECORD"
    base_url: str = ""
    region: str = "GLOBAL"

    def __init__(self):
        http_cfg = yaml_config.get("http", {})
        self.timeout = http_cfg.get("timeout_seconds", 20)
        self.user_agent = http_cfg.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 ChineseTechWire/0.4",
        )
        self.rate_limit = float(http_cfg.get("rate_limit_per_domain_seconds", 2.0))
        self.delay_min = http_cfg.get("random_delay_min", 0.4)
        self.delay_max = http_cfg.get("random_delay_max", 1.2)
        self._client: Optional[httpx.Client] = None
        # Populated by soft_fetch_html on failure — lets callers (run_documentary_source
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
    def fetch_latest(self) -> List[RawDocumentary]:
        """Discover new/interesting documentary records for watchlist terms."""
        ...
