#!/usr/bin/env python3
"""Smoke one native agent CLI adapter and print a cache-accounted result.

Example (from a directory without a project .env, because DSH refuses one that
sets launcher-owned variables like a proxy):

    cd /tmp
    python /path/to/scripts/native_cli_smoke.py --provider deepseek-dsh \
        --prompt 'Reply exactly: ok' --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.llms.native_cli import NativeCliSessionStore, get_adapter, run_with_session


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="deepseek-dsh")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--session-key", default=None)
    parser.add_argument("--session-store", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    adapter = get_adapter(
        args.provider, timeout_seconds=args.timeout, cwd=args.cwd
    )
    if args.session_key:
        store = NativeCliSessionStore(args.session_store)
        result = await run_with_session(
            adapter, args.prompt, session_key=args.session_key, store=store, cwd=args.cwd
        )
    else:
        result = await adapter.run(
            args.prompt, session_id=args.session_id, cwd=args.cwd
        )
    payload = {
        "provider": result.provider,
        "text": result.text,
        "session_id": result.session_id,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cache_read_tokens": result.usage.cache_read_tokens,
            "cache_write_tokens": result.usage.cache_write_tokens,
            "total_tokens": result.usage.total_tokens,
        },
        "cache_read_ratio": round(result.cache_read_ratio, 4),
        "duration_seconds": round(result.duration_seconds, 3),
        "events": result.events,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else result.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
