"""Compatibility import for the earlier ``rate_limit`` module name.

The in-process failure-closed bucket implementation lives in
:mod:`src.collab.ratelimit`; both names expose the same objects.
"""

from src.collab.ratelimit import *  # noqa: F401,F403
from src.collab.ratelimit import __all__ as _RATELIMIT_ALL

__all__ = list(_RATELIMIT_ALL)
