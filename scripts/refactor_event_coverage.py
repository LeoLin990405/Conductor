"""A-D7-1 read-only event/frame coverage audit for turn and compaction signals.

Usage:
    python -m scripts.refactor_event_coverage            # advisory JSON
    python -m scripts.refactor_event_coverage --strict   # exit 1 on any gap
    python -m scripts.refactor_event_coverage --out /tmp/coverage.json
"""

from __future__ import annotations

import argparse
import json
import sys

from src.server.services.refactor.event_coverage import build_coverage, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the JSON report here")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any requirement is not covered")
    args = parser.parse_args(argv)
    payload = report(build_coverage())
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)
    if args.strict and not payload["strict_ok"]:
        print("event coverage: gaps remain", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
