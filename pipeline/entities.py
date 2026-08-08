"""Entity extraction — deterministic, alias-aware, no LLM required."""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional, Set

from pydantic import BaseModel

from config import yaml_config


class EntityType(str, Enum):
    COMPANY = "COMPANY"
    PRODUCT = "PRODUCT"
    CHIP = "CHIP"
    PERSON = "PERSON"
    OTHER = "OTHER"


class Entity(BaseModel):
    name: str
    type: EntityType
    normalized: Optional[str] = None
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Alias maps — Chinese / English / transliterations all resolve to one canonical
# ---------------------------------------------------------------------------

# Core built-in map (always present). Extra aliases can be added via YAML.
_BUILTIN_COMPANY: Dict[str, str] = {
    # Nvidia — CN 英伟达 / TW 輝達·英偉達
    "英伟达": "Nvidia", "英偉達": "Nvidia", "輝達": "Nvidia",
    "nvidia": "Nvidia", "NVIDIA": "Nvidia", "nvdia": "Nvidia",
    # AMD — CN 超威 / TW 超微
    "超威": "AMD", "超威半导体": "AMD", "超微": "AMD", "超微半導體": "AMD",
    "amd": "AMD", "AMD": "AMD",
    # Intel
    "英特尔": "Intel", "英特爾": "Intel", "intel": "Intel", "Intel": "Intel", "因特尔": "Intel",
    # Qualcomm
    "高通": "Qualcomm", "qualcomm": "Qualcomm", "Qualcomm": "Qualcomm",
    # Apple
    "苹果": "Apple", "蘋果": "Apple", "apple": "Apple", "Apple": "Apple",
    # TSMC — CN 台积电 / TW 台積電
    "台积电": "TSMC", "台積電": "TSMC", "tsmc": "TSMC", "TSMC": "TSMC",
    "台湾积体电路": "TSMC", "台灣積體電路": "TSMC",
    # Samsung
    "三星": "Samsung", "samsung": "Samsung", "Samsung": "Samsung",
    "三星电子": "Samsung", "三星電子": "Samsung",
    # Huawei
    "华为": "Huawei", "華為": "Huawei", "huawei": "Huawei", "Huawei": "Huawei",
    "海思": "HiSilicon",
    # Xiaomi
    "小米": "Xiaomi", "xiaomi": "Xiaomi", "Xiaomi": "Xiaomi", "红米": "Xiaomi", "紅米": "Xiaomi",
    # Lenovo
    "联想": "Lenovo", "聯想": "Lenovo", "lenovo": "Lenovo", "Lenovo": "Lenovo", "拯救者": "Lenovo",
    # Asus / MSI / Gigabyte / Colorful
    "华硕": "Asus", "華碩": "Asus", "asus": "Asus", "ASUS": "Asus",
    "微星": "MSI", "msi": "MSI", "MSI": "MSI",
    "技嘉": "Gigabyte", "gigabyte": "Gigabyte", "Gigabyte": "Gigabyte",
    "七彩虹": "Colorful", "colorful": "Colorful",
    # Chinese silicon
    "摩尔线程": "Moore Threads", "moore threads": "Moore Threads",
    "龙芯": "Loongson", "loongson": "Loongson",
    "兆芯": "Zhaoxin", "zhaoxin": "Zhaoxin",
    "寒武纪": "Cambricon", "cambricon": "Cambricon",
    "海光": "Hygon", "hygon": "Hygon",
    "壁仞": "Biren", "biren": "Biren",
    "长鑫": "CXMT", "cxmt": "CXMT", "长鑫存储": "CXMT",
    "长江存储": "YMTC", "ymtc": "YMTC",
    # Big tech / consoles
    "索尼": "Sony", "sony": "Sony", "Sony": "Sony", "索尼互动": "Sony",
    "微软": "Microsoft", "microsoft": "Microsoft", "Microsoft": "Microsoft",
    "任天堂": "Nintendo", "nintendo": "Nintendo",
    "valve": "Valve", "Valve": "Valve", "维尔福": "Valve",
    "联发科": "MediaTek", "mediatek": "MediaTek", "MediaTek": "MediaTek", "联发科技": "MediaTek",
    "博通": "Broadcom", "broadcom": "Broadcom",
    "美光": "Micron", "micron": "Micron",
    "SK海力士": "SK Hynix", "海力士": "SK Hynix", "hynix": "SK Hynix",
    "铠侠": "Kioxia", "kioxia": "Kioxia",
    "西部数据": "Western Digital", "西数": "Western Digital",
    "希捷": "Seagate", "seagate": "Seagate",
    "戴尔": "Dell", "dell": "Dell",
    "惠普": "HP", "hp": "HP", "HP": "HP",
    "宏碁": "Acer", "acer": "Acer",
    "荣耀": "Honor", "honor": "Honor",
    "oppo": "OPPO", "OPPO": "OPPO",
    "vivo": "vivo", "Vivo": "vivo",
    "一加": "OnePlus", "oneplus": "OnePlus",
    "realme": "realme",
    "中兴": "ZTE", "zte": "ZTE",
    "字节跳动": "ByteDance", "bytedance": "ByteDance",
    "阿里": "Alibaba", "阿里巴巴": "Alibaba",
    "腾讯": "Tencent", "tencent": "Tencent",
    "百度": "Baidu", "baidu": "Baidu",
}

_BUILTIN_PERSON: Dict[str, str] = {
    "黄仁勋": "Jensen Huang",
    "苏姿丰": "Lisa Su",
    "pat gelsinger": "Pat Gelsinger",
    "基辛格": "Pat Gelsinger",
    "雷军": "Lei Jun",
    "任正非": "Ren Zhengfei",
    "余承东": "Yu Chengdong",
    "王腾": "Wang Teng",
}

_BUILTIN_CHIP_PRODUCT = [
    # GPUs
    "RTX 5090", "RTX 5080", "RTX 5070", "RTX 5060", "RTX 50",
    "RTX 4090", "RTX 4080", "RTX 4070", "RTX 4060",
    "GeForce", "Radeon", "RX 8000", "RX 9000", "RX 7900",
    # CPUs
    "Ryzen 9", "Ryzen 7", "Ryzen 5", "Ryzen AI", "EPYC", "Threadripper",
    "Core Ultra", "Core i9", "Core i7", "Core i5",
    "Nova Lake", "Panther Lake", "Arrow Lake", "Lunar Lake", "Meteor Lake",
    "Zen 5", "Zen 6", "Zen5", "Zen6",
    # Mobile / SoC
    "Snapdragon", "Dimensity", "天玑", "骁龙",
    "Apple M1", "Apple M2", "Apple M3", "Apple M4", "Apple M5", "Apple M6",
    "Kirin", "麒麟", "Exynos",
    # Consoles / handhelds
    "PlayStation 6", "PS6", "PlayStation 5", "PS5",
    "Steam Deck", "Xbox Series", "Switch 2", "Switch2",
    # Process / memory
    "3nm", "2nm", "5nm", "4nm", "18A", "14A", "N3", "N2", "N3E", "N3B",
    "GDDR7", "GDDR6", "HBM3", "HBM3E", "HBM4", "DDR5", "DDR6", "LPDDR5",
    "CXMT", "YMTC",
]


def _load_company_map() -> Dict[str, str]:
    m = dict(_BUILTIN_COMPANY)
    extra = yaml_config.get("entity_aliases", {}) or {}
    # YAML format: { "canonical": ["alias1", "alias2"] } or flat { "alias": "canonical" }
    for k, v in extra.items():
        if isinstance(v, list):
            for alias in v:
                m[str(alias)] = str(k)
            m[str(k)] = str(k)
        else:
            m[str(k)] = str(v)
    return m


def _load_chip_patterns() -> List[str]:
    extra = yaml_config.get("entity_chip_patterns", []) or []
    return list(_BUILTIN_CHIP_PRODUCT) + [str(x) for x in extra]


def extract_entities(text: str) -> List[Entity]:
    """Deterministic entity extraction. Aliases collapse to one normalized name."""
    if not text:
        return []
    found: List[Entity] = []
    seen: Set[str] = set()
    company_map = _load_company_map()
    chip_patterns = _load_chip_patterns()

    # --- Companies (longest-first so "台积电" wins over partials) ---
    for alias in sorted(company_map.keys(), key=len, reverse=True):
        if not alias:
            continue
        # Case-sensitive for pure CJK; case-insensitive for Latin
        if re.search(r"[\u4e00-\u9fff]", alias):
            hit = alias in text
        else:
            hit = alias.lower() in text.lower()
        if hit:
            canonical = company_map[alias]
            key = canonical.lower()
            if key not in seen:
                seen.add(key)
                found.append(
                    Entity(name=canonical, type=EntityType.COMPANY, normalized=canonical)
                )

    # --- Persons ---
    for alias, canonical in _BUILTIN_PERSON.items():
        if re.search(r"[\u4e00-\u9fff]", alias):
            hit = alias in text
        else:
            hit = alias.lower() in text.lower()
        if hit:
            key = "person:" + canonical.lower()
            if key not in seen:
                seen.add(key)
                found.append(
                    Entity(name=canonical, type=EntityType.PERSON, normalized=canonical)
                )

    # --- Chips / products (longest first) ---
    lower = text.lower()
    for term in sorted(chip_patterns, key=len, reverse=True):
        if term.lower() in lower or term in text:
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            chip_markers = (
                "nm", "gddr", "hbm", "ddr", "rtx", "rx ", "ryzen", "epyc",
                "snapdragon", "dimensity", "天玑", "骁龙", "kirin", "麒麟",
                "zen", "core ultra", "core i",
            )
            etype = (
                EntityType.CHIP
                if any(c in term.lower() for c in chip_markers)
                else EntityType.PRODUCT
            )
            found.append(Entity(name=term, type=etype, normalized=term))

    return found
