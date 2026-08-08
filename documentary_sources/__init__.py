"""Documentary Intelligence adapters (V0.4).

Geekbench DIRECT MONITORING: DISABLED / UNSUPPORTED
Reason: Direct automated monitoring is intentionally not supported due to
access/anti-automation constraints and maintenance cost. Historical
DocumentaryRecords with source="geekbench" remain readable. External
Geekbench URLs found in news/community posts are still classified as
upstream evidence but are never fetched automatically.
"""

from documentary_sources.base import BaseDocumentarySource, RawDocumentary
from documentary_sources.jd import JDSource

DOCUMENTARY_REGISTRY = {
    "jd": JDSource,
}

# Explicitly not registered — do not re-add without product decision
DISABLED_DOCUMENTARY_SOURCES = {
    "geekbench": "DIRECT MONITORING DISABLED — anti-automation / maintenance cost",
}

__all__ = [
    "BaseDocumentarySource",
    "RawDocumentary",
    "JDSource",
    "DOCUMENTARY_REGISTRY",
    "DISABLED_DOCUMENTARY_SOURCES",
]
