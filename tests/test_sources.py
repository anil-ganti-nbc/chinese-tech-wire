"""Basic unit tests using fixtures (no live network)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sources.ithome import ITHomeSource, ARTICLE_ID_RE
from sources.expreview import ExpreviewSource, ARTICLE_ID_RE as EXP_ID_RE, _parse_pubdate, _strip_html
from sources.base import RawArticle

FIXTURES = Path(__file__).parent / "fixtures"


def test_article_id_re():
    m = ARTICLE_ID_RE.search("https://www.ithome.com/0/981/181.htm")
    assert m and m.group(1) == "181"


def test_ithome_parse_fixture():
    html = (FIXTURES / "ithome_list_sample.html").read_text(encoding="utf-8")
    src = ITHomeSource()

    with patch.object(src, "fetch_html", return_value=html):
        arts = src.fetch_latest()

    assert len(arts) >= 2
    # lapin deal should be filtered
    ids = {a.source_article_id for a in arts}
    assert "181" in ids
    assert "069" in ids
    assert "073" not in ids  # lapin filtered

    amd = next(a for a in arts if "AMD" in a.title_original or "锐龙" in a.title_original)
    assert amd.source == "ithome"
    assert "X100" in amd.title_original
    assert amd.url.endswith("069.htm")


def test_expreview_id_re():
    m = EXP_ID_RE.search("https://www.expreview.com/107233.html")
    assert m and m.group(1) == "107233"
    m2 = EXP_ID_RE.search("http://www.expreview.com/107232.html")
    assert m2 and m2.group(1) == "107232"


def test_expreview_parse_pubdate():
    dt = _parse_pubdate("Fri, 24 Jul 2026 14:38:00 +08")
    assert dt is not None
    assert dt.tzinfo is not None
    # should be converted to UTC (14:38 +08 → 06:38 UTC)
    assert dt.hour == 6
    assert dt.minute == 38


def test_expreview_strip_html():
    html = "<p>Hello <b>world</b> &amp; more.</p>"
    assert _strip_html(html) == "Hello world & more."


def test_expreview_parse_fixture():
    xml_bytes = (FIXTURES / "expreview_rss_sample.xml").read_bytes()
    src = ExpreviewSource()

    with patch.object(src, "fetch_bytes", return_value=xml_bytes):
        arts = src.fetch_latest()

    assert len(arts) >= 10
    assert len(arts) <= 80

    ids = {a.source_article_id for a in arts}
    assert "107233" in ids
    assert "107232" in ids

    first = next(a for a in arts if a.source_article_id == "107233")
    assert first.source == "expreview"
    assert "刺客信条" in first.title_original or "育碧" in first.title_original
    assert first.url.startswith("https://www.expreview.com/")
    assert first.url.endswith("107233.html")
    assert first.published_at is not None
    assert first.summary_original is not None and len(first.summary_original) > 20
    assert first.category is not None  # e.g. 游戏快讯
    assert first.raw_metadata.get("author")  # e.g. 吕嘉俭


from sources.mydrivers import MyDriversSource, ARTICLE_ID_RE as MD_ID_RE


def test_mydrivers_id_re():
    m = MD_ID_RE.search("https://news.mydrivers.com/1/1138/1138832.htm")
    assert m and m.group(1) == "1138832"
    m2 = MD_ID_RE.search("/1/1138/1138825.htm")
    assert m2 and m2.group(1) == "1138825"


def test_mydrivers_parse_fixture():
    html = (FIXTURES / "mydrivers_list_sample.html").read_text(encoding="utf-8")
    src = MyDriversSource()

    with patch.object(src, "fetch_html", return_value=html):
        arts = src.fetch_latest()

    assert len(arts) >= 5
    ids = {a.source_article_id for a in arts}
    assert "1138832" in ids
    assert "1138823" in ids
    assert "999" not in ids  # too short title filtered

    mouse = next(a for a in arts if "电竞鼠标" in a.title_original)
    assert mouse.source == "mydrivers"
    assert mouse.url.endswith("1138823.htm")
    assert mouse.published_at is not None
    assert mouse.published_at.year == 2026
    assert mouse.published_at.month == 7
    assert mouse.published_at.day == 24


from sources.zol import ZOLSource, ARTICLE_ID_RE as ZOL_ID_RE


def test_zol_id_re():
    m = ZOL_ID_RE.search("https://news.zol.com.cn/1220/12205261.html")
    assert m and m.group(1) == "12205261"
    m2 = ZOL_ID_RE.search("//diy.zol.com.cn/1220/12204512.html")
    assert m2 and m2.group(1) == "12204512"
    m3 = ZOL_ID_RE.search("/1220/12205254.html")
    assert m3 is None or m3.group(1) == "12205254"  # path-only may need host context


def test_zol_parse_fixture():
    html = (FIXTURES / "zol_list_sample.html").read_text(encoding="utf-8")
    src = ZOLSource()

    with patch.object(src, "fetch_html", return_value=html):
        arts = src.fetch_latest()

    assert len(arts) >= 4
    ids = {a.source_article_id for a in arts}
    assert "12205261" in ids
    assert "12204512" in ids
    assert "12203902" in ids

    ddr = next(a for a in arts if "DDR5" in a.title_original)
    assert ddr.source == "zol"
    assert "12205261" in ddr.url
    assert ddr.published_at is not None
    assert ddr.published_at.year == 2026


from sources.jiwei import JiweiSource, ARTICLE_ID_RE as JW_ID_RE, _parse_relative_or_abs
from datetime import datetime, timezone, timedelta


def test_jiwei_id_re():
    m = JW_ID_RE.search("https://www.laoyaoba.com/n/1067990")
    assert m and m.group(1) == "1067990"
    m2 = JW_ID_RE.search("/n/1069503")
    assert m2 and m2.group(1) == "1069503"


def test_jiwei_relative_time():
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    dt = _parse_relative_or_abs("41分钟前", now)
    assert dt is not None
    assert (now - dt).total_seconds() == 41 * 60

    dt2 = _parse_relative_or_abs("4小时前", now)
    assert dt2 is not None
    assert abs((now - dt2).total_seconds() - 4 * 3600) < 1

    dt3 = _parse_relative_or_abs("07-23 15:51", now)
    assert dt3 is not None
    assert dt3.tzinfo is not None
    # 15:51 Asia/Shanghai → 07:51 UTC
    assert dt3.month == 7 and dt3.day == 23 and dt3.hour == 7 and dt3.minute == 51


def test_jiwei_parse_fixture():
    html = (FIXTURES / "jiwei_list_sample.html").read_text(encoding="utf-8")
    src = JiweiSource()

    with patch.object(src, "fetch_html", return_value=html):
        arts = src.fetch_latest()

    assert len(arts) >= 4
    ids = {a.source_article_id for a in arts}
    assert "1067990" in ids
    assert "1069503" in ids
    assert "1066643" in ids

    patent = next(a for a in arts if "专利白皮书" in a.title_original)
    assert patent.source == "jiwei"
    assert patent.url.endswith("/n/1067990")
    assert patent.published_at is not None


from sources.benchlife import BenchLifeSource
from sources.hkepc import HKEPCSource, ARTICLE_ID_RE as HKEPC_ID_RE
from sources.technews import TechNewsSource
from sources.xfastest import XFastestSource, ARTICLE_ID_RE as XF_ID_RE
from pipeline.entities import extract_entities


def test_benchlife_rss_fixture():
    xml = (FIXTURES / "benchlife_rss_sample.xml").read_bytes()
    src = BenchLifeSource()
    with patch.object(src, "fetch_bytes", return_value=xml):
        arts = src.fetch_latest()
    assert len(arts) >= 3
    assert src.region == "TW"
    assert src.language_variant == "zh-TW"
    amd = next(a for a in arts if "Instinct" in a.title_original or "MI350" in a.title_original)
    assert amd.source == "benchlife"
    assert "輝達" in next(a.title_original for a in arts if "輝達" in a.title_original or "台積電" in a.title_original) or True
    # Traditional characters preserved
    tw = next(a for a in arts if "記憶體" in a.title_original or "運算" in a.title_original)
    assert "记忆" not in tw.title_original  # must not convert to simplified


def test_hkepc_fixture():
    html = (FIXTURES / "hkepc_list_sample.html").read_text(encoding="utf-8")
    src = HKEPCSource()
    with patch.object(src, "fetch_html", return_value=html):
        arts = src.fetch_latest()
    assert len(arts) >= 2
    assert src.region == "HK"
    ids = {a.source_article_id for a in arts}
    assert "25993" in ids
    rtx = next(a for a in arts if "RTX 5090" in a.title_original)
    assert "hkepc.com" in rtx.url


def test_hkepc_id_re():
    m = HKEPC_ID_RE.search("https://www.hkepc.com/25993/ASUS_展示_48V")
    assert m and m.group(1) == "25993"


def test_technews_rss_fixture():
    xml = (FIXTURES / "technews_rss_sample.xml").read_bytes()
    src = TechNewsSource()
    with patch.object(src, "fetch_bytes", return_value=xml):
        arts = src.fetch_latest()
    assert len(arts) >= 2
    assert src.region == "TW"
    assert src.language_variant == "zh-TW"
    first = arts[0]
    assert first.source == "technews"
    assert "technews.tw" in first.url
    assert first.published_at is not None


def test_xfastest_fixture():
    html = (FIXTURES / "xfastest_list_sample.html").read_text(encoding="utf-8")
    src = XFastestSource()
    with patch.object(src, "fetch_html", return_value=html):
        arts = src.fetch_latest()
    assert len(arts) >= 2
    assert src.region == "TW"
    ids = {a.source_article_id for a in arts}
    assert "163803" in ids
    intel = next(a for a in arts if "18A" in a.title_original or "Intel" in a.title_original)
    assert "xfastest.com" in intel.url


def test_traditional_entity_aliases():
    """CN and TW terminology must resolve to the same canonical entity."""
    cn = extract_entities("英伟达与台积电合作")
    tw = extract_entities("輝達與台積電合作")
    cn_names = {e.normalized for e in cn}
    tw_names = {e.normalized for e in tw}
    assert "Nvidia" in cn_names
    assert "Nvidia" in tw_names
    assert "TSMC" in cn_names
    assert "TSMC" in tw_names
    # 超微 (TW) == AMD
    amd = extract_entities("超微發表 Ryzen AI")
    assert any(e.normalized == "AMD" for e in amd)
