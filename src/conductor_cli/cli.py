"""Argparse entry point for the unified Conductor operator CLI."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import io
import json
import sys
import warnings
from collections import deque
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .build_info import build_info
from .client import TOKEN_PATH, APIClient, CLIError, ServerError, sanitize_terminal, write_token
from .worktree import configured_worktree_root
from .ticket_ids import resolve_ticket_ref
from .daemon import (
    preflight_daemon_token_storage,
    save_daemon_token,
    start_local_daemon,
    local_daemon_status,
    stop_local_daemon,
)
from .lines import handle_list as _lines_list
from .lines import handle_logs as _lines_logs
from .lines import handle_show as _lines_show
from .merge import handle_merge as _merge
from .memory_cli import handle_approve as _memory_approve
from .memory_cli import handle_propose as _memory_propose
from .memory_cli import handle_recall as _memory_recall
from .memory_cli import handle_reject as _memory_reject
from .output import (
    build_envelope,
    build_completion,
    command_label,
    envelope_json,
    human_render,
    version_line,
)
from .review import handle_run as _review_run
from .review import handle_status as _review_status
from .review import handle_verdict as _review_verdict


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _usage(args: argparse.Namespace) -> dict[str, Any]:
    from .codexbar_snapshot import DEFAULT_OUTPUT
    from .usage import usage_result

    path = args.snapshot if args.snapshot is not None else DEFAULT_OUTPUT
    return usage_result(
        path=path,
        no_refresh=args.no_refresh,
        codexbar=args.codexbar,
        timeout=args.timeout,
        show_balance=args.show_balance,
    )


def _workers(args: argparse.Namespace) -> dict[str, Any]:
    """Report worker health from the local session store and CodexBar snapshot.

    Read-only by default: the snapshot is read from disk and nothing is
    written. ``--refresh`` is the one opt-in path that runs ``codexbar``
    and rewrites the snapshot file.
    """
    from .codexbar_snapshot import DEFAULT_OUTPUT
    from .workers import workers_result

    path = args.snapshot if args.snapshot is not None else DEFAULT_OUTPUT
    if args.refresh:
        from .codexbar_snapshot import refresh_snapshot

        refresh_snapshot(path, codexbar=args.codexbar, timeout=args.timeout)
    return workers_result(session_path=args.session_state, snapshot_path=path)


def _memory_export(
    args: argparse.Namespace, client: APIClient
) -> Iterator[dict[str, Any]]:
    """Yield owner-export records page by page so stdout stays bounded."""
    cursor = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, Any] = {"limit": args.limit}
        if cursor is not None:
            params["cursor"] = cursor
        if args.include_secret:
            params["include_secret"] = True
        page = client.get("/api/v1/memory/export", params=params)
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("items"), list)
            or any(not isinstance(item, dict) for item in page["items"])
        ):
            raise CLIError("Conductor returned an invalid memory export page")
        yield from page["items"]
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            return
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise CLIError("Conductor returned an invalid memory export cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _positive_finite(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _unit_float(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


class _VersionAction(argparse.Action):
    """Print the build identity when the flag is used, not when the parser is built.

    ``build_parser`` runs for every command, and the identity needs git: a
    parser that asked git on every ``conductor tickets list`` would slow
    every command down for a value only ``--version`` shows.
    """

    def __init__(
        self,
        option_strings: list[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str = "show the version and the tree it was built from",
    ) -> None:
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del parser, namespace, values, option_string
        print(version_line(), file=sys.stdout)
        raise SystemExit(0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conductor", description="Operate a Conductor instance")
    parser.add_argument("--url", help="Conductor base URL (or set CONDUCTOR_URL)")
    parser.add_argument("--version", action=_VersionAction)
    _json_flag(parser)
    commands = parser.add_subparsers(dest="command", required=False)

    version = commands.add_parser("version", help="show the CLI version and the tree it was built from")
    version.set_defaults(handler=_version)

    completion = commands.add_parser("completion", help="print a shell completion script")
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    completion.set_defaults(handler=_completion)

    usage = commands.add_parser("usage", help="show local provider usage headroom")
    usage.add_argument("--no-refresh", action="store_true", help="show the saved snapshot without refreshing providers")
    usage.add_argument("--snapshot", "--output", dest="snapshot", type=Path, help="snapshot path")
    usage.add_argument("--codexbar", default="codexbar", help="CodexBar executable path")
    usage.add_argument("--timeout", type=_positive_finite, default=40.0, help="per-provider refresh timeout in seconds")
    usage.add_argument("--show-balance", action="store_true", help="include balance amounts in output")
    usage.set_defaults(handler=_usage)

    workers = commands.add_parser(
        "workers", help="show worker sessions, context usage, and quota state"
    )
    workers.add_argument(
        "--session-state", dest="session_state", type=Path, help="session manager state file"
    )
    workers.add_argument("--snapshot", "--output", dest="snapshot", type=Path, help="snapshot path")
    workers.add_argument(
        "--refresh",
        action="store_true",
        help="refresh the CodexBar snapshot before reading it (writes the snapshot file)",
    )
    workers.add_argument("--codexbar", default="codexbar", help="CodexBar executable path")
    workers.add_argument("--timeout", type=_positive_finite, default=40.0, help="per-provider refresh timeout in seconds")
    _json_flag(workers)
    workers.set_defaults(handler=_workers)

    auth = commands.add_parser("auth", help="manage CLI authentication")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_set = auth_commands.add_parser("set", help="save a Bearer token with mode 0600")
    auth_set.set_defaults(handler=_auth_set)
    auth_status = auth_commands.add_parser("status", help="show whether a token is configured")
    auth_status.set_defaults(handler=_auth_status)

    memory = commands.add_parser("memory", help="export owner memory data")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_export = memory_commands.add_parser(
        "export", help="stream the authenticated owner's memory as JSONL"
    )
    memory_export.add_argument("--limit", type=_positive_int, default=100)
    memory_export.add_argument(
        "--include-secret",
        action="store_true",
        help="include secret entries in this explicit owner export",
    )
    memory_export.set_defaults(handler=_memory_export)
    memory_recall = memory_commands.add_parser("recall", help="search memory")
    memory_recall.add_argument("query")
    memory_recall.add_argument("--limit", type=_positive_int, default=10)
    memory_recall.set_defaults(handler=_memory_recall)
    memory_propose = memory_commands.add_parser("propose", help="stage an entry for approval")
    memory_propose.add_argument("entry")
    memory_propose.set_defaults(handler=_memory_propose)
    memory_approve = memory_commands.add_parser("approve", help="approve a proposal")
    memory_approve.add_argument("proposal_id")
    memory_approve.set_defaults(handler=_memory_approve)
    memory_reject = memory_commands.add_parser("reject", help="reject a proposal")
    memory_reject.add_argument("proposal_id")
    memory_reject.set_defaults(handler=_memory_reject)

    for name, path in (("status", "/api/v1/system/readiness"), ("health", "/health")):
        command = commands.add_parser(name, help=f"show server {name}")
        command.set_defaults(handler=_get_handler(path))

    tickets = commands.add_parser("tickets", help="manage autonomy Tickets")
    ticket_commands = tickets.add_subparsers(dest="tickets_command", required=True)
    list_tickets = ticket_commands.add_parser("list", help="list Tickets")
    list_tickets.add_argument("--status")
    list_tickets.add_argument("--attention", action="store_true")
    list_tickets.add_argument("--failed", action="store_true")
    list_tickets.add_argument("--limit", type=int, default=50)
    list_tickets.set_defaults(handler=_tickets_list)
    show_ticket = ticket_commands.add_parser("show", help="show one Ticket")
    show_ticket.add_argument("ticket_id")
    show_ticket.set_defaults(handler=_ticket_arg_handler("GET", "/api/v1/autonomy/tickets/{ticket_id}"))
    create_ticket = ticket_commands.add_parser("create", help="create a manual Ticket")
    create_ticket.add_argument("--workspace", required=True)
    create_ticket.add_argument("--prompt", required=True)
    create_ticket.add_argument("--title")
    create_ticket.add_argument("--provider", default="")
    create_ticket.add_argument("--model", default="")
    create_ticket.add_argument("--daemon")
    create_ticket.add_argument("--cwd", "--worktree", dest="cwd", help="existing worktree under --worktree-root")
    create_ticket.add_argument("--worktree-root", default="~/.conductor/worktrees", help="allowed root for Ticket worktrees")
    create_ticket.set_defaults(handler=_tickets_create)
    cancel_ticket = ticket_commands.add_parser("cancel", help="cancel a Ticket")
    cancel_ticket.add_argument("ticket_id")
    cancel_ticket.set_defaults(handler=_ticket_arg_handler("POST", "/api/v1/autonomy/tickets/{ticket_id}/stop"))
    retry_ticket = ticket_commands.add_parser("retry", help="retry a failed, blocked or waiting Ticket")
    retry_ticket.add_argument("ticket_id")
    retry_ticket.set_defaults(handler=_ticket_arg_handler("POST", "/api/v1/autonomy/tickets/{ticket_id}/retry"))
    close_ticket = ticket_commands.add_parser("close", help="close a Ticket (stops it when still open)")
    close_ticket.add_argument("ticket_id")
    close_ticket.add_argument("--reason", default="")
    close_ticket.set_defaults(handler=_ticket_close)
    followup_ticket = ticket_commands.add_parser(
        "followup", help="send a follow-up; an ended Ticket gets a new follow-up Ticket"
    )
    followup_ticket.add_argument("ticket_id")
    followup_ticket.add_argument("text")
    followup_ticket.set_defaults(handler=_ticket_followup)
    follow_ticket = ticket_commands.add_parser(
        "follow", help="follow a Ticket until it ends or needs input (Ctrl+C detaches)"
    )
    follow_ticket.add_argument("ticket_id")
    follow_ticket.add_argument("--poll", type=float, default=2.0, help="seconds between polls")
    follow_ticket.set_defaults(handler=_ticket_follow)
    switch_ticket = ticket_commands.add_parser("switch", help="switch a stuck Ticket to another model")
    switch_ticket.add_argument("ticket_id")
    switch_ticket.add_argument("--provider", default="")
    switch_ticket.add_argument("--model", default="")
    switch_ticket.add_argument("--reason", default="manual")
    switch_ticket.add_argument(
        "--candidate", action="append", default=[], metavar="PROVIDER/MODEL", help="repeatable"
    )
    switch_ticket.set_defaults(handler=_ticket_switch)
    reply_ticket = ticket_commands.add_parser("reply", help="reply to a waiting Ticket")
    reply_ticket.add_argument("ticket_id")
    reply_ticket.add_argument("text")
    reply_ticket.set_defaults(handler=_ticket_reply)
    confirm = ticket_commands.add_parser("confirm", help="confirm a Ticket clarification")
    confirm.add_argument("ticket_id")
    confirm.add_argument("--text", help="confirmation text; defaults to the proposed clarification")
    confirm.set_defaults(handler=_ticket_confirm)
    ticket_lines = ticket_commands.add_parser(
        "lines", help="show Tickets grouped by line (branch/worktree)"
    )
    ticket_lines.add_argument(
        "--include-archived", action="store_true", help="also show archived lines and Tickets"
    )
    ticket_lines.set_defaults(handler=_tickets_lines)

    sources = commands.add_parser("sources", help="manage autonomy Sources")
    source_commands = sources.add_subparsers(dest="sources_command", required=True)
    source_list = source_commands.add_parser("list", help="list Sources")
    source_list.set_defaults(handler=_get_handler("/api/v1/autonomy/sources"))
    source_add = source_commands.add_parser("add", help="add a Source from JSON config")
    source_add.add_argument("--kind", default="github", choices=("github", "feishu"))
    source_add.add_argument("--config", required=True, help="Source config as a JSON object")
    source_add.add_argument("--disabled", action="store_true")
    source_add.set_defaults(handler=_source_add)
    source_sync = source_commands.add_parser("sync", help="queue a Source poll")
    source_sync.add_argument("source_id")
    source_sync.set_defaults(handler=_post_arg_handler("/api/v1/autonomy/sources/{source_id}/poll"))
    for operation, enabled in (("enable", True), ("disable", False)):
        source_action = source_commands.add_parser(operation, help=f"{operation} a Source")
        source_action.add_argument("source_id")
        source_action.set_defaults(handler=_source_enabled, enabled=enabled)

    daemons = commands.add_parser("daemons", help="manage local worker daemons")
    daemon_commands = daemons.add_subparsers(dest="daemons_command", required=True)
    daemon_list = daemon_commands.add_parser("list", help="list registered daemons")
    daemon_list.set_defaults(handler=_get_handler("/api/v1/autonomy/daemons"))
    register = daemon_commands.add_parser("register", help="register a local daemon")
    register.add_argument("--name", default="local")
    register.add_argument("--provider", required=True)
    register.add_argument("--max-concurrency", type=int, default=1)
    register.set_defaults(handler=_daemon_register)
    start = daemon_commands.add_parser("start", help="start a registered local daemon")
    start.add_argument("daemon_id")
    start.add_argument("--cwd", default=".")
    start.add_argument("--worktree-root", default="~/.conductor/worktrees", help="allowed root for Ticket-selected worktrees")
    start.add_argument(
        "--policy",
        default="",
        help="path to this device's daemon policy file (default: ~/.config/conductor/daemon_policy.yaml when it exists)",
    )
    start.add_argument(
        "--trusted-keys",
        default="",
        help="path to the server public keys this device trusts (default: ~/.config/conductor/daemon_trusted_keys.json when it exists)",
    )
    start.add_argument("--once", action="store_true")
    start.add_argument("--breaker-failure-threshold", type=_positive_int, default=3)
    start.add_argument("--breaker-recovery-success-threshold", type=_positive_int, default=1)
    start.add_argument("--breaker-reset-timeout", type=_positive_finite, default=30.0)
    start.add_argument("--breaker-backoff-max", type=_positive_finite, default=300.0)
    start.add_argument("--breaker-jitter", type=_unit_float, default=0.2)
    start.set_defaults(handler=_daemon_start)
    stop = daemon_commands.add_parser("stop", help="stop a local daemon process")
    stop.add_argument("daemon_id")
    stop.set_defaults(handler=_daemon_stop)
    daemon_status = daemon_commands.add_parser("status", help="show local provider breaker status")
    daemon_status.add_argument("daemon_id")
    daemon_status.set_defaults(handler=_daemon_status)

    agents = commands.add_parser("agents", help="list configured agents")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_list = agents_sub.add_parser("list", help="list agents")
    agents_list.set_defaults(handler=_agents_list)
    agents_fleet = agents_sub.add_parser(
        "fleet", help="read-only fleet scan (balance shown as ok|low)"
    )
    agents_fleet.add_argument(
        "--watch", action="store_true", help="refresh the view until interrupted"
    )
    agents_fleet.add_argument(
        "--filter",
        action="append",
        metavar="KEY=VALUE",
        help="keep agents whose ROLE, STATUS, PROVIDER or MODEL contains VALUE",
    )
    agents_fleet.add_argument(
        "--sort",
        default="id",
        choices=("id", "elapsed", "commits", "status", "role", "model"),
        help="column to sort by",
    )
    agents_fleet.set_defaults(handler=_agents_fleet)

    models = commands.add_parser("models", help="list configured models")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    models_list = models_sub.add_parser("list", help="list models")
    models_list.set_defaults(handler=_get_handler("/api/v1/models"))
    models_quota = models_sub.add_parser(
        "quota", help="live CodexBar quota/headroom for providers"
    )
    models_quota.add_argument("--provider", help="only this provider")
    models_quota.add_argument(
        "--no-refresh", action="store_true", help="show the saved snapshot without refreshing providers"
    )
    models_quota.add_argument("--snapshot", "--output", dest="snapshot", type=Path, help="snapshot path")
    models_quota.add_argument("--codexbar", default="codexbar", help="CodexBar executable path")
    models_quota.add_argument("--timeout", type=_positive_finite, default=40.0, help="per-provider refresh timeout in seconds")
    models_quota.set_defaults(handler=_models_quota)

    deliberation = commands.add_parser("deliberation", help="show a deliberation")
    deliberation_sub = deliberation.add_subparsers(dest="deliberation_command", required=True)
    deliberation_show = deliberation_sub.add_parser("show", help="show a deliberation by its ID")
    deliberation_show.add_argument("deliberation_id")
    deliberation_show.set_defaults(handler=_get_arg_handler("/api/v1/autonomy/deliberations/{deliberation_id}"))

    lines = commands.add_parser(
        "lines", help="worktrees/branches under the configured worktree root"
    )
    lines_sub = lines.add_subparsers(dest="lines_command", required=True)
    lines_list = lines_sub.add_parser("list", help="list lines")
    lines_list.set_defaults(handler=_lines_list)
    lines_show = lines_sub.add_parser("show", help="show one line")
    lines_show.add_argument("line")
    lines_show.set_defaults(handler=_lines_show)
    lines_logs = lines_sub.add_parser("logs", help="tail the newest log for a line")
    lines_logs.add_argument("line")
    lines_logs.add_argument("-n", "--lines", type=int, default=40)
    lines_logs.set_defaults(handler=_lines_logs)

    review = commands.add_parser("review", help="cross-review queue (self-review refused)")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_run = review_sub.add_parser("run", help="queue a cross-review of a line")
    review_run.add_argument("line", help="line under review")
    review_run.add_argument("--by", required=True, help="reviewer line (must differ)")
    review_run.add_argument(
        "--provider",
        default="codex",
        help=(
            "reviewer provider recorded on the queue record (workflow metadata; "
            "merging is gated by the server-run Codex review)"
        ),
    )
    review_run.set_defaults(handler=_review_run)
    review_status = review_sub.add_parser("status", help="list queued reviews")
    review_status.add_argument("line", nargs="?", help="only reviews of this line")
    review_status.set_defaults(handler=_review_status)
    review_verdict = review_sub.add_parser(
        "verdict",
        help=(
            "record the reviewer's verdict for a queued review (metadata; it "
            "does not by itself authorize a merge)"
        ),
    )
    review_verdict.add_argument("review_id", help="review id from review status")
    review_verdict.add_argument(
        "--verdict",
        required=True,
        type=str.lower,
        choices=("merge", "block"),
        help="reviewer verdict metadata; a queue MERGE does not by itself authorize a merge",
    )
    review_verdict.add_argument(
        "--note", default="", help="short note stored with the verdict"
    )
    review_verdict.set_defaults(handler=_review_verdict)

    merge = commands.add_parser(
        "merge",
        help=(
            "rehearse and merge a line into a target branch after a server-run "
            "Codex review says MERGE and the tests are green (host-local only)"
        ),
    )
    merge.add_argument("line", help="line worktree name under the configured worktree root")
    merge.add_argument("--into", required=True, help="target branch (e.g. integrate/r2)")
    merge.add_argument(
        "--test-cmd",
        help=(
            "test command run in the temporary rehearsal worktree "
            "(default: the line worktree's .venv python -m pytest -q)"
        ),
    )
    merge.set_defaults(handler=_merge)

    logs = commands.add_parser("logs", help="read local backend logs")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    tail = logs_sub.add_parser("tail", help="show the end of the backend log")
    tail.add_argument("-n", "--lines", type=int, default=40)
    tail.add_argument("--file", default="~/.conductor/logs/backend.log")
    tail.set_defaults(handler=_logs_tail)

    from .clean import register_parser as register_clean_parser
    from .dispatch import register_parser as register_dispatch_parser
    from .dispatch import register_workspace_parser
    from .doctor import register_parser as register_doctor_parser
    from .github import add_commands
    from .operations import register_parser
    from .repl import register_parser as register_repl_parser
    from .skills_cli import register_parser as register_skills_parser
    from .mcp_cli import register_parser as register_mcp_parser
    from .apps_cli import register_parser as register_apps_parser
    from .collab_cli import register_parser as register_collab_parser
    from .instructions_cli import register_parser as register_instructions_parser
    from .roles_cli import register_parser as register_roles_parser
    from .browser.commands import register_parser as register_browser_parser
    from .browser.web import register_parser as register_web_parser

    add_commands(commands)
    register_dispatch_parser(commands)
    register_workspace_parser(commands)
    register_parser(commands)
    register_doctor_parser(commands)
    register_clean_parser(commands)
    register_repl_parser(commands)
    register_skills_parser(commands)
    register_mcp_parser(commands)
    register_apps_parser(commands)
    register_collab_parser(commands)
    register_instructions_parser(commands)
    register_roles_parser(commands)
    register_browser_parser(commands)
    register_web_parser(commands)
    from .skills_hub.commands import register_parser as register_skills_hub_parser

    register_skills_hub_parser(commands)

    shim = commands.add_parser("install-shim", help="install a shell shim for this Python CLI")
    shim.add_argument("--path", help="shim destination (default: writable PATH directory)")
    shim.set_defaults(handler=_install_shim)

    impact = commands.add_parser(
        "impact",
        help="show functions, files, and tests affected by a local change",
    )
    impact.add_argument("targets", nargs="*", help="repo files or symbol names")
    impact.add_argument(
        "--review",
        metavar="BASE..HEAD",
        help="flag changed symbols whose callers were not touched or tested",
    )
    impact.add_argument("--root", type=Path, help="repository root (default: nearest .git)")
    impact.add_argument(
        "--index",
        type=Path,
        help="sqlite index path (default: <root>/.conductor/impact/index.sqlite)",
    )
    impact.add_argument(
        "--depth",
        type=_positive_int,
        default=5,
        help="how many caller hops to follow (default: 5)",
    )
    _json_flag(impact)
    impact.set_defaults(handler=_impact)
    return parser


def _get_handler(path: str):
    return lambda args, client: client.get(path)


def _repl_handler(args: argparse.Namespace, client: APIClient) -> int:
    from .repl import run_repl

    return run_repl(args, client)


def _get_arg_handler(path: str):
    return lambda args, client: client.get(path.format(**vars(args)))


def _post_arg_handler(path: str):
    return lambda args, client: client.post(path.format(**vars(args)))


def _auth_set(args: argparse.Namespace, client: APIClient) -> dict[str, Any]:
    del args, client
    if not sys.stdin.isatty():
        raise CLIError("Token entry requires an interactive terminal")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Conductor Bearer token: ")
    except (getpass.GetPassWarning, EOFError) as exc:
        raise CLIError("Secure interactive token entry is unavailable") from exc
    write_token(token)
    return {"saved": True, "path": str(TOKEN_PATH), "mode": "0600"}


def _auth_status(args: argparse.Namespace, client: APIClient) -> dict[str, Any]:
    del args, client
    from .client import read_token

    return {"configured": read_token() is not None, "path": str(TOKEN_PATH)}


def _tickets_list(args: argparse.Namespace, client: APIClient) -> Any:
    params: dict[str, Any] = {"limit": args.limit}
    if args.status:
        params["status"] = args.status
    if args.attention:
        params["attention"] = True
    if args.failed:
        params["failed"] = True
    return client.get("/api/v1/autonomy/tickets", params=params)


def task_create_body(
    workspace_id: str,
    prompt: str,
    *,
    provider: str = "",
    model: str = "",
    daemon: str | None = None,
    cwd: str | None = None,
    worktree_root: str | None = None,
    title: str = "",
) -> dict[str, Any]:
    """Body for POST /api/v1/autonomy/tasks, shared by `tickets create` and the REPL."""
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "prompt": prompt,
        "worker_provider": provider,
        "worker_model": model,
        "worker_daemon_id": daemon,
    }
    if cwd is not None:
        from .worktree import configured_worktree_root, resolve_ticket_worktree

        try:
            body["cwd"] = str(
                resolve_ticket_worktree(cwd, configured_worktree_root(worktree_root))
            )
        except (OSError, ValueError) as exc:
            raise CLIError(str(exc)) from exc
    if title:
        body["title"] = title
    return body


def _tickets_create(args: argparse.Namespace, client: APIClient) -> Any:
    body = task_create_body(
        args.workspace,
        args.prompt,
        provider=args.provider,
        model=args.model,
        daemon=args.daemon,
        cwd=args.cwd,
        worktree_root=configured_worktree_root(args.worktree_root),
        title=args.title,
    )
    return client.post("/api/v1/autonomy/tasks", body)


def _ticket_arg_handler(method: str, path: str):
    """A Ticket route handler whose ``ticket_id`` may be a unique id prefix."""

    def handler(args: argparse.Namespace, client: APIClient) -> Any:
        args.ticket_id = resolve_ticket_ref(client, args.ticket_id)
        return client.request(method, path.format(**vars(args)))

    return handler


def _ticket_close(args: argparse.Namespace, client: APIClient) -> Any:
    ticket_id = resolve_ticket_ref(client, args.ticket_id)
    return client.post(f"/api/v1/autonomy/tickets/{ticket_id}/close", {"reason": args.reason})


def _ticket_followup(args: argparse.Namespace, client: APIClient) -> Any:
    ticket_id = resolve_ticket_ref(client, args.ticket_id)
    return client.post(f"/api/v1/autonomy/tickets/{ticket_id}/followup", {"text": args.text})


def _ticket_follow(args: argparse.Namespace, client: APIClient) -> Any:
    from .repl import follow_ticket

    ticket_id = resolve_ticket_ref(client, args.ticket_id)
    try:
        ticket = follow_ticket(client, ticket_id, poll=args.poll)
    except KeyboardInterrupt:
        return {"ticket_id": ticket_id, "following": False, "detached": True}
    return {
        "ticket_id": ticket_id,
        "status": ticket.get("status"),
        "summary": ticket.get("summary"),
    }


def _ticket_switch(args: argparse.Namespace, client: APIClient) -> Any:
    args.ticket_id = resolve_ticket_ref(client, args.ticket_id)
    candidates = []
    for item in args.candidate:
        provider, _, model = item.partition("/")
        candidates.append({"provider": provider, "model": model})
    body = {
        "to_provider": args.provider,
        "to_model": args.model,
        "reason": args.reason,
        "candidates": candidates,
    }
    return client.post(f"/api/v1/autonomy/tickets/{args.ticket_id}/switch", body)


def _ticket_reply(args: argparse.Namespace, client: APIClient) -> Any:
    args.ticket_id = resolve_ticket_ref(client, args.ticket_id)
    return client.post(f"/api/v1/autonomy/tickets/{args.ticket_id}/reply", {"text": args.text})


def _ticket_confirm(args: argparse.Namespace, client: APIClient) -> Any:
    args.ticket_id = resolve_ticket_ref(client, args.ticket_id)
    body = {"text": args.text} if args.text is not None else {}
    return client.post(f"/api/v1/autonomy/tickets/{args.ticket_id}/clarification/confirm", body)


def _format_ticket_lines(payload: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    groups = list(payload.get("lines") or [])
    if payload.get("ungrouped"):
        groups.append(payload["ungrouped"])
    for group in groups:
        line = group.get("line") or {}
        bits = [str(line[key]) for key in ("branch", "worktree_path") if line.get(key)]
        heading = str(line.get("name") or "Ungrouped")
        if bits:
            heading += f" ({' · '.join(bits)})"
        rows.append(
            f"{heading} — {group.get('open_count', 0)}/{group.get('ticket_count', 0)} open"
        )
        for ticket in group.get("tickets") or []:
            owner = str(ticket.get("owner") or "cloud")
            if ticket.get("worker_provider"):
                owner += f" · {ticket['worker_provider']}"
            activity = ticket.get("last_activity_at") or ticket.get("updated_at") or ""
            title = str(ticket.get("title") or "(untitled)")[:60]
            rows.append(
                f"  [{ticket.get('status', '?')}] {title} — {owner} — {activity}"
            )
    return rows or ["No ticket lines"]


def _tickets_lines(args: argparse.Namespace, client: APIClient) -> Any:
    payload = client.get(
        "/api/v1/autonomy/tickets/lines",
        params={"include_archived": bool(getattr(args, "include_archived", False))},
    )
    if getattr(args, "json", False):
        return payload
    return {"lines": _format_ticket_lines(payload)}


def _source_add(args: argparse.Namespace, client: APIClient) -> Any:
    try:
        config = json.loads(args.config)
    except json.JSONDecodeError as exc:
        raise CLIError(f"--config must be valid JSON: {exc.msg}") from exc
    if not isinstance(config, dict):
        raise CLIError("--config must be a JSON object")
    if args.kind == "github":
        config.setdefault("secret_name", "GITHUB_TOKEN")
    return client.post(
        "/api/v1/autonomy/sources",
        {"kind": args.kind, "config": config, "enabled": not args.disabled},
    )


def _agents_list(args: argparse.Namespace, client: APIClient) -> dict[str, Any]:
    del args
    return {
        "subagents": client.get("/api/v1/subagents"),
        "native_runtimes": client.get("/api/v1/native-agents"),
    }


def _models_quota(args: argparse.Namespace, client: APIClient | None) -> dict[str, Any]:
    del client
    from .codexbar_snapshot import DEFAULT_OUTPUT
    from .usage import usage_result

    path = args.snapshot if args.snapshot is not None else DEFAULT_OUTPUT
    result = usage_result(
        path=path,
        no_refresh=args.no_refresh,
        codexbar=args.codexbar,
        timeout=args.timeout,
        show_balance=True,
    )
    if args.provider:
        providers = result.get("providers")
        if isinstance(providers, list):
            result = {
                **result,
                "providers": [
                    row
                    for row in providers
                    if isinstance(row, dict) and row.get("provider") == args.provider
                ],
            }
    return result

def _agents_fleet(args: argparse.Namespace, client: APIClient) -> Any:
    from .fleet import agents_fleet

    return agents_fleet(args, client)


def _source_enabled(args: argparse.Namespace, client: APIClient) -> Any:
    return client.patch(
        f"/api/v1/autonomy/sources/{args.source_id}",
        {"enabled": args.enabled},
    )


def _daemon_register(args: argparse.Namespace, client: APIClient) -> Any:
    # Reserve the secure local credential slot before the server mints a one-time token.
    preflight_daemon_token_storage()
    registered = client.post(
        "/api/v1/autonomy/daemons",
        {"name": args.name, "provider": args.provider, "max_concurrency": args.max_concurrency},
    )
    token = registered.pop("token", None)
    if not token:
        raise CLIError("Server response did not include the one-time daemon token")
    daemon_id = str(registered["daemon_id"])
    try:
        save_daemon_token(daemon_id, token)
    except (CLIError, OSError):
        try:
            client.delete(f"/api/v1/autonomy/daemons/{daemon_id}")
        except CLIError:
            raise CLIError("Could not save the one-time token; registration may remain without a local credential") from None
        raise CLIError("Could not save the one-time token; the new daemon registration was removed") from None
    return {**registered, "token_saved": True, "token_path": "~/.config/conductor/daemons/<daemon_id>.token"}


def _daemon_start(args: argparse.Namespace, client: APIClient) -> Any:
    return start_local_daemon(
        client,
        args.daemon_id,
        args.cwd,
        args.once,
        worktree_root=configured_worktree_root(args.worktree_root),
        breaker_failure_threshold=args.breaker_failure_threshold,
        breaker_recovery_success_threshold=args.breaker_recovery_success_threshold,
        breaker_reset_timeout=args.breaker_reset_timeout,
        breaker_backoff_max=args.breaker_backoff_max,
        breaker_jitter=args.breaker_jitter,
        policy=args.policy,
        trusted_keys=args.trusted_keys,
    )


def _daemon_status(args: argparse.Namespace, client: APIClient) -> Any:
    del client
    return local_daemon_status(args.daemon_id)


def _daemon_stop(args: argparse.Namespace, client: APIClient) -> Any:
    del client
    return stop_local_daemon(args.daemon_id)


def _logs_tail(args: argparse.Namespace, client: APIClient) -> dict[str, Any]:
    del client
    if args.lines < 0:
        raise CLIError("--lines must be zero or greater")
    path = Path(args.file).expanduser()
    try:
        if args.lines == 0:
            lines = []
        else:
            with path.open(encoding="utf-8", errors="replace") as stream:
                lines = list(deque(stream, maxlen=args.lines))
    except OSError as exc:
        raise CLIError(f"Could not read log file {path}: {exc}") from exc
    return {"file": str(path), "lines": lines}


def _impact(args: argparse.Namespace) -> dict[str, Any]:
    from .impact.cli import run_impact

    return run_impact(args)


def _install_shim(args: argparse.Namespace, client: APIClient) -> dict[str, Any]:
    del client
    from .shim import install_shim

    path = install_shim(args.path)
    return {"installed": str(path)}


def _version(args: argparse.Namespace) -> dict[str, Any]:
    """The version and the tree it was built from.

    The version alone does not say which code answered, so the tree SHA and
    whether that tree still matches HEAD travel with it.
    """
    del args
    info = build_info()
    return {"name": "conductor", **info.as_dict()}


def _completion(args: argparse.Namespace) -> str | dict[str, Any]:
    script = build_completion(args.shell, build_parser())
    if getattr(args, "json", False):
        return {"shell": args.shell, "script": script}
    # Raw script on stdout so `eval "$(conductor completion bash)"` works.
    return script


def _is_human_dispatch_view(args: argparse.Namespace, result: Any) -> bool:
    """True when a dispatch read should print its ``rendered`` lines.

    The handler still returns the server object. ``output.py`` only prints a
    ``lines`` list, which on a status record is the roster, so the view is
    printed here.
    """
    if args.command != "dispatch" or getattr(args, "json", False):
        return False
    if not isinstance(result, dict):
        return False
    view = result.get("rendered")
    return isinstance(view, list) and bool(view) and all(isinstance(line, str) for line in view)


def _render(value: Any, json_mode: bool, command: str, exit_code: int = 0) -> None:
    if json_mode:
        errors = (
            None
            if exit_code == 0
            else [{"code": "nonzero_exit", "message": f"command exited with status {exit_code}"}]
        )
        envelope = build_envelope(command, data=value, errors=errors, exit_code=exit_code)
        stream = sys.stdout if exit_code == 0 else sys.stderr
        print(envelope_json(envelope), file=stream)
    else:
        print(human_render(value))


def _render_failure(message: str, code: str, *, json_mode: bool, command: str, exit_code: int) -> None:
    if json_mode:
        envelope = build_envelope(
            command,
            data=None,
            errors=[{"code": code, "message": message}],
            exit_code=exit_code,
        )
        print(envelope_json(envelope), file=sys.stderr)
    else:
        print(f"conductor: {sanitize_terminal(message)}", file=sys.stderr)


_LOCAL_COMMANDS = {
    "apps",
    "collab",
    "auth",
    "install-shim",
    "logs",
    "deploy",
    "rollback",
    "usage",
    "workers",
    "skills",
    "mcp",
    "skills-hub",
    "instructions",
    "browser",
    "web",
    "lines",
    "impact",
    "version",
    "completion",
    "doctor",
    "clean",
}
_ARGS_ONLY_COMMANDS = {
    "apps",
    "collab",
    "deploy",
    "rollback",
    "usage",
    "workers",
    "skills",
    "mcp",
    "instructions",
    "lines",
    "impact",
    "version",
    "completion",
    "doctor",
    "clean",
}


def _is_local(args: argparse.Namespace) -> bool:
    """Commands that never open an API client."""
    if args.command in _LOCAL_COMMANDS:
        return True
    if args.command == "daemons" and getattr(args, "daemons_command", None) == "status":
        return True
    if args.command == "models" and getattr(args, "models_command", None) == "quota":
        return True
    return False


def _takes_only_args(args: argparse.Namespace) -> bool:
    """Handlers registered as ``handler(args)`` instead of ``handler(args, client)``."""
    return args.command in _ARGS_ONLY_COMMANDS


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    local_envelope_commands = {"browser", "web", "skills-hub"}
    top_level_commands = {
        "usage", "auth", "status", "health", "tickets", "sources", "daemons", "agents", "models",
        "deliberation", "logs", "repl", "browser", "web", "skills", "mcp", "skills-hub", "instructions", "install-shim", "deploy",
        "rollback", "github", "doctor", "clean",
    }
    command_name = _command_from_argv(raw_argv, top_level_commands)
    try:
        if command_name in local_envelope_commands:
            with contextlib.redirect_stderr(io.StringIO()):
                args, unknown = parser.parse_known_args(raw_argv)
        else:
            args, unknown = parser.parse_known_args(raw_argv)
    except SystemExit as exc:
        if exc.code == 0 or command_name not in local_envelope_commands:
            raise
        print(envelope_json(_cli_error_envelope("invalid_input", "Invalid command arguments", "Run the command with --help for usage.")))
        return int(exc.code)
    if unknown == ["--json"]:
        args.json = True
    elif unknown:
        if args.command in local_envelope_commands:
            print(envelope_json(_cli_error_envelope("invalid_input", "Invalid command arguments", "Run the command with --help for usage.")))
            return 2
        parser.error("unrecognized arguments: " + " ".join(unknown))
    if args.command is None:
        # Bare `conductor` drops into the interactive Ticket REPL.
        args.command = "repl"
        args.handler = _repl_handler
    command = command_label(args)
    client = None
    try:
        if args.command == "daemons" and args.daemons_command == "start":
            if args.breaker_backoff_max < args.breaker_reset_timeout:
                raise CLIError("--breaker-backoff-max must be at least --breaker-reset-timeout")
        if not _is_local(args):
            client = APIClient(base_url=args.url)
        handler = getattr(args, "handler", None)
        if handler is None:
            raise CLIError("This command has no handler")
        if _takes_only_args(args):
            result = handler(args)
        else:
            result = handler(args, client)
        if args.command == "memory" and args.memory_command == "export":
            for record in result:
                print(json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str))
            return 0
        exit_code = 0
        if (
            args.command in {"deploy", "rollback", "browser", "web", "skills-hub"}
            and isinstance(result, dict)
            and isinstance(result.get("exit_code"), int)
        ):
            # Operation handlers can carry a process status alongside their
            # JSON payload. Keep that control field out of user-facing output.
            exit_code = result.pop("exit_code")
        if args.command in local_envelope_commands and isinstance(result, dict) and result.get("ok") is False:
            exit_code = 1
        if args.command in local_envelope_commands:
            if exit_code:
                if not (isinstance(result, dict) and result.get("ok") is False):
                    result = _failure_envelope("command_failed", "Command did not complete successfully", result)
            else:
                result = _success_envelope(result)
        if isinstance(result, int):
            return result
        if args.command == "usage" and not args.json:
            from .usage import render_usage

            render_usage(result, json_mode=False)
        elif args.command == "workers":
            from .workers import render_workers

            render_workers(result, json_mode=args.json)
        elif args.command == "impact":
            from .impact.cli import render_result

            if args.json:
                # Keep the CLI-wide v1 envelope; its data is the report object.
                _render(result, True, command, exit_code)
            else:
                render_result(result, json_mode=False)
        elif args.command == "browser" and args.browser_command == "run" and args.format == "yaml" and not args.json:
            import yaml

            print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end="")
        elif args.command == "agents" and getattr(args, "agents_command", None) == "fleet":
            from .fleet import parse_filters, render_fleet, select_agents

            filters = parse_filters(getattr(args, "filter", None))
            sort_key = getattr(args, "sort", "id")
            failed = bool(isinstance(result, dict) and result.get("error"))
            if args.json:
                # The JSON frame gets the same filtered/sorted rows as the
                # table, so --filter/--sort never mean different things.
                payload = dict(result) if isinstance(result, dict) else {"agents": []}
                payload["agents"] = select_agents(
                    payload.get("agents") or [], filters, sort_key
                )
                _render(payload, True, command, 1 if failed else 0)
            else:
                render_fleet(
                    result,
                    json_mode=False,
                    filters=filters,
                    sort_key=sort_key,
                )
            return 1 if failed else 0
        elif args.command in local_envelope_commands:
            # These groups own their JSON contract ({ok, data, error}); frame
            # it directly instead of nesting it in the CLI-wide envelope.
            if args.json:
                print(envelope_json(result))
            else:
                _render(result, False, command)
        elif _is_human_dispatch_view(args, result):
            print("\n".join(sanitize_terminal(line) for line in result["rendered"]))
        else:
            _render(result, args.json, command, exit_code)
        return exit_code
    except KeyboardInterrupt:
        _render_failure("interrupted", "interrupted", json_mode=args.json, command=command, exit_code=130)
        return 130
    except ServerError as exc:
        _render_failure(str(exc), "server_error", json_mode=args.json, command=command, exit_code=3)
        return 3
    except CLIError as exc:
        if args.command in local_envelope_commands:
            print(
                envelope_json(
                    _cli_error_envelope(getattr(exc, "code", "command_failed"), str(exc), getattr(exc, "hint", None))
                )
            )
        else:
            _render_failure(str(exc), "cli_error", json_mode=args.json, command=command, exit_code=1)
        return 1
    except (OSError, ValueError) as exc:
        if args.command in local_envelope_commands:
            del exc
            print(
                envelope_json(
                    _cli_error_envelope("command_failed", "Command failed", "Check the input and try again.")
                )
            )
        else:
            _render_failure(str(exc), "cli_error", json_mode=args.json, command=command, exit_code=1)
        return 1
    finally:
        if client is not None:
            client.close()
    return 0


def _success_envelope(data: Any) -> dict[str, Any]:
    from .browser.envelope import success

    if isinstance(data, dict) and isinstance(data.get("ok"), bool) and "error" in data:
        return data
    return success(data)


def _cli_error_envelope(code: str, message: str, hint: str | None) -> dict[str, Any]:
    from .browser.envelope import failure

    return failure(code[:80], message[:200], hint[:200] if isinstance(hint, str) else None)


def _failure_envelope(code: str, message: str, data: Any) -> dict[str, Any]:
    from .browser.envelope import failure

    result = failure(code, message, "Review the returned data for partial results.")
    result["data"] = data
    return result


def _command_from_argv(argv: Sequence[str], top_level_commands: set[str]) -> str:
    skip_value = False
    for argument in argv:
        if skip_value:
            skip_value = False
            continue
        if argument == "--url":
            skip_value = True
            continue
        if argument.startswith("--url="):
            continue
        if argument in top_level_commands:
            return argument
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
