"""CLI for the differential harness skeleton.

Usage (from the repository root):

    uv run python -m scripts.refactor_diff.cli validate scripts/refactor_diff/fixtures/F1/*.json
    uv run python -m scripts.refactor_diff.cli compare \
        --old scripts/refactor_diff/fixtures/F2/turn-baseline.json \
        --new scripts/refactor_diff/fixtures/F2/turn-candidate.json

``validate`` is the redaction/schema gate: it is what makes a committed fixture
safe and loadable. ``compare`` prints a Markdown report (also via ``--markdown``)
and exits non-zero on untolerated differences, so ``make refactor-diff`` can gate
a PR.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.refactor_diff.capture import CaptureError, capture_http
from scripts.refactor_diff.compare import compare_fixtures
from scripts.refactor_diff.normalize import (
    RedactionError,
    assert_redacted,
    normalize_fixture,
)
from scripts.refactor_diff.schema import FLOWS, FixtureError, load_fixture


def _load_checked(path: Path):
    fixture = load_fixture(path)
    assert_redacted(fixture)
    return fixture


def cmd_validate(args: argparse.Namespace) -> int:
    failures = 0
    for path in args.fixtures:
        try:
            fixture = _load_checked(Path(path))
        except (FixtureError, RedactionError) as exc:
            print(f"FAIL {path}: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"OK   {path} ({fixture.flow}, synthetic={fixture.synthetic})")
    return 1 if failures else 0


def cmd_compare(args: argparse.Namespace) -> int:
    try:
        old = normalize_fixture(_load_checked(Path(args.old)))
        new = normalize_fixture(_load_checked(Path(args.new)))
    except (FixtureError, RedactionError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    report = compare_fixtures(old, new)
    markdown = report.render_markdown()
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.quiet:
        print(f"{report.flow}: {'PASS' if report.ok else 'FAIL'} "
              f"({len(report.failures)} failures, {len(report.tolerated)} flagged)")
    else:
        print(markdown)
    return 0 if report.ok else 1


def cmd_capture(args: argparse.Namespace) -> int:
    try:
        fixture = capture_http(
            flow=args.flow,
            revision=args.revision,
            base_url=args.base_url,
            requests=args.request,
            app_revision=args.app_revision,
            synthetic=args.synthetic,
            timeout=args.timeout,
        )
    except (CaptureError, FixtureError, RedactionError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    path = Path(args.out)
    fixture.save(path)
    print(
        f"OK   {path} ({fixture.flow}, {len(fixture.http)} exchanges, "
        f"synthetic={fixture.synthetic})"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="refactor-diff", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="schema + redaction check for fixtures")
    validate.add_argument("fixtures", nargs="+")
    validate.set_defaults(func=cmd_validate)

    capture = sub.add_parser("capture", help="capture HTTP exchanges into a fixture")
    capture.add_argument("--flow", required=True, choices=FLOWS)
    capture.add_argument("--revision", required=True)
    capture.add_argument("--base-url", required=True, help="e.g. http://localhost:8000")
    capture.add_argument(
        "--request",
        action="append",
        required=True,
        help="METHOD:/path (repeatable), e.g. GET:/api/v1/oauth/providers",
    )
    capture.add_argument("--out", required=True, help="fixture JSON to write")
    capture.add_argument("--app-revision", default="")
    capture.add_argument(
        "--synthetic",
        action="store_true",
        help="mark the capture synthetic (local test servers); live captures default false",
    )
    capture.add_argument("--timeout", type=float, default=15.0)
    capture.set_defaults(func=cmd_capture)

    compare = sub.add_parser("compare", help="compare two normalized fixtures")
    compare.add_argument("--old", required=True, help="baseline fixture JSON")
    compare.add_argument("--new", required=True, help="candidate fixture JSON")
    compare.add_argument("--markdown", help="write the Markdown report here")
    compare.add_argument("--json", help="write the JSON report here")
    compare.add_argument("--quiet", action="store_true", help="print one summary line")
    compare.set_defaults(func=cmd_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
