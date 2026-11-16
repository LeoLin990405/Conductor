#!/usr/bin/env python3
"""Run one live Swarm turn against a running Conductor and record the proof.

This is intentionally a client-side check: it never imports the app and never
touches a database. Provide the deployment URL, a workspace id and a model that
has credentials configured; HOST_MODE=oss local stacks need no token.

What counts as proof:
- the send is accepted (HTTP 200) and reaches a terminal event without an
  `error` event;
- at least two distinct subagent task ids (or two Task/RunWorkflow tool calls)
  were observed in the SSE stream.

The prompt itself is not written to the report; only its SHA-256 and length.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import httpx

TASK_ID = re.compile(r"task:([A-Za-z0-9_-]{1,12})")
SUBTASK_KEYS = ("task_id", "agent_id", "subagent_id")


def _walk(value: Any, sink: dict[str, set[str]]) -> None:
    """Collect task ids and Task-like tool names from an event payload."""
    if isinstance(value, str):
        for match in TASK_ID.finditer(value):
            sink["task_ids"].add(match.group(1))
        return
    if isinstance(value, list):
        for item in value:
            _walk(item, sink)
        return
    if not isinstance(value, dict):
        return
    for key in SUBTASK_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,12}", candidate):
            sink["task_ids"].add(candidate)
    for key in ("name", "tool_name"):
        name = value.get(key)
        if isinstance(name, str) and name.lower() in {"task", "runworkflow", "run_workflow"}:
            sink["task_tool_calls"].add(name.lower())
    function = value.get("function")
    if isinstance(function, dict):
        _walk(function, sink)
    for item in value.values():
        _walk(item, sink)


def scan_sse(lines: Iterable[str]) -> dict[str, Any]:
    """Reduce an SSE line stream to the evidence a Swarm run needs."""
    sink: dict[str, set[str]] = {"task_ids": set(), "task_tool_calls": set()}
    event_types: Counter[str] = Counter()
    events = 0
    had_error = False
    terminal = False
    data: list[str] = []
    current_event = "message"

    def flush() -> None:
        nonlocal events, had_error, terminal
        if not data:
            return
        events += 1
        event_types[current_event] += 1
        try:
            payload = json.loads("\n".join(data))
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            _walk(payload, sink)
        if current_event == "error":
            had_error = True
        if current_event in {"turn_ended", "run_ended", "done", "interrupt"}:
            terminal = True

    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            flush()
            data = []
            current_event = "message"
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].lstrip())
    flush()

    work = sink["task_ids"] | sink["task_tool_calls"]
    return {
        "events": events,
        "event_types": dict(sorted(event_types.items())),
        "had_error": had_error,
        "terminal": terminal,
        "task_ids": sorted(sink["task_ids"]),
        "task_tool_calls": sorted(sink["task_tool_calls"]),
        "distinct_work": len(work),
        "proof": (not had_error) and len(work) >= 2,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--workspace-id", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--token", default=None, help="Bearer token; omit on HOST_MODE=oss")
    ap.add_argument(
        "--prompt",
        default="Compare three A-share banks on valuation and recent news. "
        "Split the work into parallel independent research tasks.",
    )
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--out", default="/tmp/swarm-runtime-proof.json")
    args = ap.parse_args(argv)

    prompt = args.prompt
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    body = {
        "workspace_id": args.workspace_id,
        "messages": [{"role": "user", "content": prompt}],
        "swarm": True,
        "llm_model": args.model,
    }
    try:
        with httpx.Client(timeout=args.timeout) as client:
            with client.stream(
                "POST",
                f"{args.base_url.rstrip('/')}/api/v1/threads/messages",
                json=body,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    response.read()
                    print(f"send failed: HTTP {response.status_code}", file=sys.stderr)
                    return 1
                result = scan_sse(response.iter_lines())
    except httpx.HTTPError as exc:
        print(f"transport failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": 1,
        "base_url": args.base_url,
        "workspace_id": args.workspace_id,
        "model": args.model,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_bytes": len(prompt.encode("utf-8")),
        **result,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result["proof"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
