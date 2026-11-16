#!/usr/bin/env python3
"""Opt-in, single-task Manus research smoke check. Never prints provider output."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="authorize one Manus API task using ~/.config/manus/env",
    )
    options = parser.parse_args()
    if not options.allow_network:
        print("fail 0.0s")
        return 2

    for name in (
        "PYTHON_DOTENV_DISABLED",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
    ):
        os.environ[name] = "1" if name == "PYTHON_DOTENV_DISABLED" else "false"
    logging.disable(logging.CRITICAL)
    started = time.monotonic()

    async def check() -> bool:
        from src.llms.manus import ManusAdapter

        result = await ManusAdapter(
            poll_interval=2.0,
            poll_timeout_seconds=90.0,
            timeout_seconds=30.0,
        ).run("Research the capital city of France. Return a short answer and at least one source URL.")
        text = result.text.lower()
        has_source = "http://" in text or "https://" in text
        return "paris" in text and has_source

    try:
        ok = asyncio.run(check())
    except Exception:
        ok = False
    latency = time.monotonic() - started
    print(f"{'ok' if ok else 'fail'} {latency:.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
