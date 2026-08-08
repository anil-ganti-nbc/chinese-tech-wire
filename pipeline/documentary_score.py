"""Documentary relevance / novelty / evidence scoring."""

from __future__ import annotations

import re
from typing import Any, Dict

from config import yaml_config
from documentary_sources.base import RawDocumentary
from pipeline.entities import extract_entities


def score_documentary(raw: RawDocumentary) -> Dict[str, float]:
    text = " ".join(
        filter(
            None,
            [
                raw.title,
                raw.product,
                raw.model_number,
                raw.manufacturer,
                str(raw.structured),
            ],
        )
    )
    ents = extract_entities(text)
    relevance = 40.0
    high = set(e.lower() for e in yaml_config.get("high_priority_entities", []))
    for e in ents:
        if e.normalized and e.normalized.lower() in high:
            relevance += 10
        if e.type.value in ("CHIP", "PRODUCT"):
            relevance += 5
    for kw in ("RTX", "Ryzen", "Nova Lake", "工程", "ES", "QS", "未发布", "拯救者", "ROG"):
        if kw.lower() in text.lower() or kw in text:
            relevance += 4
    relevance = max(0.0, min(100.0, relevance))

    novelty = 50.0
    anomaly = [
        r"RTX\s*60[789]0",
        r"Nova Lake",
        r"Panther Lake",
        r"engineering sample",
        r"\bES\b",
        r"未发布",
        r"工程样",
    ]
    for pat in anomaly:
        if re.search(pat, text, re.I):
            novelty += 15
    if raw.record_type == "BENCHMARK_RECORD" and raw.structured.get("cpu_name"):
        cn = str(raw.structured.get("cpu_name") or "")
        if re.search(r"\b(ES|QS|sample)\b", cn, re.I):
            novelty += 20
    novelty = max(0.0, min(100.0, novelty))

    evidence = 45.0
    if raw.record_type == "BENCHMARK_RECORD":
        evidence = 70.0
        if raw.structured.get("single_core") or raw.structured.get("multi_core"):
            evidence += 10
    elif raw.record_type == "RETAIL_LISTING":
        evidence = 55.0
        st = (raw.structured or {}).get("seller_type") or "UNKNOWN"
        if st in ("OFFICIAL_BRAND_STORE", "PLATFORM_SELF_OPERATED"):
            evidence += 25
        elif st == "AUTHORIZED_RETAILER":
            evidence += 10
        if raw.structured.get("price") is not None:
            evidence += 10
        if raw.structured.get("gpu") or raw.structured.get("cpu"):
            evidence += 8
    elif raw.record_type in ("REGULATORY_RECORD", "CERTIFICATION_RECORD"):
        evidence = 75.0
    evidence = max(0.0, min(100.0, evidence))

    source_quality = {
        "geekbench": 0.85,
        "jd": 0.70,
        "tmall": 0.65,
        "srrc": 0.90,
    }.get(raw.source, 0.60)

    cfg = yaml_config.get("documentary", {}).get("scoring", {})
    w_rel = float(cfg.get("relevance", 0.30))
    w_nov = float(cfg.get("novelty", 0.30))
    w_evi = float(cfg.get("evidence", 0.25))
    w_sq = float(cfg.get("source_quality", 0.10))
    w_cor = float(cfg.get("corroboration", 0.05))
    corroboration = 0.0
    priority = (
        relevance * w_rel
        + novelty * w_nov
        + evidence * w_evi
        + (source_quality * 100) * w_sq
        + corroboration * w_cor
    )
    return {
        "relevance_score": relevance,
        "novelty_score": novelty,
        "evidence_score": evidence,
        "source_quality": source_quality,
        "corroboration_score": corroboration,
        "priority_score": max(0.0, min(100.0, priority)),
    }
