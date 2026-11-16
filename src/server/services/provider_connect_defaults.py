"""Connection defaults for an API-key provider.

The Settings connection cards only know the BYOK provider id; the Setup wizard
gets the endpoint from the provider catalog. This helper reads the same manifest
data for a descriptor so both surfaces prefill the same sdk/Base URL/region
instead of showing an empty ``https://...`` field.
"""

from __future__ import annotations

from typing import Any

# SDKs that use their vendor SDK's built-in default endpoint leave base_url
# unset in the manifest. The editor still needs to show the endpoint it will
# use, so these are the UI-only defaults (the runtime keeps using the SDK
# default unless the user saves an override).
UI_DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}


def connect_defaults(provider: str | None) -> dict[str, Any] | None:
    """Return the provider's manifest connection defaults, or None.

    Region variants are attached only for a root API-key provider: a coding-plan
    variant's sibling region is a different subscription/key, not an alternate
    endpoint for this key.
    """
    wanted = (provider or "").strip()
    if not wanted:
        return None
    try:
        from src.llms.llm import ModelConfig

        flat = ModelConfig().flat_providers
    except Exception:
        return None
    info = flat.get(wanted)
    if not isinstance(info, dict):
        return None

    root = info.get("parent_provider") or wanted
    root_info = flat.get(root, {}) if isinstance(flat.get(root), dict) else {}
    variants: list[dict[str, Any]] = []
    if info.get("access_type", "api_key") in ("api_key", "coding_plan"):
        for key, candidate in flat.items():
            if key == wanted or not isinstance(candidate, dict):
                continue
            if candidate.get("parent_provider") != root:
                continue
            if candidate.get("region") == info.get("region"):
                continue
            if candidate.get("access_type", "api_key") != info.get("access_type", "api_key"):
                continue
            variants.append({
                "provider": key,
                "display_name": candidate.get("display_name") or root_info.get("display_name") or key,
                "region": candidate.get("region") or "intl",
                "sdk": candidate.get("sdk"),
                "base_url": candidate.get("base_url"),
                "use_response_api": bool(candidate.get("use_response_api", False)),
            })

    return {
        "sdk": info.get("sdk"),
        "base_url": info.get("base_url") or UI_DEFAULT_BASE_URLS.get(wanted),
        "use_response_api": bool(info.get("use_response_api", False)),
        "region": info.get("region") or "intl",
        "dynamic_models": bool(info.get("dynamic_models", False)),
        "region_variants": variants,
    }
