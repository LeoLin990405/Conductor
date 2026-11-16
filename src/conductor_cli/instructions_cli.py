"""``conductor instructions`` — preview, render, diff, apply and roll back
shared instruction files."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from instructions_hub import apply as hub_apply
from instructions_hub import core as hub_core
from instructions_hub.core import HubError, resolve_home
from instructions_hub.fragments import load_fragments
from instructions_hub.profiles import load_profiles
from instructions_hub.render import render
from instructions_hub.store import diff_one, resolve_library, status
from instructions_hub.targets import known_targets

from .client import CLIError

_OPERATOR_YES = (
    "writing the operator HOME needs --yes; run without --apply first and review the diff"
)

_OPERATOR_ROLLBACK_YES = (
    "rolling back the operator HOME needs --yes; run `snapshots --operator-home` first"
)


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    hub = subparsers.add_parser(
        "instructions",
        help="preview, render, apply and roll back shared agent instruction files",
    )
    hub.set_defaults(handler=_handle)
    commands = hub.add_subparsers(dest="instructions_command", required=True)

    listing = commands.add_parser("list", help="list fragments, profiles, and targets")
    _home_args(listing)
    listing.add_argument("--library", type=Path)

    st = commands.add_parser("status", help="compare rendered files with a HOME")
    _common(st)
    st.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="CLI id to report (repeatable; default is all ten targets)",
    )

    rendered = commands.add_parser("render", help="print one rendered instruction file")
    _common(rendered)
    rendered.add_argument("--target", required=True, help="CLI id (one of the 10 targets)")

    differed = commands.add_parser("diff", help="show current vs rendered for one CLI")
    _common(differed)
    differed.add_argument("--target", required=True)

    applied = commands.add_parser(
        "apply",
        help="plan (default) or write rendered files, with a snapshot for rollback",
    )
    _common(applied)
    applied.add_argument(
        "--apply",
        action="store_true",
        help="write the files; without it this is a dry run",
    )
    applied.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="CLI id to apply (repeatable; default is all ten targets)",
    )
    applied.add_argument(
        "--take-over",
        action="append",
        dest="take_over",
        default=[],
        metavar="TARGET",
        help="replace this target's unmanaged file (repeatable)",
    )
    applied.add_argument(
        "--yes",
        action="store_true",
        help="confirm writing the operator HOME (needed with --operator-home --apply)",
    )

    rolled = commands.add_parser(
        "rollback", help="restore the files of the newest applied snapshot"
    )
    _home_args(rolled)
    rolled.add_argument("--library", type=Path)
    rolled.add_argument("snapshot_id", nargs="?", help="snapshot id (default: newest applied)")
    rolled.add_argument(
        "--force",
        action="store_true",
        help="restore even files that changed since the apply",
    )
    rolled.add_argument(
        "--yes",
        action="store_true",
        help="confirm writing the operator HOME (needed with --operator-home)",
    )

    snaps = commands.add_parser("snapshots", help="list apply snapshots, newest first")
    _home_args(snaps)


def _home_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--home",
        type=Path,
        help="isolated HOME (never the operator HOME)",
    )
    group.add_argument(
        "--operator-home",
        action="store_true",
        help="the operator's login HOME (apply also needs --yes)",
    )


def _common(parser: argparse.ArgumentParser) -> None:
    _home_args(parser)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--profile", default="operator")


def _target_home(args: argparse.Namespace) -> tuple[Path, bool]:
    """The HOME argument and whether it is the operator HOME."""
    if getattr(args, "operator_home", False):
        return hub_core.operator_home(), True
    return args.home, False


def _handle(args: argparse.Namespace) -> dict[str, Any]:
    command = args.instructions_command
    target_home, operator = _target_home(args)
    try:
        if command == "list":
            # The same library the other three commands read: the one under
            # the isolated HOME unless --library names another.
            home = resolve_home(target_home, operator=operator)
            library = resolve_library(home, args.library, operator=operator)
            fragments = load_fragments(library)
            profiles = load_profiles(library)
            return {
                "fragments": sorted(fragments),
                "profiles": {
                    name: {
                        "fragments": list(profile.fragments),
                        "extra": {key: list(values) for key, values in profile.extra.items()},
                    }
                    for name, profile in profiles.items()
                },
                "targets": known_targets(),
            }
        if command == "status":
            return status(
                target_home,
                profile=args.profile,
                targets=getattr(args, "targets", None),
                library=args.library,
                operator=operator,
            )
        if command == "render":
            # The preview reads the same library status and diff read: the one
            # under the isolated HOME unless --library names another. A
            # preview built from the bundled fragments would show the
            # operator content that the HOME-local library replaces.
            home = resolve_home(target_home, operator=operator)
            library = resolve_library(home, args.library, operator=operator)
            rendered = render(
                args.profile,
                args.target,
                library=library if library.exists() else None,
                home=home,
            )
            return {
                "target": rendered.target,
                "profile": rendered.profile,
                # The path of the file that carries the instructions, the same
                # file status and diff name in their summary.
                "path": str(rendered.content.path),
                "digest": rendered.digest,
                "bytes": rendered.bytes,
                "fragments": list(rendered.fragments),
                "body": rendered.body,
                "files": [
                    {
                        "role": planned.role,
                        "path": str(planned.path),
                        "body": planned.body,
                        "digest": planned.digest,
                        "bytes": planned.bytes,
                    }
                    for planned in rendered.files
                ],
            }
        if command == "diff":
            return diff_one(
                target_home,
                args.profile,
                args.target,
                library=args.library,
                operator=operator,
            )
        if command == "apply":
            options = {
                "profile": args.profile,
                "targets": getattr(args, "targets", None),
                "library": args.library,
                "take_over": list(args.take_over or ()),
                "operator": operator,
            }
            if not args.apply:
                return hub_apply.plan_apply(target_home, **options)
            if operator and not args.yes:
                raise CLIError(_OPERATOR_YES)
            return hub_apply.apply(target_home, **options)
        if command == "rollback":
            if operator and not args.yes:
                raise CLIError(_OPERATOR_ROLLBACK_YES)
            return hub_apply.rollback(
                target_home,
                args.snapshot_id,
                library=args.library,
                force=args.force,
                operator=operator,
            )
        if command == "snapshots":
            return {"snapshots": hub_apply.list_snapshots(target_home, operator=operator)}
    except (HubError, KeyError, ValueError, OSError) as exc:
        raise CLIError(str(exc)) from exc
    raise CLIError(f"unknown instructions command {command!r}")
