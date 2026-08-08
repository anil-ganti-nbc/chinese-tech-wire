"""Timezone correctness + source-race regression tests."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pipeline.deduplicate import get_or_create_cluster
from pipeline.normalize import normalize_raw
from sources.base import BaseSource, RawArticle, REGION_TIMEZONES
from sources.benchlife import BenchLifeSource
from sources.hkepc import HKEPCSource
from sources.ithome import ITHomeSource
from sources.jiwei import _parse_relative_or_abs
from sources.mydrivers import MyDriversSource
from sources.technews import TechNewsSource, _parse_rfc2822 as technews_rfc
from sources.expreview import _parse_pubdate as exp_parse_pubdate


# ---------------------------------------------------------------------------
# 1. Naive local timestamps → correct UTC
# ---------------------------------------------------------------------------

def test_cn_naive_timestamp_to_utc():
    src = ITHomeSource()
    assert src.region == "CN"
    dt = src.parse_datetime("2026-07-24 14:00")
    assert dt is not None
    assert dt.tzinfo is not None
    # 14:00 Asia/Shanghai → 06:00 UTC
    assert dt == datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)


def test_tw_naive_timestamp_to_utc():
    src = BenchLifeSource()
    assert src.region == "TW"
    dt = src.parse_datetime("2026-07-24 14:30")
    assert dt is not None
    assert dt == datetime(2026, 7, 24, 6, 30, tzinfo=timezone.utc)


def test_hk_naive_timestamp_to_utc():
    src = HKEPCSource()
    assert src.region == "HK"
    dt = src.parse_datetime("2026-07-24 14:00")
    assert dt is not None
    assert dt == datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)


def test_mydrivers_naive_matches_cn_zone():
    src = MyDriversSource()
    dt = src.parse_datetime("2026-07-24 15:44")
    assert dt == datetime(2026, 7, 24, 7, 44, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 2. Timezone-aware RSS timestamps remain correct (no double-shift)
# ---------------------------------------------------------------------------

def test_rss_aware_expreview_not_double_shifted():
    # 14:38 +08 → 06:38 UTC
    dt = exp_parse_pubdate("Fri, 24 Jul 2026 14:38:00 +08")
    assert dt is not None
    assert dt.hour == 6 and dt.minute == 38
    assert dt.utcoffset().total_seconds() == 0


def test_rss_aware_technews():
    dt = technews_rfc("Fri, 24 Jul 2026 08:58:10 +0000")
    assert dt is not None
    assert dt == datetime(2026, 7, 24, 8, 58, 10, tzinfo=timezone.utc)


def test_rss_plus0800_full():
    dt = technews_rfc("Fri, 24 Jul 2026 14:00:00 +0800")
    assert dt is not None
    assert dt == datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 3. Relative timestamps
# ---------------------------------------------------------------------------

def test_relative_minutes_anchor_utc():
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    dt = _parse_relative_or_abs("41分钟前", now)
    assert dt == datetime(2026, 7, 24, 13, 19, tzinfo=timezone.utc)


def test_relative_absolute_local_to_utc():
    now = datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc)
    dt = _parse_relative_or_abs("2026-07-23 15:51", now)
    # 15:51 Asia/Shanghai → 07:51 UTC
    assert dt == datetime(2026, 7, 23, 7, 51, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 4. Source-race ordering across regions
# ---------------------------------------------------------------------------

def test_source_race_benchlife_first():
    """BenchLife 14:00 Taipei before ITHome 14:10 Shanghai before MyDrivers 14:15."""
    bl = BenchLifeSource()
    ith = ITHomeSource()
    md = MyDriversSource()

    t_bl = bl.parse_datetime("2026-07-24 14:00")
    t_ith = ith.parse_datetime("2026-07-24 14:10")
    t_md = md.parse_datetime("2026-07-24 14:15")

    assert t_bl == datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)
    assert t_ith == datetime(2026, 7, 24, 6, 10, tzinfo=timezone.utc)
    assert t_md == datetime(2026, 7, 24, 6, 15, tzinfo=timezone.utc)

    # Ordering by published_at identifies first-seen
    ordered = sorted(
        [("benchlife", t_bl), ("ithome", t_ith), ("mydrivers", t_md)],
        key=lambda x: x[1],
    )
    assert ordered[0][0] == "benchlife"
    assert ordered[1][0] == "ithome"
    assert ordered[2][0] == "mydrivers"


def test_source_race_cross_midnight():
    """Local 23:50 TW vs local 00:10 next day CN — UTC order must not invert."""
    bl = BenchLifeSource()
    ith = ITHomeSource()

    # 2026-07-24 23:50 Taipei = 15:50 UTC
    t_bl = bl.parse_datetime("2026-07-24 23:50")
    # 2026-07-25 00:10 Shanghai = 16:10 UTC
    t_ith = ith.parse_datetime("2026-07-25 00:10")

    assert t_bl == datetime(2026, 7, 24, 15, 50, tzinfo=timezone.utc)
    assert t_ith == datetime(2026, 7, 24, 16, 10, tzinfo=timezone.utc)
    assert t_bl < t_ith


# ---------------------------------------------------------------------------
# 5. normalize must not treat naive as UTC
# ---------------------------------------------------------------------------

def test_normalize_naive_uses_region_not_utc():
    raw = RawArticle(
        source="ithome",
        source_article_id="1",
        title_original="测试",
        url="https://example.com/1",
        published_at=datetime(2026, 7, 24, 14, 0),  # naive — slip-through
        raw_metadata={"region": "CN", "timezone": "Asia/Shanghai"},
    )
    data = normalize_raw(raw)
    pub = data["published_at"]
    assert pub.tzinfo is not None
    assert pub == datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)


def test_normalize_already_utc_unchanged():
    raw = RawArticle(
        source="expreview",
        source_article_id="1",
        title_original="测试",
        url="https://example.com/1",
        published_at=datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc),
        raw_metadata={"region": "CN"},
    )
    data = normalize_raw(raw)
    assert data["published_at"] == datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)


def test_region_timezone_map():
    assert REGION_TIMEZONES["CN"] == "Asia/Shanghai"
    assert REGION_TIMEZONES["TW"] == "Asia/Taipei"
    assert REGION_TIMEZONES["HK"] == "Asia/Hong_Kong"
    # All three are currently UTC+8 (no DST)
    for name in REGION_TIMEZONES.values():
        z = ZoneInfo(name)
        offset = datetime(2026, 7, 24, 12, 0, tzinfo=z).utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 8 * 3600
