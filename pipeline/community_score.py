"""Community signal classification and scoring. Deterministic, no LLM required."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config import yaml_config
from pipeline.entities import extract_entities
from community_sources.base import RawThread

# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------
SIGNAL_TYPES = (
    "SPECULATION",
    "RUMOR",
    "CLAIM",
    "FIRSTHAND_CLAIM",
    "PHOTO_EVIDENCE",
    "SCREENSHOT_EVIDENCE",
    "BENCHMARK_EVIDENCE",
    "RETAIL_EVIDENCE",
    "DOCUMENT_EVIDENCE",
    "OFFICIAL_REFERENCE",
    "REPOST",
    "DISCUSSION",
    "UNKNOWN",
)

# Keyword banks (CN + TW variants)
SPECULATION_KW = [
    "我觉得", "我覺得", "我认为", "我認為", "个人认为", "個人認為",
    "猜测", "猜測", "估计", "估計", "应该是", "應該是", "大概", "可能是",
    "会不会", "會不會", "是不是", "i think", "probably", "maybe",
]
RUMOR_KW = [
    "传闻", "傳聞", "据说", "據說", "听说", "聽說", "有消息", "消息人士",
    "爆料", "曝光", "泄露", "洩露", "洩漏", "内部消息", "內部消息",
]
FIRSTHAND_KW = [
    "拿到", "收到", "到手", "上机", "上機", "实测", "實測", "我测了", "我測了",
    "我这边", "我這邊", "我们店", "我們店", "经销商", "經銷商", "渠道给", "渠道給",
    "样品", "樣品", "工程样", "工程樣", "ES", "QS", "实拍", "實拍",
    "朋友送", "店里", "店裡", "我手上",
]
PHOTO_KW = ["实拍", "實拍", "真机", "真機", "实物", "實物", "照片", "拍照"]
SCREENSHOT_KW = ["截图", "截圖", "screenshot", "GPU-Z", "CPU-Z", "HWInfo", "AIDA64"]
BENCHMARK_KW = ["跑分", "geekbench", "cinebench", "3dmark", "分数", "分數", "benchmark"]
RETAIL_KW = ["上架", "京东", "京東", "天猫", "天貓", "jd.com", "tmall", "价格", "價格"]
REPOST_KW = ["转载", "轉載", "来源：", "來源：", "转自", "轉自", "原文", "ithome", "mydrivers"]

KNOWN_DOMAINS = {
    "weibo.com": "weibo",
    "bilibili.com": "bilibili",
    "jd.com": "jd",
    "tmall.com": "tmall",
    "geekbench.com": "geekbench",
    "browser.geekbench.com": "geekbench",
    "valid.x86.fr": "cpu-z",
    "techpowerup.com": "techpowerup",
    "ithome.com": "ithome",
    "mydrivers.com": "mydrivers",
    "expreview.com": "expreview",
    "benchlife.info": "benchlife",
    "technews.tw": "technews",
    "chiphell.com": "chiphell",
    "mobile01.com": "mobile01",
    "ptt.cc": "ptt",
    "coolaler.com": "coolaler",
}


def classify_external_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        for domain, kind in KNOWN_DOMAINS.items():
            if host.endswith(domain):
                return kind
    except Exception:
        pass
    return "other"


def extract_urls(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    found = re.findall(r"https?://[^\s<>\"']+", text)
    out = []
    seen = set()
    for u in found:
        u = u.rstrip(").,];>")
        if u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "source_type": classify_external_url(u)})
    return out


def classify_signal(raw: RawThread) -> Tuple[str, float]:
    """Return (signal_type, confidence 0-1). Deterministic."""
    text = (raw.title_original or "") + " " + (raw.op_text_original or "")
    lower = text.lower()
    urls = raw.external_urls or extract_urls(text)

    # Repost of known news outlet
    news_hosts = {"ithome", "mydrivers", "expreview", "benchlife", "technews", "zol", "jiwei"}
    if any(u.get("source_type") in news_hosts for u in urls):
        return "REPOST", 0.85

    if raw.image_count > 0 and any(k in text for k in PHOTO_KW + FIRSTHAND_KW):
        return "PHOTO_EVIDENCE", 0.8
    if raw.image_count > 0 and any(k in text for k in SCREENSHOT_KW):
        return "SCREENSHOT_EVIDENCE", 0.75
    if any(u.get("source_type") == "geekbench" for u in urls) or any(
        k in lower for k in BENCHMARK_KW
    ):
        if raw.image_count > 0 or any(u.get("source_type") == "geekbench" for u in urls):
            return "BENCHMARK_EVIDENCE", 0.75
    if any(u.get("source_type") in ("jd", "tmall") for u in urls) or any(
        k in text for k in RETAIL_KW
    ):
        return "RETAIL_EVIDENCE", 0.7
    if any(k in text for k in FIRSTHAND_KW):
        return "FIRSTHAND_CLAIM", 0.65
    if any(k in text for k in RUMOR_KW):
        return "RUMOR", 0.55
    if any(k in text for k in SPECULATION_KW):
        return "SPECULATION", 0.7
    if any(k in text for k in REPOST_KW):
        return "REPOST", 0.6
    if raw.reply_count > 5:
        return "DISCUSSION", 0.4
    return "UNKNOWN", 0.3


def score_firsthand(raw: RawThread, signal_type: str) -> float:
    score = 20.0
    text = (raw.title_original or "") + " " + (raw.op_text_original or "")
    if any(k in text for k in FIRSTHAND_KW):
        score += 30
    if signal_type in ("PHOTO_EVIDENCE", "SCREENSHOT_EVIDENCE", "BENCHMARK_EVIDENCE"):
        score += 25
    if raw.image_count >= 2:
        score += 15
    elif raw.image_count == 1:
        score += 8
    if any(k in text for k in SPECULATION_KW):
        score -= 25
    if signal_type == "REPOST":
        score -= 30
    return max(0.0, min(100.0, score))


def score_evidence(raw: RawThread, signal_type: str) -> float:
    score = 15.0
    if raw.image_count > 0:
        score += 20 + min(raw.image_count, 5) * 5
    if raw.attachment_count > 0:
        score += 15
    urls = raw.external_urls or []
    evidence_types = {"geekbench", "cpu-z", "techpowerup", "jd", "tmall"}
    if any(u.get("source_type") in evidence_types for u in urls):
        score += 25
    if signal_type in (
        "PHOTO_EVIDENCE",
        "SCREENSHOT_EVIDENCE",
        "BENCHMARK_EVIDENCE",
        "RETAIL_EVIDENCE",
        "DOCUMENT_EVIDENCE",
    ):
        score += 20
    if signal_type in ("SPECULATION", "DISCUSSION"):
        score -= 20
    if signal_type == "REPOST":
        score -= 25
    return max(0.0, min(100.0, score))


def score_relevance(raw: RawThread) -> float:
    text = (raw.title_original or "") + " " + (raw.op_text_original or "")
    ents = extract_entities(text)
    score = 35.0
    high = set(e.lower() for e in yaml_config.get("high_priority_entities", []))
    for e in ents:
        if e.normalized and e.normalized.lower() in high:
            score += 12
        if e.type.value in ("CHIP", "PRODUCT"):
            score += 6
    hw = [
        "显卡", "顯卡", "gpu", "cpu", "处理器", "處理器", "芯片", "晶片",
        "内存", "記憶體", "ddr", "gddr", "hbm", "ssd", "主板", "主機板",
        "工程样", "工程樣", "跑分", "笔记本", "筆電", "掌机", "掌機",
        "rtx", "ryzen", "骁龙", "天玑",
    ]
    lower = text.lower()
    for h in hw:
        if h in lower or h in text:
            score += 4
    noise = ["求助", "怎么选", "怎麼選", "装机推荐", "裝機推薦", "闲聊", "閒聊", "请益", "請益"]
    for n in noise:
        if n in text:
            score -= 12
    return max(0.0, min(100.0, score))


def score_velocity(reply_count: int, view_count: int, hours_old: float = 1.0) -> float:
    """Conservative age-based estimate when no prior snapshot exists."""
    hours_old = max(hours_old, 0.25)
    rpm = reply_count / hours_old
    score = min(40.0, rpm * 8) + min(30.0, (view_count / hours_old) / 50)
    return max(0.0, min(100.0, score))


def score_velocity_longitudinal(snapshots: list) -> float:
    """Velocity from ThreadMetrics snapshots: [(observed_at, replies, views), ...] oldest first.

    Prefer recent delta rate over lifetime averages.
    """
    if not snapshots:
        return 0.0
    if len(snapshots) < 2:
        return 20.0

    from datetime import timezone as _tz

    older = snapshots[-2]
    newer = snapshots[-1]
    t0, r0, v0 = older[0], older[1], older[2]
    t1, r1, v1 = newer[0], newer[1], newer[2]
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=_tz.utc)
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=_tz.utc)
    elapsed_h = max((t1 - t0).total_seconds() / 3600.0, 1.0 / 60.0)

    reply_delta = max(r1 - r0, 0)
    view_delta = max(v1 - v0, 0)
    rpm = reply_delta / elapsed_h
    vph = view_delta / elapsed_h

    score = min(50.0, rpm * 5) + min(30.0, vph / 40)

    if len(snapshots) >= 3:
        prev = snapshots[-3]
        tp, rp = prev[0], prev[1]
        if tp.tzinfo is None:
            tp = tp.replace(tzinfo=_tz.utc)
        prev_h = max((t0 - tp).total_seconds() / 3600.0, 1.0 / 60.0)
        prev_rpm = max(r0 - rp, 0) / prev_h
        if rpm > prev_rpm * 1.5 and rpm > 2:
            score += 15
    return max(0.0, min(100.0, score))


def compute_corroboration(
    this_platform: str,
    this_title: str,
    this_urls: list,
    this_signal_type: str,
    peers: list,
) -> float:
    """Conservative corroboration from independent peer community signals.

    peers: list of dicts with keys platform, title, urls, signal_type
    """
    if this_signal_type == "REPOST" or not peers:
        return 0.0

    score = 0.0
    for peer in peers:
        if peer.get("platform") == this_platform:
            continue
        peer_type = peer.get("signal_type") or "UNKNOWN"
        if peer_type == "REPOST":
            continue

        peer_title = peer.get("title") or ""
        peer_urls = peer.get("urls") or []
        peer_blob = peer_title + " " + " ".join(
            (u if isinstance(u, str) else u.get("url", "")) for u in peer_urls
        )

        # Obvious repost / link-back to this platform
        if this_platform.lower() in peer_blob.lower():
            continue
        is_link_repost = False
        for u in peer_urls:
            us = u if isinstance(u, str) else u.get("url", "")
            us_l = us.lower()
            if this_platform == "chiphell" and "chiphell.com" in us_l:
                is_link_repost = True
            if this_platform == "mobile01" and "mobile01.com" in us_l:
                is_link_repost = True
            if this_platform == "ptt" and "ptt.cc" in us_l:
                is_link_repost = True
            if this_platform == "coolaler" and "coolaler.com" in us_l:
                is_link_repost = True
        if is_link_repost:
            continue

        from pipeline.deduplicate import title_similarity
        sim = title_similarity(this_title, peer_title)
        if sim < 0.62:
            continue

        boost = 25
        if peer_type in (
            "PHOTO_EVIDENCE",
            "SCREENSHOT_EVIDENCE",
            "BENCHMARK_EVIDENCE",
            "RETAIL_EVIDENCE",
            "FIRSTHAND_CLAIM",
        ):
            boost = 40
        score += boost

    return max(0.0, min(100.0, score))


def score_novelty(signal_type: str, is_repost: bool) -> float:
    if is_repost or signal_type == "REPOST":
        return 15.0
    if signal_type in ("PHOTO_EVIDENCE", "BENCHMARK_EVIDENCE", "RETAIL_EVIDENCE"):
        return 85.0
    if signal_type in ("FIRSTHAND_CLAIM", "SCREENSHOT_EVIDENCE"):
        return 75.0
    if signal_type == "RUMOR":
        return 55.0
    if signal_type == "SPECULATION":
        return 30.0
    return 45.0


def compute_priority(
    relevance: float,
    novelty: float,
    evidence: float,
    firsthand: float,
    velocity: float,
    community: float,
    corroboration: float,
    signal_type: str,
) -> float:
    cfg = yaml_config.get("community", {}).get("scoring", {})
    w_rel = float(cfg.get("relevance", 0.30))
    w_nov = float(cfg.get("novelty", 0.20))
    w_evi = float(cfg.get("evidence", 0.20))
    w_fh = float(cfg.get("firsthand", 0.10))
    w_vel = float(cfg.get("velocity", 0.10))
    w_com = float(cfg.get("community", 0.05))
    w_cor = float(cfg.get("corroboration", 0.05))

    raw = (
        relevance * w_rel
        + novelty * w_nov
        + evidence * w_evi
        + firsthand * w_fh
        + velocity * w_vel
        + community * w_com
        + corroboration * w_cor
    )
    # Speculation cap
    cap = float(yaml_config.get("community", {}).get("speculation_priority_cap", 60))
    if signal_type == "SPECULATION":
        raw = min(raw, cap)
    if signal_type == "REPOST":
        raw = min(raw, 45)
    return max(0.0, min(100.0, raw))


def score_thread(raw: RawThread, hours_old: float = 2.0) -> Dict[str, Any]:
    """Full scoring pass on a RawThread."""
    if not raw.external_urls:
        raw.external_urls = extract_urls(
            (raw.title_original or "") + " " + (raw.op_text_original or "")
        )
    signal_type, conf = classify_signal(raw)
    is_repost = signal_type == "REPOST"
    ents = extract_entities(
        (raw.title_original or "") + " " + (raw.op_text_original or "")
    )
    relevance = score_relevance(raw)
    evidence = score_evidence(raw, signal_type)
    firsthand = score_firsthand(raw, signal_type)
    velocity = score_velocity(raw.reply_count, raw.view_count, hours_old)
    novelty = score_novelty(signal_type, is_repost)
    community = 40.0  # low weight until author history exists
    corroboration = 0.0
    priority = compute_priority(
        relevance, novelty, evidence, firsthand, velocity, community, corroboration, signal_type
    )
    return {
        "signal_type": signal_type,
        "signal_confidence": conf,
        "firsthand_score": firsthand,
        "evidence_score": evidence,
        "community_score": community,
        "velocity_score": velocity,
        "relevance_score": relevance,
        "novelty_score": novelty,
        "corroboration_score": corroboration,
        "priority_score": priority,
        "entities": ents,
        "external_urls": raw.external_urls,
    }
