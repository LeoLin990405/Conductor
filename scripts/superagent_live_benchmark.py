#!/usr/bin/env python3
"""Real-model timing benchmark for Super-agent.

This submits real manual Tickets through the running Conductor API, using a
native Worker provider (for example codex or claude-code), and measures
submit -> terminal status. Two real arms are compared on the SAME prompts:

  parallel   all requests are submitted at once (Super-agent Tickets)
  sequential requests are submitted and awaited one at a time

It is a live model benchmark, not the synthetic scheduling model in
scripts/superagent_pressure.py. A 2-request pilot is cheap; the article's
300-message / 20-request workload is expensive and not run by default.

Example (local OSS setup):
    .venv/bin/python scripts/superagent_live_benchmark.py \
      --user-id local-dev-user --workspace-id <home-workspace-id> \
      --provider codex --requests 2 --runs 1 --json /tmp/live-bench.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

TERMINAL = {"completed", "failed", "cancelled"}
WAITING = {"waiting", "blocked"}


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * min(1.0, max(0.0, q))
    lo = int(pos)
    hi = min(len(ordered) - 1, lo + 1)
    return ordered[lo] * (1 - (pos - lo)) + ordered[hi] * (pos - lo)


class Client:
    def __init__(self, base_url: str, user_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": self.user_id,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc

    def submit(self, *, prompt: str, title: str, workspace_id: str, provider: str) -> str:
        row = self._request(
            "POST",
            "/api/v1/autonomy/tasks",
            {
                "prompt": prompt,
                "title": title,
                "workspace_id": workspace_id,
                "worker_provider": provider,
            },
        )
        return str(row["ticket_id"])

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")


def wait_terminal(
    client: Client,
    ticket_id: str,
    *,
    timeout: float,
    poll: float,
) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = client.ticket(ticket_id)
        status = str(row.get("status") or "")
        if status in TERMINAL:
            return status, str(row.get("error") or "")
        if status in WAITING:
            return status, str(row.get("summary") or "")
        time.sleep(poll)
    return "timeout", ""


def run_arm(
    client: Client,
    prompts: list[str],
    *,
    workspace_id: str,
    provider: str,
    parallel: bool,
    timeout: float,
    poll: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if not parallel:
        for index, prompt in enumerate(prompts):
            started = time.monotonic()
            ticket_id = client.submit(
                prompt=prompt,
                title=f"live-bench {index}",
                workspace_id=workspace_id,
                provider=provider,
            )
            status, error = wait_terminal(
                client, ticket_id, timeout=timeout, poll=poll
            )
            results.append(
                {
                    "index": index,
                    "ticket_id": ticket_id,
                    "status": status,
                    "error": error,
                    "seconds": round(time.monotonic() - started, 3),
                }
            )
        return results

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
        ticket_ids = list(
            pool.map(
                lambda pair: client.submit(
                    prompt=pair[1],
                    title=f"live-bench {pair[0]}",
                    workspace_id=workspace_id,
                    provider=provider,
                ),
                list(enumerate(prompts)),
            )
        )
    for index, ticket_id in enumerate(ticket_ids):
        status, error = wait_terminal(client, ticket_id, timeout=timeout, poll=poll)
        results.append(
            {
                "index": index,
                "ticket_id": ticket_id,
                "status": status,
                "error": error,
                "seconds": round(time.monotonic() - started, 3),
            }
        )
    return results


def _arm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    good = [row["seconds"] for row in rows if row["status"] == "completed"]
    failed = [row for row in rows if row["status"] != "completed"]
    return {
        "completed": len(good),
        "failed": len(failed),
        "p50_seconds": round(quantile(good, 0.50), 2),
        "p95_seconds": round(quantile(good, 0.95), 2),
        "mean_seconds": round(statistics.fmean(good), 2) if good else 0.0,
        "requests": rows,
    }


def run(
    client: Client,
    *,
    workspace_id: str,
    provider: str,
    requests: int,
    runs: int,
    timeout: float,
    poll: float,
) -> dict[str, Any]:
    prompts = [
        f"Compute {17 + i} * {23 + i} and reply with only the integer result. "
        "Do not use tools and do not modify files."
        for i in range(requests)
    ]
    arms: dict[str, list[dict[str, Any]]] = {"parallel": [], "sequential": []}
    for _ in range(runs):
        for name, parallel in (("parallel", True), ("sequential", False)):
            rows = run_arm(
                client,
                prompts,
                workspace_id=workspace_id,
                provider=provider,
                parallel=parallel,
                timeout=timeout,
                poll=poll,
            )
            arms[name].append(rows)
    summary = {
        name: [_arm_summary(rows) for rows in run_rows]
        for name, run_rows in arms.items()
    }
    return {
        "kind": "live-native-model-benchmark",
        "provider": provider,
        "workspace_id": workspace_id,
        "config": {"requests": requests, "runs": runs, "timeout_seconds": timeout},
        "parallel": {
            "p50_seconds": round(
                statistics.median(row["p50_seconds"] for row in summary["parallel"]), 2
            ),
            "p95_seconds": round(
                statistics.median(row["p95_seconds"] for row in summary["parallel"]), 2
            ),
            "runs": summary["parallel"],
        },
        "sequential": {
            "p50_seconds": round(
                statistics.median(row["p50_seconds"] for row in summary["sequential"]), 2
            ),
            "p95_seconds": round(
                statistics.median(row["p95_seconds"] for row in summary["sequential"]), 2
            ),
            "runs": summary["sequential"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", default="local-dev-user")
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--requests", type=int, default=2)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)
    result = run(
        Client(args.base_url, args.user_id),
        workspace_id=args.workspace_id,
        provider=args.provider,
        requests=args.requests,
        runs=args.runs,
        timeout=args.timeout,
        poll=args.poll,
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        from pathlib import Path

        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
