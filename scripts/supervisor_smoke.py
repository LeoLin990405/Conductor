#!/usr/bin/env python3
"""End-to-end Supervisor smoke: one Ticket, progress and final result only.

The smoke deliberately does not print Worker text or tool names. It creates a
manual Ticket in the Home workspace, follows the activity stream, and reports
the Supervisor-visible phases and the final summary. It requires a local
server and a live Codex daemon; it is not a unit test.

Usage::

    .venv/bin/python scripts/supervisor_smoke.py
    .venv/bin/python scripts/supervisor_smoke.py --timeout 240 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx

PROMPT = "Reply with exactly SUPERVISOR_SMOKE_OK and no other text. Do not use tools."
EXPECTED = "SUPERVISOR_SMOKE_OK"


async def _choose_daemon(client: httpx.AsyncClient) -> str:
    response = await client.get("/api/v1/autonomy/daemons")
    response.raise_for_status()
    rows = response.json().get("daemons") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("provider") == "codex" and row.get("daemon_id"):
            return str(row["daemon_id"])
    raise SystemExit("no registered codex daemon")


async def run(*, base_url: str, user_id: str, timeout: float, as_json: bool) -> int:
    headers = {"X-User-Id": user_id}
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, timeout=30.0
    ) as client:
        workspace = (await client.post("/api/v1/workspaces/home")).json()
        workspace_id = str(workspace.get("workspace_id") or "")
        if not workspace_id:
            raise SystemExit("server did not return a Home workspace")
        daemon_id = await _choose_daemon(client)
        response = await client.post(
            "/api/v1/autonomy/tasks",
            json={
                "prompt": PROMPT,
                "workspace_id": workspace_id,
                "worker_daemon_id": daemon_id,
            },
        )
        response.raise_for_status()
        ticket_id = str(response.json().get("ticket_id") or "")
        if not ticket_id:
            raise SystemExit("server did not return a Ticket id")

        phases: list[str] = []
        summary = ""
        terminal = ""
        path = f"/api/v1/autonomy/tickets/{ticket_id}/activity/stream"
        started = asyncio.get_running_loop().time()
        try:
            async with client.stream(
                "GET", path, headers={"Accept": "text/event-stream"}
            ) as stream:
                stream.raise_for_status()
                event_name = ""
                data: list[str] = []
                async for line in stream.aiter_lines():
                    if asyncio.get_running_loop().time() - started > timeout:
                        raise TimeoutError("smoke timed out")
                    if line.startswith("event:"):
                        event_name = line.partition(":")[2].strip()
                        continue
                    if line.startswith("data:"):
                        data.append(line.partition(":")[2].strip())
                        continue
                    if line:
                        continue
                    if not data:
                        event_name = ""
                        continue
                    try:
                        payload = json.loads("\n".join(data))
                    except json.JSONDecodeError:
                        payload = {}
                    data = []
                    name, event_name = event_name, ""
                    if not isinstance(payload, dict):
                        continue
                    if name == "status":
                        phase = str(payload.get("status") or "")
                        if phase and phase not in phases:
                            phases.append(phase)
                    elif name == "activity":
                        if str(payload.get("kind") or "") == "report":
                            summary = str(payload.get("summary") or "")
                    elif name == "end":
                        terminal = str(payload.get("status") or "")
                        summary = str(payload.get("summary") or summary)
                        break
        except (TimeoutError, httpx.HTTPError) as exc:
            print(f"smoke failed: {exc}", file=sys.stderr)
            return 1

        result = {
            "ticket_id": ticket_id,
            "phases": phases,
            "terminal": terminal,
            "summary": summary,
            "ok": terminal == "completed" and EXPECTED in summary,
        }
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"ticket: {ticket_id}")
            print(f"phases: {' -> '.join(phases)}")
            print(f"terminal: {terminal or 'none'}")
            print(f"summary: {summary or 'none'}")
            print("result:", "OK" if result["ok"] else "FAILED")
        return 0 if result["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", default="local-dev-user")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    return asyncio.run(
        run(
            base_url=args.base_url.rstrip("/"),
            user_id=args.user_id,
            timeout=max(5.0, args.timeout),
            as_json=args.json,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
