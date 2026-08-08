"""Community Intelligence adapters (V0.3). Separate from news publication sources.

PTT PC_Shopping: DISABLED from active production ingestion.
Reason: PTT's own web gateway (ptt.cc) is currently returning HTTP 500
"Server Too Busy" — confirmed via direct probe of the board index, another
board, hotboards.html, and 10/10 sampled stored article URLs, all 500.
This is a PTT-side reliability issue, not a CTW URL-construction bug, not
an anti-bot block, and not fixable by changing headers/cookies/retries.
Historical CommunityThread rows with platform="ptt" remain fully readable
everywhere (newsroom, lead timelines, clusters). PTTSource and its fixture
tests are kept for parser compatibility and easy re-enable once PTT
recovers — do not re-add to COMMUNITY_REGISTRY without re-probing first.
"""

from community_sources.base import BaseCommunitySource, RawThread
from community_sources.chiphell import ChiphellSource
from community_sources.mobile01 import Mobile01Source
from community_sources.ptt import PTTSource
from community_sources.coolaler import CoolalerSource

COMMUNITY_REGISTRY = {
    "chiphell": ChiphellSource,
    "mobile01": Mobile01Source,
    "coolaler": CoolalerSource,
}

# Explicitly not registered — do not re-add without re-probing PTT first.
DISABLED_COMMUNITY_SOURCES = {
    "ptt": "PTT web gateway returning HTTP 500 Server Too Busy — site-side, not a CTW bug",
}

__all__ = [
    "BaseCommunitySource",
    "RawThread",
    "ChiphellSource",
    "Mobile01Source",
    "PTTSource",
    "CoolalerSource",
    "COMMUNITY_REGISTRY",
    "DISABLED_COMMUNITY_SOURCES",
]
