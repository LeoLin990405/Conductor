"""Concrete API-key / plan routes for a provider family, for Settings cards.

The Setup wizard builds these from the live provider catalog. Settings only had
a descriptor and therefore showed a single "enter API key" action, which hid a
family's Agent/Token plan route (MiniMax M Plan, Qwen Token Plan, StepFun Step
Plan, …). This module projects the same catalog options into a small list the
Settings row can render as explicit choices.
"""

from __future__ import annotations

from typing import Any

_PLAN_ACCESS = {"api_key", "coding_plan"}


def _option(entry: dict[str, Any], variant: dict[str, Any] | None = None) -> dict[str, Any]:
    variant = variant or {}
    provider = str(variant.get("provider") or entry.get("provider") or "")
    return {
        "provider": provider,
        "display_name": str(variant.get("display_name") or entry.get("display_name") or provider),
        "access_type": str(entry.get("access_type") or "api_key"),
        "plan_kind": entry.get("plan_kind"),
        "plan_name": entry.get("plan_name"),
        "sdk": variant.get("sdk") or entry.get("sdk"),
        "base_url": variant.get("base_url") or entry.get("base_url"),
        "use_response_api": bool(
            variant.get("use_response_api", entry.get("use_response_api", False))
        ),
        "region": str(variant.get("region") or entry.get("region") or "intl"),
        "dynamic_models": bool(entry.get("dynamic_models", False)),
    }


def options_for(brand: str, fallback_provider: str | None = None) -> list[dict[str, Any]]:
    """Provider-family API-key and plan routes, in UI order."""
    from src.server.app.api_keys import _build_provider_catalog

    try:
        catalog = _build_provider_catalog()
    except Exception:
        return []

    wanted = {(brand or "").strip(), (fallback_provider or "").strip()}
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in catalog:
        if entry.get("deprecated"):
            continue
        if entry.get("brand_key") not in wanted:
            continue
        access = entry.get("access_type")
        if access not in _PLAN_ACCESS:
            continue
        candidates = [None, *(entry.get("region_variants") or [])]
        for variant in candidates:
            option = _option(entry, variant)
            provider = option["provider"]
            if not provider or provider in seen:
                continue
            seen.add(provider)
            options.append(option)

    # API-key routes before plans; inside each, domestic before overseas.
    options.sort(
        key=lambda o: (
            0 if o["access_type"] == "api_key" else 1,
            0 if o["region"] == "cn" else 1,
            o["provider"],
        )
    )
    return options
