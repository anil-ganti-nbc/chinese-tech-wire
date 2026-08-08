"""Upstream source detection — deterministic heuristics. Never fabricate."""

from __future__ import annotations

import re
from typing import Optional, Tuple

# source_type values:
# ORIGINAL_REPORTING | OFFICIAL | RETAIL_LISTING | REGULATORY | BENCHMARK
# SOCIAL_MEDIA | LEAK | REPOST | ANALYSIS | UNKNOWN


def detect_upstream(
    title: str, summary: Optional[str] = None
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Return (source_type, upstream_source, upstream_url).
    upstream_url is only set when a concrete URL-like reference is present.
    Never invent an upstream when confidence is low.
    """
    text = (title or "") + " " + (summary or "")
    lower = text.lower()

    # --- Social media ---
    if any(k in text for k in ["微博", "weibo"]):
        return "SOCIAL_MEDIA", "Weibo", None
    if any(k in text for k in ["bilibili", "哔哩哔哩", "B站"]):
        return "SOCIAL_MEDIA", "Bilibili", None
    if any(k in text for k in ["推特", "twitter", "x.com"]):
        return "SOCIAL_MEDIA", "X/Twitter", None

    # --- Retail ---
    if any(k in text for k in ["京东", "jd.com", "天猫", "tmall", "淘宝", "上架", "开售价"]):
        src = "JD/Tmall"
        if "京东" in text or "jd.com" in lower:
            src = "JD.com"
        elif "天猫" in text or "tmall" in lower:
            src = "Tmall"
        return "RETAIL_LISTING", src, None

    # --- Regulatory / certification ---
    if any(
        k in text
        for k in ["3C认证", "3c认证", "FCC", "fcc", "工信部", "入网许可", "无线电型号核准", "监管"]
    ):
        return "REGULATORY", None, None

    # --- Benchmarks ---
    if any(
        k in text
        for k in [
            "跑分", "天梯榜", "Geekbench", "geekbench", "Cinebench", "cinebench",
            "3DMark", "3dmark", "PassMark", "CrossMark", "Benchmark", "benchmark",
        ]
    ):
        return "BENCHMARK", None, None

    # --- Official / OEM ---
    if any(
        k in text
        for k in ["官方", "官宣", "发布会", "正式发布", "press release", "公告", "新闻稿"]
    ):
        return "OFFICIAL", None, None

    # --- Leak / rumor language (Chinese first) ---
    leak_kw = [
        "曝光", "泄露", "爆料", "消息人士", "供应链", "业内人士", "知情人士",
        "工程样机", "工程样品", "未发布", "未官宣", "据悉", "有消息称",
        "爆料人", "泄密", "内部消息",
    ]
    if any(k in text for k in leak_kw):
        return "LEAK", None, None

    # --- Repost / aggregation ---
    if any(k in text for k in ["转载", "来源：", "综合报道", "据报道", "援引"]):
        # Try to extract a named publication if present
        m = re.search(r"(?:来源|援引)[：:]\s*([^\s，,。]{2,20})", text)
        up = m.group(1) if m else None
        return "REPOST", up, None

    # --- Analysis / opinion ---
    if any(k in text for k in ["分析", "解读", "观点", "评测", "深度"]):
        return "ANALYSIS", None, None

    return "UNKNOWN", None, None
