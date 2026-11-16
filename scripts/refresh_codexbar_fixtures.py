#!/usr/bin/env python3
"""Capture sanitized CodexBar output as test fixtures for the snapshot rules.

Run after CodexBar updates upstream:

    .venv/bin/python scripts/refresh_codexbar_fixtures.py

It writes tests/fixtures/codexbar/<version>/<provider>.json for the providers
whose parsing matters most (codex: plan windows plus credits; cursor: bucket
windows; stepfun: a plain quota provider). Identity fields are dropped and
numbers that could identify an account are replaced, so the files are safe to
commit. Then run the unit tests: a test fails when the fixtures contain an
output key the rules do not know, and the rules plus
VALIDATED_CODEXBAR_VERSION in src/conductor_cli/codexbar_snapshot.py are
updated to match (see docs/CODEXBAR.md).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROVIDERS = ("codex", "cursor", "stepfun")
_DROP = {"diagnostic", "events"}
# Any key whose name contains one of these is dropped, nested or not, so a new
# identity field upstream (accountId, userId, workspaceName, ...) never lands
# in a committed fixture.
_IDENTITY_PARTS = ("account", "user", "email", "org", "workspace", "login", "identity", "name")
# Values replaced with a fixed number so no real balance or spend is committed.
_SCRUB_NUMBERS = {"remaining", "balance", "used", "limit"}
_FIXED = 1000.0


def sanitize(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            lowered = key.lower()
            if key in _DROP or any(part in lowered for part in _IDENTITY_PARTS):
                continue
            if key in _SCRUB_NUMBERS and isinstance(item, (int, float)):
                out[key] = _FIXED
                continue
            out[key] = sanitize(item)
        return out
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str) and "@" in value:
        return "redacted"
    return value


def _version(codexbar: str) -> str:
    out = subprocess.run([codexbar, "--version"], capture_output=True, text=True, check=True)
    first = out.stdout.strip().splitlines()[0]
    if not first.startswith("CodexBar "):
        raise SystemExit(f"unexpected --version output: {first[:40]!r}")
    return first.split(" ", 1)[1].strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codexbar", default="codexbar")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "codexbar",
    )
    args = parser.parse_args(argv)
    version = _version(args.codexbar)
    target = args.root / version
    target.mkdir(parents=True, exist_ok=True)
    for provider in PROVIDERS:
        done = subprocess.run(
            [args.codexbar, "usage", "--provider", provider, "--json-only"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            print(f"{provider}: codexbar exited {done.returncode}", file=sys.stderr)
            continue
        payload = sanitize(json.loads(done.stdout))
        path = target / f"{provider}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
