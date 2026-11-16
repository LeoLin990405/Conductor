"""Bounded Router prompt payloads and per-Source admission modes."""

from __future__ import annotations

import json
from typing import Any

ROUTER_PAYLOAD_BUDGET_CHARS = 55_000
SOURCE_MODES = {"respond-only-explicit", "candidate", "ignore"}


def source_mode(config: dict[str, Any] | None) -> str:
    """Return a known source mode; old configs retain candidate behavior."""
    raw = str((config or {}).get("routing_mode") or "candidate").strip().lower()
    raw = raw.replace("_", "-")
    return raw if raw in SOURCE_MODES else "ignore"


def source_allows_candidate(
    trigger: dict[str, Any],
    config: dict[str, Any],
    accept_mode: str,
    admission: dict[str, Any] | None,
) -> bool:
    """Layer Source routing mode over the existing admission and accept rules."""
    mode = source_mode(config)
    if mode == "ignore":
        return False
    strength = _admitted_strength(trigger, config, admission)
    if mode == "respond-only-explicit":
        return strength == "explicit"
    return strength == "explicit" or (
        strength == "inferred" and accept_mode in {"observe", "auto"}
    )


def _admitted_strength(
    trigger: dict[str, Any], config: dict[str, Any], admission: dict[str, Any] | None
) -> str | None:
    from src.server.services.autonomy.policy import admit

    return admit(trigger, config, admission)


def bounded_json(value: dict[str, Any], *, max_chars: int = ROUTER_PAYLOAD_BUDGET_CHARS) -> str:
    """Serialize valid JSON under budget, trimming prose while preserving IDs."""
    max_chars = int(max_chars)
    if max_chars < 1:
        raise ValueError("JSON budget must be positive")
    payload = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    def encode(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)

    encoded = encode(payload)
    if len(encoded) <= max_chars:
        return encoded

    def collect(node: Any, path: tuple[str, ...] = ()) -> list[tuple[int, Any, str]]:
        values: list[tuple[int, Any, str]] = []
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, str) and child and not (
                    key == "eval_id" or key.endswith("_id") or key in {"thread_key", "id"}
                ):
                    values.append((len(child), node, key))
                else:
                    values.extend(collect(child, (*path, str(key))))
        elif isinstance(node, list):
            for child in node:
                values.extend(collect(child, path))
        return values

    # Shrink the largest non-identifier prose field at each step. Every step
    # makes progress, including one-character strings, and re-encodes valid JSON.
    for _ in range(512):
        encoded = encode(payload)
        if len(encoded) <= max_chars:
            return encoded
        strings = collect(payload)
        if not strings:
            break
        _, node, key = max(strings, key=lambda item: item[0])
        old = node[key]
        new_len = max(0, len(old) - max(1, len(encoded) - max_chars))
        node[key] = old[:new_len]
    # If a candidate envelope still does not fit, retain the paired Eval and
    # Trigger IDs. They are required for the Router tool to make a decision.
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        compact = {
            "kind": payload.get("kind"),
            "eval_ids": payload.get("eval_ids") or [payload.get("eval_id")],
            "candidates": [
                {
                    key: item.get(key)
                    for key in ("eval_id", "trigger_id", "strength", "thread_key")
                    if item.get(key) is not None
                }
                for item in candidates if isinstance(item, dict)
            ],
            "thread_context": "Read with autonomy_read(resource='thread', resource_id=...).",
        }
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= max_chars:
            return encoded
    raise ValueError("JSON budget is too small to preserve required resource IDs")
