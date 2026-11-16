#!/usr/bin/env python3
"""Read-only E2E check for the unified PTC orchestration snapshot.

It proves the live backend exposes one worker view for a real thread and that
the shape is consumable by the frontend. It does not start a model turn.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base.rstrip('/')}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--out", default="/tmp/orchestration-e2e-proof.json")
    args = parser.parse_args()

    threads = _get(args.base_url, "/api/v1/threads?limit=5").get("threads") or []
    thread_id = args.thread_id or (threads[0]["thread_id"] if threads else "")
    if not thread_id:
        print("no thread available", file=sys.stderr)
        return 2
    snapshot = _get(args.base_url, f"/api/v1/threads/{thread_id}/orchestration")
    workers = snapshot.get("workers")
    counts = snapshot.get("counts")
    assert isinstance(workers, list), "workers must be a list"
    assert isinstance(counts, dict), "counts must be an object"
    assert {"running", "completed", "failed"} <= set(counts), "counts shape"
    for worker in workers:
        assert {"id", "kind", "provider", "status"} <= set(worker), f"worker shape: {worker}"
    proof = {
        "thread_id": thread_id,
        "swarm_enabled": bool(snapshot.get("swarm_enabled")),
        "workers": len(workers),
        "counts": counts,
        "kinds": sorted({worker["kind"] for worker in workers}),
    }
    Path(args.out).write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(proof, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
