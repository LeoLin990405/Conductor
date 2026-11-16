"""Shared agent instruction fragments, profiles, and per-CLI renderers.

I1 is preview, render, and diff. I2 (``apply.py``) adds apply, rollback and
snapshots; the operator HOME is reached only with ``operator=True``.
The agent id table follows ``src/skills_hub/adapters.py`` (copied, not imported).
"""

from .core import HubError
from .targets import TARGETS, Target, known_targets

__all__ = ["HubError", "TARGETS", "Target", "known_targets"]
