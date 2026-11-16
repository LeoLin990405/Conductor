"""Local change-impact analysis for Python and TypeScript.

The index is built with tree-sitter (MIT) and the official Python and
TypeScript grammars. Symbol, call, and import facts are stored in a sqlite
file under ``.conductor/impact``. This package does not read or embed any
GitNexus code.
"""

from .analyze import impact_report
from .index import ImpactIndex
from .review import review_range

__all__ = ["ImpactIndex", "impact_report", "review_range"]
