"""``conductor dispatch`` — start, watch and gate an auto-dispatch run.

The verbs are matched by hand rather than with argparse subparsers, so the
refusal of an unknown verb can name the verb that was probably meant. A run
starts only with the explicit form ``conductor dispatch start "<goal>"`` (N85):
any other first word that is not a verb is refused with a "did you mean"
hint, never taken as a goal. (The old bare form ``conductor dispatch
"<goal>"`` started a real dispatch for typos such as ``dispatch show <id>`` or
``dispatch list``, so it is gone, quoted or not.) ``start`` also refuses a
one-word goal and a goal that opens with a verb-like word (``start list``).
``status`` and ``active`` are reads; ``start`` and the
other verbs carry an operator decision and are the only writes: approve,
reject, cancel or abort a dispatch (``abort`` ends one in any live state),
``deliver`` an approved one (push and open its PR), reassign a blocked line's model, unblock a wedged
line so it can be reassigned, adjudicate a reviewer scope conflict, or
``recover`` a parked line (N76: run its next recovery step now, ``--split`` it
off the round, or hand it to a person with ``--human``; N87: ``--rereview``
says the operator fixed the branch, so G2 and review run again on its head).
``cost`` is a read (N30): ``cost <id>`` for one dispatch's cost per line and
model, ``cost [--limit N]`` for the by-model and waste-by-class tables over the
latest N.
``--note`` goes with any write; ``--force`` (stop a running line first) only
with reject, cancel and abort; ``--split`` / ``--human`` / ``--rereview`` only
with recover. ``--workers`` restricts a new run to the named workers.
``--workspace`` names a registered dispatch workspace
(``conductor workspace add|list|show|remove``): the server resolves it and
fetches its remote before the run starts.

A server that does not implement the dispatch API yet answers 404/405/501: the
start and status reads then fall back to a local fake record, so the command
still works while the endpoint is absent. A 5xx never falls back — a failing
server must not look like a feature that is merely missing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import APIError, APIClient, CLIError, ServerError, sanitize_terminal
from .dispatch_timeline import render_timeline_lines
from .review_evidence_status import review_evidence_hints, review_evidence_lines

DISPATCH_PATH = "/api/v1/dispatch"
WORKSPACES_PATH = "/api/v1/dispatch-workspaces"
_ABSENT_STATUSES = (404, 405, 501)

VERBS = (
    "start",
    "status",
    "active",
    "approve",
    "reject",
    "cancel",
    "reassign",
    "unblock",
    "adjudicate",
    "recover",
    "abort",
    "deliver",
    "cost",
    "assignments",
)

ADJUDICATION_RULINGS = ("in-scope", "out-of-scope")
_NOTE_VERBS = frozenset(
    {"approve", "reject", "cancel", "abort", "unblock", "adjudicate", "recover"}
)
_FORCE_VERBS = frozenset({"reject", "cancel", "abort"})
_SCOPE_CONFLICTS = frozenset({"reviewer_conflict_scope", "reviewer_conflict_security"})
_REASSIGNABLE_BLOCKS = frozenset({"no_new_commit", "uncommitted_changes"})
#: Words people type for a read that the CLI spells differently (N85). Each
#: maps to the verb the refusal suggests.
_VERB_ALIASES = {
    "show": "status <id>",
    "get": "status <id>",
    "info": "status <id>",
    "view": "status <id>",
    "describe": "status <id>",
    "inspect": "status <id>",
    "watch": "status <id>",
    "list": "active",
    "ls": "active",
    "ps": "active",
    "running": "active",
    "help": "--help",
    "stop": "cancel <id>",
    "kill": "abort <id>",
    "retry": "recover <id> <line-id>",
    "resume": "recover <id> <line-id>",
}
#: A ``start`` goal that opens with one of these words is a mistyped verb.
_VERB_LIKE = frozenset(_VERB_ALIASES) | frozenset(
    {"status", "active", "approve", "reject", "cancel", "reassign", "unblock",
     "adjudicate", "recover", "abort", "deliver", "start"}
)
START_FORM = 'conductor dispatch start "<goal>"'

USAGE = (
    'conductor dispatch start "<goal>" [--workspace ID|NAME] [--workers W,W] | '
    "status <id> | active | "
    "approve <id> [--note T] | reject <id> [--note T] [--force] | "
    "cancel <id> [--note T] [--force] | abort <id> [--note T] [--force] | "
    "deliver <id> | reassign <id> <line-id> <model> | "
    "unblock <id> <line-id> [--note T] | "
    "adjudicate <id> <line-id> in-scope|out-of-scope [--note T] | "
    "recover <id> [<line-id>] [--split|--human|--rereview] [--note T] | "
    "cost [<id>] [--limit N] | assignments <id>"
)
#: N30: ``cost --limit N`` default and the server's bound.
COST_DEFAULT_LIMIT = 50
COST_MAX_LIMIT = 500
COST_UNSUPPORTED = "cost report not supported by this server"


class DispatchUsageError(CLIError, ValueError):
    """Bad dispatch invocation; both the CLI and the verb router reject it."""


def register_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the dispatch command group on the shared parser."""
    dispatch = subparsers.add_parser(
        "dispatch",
        help="run a goal as parallel auto-dispatched work lines, or show status",
    )
    dispatch.add_argument(
        "args",
        nargs="*",
        metavar="verb",
        help=USAGE
        + " (only 'start' begins a run; an unknown verb is refused, never "
        "taken as a goal; a fake local record is kept if the API is absent)",
    )
    dispatch.add_argument(
        "--note",
        default=None,
        help="short note stored with approve/reject/cancel/unblock/adjudicate",
    )
    dispatch.add_argument(
        "--force",
        action="store_true",
        help="reject/cancel only: stop a running line first instead of refusing; "
        "start --source-ref: start even if a dispatch for that ref is active",
    )
    dispatch.add_argument(
        "--split",
        action="store_true",
        help="recover only: split the line (and its dependants) off this round",
    )
    dispatch.add_argument(
        "--human",
        action="store_true",
        help="recover only: hand the line to a person and stop automatic steps",
    )
    dispatch.add_argument(
        "--rereview",
        action="store_true",
        help="recover only: the operator fixed the branch; run G2 and review "
        "again on its current head",
    )
    dispatch.add_argument(
        "--workspace",
        default=None,
        metavar="ID|NAME",
        help="start only: run in a registered dispatch workspace (fetched before start)",
    )
    dispatch.add_argument(
        "--workers",
        default=None,
        metavar="W,W",
        help="start only: the workers this dispatch may use (comma-separated)",
    )
    dispatch.add_argument(
        "--source-ref",
        default=None,
        metavar="REF",
        help="start only: the ticket or issue this dispatch is for (#59, "
        "owner/repo#59, ticket:ID); with --force, start even if one is active",
    )
    dispatch.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=f"cost only: how many recent dispatches to sum (1..{COST_MAX_LIMIT}, "
        f"default {COST_DEFAULT_LIMIT})",
    )
    dispatch.set_defaults(handler=_dispatch)


WORKSPACE_USAGE = (
    "conductor workspace add <path> [--name N] [--remote URL] "
    "[--sandbox-policy P] [--default-worker W] [--fetch] | "
    "list | show <id|name> | remove <id|name>"
)


def register_workspace_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``conductor workspace``: the dispatch checkout registry."""
    workspace = subparsers.add_parser(
        "workspace",
        help="register and inspect the checkouts auto-dispatch may run against",
        description=WORKSPACE_USAGE,
    )
    verbs = workspace.add_subparsers(dest="workspace_command", required=True)

    add = verbs.add_parser("add", help="register a checkout by path")
    add.add_argument("path", help="repo root of the checkout")
    add.add_argument(
        "--name",
        default=None,
        help="registry name (default: the path's directory name)",
    )
    add.add_argument(
        "--remote",
        dest="git_remote",
        default=None,
        help="git remote to fetch (default: the checkout's origin)",
    )
    add.add_argument(
        "--sandbox-policy",
        default="",
        help="sandbox policy the workspace's workers inherit "
        "(workspace-write; read-only is refused for a write worker)",
    )
    add.add_argument(
        "--default-worker",
        default="",
        help="worker provider a dispatch starts with by default",
    )
    add.add_argument(
        "--fetch",
        action="store_true",
        help="run git fetch after registering",
    )
    add.add_argument("--json", action="store_true", help="print the result as JSON")
    add.set_defaults(handler=_workspace_add)

    listing = verbs.add_parser("list", help="list registered checkouts")
    listing.add_argument("--json", action="store_true", help="print the result as JSON")
    listing.set_defaults(handler=_workspace_list)

    show = verbs.add_parser("show", help="show one checkout by id or name")
    show.add_argument("workspace", help="workspace id or name")
    show.add_argument("--json", action="store_true", help="print the result as JSON")
    show.set_defaults(handler=_workspace_show)

    remove = verbs.add_parser("remove", help="remove a checkout row")
    remove.add_argument("workspace", help="workspace id or name")
    remove.add_argument("--json", action="store_true", help="print the result as JSON")
    remove.set_defaults(handler=_workspace_remove)


def _workspace_path(workspace: str) -> str:
    return f"{WORKSPACES_PATH}/{workspace}"


def _workspace_add(args: argparse.Namespace, client: APIClient) -> Any:
    path = str(args.path or "").strip()
    if not path:
        raise DispatchUsageError("workspace add needs a checkout path")
    name = str(args.name or "").strip() or Path(path.rstrip("/")).name
    if not name:
        raise DispatchUsageError("workspace add could not derive a name; pass --name")
    body: dict[str, Any] = {
        "name": name,
        "path": path,
        "sandbox_policy": getattr(args, "sandbox_policy", "") or "",
        "default_worker": getattr(args, "default_worker", "") or "",
    }
    if getattr(args, "git_remote", None) is not None:
        body["git_remote"] = args.git_remote
    created = client.post(WORKSPACES_PATH, body)
    if not isinstance(created, dict):
        raise CLIError("Workspace API returned an unexpected payload")
    if getattr(args, "fetch", False):
        workspace_id = str(created.get("workspace_id") or "")
        if workspace_id:
            created = {
                "workspace": created,
                "fetch": client.post(f"{_workspace_path(workspace_id)}/fetch"),
            }
    return created


def _workspace_list(args: argparse.Namespace, client: APIClient) -> Any:
    del args
    payload = client.get(WORKSPACES_PATH)
    if isinstance(payload, list):
        return {"workspaces": payload}
    if not isinstance(payload, dict):
        raise CLIError("Workspace API returned an unexpected payload")
    return payload


def _workspace_show(args: argparse.Namespace, client: APIClient) -> Any:
    workspace = str(args.workspace or "").strip()
    if not workspace:
        raise DispatchUsageError("workspace show needs an id or name")
    payload = client.get(_workspace_path(workspace))
    if not isinstance(payload, dict):
        raise CLIError("Workspace API returned an unexpected payload")
    return payload


def _workspace_remove(args: argparse.Namespace, client: APIClient) -> Any:
    workspace = str(args.workspace or "").strip()
    if not workspace:
        raise DispatchUsageError("workspace remove needs an id or name")
    payload = client.delete(_workspace_path(workspace))
    if payload is None:
        return {"workspace": workspace, "deleted": True}
    if not isinstance(payload, dict):
        raise CLIError("Workspace API returned an unexpected payload")
    return payload


def _words(args: argparse.Namespace) -> list[str]:
    """The verb/goal words, from the parser's list or a legacy target pair."""
    words = [word for word in (getattr(args, "args", None) or []) if word]
    if words:
        return words
    target = getattr(args, "target", None)
    if not target:
        return []
    pair = [target]
    dispatch_id = getattr(args, "dispatch_id", None)
    if dispatch_id:
        pair.append(dispatch_id)
    return pair


def unknown_verb_message(word: str) -> str:
    """The refusal for a first word that is not a verb (N85), with a hint."""
    import difflib

    shown = sanitize_terminal(str(word))[:40]
    key = str(word).strip().lower()
    hint = _VERB_ALIASES.get(key)
    if hint is None:
        close = difflib.get_close_matches(key, VERBS, n=1, cutoff=0.6)
        hint = close[0] if close else None
    text = f"unknown dispatch verb {shown!r}"
    if hint:
        text += f"; did you mean 'conductor dispatch {hint}'?"
    return (
        f"{text} Nothing was started. To start a run use {START_FORM}; "
        f"the verbs are {', '.join(VERBS)}."
    )


def start_goal_refusal(words: list[str]) -> str | None:
    """Why ``dispatch start <words>`` must not start a run, or None (N85).

    A goal is the joined words. An empty goal, a one-word goal, and a goal
    whose first word is a verb or a verb-like read (``start list``, ``start
    show d1``) are refused: those are mistyped commands, not work.
    """
    goal = " ".join(words).strip()
    if not goal:
        return f"dispatch start needs a goal: {START_FORM}"
    parts = goal.split()
    if parts[0].lower() in _VERB_LIKE:
        return (
            f"dispatch start goal begins with the verb-like word {sanitize_terminal(parts[0])[:40]!r}; "
            "nothing was started. Use the verb itself, or quote a goal that "
            "describes the work"
        )
    if len(parts) < 2:
        return (
            f"dispatch start goal {sanitize_terminal(goal)[:40]!r} is a single word; "
            "nothing was started. Describe the work in a few words"
        )
    return None


def _dispatch(args: argparse.Namespace, client: APIClient) -> Any:
    words = _words(args)
    if not words:
        raise DispatchUsageError(f"pass a verb: {USAGE}")
    verb, rest = words[0], words[1:]
    if verb not in VERBS:
        # N85: an unknown word is never a goal. Refuse before any flag check
        # so the hint is what the operator reads.
        raise DispatchUsageError(unknown_verb_message(verb))
    note = getattr(args, "note", None)
    force = bool(getattr(args, "force", False))
    workspace = getattr(args, "workspace", None)
    named = verb
    if note is not None and named not in _NOTE_VERBS:
        raise DispatchUsageError(
            f"--note goes with {', '.join(sorted(_NOTE_VERBS))}, not dispatch {named}"
        )
    source_ref = getattr(args, "source_ref", None)
    if source_ref is not None and verb != "start":
        raise DispatchUsageError(
            f"--source-ref goes with dispatch start, not dispatch {verb}"
        )
    if force and named not in _FORCE_VERBS and not (named == "start" and source_ref):
        raise DispatchUsageError(
            f"--force goes with reject or cancel, not dispatch {named}"
        )
    split = bool(getattr(args, "split", False))
    human = bool(getattr(args, "human", False))
    rereview = bool(getattr(args, "rereview", False))
    chosen = [
        flag
        for flag, on in (("--split", split), ("--human", human), ("--rereview", rereview))
        if on
    ]
    if chosen and named != "recover":
        raise DispatchUsageError(f"{chosen[0]} goes with recover, not dispatch {named}")
    if len(chosen) > 1:
        raise DispatchUsageError(
            "dispatch recover takes one of --split, --human or --rereview, not both"
        )
    if workspace and verb != "start":
        raise DispatchUsageError(
            f"--workspace goes with dispatch start, not dispatch {verb}"
        )
    workers = _workers(getattr(args, "workers", None))
    if workers and verb != "start":
        raise DispatchUsageError(
            f"--workers goes with dispatch start, not dispatch {verb}"
        )
    limit = getattr(args, "limit", None)
    if limit is not None and named != "cost":
        raise DispatchUsageError(f"--limit goes with cost, not dispatch {named}")
    if verb == "start":
        refused = start_goal_refusal(rest)
        if refused:
            raise DispatchUsageError(refused)
        return _start(
            " ".join(rest).strip(), client, workspace=workspace, workers=workers,
            source_ref=source_ref, force=force,
        )
    as_json = bool(getattr(args, "json", False))
    if verb == "status":
        # A bare ``status`` is the active list, mirroring ``active``; one id
        # reads that dispatch. Both spellings are in the help text.
        if not rest:
            return _active(client, as_json=as_json)
        return _status(_one(rest, "status"), client, as_json=as_json)
    if verb == "active":
        return _active(client, as_json=as_json)
    if verb == "cost":
        return _cost(rest, client, limit=limit, as_json=as_json)
    if verb == "assignments":
        ident = _one(rest, "assignments")
        _validate_id(ident)
        payload = client.get(f"{DISPATCH_PATH}/{ident}/assignments")
        return _present(payload, as_json=as_json, renderer=render_assignments)
    if verb == "approve":
        payload = client.post(
            f"{DISPATCH_PATH}/{_one(rest, 'approve')}/approve", _body(note=note)
        )
        # --json keeps the server object; the human view is a summary, never
        # a repr of the line dicts.
        return _present(payload, as_json=as_json, renderer=render_approve)
    if verb == "deliver":
        payload = client.post(f"{DISPATCH_PATH}/{_one(rest, 'deliver')}/deliver", {})
        return _present(payload, as_json=as_json, renderer=render_status)
    if verb in ("reject", "cancel", "abort"):
        payload = client.post(
            f"{DISPATCH_PATH}/{_one(rest, verb)}/{verb}", _body(note=note, force=force)
        )
        return _present(payload, as_json=as_json, renderer=render_status)
    if verb == "unblock":
        dispatch_id, line_id = _two(rest, "unblock")
        payload = client.post(
            f"{DISPATCH_PATH}/{dispatch_id}/lines/{line_id}/unblock", _body(note=note)
        )
        return _present(payload, as_json=as_json, renderer=render_status)
    if verb == "recover":
        mode = (
            "split" if split else "human" if human else "rereview" if rereview else "next"
        )
        return _recover(rest, client, mode=mode, note=note, as_json=as_json)
    if verb == "adjudicate":
        dispatch_id, line_id, ruling = _adjudication(rest)
        body = {"ruling": ruling, **_body(note=note)}
        payload = client.post(
            f"{DISPATCH_PATH}/{dispatch_id}/lines/{line_id}/adjudicate", body
        )
        return _present(payload, as_json=as_json, renderer=render_status)
    dispatch_id, line_id, model = _three(rest)
    payload = client.post(
        f"{DISPATCH_PATH}/{dispatch_id}/lines/{line_id}/reassign", {"model": model}
    )
    # A reassign during a drive is queued, not applied: say so.
    return _present(payload, as_json=as_json, renderer=render_status)


def _cost(
    rest: list[str], client: APIClient, *, limit: int | None, as_json: bool
) -> Any:
    """``cost <id>`` or ``cost [--limit N]`` (N30).

    No local fallback: a server without the endpoint is told apart from a
    dispatch that cost nothing, so a 404/405/501 is an error, never a fake.
    """
    if len(rest) > 1 or (rest and not rest[0]):
        raise DispatchUsageError("dispatch cost takes [<dispatch-id>] [--limit N]")
    if rest and limit is not None:
        raise DispatchUsageError("dispatch cost takes <dispatch-id> or --limit N, not both")
    if rest:
        path = f"{DISPATCH_PATH}/{_validate_id(rest[0])}/cost"
        params = None
        renderer = render_cost
    else:
        value = COST_DEFAULT_LIMIT if limit is None else limit
        if not 1 <= value <= COST_MAX_LIMIT:
            raise DispatchUsageError(f"--limit must be 1..{COST_MAX_LIMIT}")
        path = f"{DISPATCH_PATH}/cost"
        params = {"limit": value}
        renderer = render_cost_aggregate
    try:
        payload = client.get(path, params=params)
    except (APIError, ServerError) as exc:
        if getattr(exc, "status_code", None) not in _ABSENT_STATUSES:
            raise
        if rest and "not found" in str(exc).lower() and "dispatch" in str(exc).lower():
            # The route exists and the id does not: say that, not "unsupported".
            raise CLIError(f"dispatch {rest[0]} not found") from exc
        raise CLIError(COST_UNSUPPORTED) from exc
    if not isinstance(payload, dict):
        raise CLIError("Dispatch API returned an unexpected payload")
    return _present(payload, as_json=as_json, renderer=renderer)


def _cost_secs(value: Any, *, missing: str = "-") -> str:
    """``18m02s`` / ``1h05m`` / ``42s``; ``missing`` when unknown."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        return missing
    total = int(round(value))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _cost_tokens(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{round(value / 1000)}k"
    return str(int(value))


def _cost_ratio(value: Any, *, percent: bool = False) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "n/a"
    return f"{value * 100:.0f}%" if percent else f"{value:.2f}"


def _cost_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Columns padded to their widest cell, two spaces apart, like status."""
    cells = [header, *rows]
    widths = [max(len(row[index]) for row in cells) for index in range(len(header))]
    return [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in cells
    ]


def _cost_model_lines(payload: dict[str, Any]) -> list[str]:
    rows = [
        [
            _clip(row.get("model"), 24) or "-",
            str(row.get("attempts", 0)),
            _cost_secs(row.get("spent_wall_secs")),
            str(row.get("merged", 0)),
            _cost_secs(row.get("cost_per_merged_line_wall_secs"), missing="n/a"),
            _cost_ratio(row.get("merge_yield")),
            _cost_ratio(row.get("waste_share"), percent=True),
        ]
        for row in payload.get("by_model") or []
        if isinstance(row, dict)
    ]
    return _cost_table(
        ["by model", "attempts", "spent(wall)", "merged", "cost/merged", "yield", "waste%"],
        rows,
    )


def _cost_waste_lines(payload: dict[str, Any]) -> list[str]:
    rows = []
    for row in payload.get("waste_by_class") or []:
        if not isinstance(row, dict):
            continue
        models = row.get("by_model") if isinstance(row.get("by_model"), dict) else {}
        name = _clip(row.get("class"), 32) or "unknown"
        if row.get("infrastructure"):
            name += " (infra)"
        rows.append(
            [
                name,
                str(row.get("attempts", 0)),
                _cost_secs(row.get("wall_secs")),
                ",".join(_clip(model, 24) for model in sorted(models)) or "-",
            ]
        )
    return _cost_table(["waste by class", "attempts", "wall", "models"], rows)


def _cost_waste_cell(line: dict[str, Any]) -> str:
    parts = []
    for item in line.get("waste") or []:
        if not isinstance(item, dict):
            continue
        text = f"{_clip(item.get('model'), 24)} {_clip(item.get('class'), 32)}"
        if int(item.get("attempts") or 0) > 1:
            text += f" x{item.get('attempts')}"
        if isinstance(item.get("wall_secs"), (int, float)):
            text += f" ({_cost_secs(item.get('wall_secs'))})"
        parts.append(text.strip())
    return "; ".join(parts) or "-"


def render_cost(report: dict[str, Any]) -> list[str]:
    """``conductor dispatch cost <id>``: per line, per model, waste by class."""
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    head = (
        f"dispatch {_clip(report.get('dispatch_id'), 12)}  state {_clip(report.get('state'), 24)}"
        f"  basis {_clip(report.get('basis'), 12)}  lines {counts.get('lines', 0)}"
        f"  merged {counts.get('merged', 0)}  passed {counts.get('passed', 0)}"
        f"  blocked {counts.get('blocked', 0)}"
        f"  failed {counts.get('failed', 0)}"
    )
    rows = [
        [
            _clip(line.get("line_id"), 24),
            _clip(line.get("outcome"), 12),
            _clip(line.get("author"), 24) or "-",
            str(line.get("attempts", 0)),
            str(line.get("fix_rounds", 0)),
            _cost_secs(line.get("wall_secs")),
            _cost_tokens(line.get("tokens_cli")),
            _cost_tokens(line.get("tokens_est")),
            _cost_waste_cell(line),
        ]
        for line in report.get("lines") or []
        if isinstance(line, dict)
    ]
    return [
        head,
        *_cost_table(
            ["line", "outcome", "author", "attempts", "fix", "wall", "tok(cli)", "tok(est)", "waste"],
            rows,
        ),
        *_cost_model_lines(report),
        *_cost_waste_lines(report),
        f"quota: {_clip(report.get('quota_note'), 80)}",
    ]


def render_cost_aggregate(report: dict[str, Any]) -> list[str]:
    """``conductor dispatch cost [--limit N]``: by model and waste by class only."""
    basis = report.get("basis") if isinstance(report.get("basis"), dict) else {}
    head = (
        f"cost over {report.get('dispatches', 0)} dispatches  limit {report.get('limit')}"
        f"  ledger {basis.get('ledger', 0)}  legacy {basis.get('legacy', 0)}"
    )
    return [
        head,
        *_cost_model_lines(report),
        *_cost_waste_lines(report),
        f"quota: {_clip(report.get('quota_note'), 80)}",
    ]


def _recover(
    rest: list[str],
    client: APIClient,
    *,
    mode: str,
    note: str | None,
    as_json: bool,
) -> Any:
    """``recover <id> <line>``, or every parked line of ``<id>`` when no line is named.

    A refusal for one line of the whole-dispatch form is reported next to
    that line and the others still run; a single named line raises as the
    other verbs do.
    """
    if len(rest) not in (1, 2) or not all(rest):
        raise DispatchUsageError("dispatch recover takes <dispatch-id> [<line-id>]")
    dispatch_id = rest[0]
    _validate_id(dispatch_id)
    body = {"mode": mode, **_body(note=note)}
    if len(rest) == 2:
        payload = client.post(
            f"{DISPATCH_PATH}/{dispatch_id}/lines/{rest[1]}/recover", body
        )
        if isinstance(payload, dict) and not as_json:
            presented = _present(payload, as_json=False, renderer=render_status)
            presented["rendered"] = [
                *_recover_result_lines(payload, rest[1], mode),
                "",
                *presented["rendered"],
            ]
            return presented
        return payload
    if mode != "next":
        raise DispatchUsageError(f"dispatch recover --{mode} takes <dispatch-id> <line-id>")
    summary = client.get(f"{DISPATCH_PATH}/{dispatch_id}")
    parked = [
        _line_id(row)
        for row in _record_lines(summary if isinstance(summary, dict) else {})
        if (_clip(row.get("status"), 40) or "").upper() == "BLOCKED" and _line_id(row)
    ]
    results: list[dict[str, Any]] = []
    payload: Any = summary
    for line_id in parked:
        try:
            payload = client.post(
                f"{DISPATCH_PATH}/{dispatch_id}/lines/{line_id}/recover", body
            )
            results.append({"line_id": line_id, "result": "requested"})
        except APIError as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            results.append({"line_id": line_id, "result": "refused", "reason": str(exc)})
    if as_json:
        return {"dispatch": payload, "recover": results}
    rendered = [f"recover {dispatch_id}: {len(parked)} parked line(s)"]
    if not parked:
        rendered.append("  nothing is parked")
    for item in results:
        text = f"  {item['line_id']}: {item['result']}"
        if item.get("reason"):
            text += f" ({_clip(item['reason'], 160)})"
        rendered.append(text)
    view = _present(payload, as_json=False, renderer=render_status) if isinstance(payload, dict) else {}
    presented = dict(view) if isinstance(view, dict) else {}
    presented["recover"] = results
    presented["rendered"] = [*rendered, "", *(view.get("rendered", []) if isinstance(view, dict) else [])]
    return presented


def _recover_result_lines(summary: dict[str, Any], line_id: str, mode: str) -> list[str]:
    entry = _liveness_entry(summary, line_id)
    head = f"recover {line_id}: {mode} requested"
    if mode == "human":
        return [f"{head}; a person owns the line now, no automatic step runs"]
    if mode == "rereview":
        return [f"{head}; G2 and review run again on the branch head"]
    if not entry:
        return [f"{head}; the drive runs it next"]
    return [
        f"{head}; the drive runs it next",
        f"  budget_left {entry.get('budget_left', '—')}",
    ]


def _body(*, note: str | None = None, force: bool = False) -> dict[str, Any]:
    """The decision body: a note and force are sent only when the operator gave them.

    The server's defaults are an empty note and no force, so an omitted key
    means the same as the default and a plain ``approve``/``reject`` still
    posts ``{}``.
    """
    body: dict[str, Any] = {}
    if note:
        body["note"] = note
    if force:
        body["force"] = True
    return body


def _workers(raw: str | None) -> list[str]:
    """``--workers kimi,glm`` as a list; blanks and repeats dropped."""
    if raw is None:
        return []
    names: list[str] = []
    for item in str(raw).split(","):
        name = item.strip().lower()
        if name and name not in names:
            names.append(name)
    if not names:
        raise DispatchUsageError("--workers needs at least one worker name")
    return names


def _start(
    goal: str,
    client: APIClient,
    *,
    workspace: str | None = None,
    workers: list[str] | None = None,
    source_ref: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"goal": goal}
    if workspace:
        body["workspace"] = workspace
    if workers:
        body["workers"] = list(workers)
    if source_ref:
        body["source_ref"] = source_ref
        if force:
            body["force"] = True
    try:
        created = client.post(DISPATCH_PATH, body)
    except (APIError, ServerError) as exc:
        if getattr(exc, "status_code", None) not in _ABSENT_STATUSES:
            raise
        if workspace or workers:
            # A fake local row is not a dispatch in the selected workspace.
            # A 404 here is the server refusing to resolve that workspace (or
            # an older server with no dispatch route at all); either way the
            # operator must hear it instead of a successful-looking fake.
            where = f"in workspace {workspace!r}" if workspace else "with --workers"
            raise CLIError(f"cannot start {where}: {exc}") from exc
        return _create_fake(goal)
    if not isinstance(created, dict):
        raise CLIError("Dispatch API returned an unexpected payload")
    return created


def _status(dispatch_id: str, client: APIClient, *, as_json: bool = False) -> Any:
    # Validate before the URL is built: ``..`` would otherwise be normalized
    # by the HTTP client into a different path.
    _validate_id(dispatch_id)
    try:
        payload = client.get(f"{DISPATCH_PATH}/{dispatch_id}")
    except (APIError, ServerError) as exc:
        if getattr(exc, "status_code", None) not in _ABSENT_STATUSES:
            raise
        payload = _read_fake(dispatch_id)
    return _present(payload, as_json=as_json, renderer=render_status)


def _active(client: APIClient, *, as_json: bool = False) -> Any:
    try:
        active = client.get(f"{DISPATCH_PATH}/status/active")
    except (APIError, ServerError) as exc:
        if getattr(exc, "status_code", None) not in _ABSENT_STATUSES:
            raise
        active = {"active": _read_fake_active(), "fake": True}
    if isinstance(active, list):
        active = {"active": active}
    elif not isinstance(active, dict):
        raise CLIError("Dispatch API returned an unexpected payload")
    return _present(active, as_json=as_json, renderer=render_active)


def _present(payload: Any, *, as_json: bool, renderer: Any) -> Any:
    """Keep the server object and add the human lines under ``rendered``.

    ``--json`` returns the server object unchanged, including its key order.
    The CLI prints ``rendered``. ``output.py`` is unchanged.
    An active payload has no ``lines`` key, so the string view is also
    stored there for the existing active-list assertion.
    """
    if as_json or not isinstance(payload, dict):
        return payload
    view = renderer(payload)
    presented = dict(payload)
    presented["rendered"] = view
    if "lines" not in presented:
        presented["lines"] = view
    return presented


def _clip(value: Any, limit: int) -> str:
    """First line only, control characters removed, then truncated."""
    if value is None:
        return ""
    text = sanitize_terminal(str(value)).replace("\r", " ").split("\n", 1)[0].strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _record_lines(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = summary.get("lines")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _line_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("name") or "")


def _depends_on(row: dict[str, Any]) -> list[str]:
    raw = row.get("depends_on")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def dag_layers(summary: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Layer lines and warnings. Cycles and missing deps do not raise."""
    rows = _record_lines(summary)
    order: list[str] = []
    deps: dict[str, list[str]] = {}
    for row in rows:
        ident = _line_id(row)
        if not ident or ident in deps:
            continue
        order.append(ident)
        deps[ident] = _depends_on(row)
    known = set(order)
    warnings: list[str] = []
    for ident in order:
        missing = [item for item in deps[ident] if item not in known]
        if missing:
            warnings.append(f"⚠ missing dep {ident} ← {', '.join(missing)}")
    remaining = list(order)
    layers: list[list[str]] = []
    while remaining:
        ready = [
            ident
            for ident in remaining
            if all(item not in remaining for item in deps[ident] if item in known)
        ]
        if not ready:
            warnings.append("⚠ cycle " + ", ".join(remaining))
            layers.append(list(remaining))
            break
        layers.append(ready)
        remaining = [ident for ident in remaining if ident not in set(ready)]
    rendered: list[str] = []
    for index, layer in enumerate(layers):
        labels: list[str] = []
        for ident in layer:
            present = [item for item in deps[ident] if item in known]
            if present:
                labels.append(f"{ident} ← {', '.join(present)}")
            else:
                labels.append(ident)
        rendered.append(f"L{index}: {', '.join(labels)}")
    if not rendered:
        rendered.append("L0: —")
    return rendered, warnings


def _gate_record(summary: dict[str, Any], line_id: str) -> dict[str, Any] | None:
    gates = summary.get("gates")
    if not isinstance(gates, dict) or not gates:
        return None
    bucket = gates.get("lines")
    if not isinstance(bucket, dict):
        return None
    row = bucket.get(line_id)
    return row if isinstance(row, dict) else None


def _verdict_label(value: Any) -> str:
    """Compare the outer verdict in lowercase. The label is MERGE or BLOCK."""
    text = _clip(value, 40)
    if not text:
        return "—"
    lowered = text.lower()
    if lowered == "merge":
        return "MERGE"
    if lowered == "block":
        return "BLOCK"
    return text


def _verdict(record: dict[str, Any] | None) -> str:
    """Outer review verdict only. ``machine`` is internal and not shown."""
    if not isinstance(record, dict):
        return "—"
    inner = record.get("gates")
    if isinstance(inner, dict):
        primary = inner.get("primary_review")
        if isinstance(primary, dict) and primary.get("verdict"):
            return _verdict_label(primary.get("verdict"))
    history = record.get("history")
    if isinstance(history, list):
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            if item.get("verdict"):
                return _verdict_label(item.get("verdict"))
            if item.get("result"):
                return _verdict_label(item.get("result"))
    return "—"


def _token_issued(summary: dict[str, Any]) -> bool:
    if summary.get("approval_token"):
        return True
    gates = summary.get("gates")
    if isinstance(gates, dict):
        approval = gates.get("approval")
        if isinstance(approval, dict) and approval.get("token_present"):
            return True
    return False


def _now() -> float:
    """The render clock. Tests pass ``now=`` instead of patching time."""
    return time.time()


def _assignment_lines(summary: dict[str, Any]) -> list[str]:
    """N73: the Super-agent's pick next to the selector's, one row per line."""
    view = summary.get("assignment")
    if not isinstance(view, dict) or not view:
        return []
    rows = ["", "assignment (Super-agent vs selector)"]
    for line_id in sorted(view):
        item = view.get(line_id)
        if not isinstance(item, dict):
            continue
        rows.append(
            f"  {_clip(line_id, 60)}  {_clip(item.get('mode'), 10) or '—'}  "
            f"{_clip(item.get('source'), 20) or '—'}  "
            f"pick {_clip(item.get('model'), 20) or '—'}"
            f"/{_clip(item.get('effort'), 10) or 'default'}"
            f"/{_clip(item.get('runtime'), 16) or 'default'}  "
            f"selector {_clip(item.get('selector_choice'), 20) or '—'}"
            + (
                f"  fallback {_clip(item.get('fallback_reason'), 40)}"
                if item.get("fallback_reason")
                else ""
            )
        )
    return rows


def render_assignments(payload: dict[str, Any]) -> list[str]:
    """``dispatch assignments ID``: every recorded attempt per line."""
    rows = [f"{_clip(payload.get('dispatch_id'), 80) or '—'} assignments"]
    lines = payload.get("lines") if isinstance(payload.get("lines"), dict) else {}
    if not lines:
        rows.append("  (none recorded)")
    for line_id in sorted(lines):
        rows.append(f"  {_clip(line_id, 60)}")
        for entry in lines[line_id] or []:
            if not isinstance(entry, dict):
                continue
            choice = entry.get("choice") or {}
            selector = entry.get("selector_choice") or {}
            codes = ",".join(
                str(item.get("reason_code") or item.get("status") or "")
                for item in entry.get("feasibility") or []
                if isinstance(item, dict)
            )
            rows.append(
                f"    #{entry.get('seq', '?')} {_clip(entry.get('mode'), 10)} "
                f"{_clip(entry.get('source'), 20)} pick {_clip(choice.get('model'), 20) or '—'}"
                f"/{_clip(choice.get('effort'), 10) or 'default'} "
                f"selector {_clip(selector.get('model'), 20) or '—'}"
                + (f" feasibility {codes}" if codes else "")
                + (f" fallback {_clip(entry.get('fallback_reason'), 40)}" if entry.get("fallback_reason") else "")
            )
            if choice.get("reason"):
                rows.append(f"      reason: {_clip(choice.get('reason'), 200)}")
    return rows


def render_approve(summary: dict[str, Any]) -> list[str]:
    """``conductor dispatch approve``: what was approved and where it went, then status."""
    ident = _clip(summary.get("id"), 80) or "—"
    state = _clip(summary.get("state"), 40) or "—"
    rows = _record_lines(summary)
    head = f"approved {ident}: state {state}, {len(rows)} line(s)"
    branch = _clip(summary.get("integrate_branch"), 120)
    if branch:
        head += f", branch {branch}"
    return [head, "", *render_status(summary)]


def render_status(summary: dict[str, Any], *, now: float | None = None) -> list[str]:
    """Roster, DAG and gates for one dispatch. Secrets and task text stay out."""
    if now is None:
        now = _now()
    lines: list[str] = []
    if summary.get("fake"):
        lines.append("(local fake record)")
    ident = _clip(summary.get("id"), 80) or "—"
    state = _clip(summary.get("state"), 40) or "—"
    percent = summary.get("progress_pct")
    percent_text = (
        f"{percent}%"
        if isinstance(percent, (int, float)) and not isinstance(percent, bool)
        else "—"
    )
    branch = _clip(summary.get("integrate_branch"), 120) or "—"
    lines.append(f"{ident}  {state}  {percent_text}  {branch}")
    created = _clip(summary.get("created_at"), 40)
    if created:
        lines.append(created)
    goal = _clip(summary.get("goal"), 200)
    if goal:
        lines.append(goal)
    message = _clip(summary.get("message"), 200)
    if message:
        lines.append(message)
    if state == "APPROVAL":
        lines.append(
            f"approval pending — conductor dispatch approve {ident} | "
            f"conductor dispatch reject {ident}"
        )
    if _token_issued(summary):
        lines.append("token issued")
    lines.extend(_drain_lines(summary))
    lines.extend(_assignment_lines(summary))
    lines.extend(_needs_human_lines(summary))
    lines.extend(_reassign_lines(summary))
    lines.extend(_parallel_lines(summary))
    skips = _provider_skip_lines(summary, now)
    if skips:
        lines.append("")
        lines.append("provider skips")
        lines.extend(skips)
    lines.append("")
    lines.append("roster")
    rows = _record_lines(summary)
    if not rows:
        lines.append("—")
    for row in rows:
        line_id = _line_id(row) or "—"
        record = _gate_record(summary, line_id)
        gate = _clip(record.get("phase"), 40) if record else "—"
        fix = record.get("fix_round") if record and record.get("fix_round") is not None else "—"
        depends = _depends_on(row)
        lines.append(
            "  ".join(
                [
                    line_id,
                    _clip(row.get("status"), 40) or "—",
                    _clip(row.get("assigned_model"), 40) or "—",
                    gate or "—",
                    str(fix),
                    _verdict(record),
                    ", ".join(depends) if depends else "—",
                ]
            )
        )
        if record:
            lines.extend(_gate_lines(record))
        clipped_id = _clip(line_id, 80) or "—"
        liveness = _liveness_entry(summary, line_id)
        if liveness:
            lines.extend(
                _liveness_lines(ident, clipped_id, row, liveness, _liveness_mode(summary))
            )
        else:
            lines.extend(_line_hints(ident, clipped_id, row))
    lines.append("")
    lines.append("dag")
    layers, warnings = dag_layers(summary)
    lines.extend(layers)
    lines.extend(warnings)
    unassigned = _unassigned(summary)
    if unassigned:
        lines.append("")
        lines.append("unassigned")
        lines.extend(unassigned)
    return lines


def _reassign_lines(summary: dict[str, Any]) -> list[str]:
    """Queued human reassigns, and the ones the drive dropped with a reason."""
    section = summary.get("human_reassigns")
    if not isinstance(section, dict):
        return []
    lines: list[str] = []
    for kind in ("queued", "dropped"):
        items = section.get(kind)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            line_id = _clip(item.get("line_id"), 80) or "—"
            model = _clip(item.get("model"), 40) or "—"
            text = f"{kind}: {line_id} → {model}"
            reason = _clip(item.get("reason"), 80)
            if kind == "dropped" and reason:
                text += f" ({reason})"
            lines.append(text)
    return lines


_SKIP_AUDIT_LIMIT = 5


def _gates(summary: dict[str, Any]) -> dict[str, Any]:
    gates = summary.get("gates")
    return gates if isinstance(gates, dict) else {}


def _scheduler(summary: dict[str, Any]) -> dict[str, Any]:
    scheduler = _gates(summary).get("scheduler")
    return scheduler if isinstance(scheduler, dict) else {}


def _epoch(value: Any) -> float | None:
    """A finite epoch-seconds number, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _until_text(until: float, now: float) -> str:
    """``14:05Z`` today (UTC), with the date when the reset is another day."""
    try:
        moment = datetime.fromtimestamp(until, UTC)
    except (OverflowError, OSError, ValueError):
        return "unknown"
    try:
        today = datetime.fromtimestamp(now, UTC).date()
    except (OverflowError, OSError, ValueError):
        today = None
    if moment.date() == today:
        return moment.strftime("%H:%MZ")
    return moment.strftime("%Y-%m-%d %H:%MZ")


def _parallel_lines(summary: dict[str, Any]) -> list[str]:
    """N31: how many lines run at once, of the drive's limit."""
    parallel = _scheduler(summary).get("parallel")
    if not isinstance(parallel, dict):
        return []
    limit = parallel.get("limit")
    limit_text = (
        str(limit) if isinstance(limit, int) and not isinstance(limit, bool) else "—"
    )
    raw = parallel.get("active")
    active = [_clip(item, 80) for item in raw] if isinstance(raw, list) else []
    active = [item for item in active if item]
    text = f"parallel: {len(active)} of {limit_text} lines active"
    if active:
        text += f" ({', '.join(active)})"
    return [text]


def _provider_skip_lines(summary: dict[str, Any], now: float) -> list[str]:
    """N25a: providers held back from new launches, then recent skip decisions."""
    lines: list[str] = []
    skips = _gates(summary).get("provider_skips")
    if isinstance(skips, dict):
        for provider in sorted(skips, key=str):
            entry = skips[provider]
            if not isinstance(entry, dict):
                continue
            until = _epoch(entry.get("until"))
            if until is not None and until <= now:
                continue  # expired: the provider is eligible again
            name = _clip(provider, 40) or "—"
            when = "until unknown" if until is None else f"until {_until_text(until, now)}"
            detail = [
                item
                for item in (_clip(entry.get("reason"), 60), _clip(entry.get("source"), 40))
                if item
            ]
            text = f"skipping {name} for new launches {when}"
            if detail:
                text += f" ({', '.join(detail)})"
            lines.append(text)
    audit = _scheduler(summary).get("audit")
    if isinstance(audit, list):
        recent = [
            item
            for item in audit
            if isinstance(item, dict) and item.get("kind") == "provider_skipped"
        ][-_SKIP_AUDIT_LIMIT:]
        for item in recent:
            line_id = _clip(item.get("line_id"), 80) or "—"
            provider = _clip(item.get("provider"), 40) or "—"
            replacement = _clip(item.get("replacement"), 40) or "none eligible"
            text = f"{line_id}: {provider} skipped → {replacement}"
            reason = _clip(item.get("reason"), 60)
            if reason:
                text += f" ({reason})"
            lines.append(text)
        # N63: a review that went to another reviewer than the first pick.
        fallbacks = [
            item
            for item in audit
            if isinstance(item, dict) and item.get("kind") == "reviewer_fallback"
        ][-_SKIP_AUDIT_LIMIT:]
        for item in fallbacks:
            line_id = _clip(item.get("line_id"), 80) or "—"
            stage = _clip(item.get("stage"), 40) or "review"
            previous = _clip(item.get("from"), 40) or "—"
            reviewer = _clip(item.get("reviewer"), 40) or "—"
            lines.append(f"{line_id}: {stage} fell back {previous} → {reviewer}")
    return lines


def _liveness_entry(summary: dict[str, Any], line_id: str) -> dict[str, Any]:
    section = summary.get("liveness")
    lines = section.get("lines") if isinstance(section, dict) else None
    entry = lines.get(line_id) if isinstance(lines, dict) else None
    return entry if isinstance(entry, dict) else {}


def _liveness_mode(summary: dict[str, Any]) -> str | None:
    section = summary.get("liveness")
    mode = section.get("mode") if isinstance(section, dict) else None
    if not isinstance(mode, str):
        return None
    return _clip(mode, 20) or None


def _needs_human_lines(summary: dict[str, Any]) -> list[str]:
    """N76: the lines of this dispatch a person owns, once, near the top."""
    section = summary.get("liveness")
    people = section.get("needs_human") if isinstance(section, dict) else None
    if not isinstance(people, list) or not people:
        return []
    names = ", ".join(_clip(item, 80) for item in people if item)
    return [f"needs a person: {names}"] if names else []


def _liveness_lines(
    ident: str,
    line_id: str,
    row: dict[str, Any],
    entry: dict[str, Any],
    mode: str | None = None,
) -> list[str]:
    """Owner, next step and budget of one open line, and the one way to move it.

    A parked line names its next step and the ``recover`` command instead of
    a list of verbs; a scope conflict still names ``adjudicate``, the only
    decision that moves it.
    """
    owner = _clip(entry.get("owner"), 40) or "—"
    budget = entry.get("budget_left")
    budget_text = str(budget) if isinstance(budget, int) and not isinstance(budget, bool) else "—"
    parts = [f"owner {owner}"]
    status = (_clip(row.get("status"), 40) or "").upper()
    step = _clip(entry.get("next_step"), 40)
    next_at = _clip(entry.get("next_at"), 40)
    if status == "BLOCKED":
        parts.append(f"next_step {step or '—'}")
        parts.append(f"next_at {next_at or '—'}")
    parts.append(f"budget_left {budget_text}")
    lines = ["    " + " · ".join(parts)]
    pending = _clip(entry.get("recover_pending"), 20)
    if pending:
        lines.append(f"    recover {pending} requested; the drive runs it next")
    if status != "BLOCKED":
        return lines
    reason = _clip(row.get("failure_reason"), 80)
    recover = f"conductor dispatch recover {ident} {line_id}"
    if entry.get("needs_human"):
        why = _clip(entry.get("human_reason"), 80) or reason or "—"
        lines.append(f"    needs a person ({why})")
        if reason in _SCOPE_CONFLICTS:
            lines.append(
                f"    → conductor dispatch adjudicate {ident} {line_id} in-scope|out-of-scope"
            )
        else:
            lines.append(f"    → manual: {recover} --split  (or reassign <model>)")
        return lines
    if reason in _SCOPE_CONFLICTS:
        lines.append(
            f"    → conductor dispatch adjudicate {ident} {line_id} in-scope|out-of-scope"
        )
        return lines
    when = f" at {next_at}" if next_at else ""
    if mode in (None, "enforce"):
        lines.append(f"    → next: {step or '—'}{when}; manual: {recover}")
    else:
        # shadow/off record the step but never take it on their own.
        lines.append(
            f"    → next: {step or '—'} (liveness {mode}: runs only on request); "
            f"manual: {recover}"
        )
    return lines


def _line_hints(ident: str, line_id: str, row: dict[str, Any]) -> list[str]:
    """The operator command that moves a line on, for the reasons that need one."""
    reason = _clip(row.get("failure_reason"), 80)
    if reason in _SCOPE_CONFLICTS:
        return [
            f"    → conductor dispatch adjudicate {ident} {line_id} in-scope|out-of-scope"
        ]
    status = (_clip(row.get("status"), 40) or "").upper()
    if status == "BLOCKED" and reason in _REASSIGNABLE_BLOCKS:
        return [
            f"    → conductor dispatch reassign {ident} {line_id} <model>",
            f"      (parked before the fix? conductor dispatch unblock {ident} {line_id} first)",
        ]
    if status == "BLOCKED" and reason in ("review_unavailable", "reviewers_quota_exhausted"):
        # N42: the drive's retry budget is spent; unblock retries the review.
        return [f"    → conductor dispatch unblock {ident} {line_id}  (retry the review)"]
    return review_evidence_hints(ident, line_id, row)


def _gate_lines(record: dict[str, Any]) -> list[str]:
    """Eight named positions. Evidence and the machine record stay out."""
    lines: list[str] = []
    blocker = _clip(record.get("blocking_reviewer"), 40)
    if blocker:
        lines.append(f"    blocking_reviewer  {blocker}")
    if record.get("blocking_rounds") is not None:
        lines.append(f"    blocking_rounds  {record.get('blocking_rounds')}")
    options = record.get("replan_options")
    if isinstance(options, list) and options:
        lines.append("    replan_options  " + ", ".join(_clip(item, 40) for item in options))
    lines.extend(_shadow_lines(record))
    lines.extend(review_evidence_lines(record))
    lines.extend(render_timeline_lines(record))
    return lines


def _drain_lines(summary: dict[str, Any]) -> list[str]:
    """N64: the drain flag as the backend serving this status reads it."""
    drain = summary.get("drain")
    if not isinstance(drain, dict) or not drain:
        return []
    path = _clip(drain.get("path"), 200) or "—"
    if not drain.get("drained"):
        return [f"drain: off (backend reads {path})"]
    detail = [
        item
        for item in (
            _clip(drain.get("set_at"), 40) and f"since {_clip(drain.get('set_at'), 40)}",
            _clip(drain.get("reason"), 80) and f"reason {_clip(drain.get('reason'), 80)}",
        )
        if item
    ]
    text = "drain: on — no new launch or fix round starts"
    if detail:
        text += f" ({', '.join(detail)})"
    return [text, f"  flag {path}"]


def _shadow_lines(record: dict[str, Any]) -> list[str]:
    """N58: what a shadow-mode G2 check would have refused; nothing was refused."""
    gates = record.get("gates")
    cell = gates.get("G2") if isinstance(gates, dict) else None
    entries = cell.get("shadow") if isinstance(cell, dict) else None
    lines: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        check = _clip(entry.get("check"), 40) or "—"
        refuse = _clip(entry.get("would_refuse"), 80) or "—"
        lines.append(f"    shadow  {check} would refuse: {refuse}")
    return lines


def _unassigned(summary: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for row in _record_lines(summary):
        if row.get("assigned_model"):
            continue
        reason = row.get("failure_reason")
        if not reason:
            blockers = row.get("blockers")
            if isinstance(blockers, list) and blockers:
                reason = blockers[0]
        found.append(f"{_line_id(row) or '—'}  {_clip(reason, 120) or '—'}")
    return found


def render_active(payload: dict[str, Any]) -> list[str]:
    """One summary line per dispatch. ``--json`` does not use this."""
    lines: list[str] = []
    if payload.get("fake"):
        lines.append("(local fake record)")
    rows = payload.get("dispatches")
    if not isinstance(rows, list):
        rows = payload.get("active")
    if not isinstance(rows, list):
        rows = []
    written = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        percent = row.get("progress_pct")
        percent_text = (
            f"{percent}%"
            if isinstance(percent, (int, float)) and not isinstance(percent, bool)
            else "—"
        )
        lines.append(
            "  ".join(
                [
                    _clip(row.get("id"), 80) or "—",
                    _clip(row.get("state"), 40) or "—",
                    percent_text,
                    _clip(row.get("goal"), 200),
                ]
            ).rstrip()
        )
        written = True
    if not written:
        lines.append("(no active dispatches)")
    return lines


def _one(rest: list[str], verb: str) -> str:
    if len(rest) != 1 or not rest[0]:
        raise ValueError(f"dispatch {verb} takes one dispatch id")
    return rest[0]


def _two(rest: list[str], verb: str) -> tuple[str, str]:
    if len(rest) != 2 or not all(rest):
        raise DispatchUsageError(f"dispatch {verb} takes <dispatch-id> <line-id>")
    return rest[0], rest[1]


def _adjudication(rest: list[str]) -> tuple[str, str, str]:
    if len(rest) != 3 or not all(rest):
        raise DispatchUsageError(
            "dispatch adjudicate takes <dispatch-id> <line-id> in-scope|out-of-scope"
        )
    ruling = rest[2].strip().lower().replace("_", "-")
    if ruling not in ADJUDICATION_RULINGS:
        raise DispatchUsageError(
            f"dispatch adjudicate ruling must be in-scope or out-of-scope, not {rest[2]!r}"
        )
    return rest[0], rest[1], ruling


def _three(rest: list[str]) -> tuple[str, str, str]:
    if len(rest) != 3 or not all(rest):
        raise ValueError("dispatch reassign takes <dispatch-id> <line-id> <model>")
    return rest[0], rest[1], rest[2]


def _fake_dir() -> Path:
    configured = os.environ.get("CONDUCTOR_DISPATCH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "conductor" / "dispatches"


def _write_fake(record: dict[str, Any]) -> dict[str, Any]:
    directory = _fake_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{record['id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def _validate_id(dispatch_id: str) -> str:
    """Reject ids that would escape the fake-file or URL path segment."""
    if (
        not dispatch_id
        or Path(dispatch_id).name != dispatch_id
        or dispatch_id in {".", ".."}
        or ".." in dispatch_id
        or any(char in dispatch_id for char in "/\\")
    ):
        raise CLIError(f"Invalid dispatch id: {dispatch_id!r}")
    return dispatch_id


def _read_fake(dispatch_id: str) -> dict[str, Any]:
    _validate_id(dispatch_id)
    path = _fake_dir() / f"{dispatch_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CLIError(f"Dispatch {dispatch_id!r} not found") from exc
    except json.JSONDecodeError as exc:
        raise CLIError(f"Dispatch record {path} is corrupt") from exc
    if not isinstance(record, dict):
        raise CLIError(f"Dispatch record {path} is corrupt")
    return record


def _read_fake_active() -> list[dict[str, Any]]:
    directory = _fake_dir()
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("state") not in {"COMPLETE", "ABORT", "ERROR"}:
            records.append(record)
    return records


def _create_fake(goal: str) -> dict[str, Any]:
    record = {
        "id": f"local-{uuid.uuid4().hex[:8]}",
        "goal": goal,
        "state": "SUBMITTED",
        "lines": [],
        "fake": True,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    return _write_fake(record)


handle_dispatch = _dispatch
