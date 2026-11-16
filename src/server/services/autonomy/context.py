"""Bounded Router context snapshots with durable references for spilled rows."""

from __future__ import annotations

import json
from typing import Any

THREAD_CONTEXT_BUDGET_CHARS = 12_000


def _clip(value: Any, chars: int) -> str:
    text = str(value or "")
    return text if len(text) <= chars else text[: chars - 1] + "…"


def _serialized_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _speculative_context(row: dict[str, Any]) -> dict[str, Any]:
    """Inline a recorded speculative outcome when this Ticket row has one.

    The evidence keeps the Worker's bounded closing words, so the Router can
    see what an earlier observe run on this Thread concluded (and whether a
    person approved or discarded it) without another read.
    """
    outcome = (row.get("mandate") or {}).get("speculative_outcome")
    if not isinstance(outcome, dict) or not outcome.get("outcome"):
        return {}
    evidence = outcome.get("evidence")
    evidence_map = evidence if isinstance(evidence, dict) else {}
    recorded = {
        "outcome": _clip(outcome.get("outcome"), 40),
        "recorded_at": _clip(outcome.get("recorded_at"), 40),
        "worker_state": _clip(evidence_map.get("worker_state"), 40),
        "worker_summary": _clip(evidence_map.get("worker_summary"), 300),
    }
    actor = _clip(evidence_map.get("actor"), 40)
    if actor:
        recorded["actor"] = actor
    return {"speculative_outcome": recorded}


def thread_context(
    triggers: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    *,
    max_chars: int = THREAD_CONTEXT_BUDGET_CHARS,
    thread_key: str = "",
) -> dict[str, Any]:
    """Inline compact rows; spill references stay bounded and readable by page."""
    max_chars = int(max_chars)
    result: dict[str, Any] = {
        "budget": {"max_chars": max_chars, "used_chars": 0},
        "thread_key": str(thread_key),
        "triggers": [],
        "tickets": [],
        "spilled": {"triggers": [], "tickets": []},
        "spill_omitted": {"triggers": 0, "tickets": 0},
        "spill": "Full rows are available via autonomy_read(resource='thread', resource_id=thread_key, offset, limit).",
    }

    def seal() -> str:
        for _ in range(4):
            size = _serialized_size(result)
            if result["budget"]["used_chars"] == size:
                break
            result["budget"]["used_chars"] = size
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    encoded = seal()
    if max_chars < len(encoded):
        raise ValueError(
            f"thread context budget {max_chars} is below the minimum envelope "
            f"size {len(encoded)}"
        )

    rows = (
        ("triggers", "trigger_id", [
            {
                "trigger_id": str(row.get("trigger_id") or ""),
                "actor": _clip(row.get("actor_login"), 100),
                "addressed": bool(row.get("addressed")),
                "text": _clip(row.get("text"), 500),
                "text_is_untrusted_source_content": True,
            }
            for row in triggers
        ]),
        ("tickets", "ticket_id", [
            {
                **{
                    "ticket_id": str(row.get("ticket_id") or ""),
                    "title": _clip(row.get("title"), 160),
                    "status": _clip(row.get("status"), 40),
                    "summary": _clip(row.get("summary"), 300),
                },
                # A prior speculative run's recorded conclusion on this Thread:
                # the Router uses it to avoid re-observing settled signals.
                **_speculative_context(row),
            }
            for row in tickets
        ]),
    )
    for bucket, id_key, items in rows:
        for item in items[:8]:
            result[bucket].append(item)
            encoded = seal()
            if len(encoded) <= max_chars:
                continue
            result[bucket].pop()
            row_id = item[id_key]
            spill_bucket = result["spilled"][bucket]
            spill_bucket.append(row_id)
            encoded = seal()
            if len(encoded) > max_chars:
                spill_bucket.pop()
                result["spill_omitted"][bucket] += 1
                encoded = seal()
                if len(encoded) > max_chars:
                    raise ValueError("thread context budget cannot fit spill metadata")
        for item in items[8:]:
            row_id = item[id_key]
            spill_bucket = result["spilled"][bucket]
            spill_bucket.append(row_id)
            encoded = seal()
            if len(encoded) > max_chars:
                spill_bucket.pop()
                result["spill_omitted"][bucket] += 1
                encoded = seal()
                if len(encoded) > max_chars:
                    raise ValueError("thread context budget cannot fit spill metadata")

    result["budget"]["used_chars"] = len(encoded)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) != result["budget"]["used_chars"] or len(encoded) > max_chars:
        raise ValueError("thread context serialization exceeded its character budget")
    return result
