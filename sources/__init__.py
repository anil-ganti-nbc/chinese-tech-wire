"""News source adapters."""

from sources.base import BaseSource, RawArticle
from sources.ithome import ITHomeSource
from sources.mydrivers import MyDriversSource
from sources.expreview import ExpreviewSource
from sources.zol import ZOLSource
from sources.jiwei import JiweiSource
from sources.benchlife import BenchLifeSource
from sources.hkepc import HKEPCSource
from sources.technews import TechNewsSource
from sources.xfastest import XFastestSource

SOURCE_REGISTRY = {
    "ithome": ITHomeSource,
    "mydrivers": MyDriversSource,
    "expreview": ExpreviewSource,
    "zol": ZOLSource,
    "jiwei": JiweiSource,
    "benchlife": BenchLifeSource,
    "hkepc": HKEPCSource,
    "technews": TechNewsSource,
    "xfastest": XFastestSource,
}

__all__ = ["SOURCE_REGISTRY", "BaseSource", "RawArticle"]
