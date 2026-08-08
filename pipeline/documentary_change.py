"""Meaningful change detection for documentary snapshots."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Fields whose changes are intelligence-worthy
MEANINGFUL_KEYS = {
    "price",
    "cpu",
    "cpu_name",
    "gpu",
    "ram",
    "storage",
    "display",
    "availability",
    "model_number",
    "single_core",
    "multi_core",
    "gpu_score",
    "release_date",
    "seller_type",
    "images",
    "image_count",
    "status",
    "certificate_number",
}

SPEC_KEYS = {"cpu", "cpu_name", "gpu", "ram", "storage", "display"}
SCORE_KEYS = {"single_core", "multi_core", "gpu_score"}


def detect_changes(
    prev: Optional[Dict[str, Any]],
    curr: Dict[str, Any],
    *,
    is_new: bool = False,
) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """Return list of (event_type, before, after).

    Ignores pure marketing/layout noise — only MEANINGFUL_KEYS.
    """
    events: List[Tuple[str, Optional[str], Optional[str]]] = []
    if is_new or prev is None:
        events.append(("NEW_RECORD", None, _summary(curr)))
        return events

    for key in MEANINGFUL_KEYS:
        old_v = prev.get(key)
        new_v = curr.get(key)
        if _empty(old_v) and _empty(new_v):
            continue
        if _same(old_v, new_v):
            continue

        if key == "price":
            if _empty(old_v) and not _empty(new_v):
                events.append(("PRICE_ADDED", None, str(new_v)))
            else:
                events.append(("PRICE_CHANGED", str(old_v), str(new_v)))
        elif key in SPEC_KEYS:
            if _empty(old_v) and not _empty(new_v):
                events.append(("SPEC_ADDED", key, str(new_v)))
            else:
                events.append(("SPEC_CHANGED", f"{key}={old_v}", f"{key}={new_v}"))
        elif key == "model_number":
            if _empty(old_v) and not _empty(new_v):
                events.append(("MODEL_REVEALED", None, str(new_v)))
            else:
                events.append(("SPEC_CHANGED", str(old_v), str(new_v)))
        elif key in ("images", "image_count"):
            try:
                oi = int(old_v or 0)
                ni = int(new_v or 0)
            except Exception:
                oi, ni = 0, 1
            if ni > oi:
                events.append(("IMAGE_ADDED", str(oi), str(ni)))
        elif key == "availability":
            events.append(("AVAILABILITY_CHANGED", str(old_v), str(new_v)))
        elif key in SCORE_KEYS:
            if _empty(old_v) and not _empty(new_v):
                events.append(("BENCHMARK_APPEARED", key, str(new_v)))
            else:
                events.append(("SPEC_CHANGED", f"{key}={old_v}", f"{key}={new_v}"))
        elif key in ("status", "certificate_number"):
            if _empty(old_v) and not _empty(new_v):
                events.append(("CERTIFICATION_APPEARED", key, str(new_v)))
            else:
                events.append(("SPEC_CHANGED", str(old_v), str(new_v)))

    return events


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


def _summary(curr: Dict[str, Any]) -> str:
    parts = []
    for k in ("cpu_name", "cpu", "gpu", "price", "model_number", "single_core"):
        if curr.get(k) is not None:
            parts.append(f"{k}={curr[k]}")
    return ", ".join(parts)[:300]
