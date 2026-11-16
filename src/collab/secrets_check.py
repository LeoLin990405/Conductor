"""Compatibility import for the earlier ``secrets_check`` module name.

The payload boundary lives in :mod:`src.collab.payload`; both names expose the
same scanner and refusal helpers.
"""

from src.collab.payload import *  # noqa: F401,F403
from src.collab.payload import __all__ as _PAYLOAD_ALL

__all__ = list(_PAYLOAD_ALL)
