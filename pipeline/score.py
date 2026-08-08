"""Relevance, novelty and priority scoring. Fully deterministic for V0.1."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import yaml_config
from pipeline.entities import Entity, extract_entities

logger = logging.getLogger(__name__)


def _load_weights() -> Dict[str, Any]:
    return yaml_config.get("scoring", {})


def score_relevance(title: str, summary: Optional[str] = None, entities: Optional[List[Entity]] = None) -> float:
    """0-100 relevance for Notebookcheck-style hardware journalist."""
    text = (title or "") + " " + (summary or "")
    lower = text.lower()
    score = 40.0  # base

    high_ents = set(e.lower() for e in yaml_config.get("high_priority_entities", []))
    high_terms = [t.lower() for t in yaml_config.get("high_priority_terms", [])]
    noise = [n.lower() for n in yaml_config.get("noise_keywords", [])]

    # Entity boosts
    if entities:
        for e in entities:
            if e.normalized and e.normalized.lower() in high_ents:
                score += 12
            if e.type.value in ("CHIP", "PRODUCT"):
                score += 6

    # Term boosts
    for term in high_terms:
        if term in lower:
            score += 5

    # Strong hardware signals
    hw_signals = [
        "显卡", "gpu", "cpu", "处理器", "芯片", "制程", "工艺", "跑分", "天梯",
        "笔记本", "laptop", "掌机", "handheld", "显存", "gddr", "hbm",
        "ssd", "内存", "ddr", "主板", "电源", "散热器", "工程样", "规格",
        "发布", "上市", "定价", "首发", "曝光", "泄露",
    ]
    for s in hw_signals:
        if s in lower:
            score += 3

    # Noise penalty
    for n in noise:
        if n in lower:
            score -= 15

    # Phone / EV / general AI soft penalty unless hardware angle
    soft_neg = ["手机", "phone", "电动车", "ev", "汽车", "软件更新", "app"]
    for s in soft_neg:
        if s in lower and not any(h in lower for h in ["芯片", "soc", "处理器", "gpu"]):
            score -= 8

    return max(0.0, min(100.0, score))


def score_novelty(
    title: str,
    is_first_in_cluster: bool = True,
    hours_since_first: float = 0.0,
    has_new_spec: bool = False,
    is_rumor: bool = False,
) -> float:
    """0-100. Higher when truly new information."""
    score = 55.0
    if is_first_in_cluster:
        score += 25
    else:
        # later reports lose novelty fast
        score -= min(40.0, hours_since_first * 1.5)

    if has_new_spec:
        score += 15
    if is_rumor:
        score += 5  # rumors can be novel even if low confidence

    lower = (title or "").lower()
    novelty_boost = ["首次", "首发", "未发布", "工程样", "新规格", "跑分", "天梯", "曝光", "泄露", "新", "首"]
    for b in novelty_boost:
        if b in lower:
            score += 4

    return max(0.0, min(100.0, score))


def detect_rumor(title: str, summary: Optional[str] = None) -> tuple[bool, float]:
    text = (title or "") + " " + (summary or "")
    zh_keys = yaml_config.get("rumor_keywords_zh", [])
    en_keys = yaml_config.get("rumor_keywords_en", [])
    hits = 0
    for k in zh_keys + en_keys:
        if k in text:
            hits += 1
    if hits == 0:
        return False, 0.0
    conf = min(0.95, 0.35 + hits * 0.2)
    return True, conf


def compute_priority(
    relevance: float,
    novelty: float,
    source: str,
    published_at: Optional[datetime] = None,
    rumor_flag: bool = False,
    bonuses: Optional[Dict[str, bool]] = None,
) -> float:
    cfg = _load_weights()
    w_rel = cfg.get("relevance_weight", 0.45)
    w_nov = cfg.get("novelty_weight", 0.35)
    w_src = cfg.get("source_quality_weight", 0.10)
    w_fresh = cfg.get("freshness_weight", 0.10)

    src_q = yaml_config.get("source_quality", {}).get(source, 0.7) * 100

    # Freshness: higher if published recently
    freshness = 50.0
    if published_at:
        now = datetime.now(timezone.utc)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        hours = (now - published_at).total_seconds() / 3600.0
        if hours < 1:
            freshness = 100.0
        elif hours < 6:
            freshness = 85.0
        elif hours < 24:
            freshness = 70.0
        elif hours < 72:
            freshness = 50.0
        else:
            freshness = 30.0

    base = (
        relevance * w_rel
        + novelty * w_nov
        + src_q * w_src
        + freshness * w_fresh
    )

    bonus_cfg = cfg.get("bonuses", {})
    b = bonuses or {}
    extra = 0.0
    if b.get("benchmark"):
        extra += bonus_cfg.get("benchmark", 0)
    if b.get("retail_listing"):
        extra += bonus_cfg.get("retail_listing", 0)
    if b.get("regulatory"):
        extra += bonus_cfg.get("regulatory", 0)
    if b.get("engineering_sample"):
        extra += bonus_cfg.get("engineering_sample", 0)
    if b.get("new_specs"):
        extra += bonus_cfg.get("new_specs", 0)
    if b.get("new_price"):
        extra += bonus_cfg.get("new_price", 0)
    if b.get("new_launch_date"):
        extra += bonus_cfg.get("new_launch_date", 0)
    if b.get("leak") or rumor_flag:
        extra += bonus_cfg.get("leak", 0)

    return max(0.0, min(100.0, base + extra))


def score_article(
    title: str,
    summary: Optional[str] = None,
    source: str = "",
    published_at: Optional[datetime] = None,
    is_first: bool = True,
    hours_since_first: float = 0.0,
) -> Dict[str, Any]:
    entities = extract_entities(title + " " + (summary or ""))
    relevance = score_relevance(title, summary, entities)
    is_rumor, rumor_conf = detect_rumor(title, summary)
    novelty = score_novelty(title, is_first, hours_since_first, is_rumor=is_rumor)

    # Simple bonus detection from text
    lower = (title or "").lower()
    bonuses = {
        "benchmark": any(x in lower for x in ["跑分", "天梯", "benchmark", "geekbench", "cinebench"]),
        "engineering_sample": any(x in lower for x in ["工程样", "engineering sample", "es "]),
        "leak": is_rumor,
        "new_specs": any(x in lower for x in ["规格", "参数", "specs"]),
        "new_price": any(x in lower for x in ["价格", "售价", "定价", "price"]),
        "new_launch_date": any(x in lower for x in ["上市", "发布", "launch", "发售"]),
        "retail_listing": any(x in lower for x in ["上架", "京东", "天猫", "jd.com", "listing"]),
        "regulatory": any(x in lower for x in ["认证", "3c", "fcc", "监管"]),
    }

    priority = compute_priority(
        relevance, novelty, source, published_at, is_rumor, bonuses
    )

    return {
        "relevance_score": round(relevance, 1),
        "novelty_score": round(novelty, 1),
        "priority_score": round(priority, 1),
        "rumor_flag": is_rumor,
        "rumor_confidence": round(rumor_conf, 2),
        "entities": entities,
        "bonuses": bonuses,
    }
