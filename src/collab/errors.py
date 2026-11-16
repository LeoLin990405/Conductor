"""Compatibility import for callers that used the earlier ``errors`` module.

The closed vocabulary now lives in :mod:`src.collab.types`, next to the value
objects it describes. This module re-exports it so both import spellings name
the same classes.
"""

from src.collab.types import *  # noqa: F401,F403
from src.collab.types import __all__ as _TYPES_ALL

__all__ = list(_TYPES_ALL)
