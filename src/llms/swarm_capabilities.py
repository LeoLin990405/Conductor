"""Per-model Swarm capability declarations.

The table is product data, not a guess made by the UI. A model absent from the
``models`` map follows ``default``; only an explicit ``false`` hides Swarm.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Mapping

_PATH = Path(__file__).parent / "manifest" / "swarm_capabilities.json"


@lru_cache(maxsize=1)
def _table() -> tuple[bool, Mapping[str, bool]]:
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
        default = bool(raw.get("default", True))
        models = {
            str(name): bool(enabled)
            for name, enabled in (raw.get("models") or {}).items()
        }
        return default, models
    except Exception:
        # A malformed capability table must not hide a working product: the
        # conservative fallback is to keep Swarm available and let the
        # Subagents switch gate the actual fan-out.
        return True, {}


def supports_swarm(model_name: str) -> bool:
    """Whether the model may use Swarm, by the product's capability table."""
    default, models = _table()
    return models.get(model_name, default)
