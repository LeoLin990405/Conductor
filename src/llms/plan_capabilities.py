"""Provider plan and API-region metadata for the setup picker.

This is product data, not a guess from the wire access type: a provider's
``coding_plan`` access can be a Token Plan, an Agent Plan, or a coding plan
with its own name. The matrix also records whether the provider exposes
distinct domestic and overseas API endpoints, which ``region_variants`` alone
cannot tell for a family that has not configured every variant yet.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

_PATH = Path(__file__).parent / "manifest" / "plan_capabilities.json"


@lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        # A malformed product table must not make the provider picker unusable.
        return {"families": {}, "plans": {}, "aliases": {}}


def family_key(brand: str) -> str:
    aliases: Mapping[str, str] = _table().get("aliases") or {}
    return aliases.get(brand, brand)


def family_capabilities(brand: str) -> dict[str, Any] | None:
    """Token/Agent plan and API-region capabilities for a brand family."""
    family = family_key(brand)
    entry = (_table().get("families") or {}).get(family)
    if not entry:
        return None
    return {
        "token_plan": bool(entry.get("token_plan", False)),
        "agent_plan": bool(entry.get("agent_plan", False)),
        "api_regions": list(entry.get("api_regions") or []),
    }


def plan_metadata(provider: str) -> dict[str, str] | None:
    """The plan kind/name for a plan access variant, when the product names it."""
    entry = (_table().get("plans") or {}).get(provider)
    if not entry:
        return None
    kind = entry.get("kind")
    if kind not in {"token", "agent"}:
        return None
    return {"kind": kind, "name": str(entry.get("name") or "")}
