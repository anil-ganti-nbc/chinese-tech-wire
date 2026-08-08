"""Geekbench Browser — public CPU/GPU result search.

Discovery: https://browser.geekbench.com/v6/cpu/search?q=TERM
Result URL: https://browser.geekbench.com/v6/cpu/{id}
No login required for public results.
Region: GLOBAL
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from config import yaml_config
from documentary_sources.base import BaseDocumentarySource, RawDocumentary

logger = logging.getLogger(__name__)

RESULT_RE = re.compile(r"/v6/(cpu|compute|gpu)/(\d+)", re.I)
SCORE_RE = re.compile(r"(\d[\d,]*)")
MHZ_RE = re.compile(r"([\d.]+)\s*MHz", re.I)
CORES_RE = re.compile(r"(\d+)\s*cores?", re.I)
UPLOADED_RE = re.compile(
    r"Uploaded\s+(\w+\s+\d{1,2},\s+\d{4})", re.I
)


def _default_watchlist() -> List[str]:
    cfg = yaml_config.get("documentary", {}).get("watchlists", {})
    terms = cfg.get("benchmark") or cfg.get("default") or []
    if terms:
        return list(terms)
    return [
        "RTX 6090",
        "RTX 6080",
        "RTX 6070",
        "Nova Lake",
        "Panther Lake",
        "Ryzen AI",
        "engineering sample",
        "ES ",
        "Snapdragon 8 Elite",
        "Dimensity",
        "Loongson",
        "Moore Threads",
    ]


class GeekbenchSource(BaseDocumentarySource):
    name = "geekbench"
    record_type = "BENCHMARK_RECORD"
    base_url = "https://browser.geekbench.com"
    region = "GLOBAL"

    def fetch_latest(self) -> List[RawDocumentary]:
        records: List[RawDocumentary] = []
        seen: set[str] = set()
        terms = _default_watchlist()
        # Also pull latest CPU feed once
        pages = [f"{self.base_url}/v6/cpu"]
        for term in terms[:8]:  # bound request volume
            pages.append(f"{self.base_url}/v6/cpu/search?q={term.replace(' ', '+')}")

        for page_url in pages:
            html = self.soft_fetch_html(page_url)
            if not html:
                continue
            try:
                soup = BeautifulSoup(html, "lxml")
                # Results appear as links to /v6/cpu/ID near CPU description text
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    m = RESULT_RE.search(href)
                    if not m:
                        continue
                    kind, rid = m.group(1).lower(), m.group(2)
                    if rid in seen:
                        continue
                    # Prefer cpu results
                    if kind not in ("cpu", "compute", "gpu"):
                        continue
                    seen.add(rid)
                    url = self.make_absolute(f"/v6/{kind}/{rid}")
                    # Context blob around the link
                    parent = a.parent
                    blob = ""
                    for _ in range(4):
                        if parent is None:
                            break
                        blob = parent.get_text(" ", strip=True)
                        if len(blob) > 40:
                            break
                        parent = getattr(parent, "parent", None)

                    system_name = (a.get_text() or "").strip()
                    cpu_name = None
                    # Heuristic: "AMD Ryzen 9 9950X 4300 MHz (16 cores)"
                    cpu_m = re.search(
                        r"((?:AMD|Intel|Apple|Qualcomm|Samsung|MediaTek|NVIDIA|Nvidia)[^,]{3,80}?)(?:\s+\d+\s*MHz|\s+\()",
                        blob,
                    )
                    if cpu_m:
                        cpu_name = cpu_m.group(1).strip()
                    mhz = None
                    mm = MHZ_RE.search(blob)
                    if mm:
                        try:
                            mhz = float(mm.group(1))
                        except Exception:
                            pass
                    cores = None
                    cm = CORES_RE.search(blob)
                    if cm:
                        cores = int(cm.group(1))

                    scores = SCORE_RE.findall(blob)
                    single = multi = None
                    # last two large numbers often scores
                    nums = [int(s.replace(",", "")) for s in scores if len(s.replace(",", "")) >= 3]
                    if len(nums) >= 2:
                        single, multi = nums[-2], nums[-1]
                    elif len(nums) == 1:
                        single = nums[0]

                    published = None
                    um = UPLOADED_RE.search(blob)
                    if um:
                        try:
                            published = datetime.strptime(um.group(1), "%b %d, %Y").replace(
                                tzinfo=timezone.utc
                            )
                        except Exception:
                            pass

                    title = cpu_name or system_name or f"Geekbench {rid}"
                    structured = {
                        "benchmark": "Geekbench 6",
                        "kind": kind,
                        "system": system_name,
                        "cpu_name": cpu_name,
                        "cpu_mhz": mhz,
                        "cpu_cores": cores,
                        "single_core": single,
                        "multi_core": multi,
                        "platform_blob": blob[:500],
                    }
                    records.append(
                        RawDocumentary(
                            record_type=self.record_type,
                            source=self.name,
                            source_record_id=f"{kind}-{rid}",
                            url=url,
                            canonical_url=url,
                            title=title,
                            product=system_name or None,
                            model_number=cpu_name,
                            region=self.region,
                            published_at=published,
                            structured=structured,
                            raw_metadata={"search_page": page_url},
                        )
                    )
            except Exception as e:
                logger.warning("[%s] parse %s: %s", self.name, page_url, e)

        # Filter to watchlist relevance or anomaly keywords
        filtered = []
        watch_l = [w.lower() for w in terms]
        anomaly = [
            "engineering sample",
            " eng sample",
            " es ",
            "qs ",
            "unreleased",
            "nova lake",
            "panther lake",
            "rtx 60",
            "ryzen ai 3",
        ]
        for r in records:
            blob = ((r.title or "") + " " + str(r.structured)).lower()
            if any(w in blob for w in watch_l) or any(a in blob for a in anomaly):
                filtered.append(r)
            elif not terms:
                filtered.append(r)
        # If search was open latest feed, keep a small sample of filtered only
        logger.info("[%s] discovered %d records (%d after watch filter)", self.name, len(records), len(filtered))
        return (filtered or records)[:40]
