#!/usr/bin/env python3
"""Offline harness eval. Reports are proposals only.

Historical lines are not read from the database. Export them by hand and point
``samples.source`` at the file:

    conductor dispatch status <id> --json
    GET /api/v1/dispatch/{id}/lines

``plan``, and ``run`` without ``--live``, only validate and write ``plan.json``.
``run --live`` also requires ``--confirm-runs`` equal to the planned run count.

Live workers queue on the same per-provider serial lock as live dispatch.
Pass ``--serial-lock-dir`` when this shell's environment differs from the
backend's: the directory must be the one the backend's launcher uses.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Launched as a file, so the repo root is not on sys.path until this line.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _quiet_import_logs() -> None:
    """Keep import-time debug logs off stdout.

    Importing the runner pulls in ``src.tools`` and through it
    ``ptc_agent.core.tool_generator``, which renders an emission probe at
    import to compute ``MCP_CLIENT_CODEGEN_VERSION`` and logs
    ``Generating tool module server=probe`` at debug level. With structlog
    unconfigured those lines land on stdout, mixed into the plan output.
    Only the CLI process is configured: INFO and above, on stderr.
    """
    import logging

    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )


if __name__ == "__main__":
    _quiet_import_logs()

from src.tools.auto_dispatch.harness_eval.runner import execute, regenerate  # noqa: E402

_EXPORT_HELP = """
export historical lines yourself, then set samples.source to that JSON file:
  conductor dispatch status <id> --json
  GET /api/v1/dispatch/{id}/lines
This command does not read the database, the outcome store, or live config.
"""


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Returns the process status code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "report":
        return asyncio.run(regenerate(Path(args.output_dir)))
    live = args.command == "run" and bool(args.live)
    factory = _live_backend_factory if live else None
    confirm = args.confirm_runs if args.command == "run" else None
    purge = bool(args.purge) if args.command == "run" else False
    lock_dir = args.serial_lock_dir if args.command == "run" else None
    return asyncio.run(
        execute(
            Path(args.experiment),
            live=live,
            confirm_runs=confirm,
            purge=purge,
            review_backend_factory=factory,
            serial_lock_dir=Path(lock_dir).expanduser() if lock_dir else None,
        )
    )


def _live_backend_factory(repo: Path):
    """Build the production reviewer. Imported only when a live run calls it."""
    from src.server.services.dispatch import NativeReviewBackend

    return NativeReviewBackend(cwd=repo)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_eval",
        description=(
            "Replay stored dispatch lines under AGENTS.md and skills-list "
            "variants. The report is a proposal and does not change routing."
        ),
        epilog=_EXPORT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser(
        "plan",
        help="validate, select lines, and write plan.json",
        description="Dry-run. Writes plan.json and prints the matrix. Launches nothing.",
        epilog=_EXPORT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    plan.add_argument("experiment", help="path to experiment.json")
    run = sub.add_parser(
        "run",
        help="dry-run, or live when --live and --confirm-runs agree",
        description=(
            "Without --live this is the same as plan. Live mode reserves budget "
            "and launches only after --confirm-runs equals the planned run count."
        ),
        epilog=_EXPORT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("experiment", help="path to experiment.json")
    run.add_argument(
        "--live",
        action="store_true",
        help="launch workers; also requires --confirm-runs equal to the plan",
    )
    run.add_argument(
        "--confirm-runs",
        type=int,
        default=None,
        help="must equal the planned run count or live mode refuses to start",
    )
    run.add_argument(
        "--purge",
        action="store_true",
        help="delete eval repos and worktrees under output_dir after a live run",
    )
    run.add_argument(
        "--serial-lock-dir",
        default=None,
        metavar="DIR",
        help=(
            "per-provider serial lock directory shared with live dispatch. It must "
            "match the backend's launcher environment: its "
            "CONDUCTOR_DISPATCH_SERIAL_LOCK_DIR, else <CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR>/locks, "
            "else ${XDG_STATE_HOME:-$HOME/.local/state}/conductor/dispatch-launch/locks. "
            "Default: derived the same way from this shell's environment, which is "
            "wrong if the backend runs with a different one"
        ),
    )
    report = sub.add_parser(
        "report",
        help="rebuild report.json and report.md from runs.jsonl",
        description="Does not launch workers or reviewers.",
    )
    report.add_argument("output_dir", help="directory that contains plan.json")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
