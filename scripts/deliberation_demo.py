#!/usr/bin/env python3
"""Offline sqlite demo of multi-agent deliberation.

Uses local non-interactive CLIs `codex exec`, `kimi -p`, `grok -p`.
Do not point it at the live Postgres. The parent session runs it at most once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Running this file by path puts scripts/ on sys.path; the package is the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.server.services.deliberation.cli_adapter import LocalCliParticipantAdapter
from src.server.services.deliberation.errors import DeliberationError
from src.server.services.deliberation.machine import create_deliberation, start_deliberation
from src.server.services.deliberation.planner_adapter import MergingPlannerAdapter
from src.server.services.deliberation.sqlite_store import SqliteDeliberationStore
from src.server.services.deliberation.thread_port import MemoryThreadPort

_DEFAULT_OBJECTIVE = (
    "Add a pure function add(a, b) that returns the sum of a and b, "
    "and one unit test that checks add(2, 3) == 5. Plan only; do not edit files."
)
_CLI_AGENTS = ("codex", "kimi", "grok")


def _max_rounds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max-rounds must be an integer >= 1") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("max-rounds must be an integer >= 1")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objective", default=_DEFAULT_OBJECTIVE)
    parser.add_argument(
        "--max-rounds",
        type=_max_rounds,
        default=1,
        help="demo default is 1; the service default is 3",
    )
    parser.add_argument(
        "--db",
        default="",
        help="sqlite path (default: /tmp/deliberation-demo-<pid>.sqlite)",
    )
    parser.add_argument(
        "--cwd",
        default="",
        help="working directory for the CLIs (default: a temp dir)",
    )
    return parser.parse_args(argv)


def _one_line(body: object) -> str:
    return " ".join(str(body or "").split())


def _plan_step_keys(plan: object) -> list[str]:
    if not isinstance(plan, dict):
        return []
    keys: list[str] = []
    for step in plan.get("steps") or []:
        if isinstance(step, dict) and step.get("step_key"):
            keys.append(str(step["step_key"]))
    return keys


def _print_transcript(result: dict[str, Any]) -> None:
    for item in result.get("transcript") or []:
        line = (
            f"round {item.get('round_index')} {item.get('agent_key')} "
            f"{item.get('kind')}: {_one_line(item.get('body'))}"
        )
        plan = item.get("plan")
        if plan is not None:
            line += " step_keys: " + ", ".join(_plan_step_keys(plan))
        print(line)
    for row in result.get("dissents") or []:
        print(
            f"dissent {row.get('round_index')} {row.get('agent_key')}: "
            f"{_one_line(row.get('reason'))}"
        )
    print(f"status: {result.get('status')}")
    print(f"decision_reason: {result.get('decision_reason')}")
    print(f"round_index: {result.get('round_index')}")
    dag = [
        {
            "step_key": step.get("step_key"),
            "assigned_agent": step.get("assigned_agent"),
            "depends_on": list(step.get("depends_on") or []),
            "status": step.get("status"),
            "daemon_id": step.get("daemon_id"),
        }
        for step in result.get("steps") or []
    ]
    print("dag:")
    print(json.dumps(dag, indent=2))
    handoff = result.get("handoff") or {}
    dispatched = [
        str(step.get("step_key"))
        for step in handoff.get("dispatched") or []
        if isinstance(step, dict)
    ]
    print("handoff.dispatched: " + ", ".join(dispatched))
    print(f"handoff.parallel: {handoff.get('parallel')}")


def _participants() -> list[dict[str, str]]:
    rows = [
        {"agent_key": agent, "role": "participant", "adapter": "cli"}
        for agent in _CLI_AGENTS
    ]
    rows.append({"agent_key": "planner", "role": "planner", "adapter": "mock"})
    return rows


def _adapters(cwd: str) -> dict[str, Any]:
    adapters: dict[str, Any] = {
        agent: LocalCliParticipantAdapter(agent_key=agent, cwd=cwd, timeout=180)
        for agent in _CLI_AGENTS
    }
    adapters["planner"] = MergingPlannerAdapter()
    return adapters


async def main() -> int:
    args = _parse_args()
    db_path = args.db or f"/tmp/deliberation-demo-{os.getpid()}.sqlite"
    cwd = args.cwd or tempfile.mkdtemp(prefix="deliberation-demo-")
    print(f"sqlite: {db_path}")
    print(f"cwd: {cwd}")
    store = SqliteDeliberationStore(db_path)
    thread_port = MemoryThreadPort()
    try:
        created = await create_deliberation(
            store,
            ticket_id="demo-ticket",
            user_id="demo-user",
            objective=args.objective,
            participants=_participants(),
            max_rounds=args.max_rounds,
            thread_port=thread_port,
        )
        result = await start_deliberation(
            store,
            created["deliberation_id"],
            _adapters(cwd),
            thread_port=thread_port,
        )
    except DeliberationError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    _print_transcript(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
