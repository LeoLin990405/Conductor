"""``conductor agents fleet`` — the operator view of the local agent fleet.

The command is a thin client of the read-only fleet endpoint: it fetches one
scan, filters and sorts it locally, and renders the same columns the dashboard
shows. It never talks to a worktree, a log file or a provider itself.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from typing import Any

from .client import json_dumps_safe, sanitize_terminal

FLEET_PATH = "/api/v1/agents/fleet"
WATCH_INTERVAL_SECONDS = 5.0

_COLUMNS = (
    ("id", "AGENT"),
    ("model", "MODEL"),
    ("role", "ROLE"),
    ("status", "STATUS"),
    ("pid", "PID"),
    ("elapsed", "ELAPSED"),
    ("branch", "BRANCH"),
    ("commits_ahead", "AHEAD"),
    ("tests_state", "TESTS"),
    ("provider_balance", "BAL"),
    ("log_tail", "TAIL"),
)

_FILTER_KEYS = ("role", "status", "provider", "model")
_SORT_KEYS = ("id", "elapsed", "commits", "status", "role", "model")
_STATUS_RANK = {"running": 0, "error": 1, "stalled": 2, "unknown": 3, "done": 4}


def fetch_fleet(client: Any) -> dict[str, Any]:
    """Fetch one fleet scan from the backend."""
    payload = client.get(FLEET_PATH)
    if not isinstance(payload, dict):
        # The endpoint answers 200 with a scan object, so anything else is a
        # response this client cannot read. Reporting it as an empty fleet
        # would answer a question nobody asked and look like success.
        return {"agents": [], "error": "The fleet endpoint returned an unexpected response."}
    return payload


def parse_filters(values: Sequence[str] | None) -> dict[str, str]:
    """Parse repeated ``--filter key=value`` flags into a lowercase match map."""
    filters: dict[str, str] = {}
    for value in values or ():
        key, separator, expected = value.partition("=")
        key = key.strip().lower()
        if not separator or key not in _FILTER_KEYS or not expected.strip():
            raise ValueError(
                f"--filter must be one of {', '.join(_FILTER_KEYS)} as key=value"
            )
        filters[key] = expected.strip().lower()
    return filters


def format_elapsed(seconds: Any) -> str:
    """Render an elapsed duration compactly; unknown stays a dash."""
    if not isinstance(seconds, int) or seconds < 0:
        return "-"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02}h"
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes:02}:{secs:02}"


def _row(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(agent.get("id") or ""),
        "model": str(agent.get("model") or "-"),
        "provider": str(agent.get("provider") or "-"),
        "role": str(agent.get("role") or "-"),
        "status": str(agent.get("status") or "unknown"),
        "pid": str(agent.get("pid") or "-"),
        "elapsed": format_elapsed(agent.get("elapsed_secs")),
        "elapsed_secs": agent.get("elapsed_secs") if isinstance(agent.get("elapsed_secs"), int) else -1,
        "branch": str(agent.get("branch") or "-"),
        "commits_ahead": (
            str(agent["commits_ahead"])
            if isinstance(agent.get("commits_ahead"), int)
            else "-"
        ),
        "tests_state": str(agent.get("tests_state") or "-"),
        "provider_balance": str(agent.get("provider_balance") or "unknown"),
        "log_tail": " ".join(str(agent.get("log_tail") or "").split())[:40] or "-",
    }


def filter_agents(agents: Sequence[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    """Keep the agents whose rendered row contains every filter value."""
    kept: list[dict[str, Any]] = []
    for agent in agents:
        row = _row(agent)
        if all(expected.lower() in row[key].lower() for key, expected in filters.items()):
            kept.append(agent)
    return kept


def filter_rows(agents: Sequence[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    """Rendered rows for the agents that survive the filters."""
    return [_row(agent) for agent in filter_agents(agents, filters)]


def _sort_key(key: str):
    """The sort key of one column, unknown-safe and broken by agent id.

    A row that knows nothing about a column sorts last rather than first: an
    unknown count is not the largest one.
    """
    if key == "commits":
        # Most commits first, the way the dashboard's AHEAD column reads them;
        # the CLI's ascending order disagreed with the page over the same rows.
        return lambda row: (
            row["commits_ahead"] == "-",
            -(int(row["commits_ahead"]) if row["commits_ahead"] != "-" else 0),
            row["id"],
        )
    if key == "elapsed":
        # Unknown elapsed sorts last: its sentinel is negative, so negating it
        # alone would place it among the short durations.
        return lambda row: (row["elapsed_secs"] < 0, -row["elapsed_secs"], row["id"])
    if key == "status":
        return lambda row: (_STATUS_RANK.get(row["status"], 9), row["id"])
    if key in ("role", "model"):
        return lambda row: (row[key], row["id"])
    return lambda row: (row["id"],)


def sort_rows(rows: Sequence[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Sort rendered rows by one column, unknown-safe."""
    return sorted(rows, key=_sort_key(key))


def select_agents(
    agents: Sequence[dict[str, Any]], filters: dict[str, str], sort_key: str
) -> list[dict[str, Any]]:
    """The scan's own agents, as the table would show them.

    ``--filter`` and ``--sort`` describe the view, not the transport: a reader
    that asked for JSON wanted the same rows in the same order, and both flags
    were dropped on the floor for it while the help text promised they hold.
    The rules are the rendered row's, so the two renderers cannot drift.
    """
    key = _sort_key(sort_key)
    return sorted(filter_agents(agents, filters), key=lambda agent: key(_row(agent)))


def render_table(rows: Sequence[dict[str, Any]]) -> str:
    """Render rows as an aligned table with the dashboard's columns."""
    widths = {
        key: max(len(label), *(len(row[key]) for row in rows)) if rows else len(label)
        for key, label in _COLUMNS
    }
    header = "  ".join(label.ljust(widths[key]) for key, label in _COLUMNS)
    lines = [header, "  ".join("-" * widths[key] for key, _ in _COLUMNS)]
    for row in rows:
        lines.append(
            "  ".join(
                sanitize_terminal(row[key]).ljust(widths[key]) for key, _ in _COLUMNS
            )
        )
    return "\n".join(lines)


def _agents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The agents one frame carries; a frame without a list carries none."""
    agents = payload.get("agents")
    return agents if isinstance(agents, list) else []


def render_fleet(
    payload: dict[str, Any],
    *,
    json_mode: bool,
    filters: dict[str, str],
    sort_key: str,
    out: Any = None,
) -> None:
    """Print one fleet frame as JSON or as a table."""
    stream = out or sys.stdout
    if json_mode:
        frame = dict(payload)
        frame["agents"] = select_agents(_agents(payload), filters, sort_key)
        print(json_dumps_safe(frame), file=stream)
        return
    rows = sort_rows(filter_rows(_agents(payload), filters), sort_key)
    error = payload.get("error")
    if rows:
        print(render_table(rows), file=stream)
    elif error:
        # The scan did not run, so there is no fleet to report. "No agents
        # found" answers a different question — an empty fleet — and reads as
        # a clean scan to anything watching the exit status.
        print("The fleet scan failed.", file=stream)
    elif filters:
        print("No agents match the current filters.", file=stream)
    else:
        print("No agents found.", file=stream)
    if error:
        # The backend's wording, not ours: it knows whether discovery is off
        # or the scan raised.
        print(f"note: {sanitize_terminal(str(error))}", file=sys.stderr)


def _scan_failed(payload: dict[str, Any]) -> bool:
    """Whether one frame reports a scan that did not complete.

    The endpoint answers HTTP 200 whether the scan ran or not — a disabled
    deployment and a scan that raised both come back as a frame with an
    ``error`` and no agents — so the status code cannot tell a caller the
    difference. The exit status has to.
    """
    return bool(payload.get("error"))


def run_agents_fleet(
    args, client: Any, *, sleep: Any = time.sleep, out: Any = None
) -> int:
    """Fetch and render the fleet once, or watch it until interrupted.

    Returns 1 for a frame that reports a failed scan, so automation that runs
    one scan and checks the status learns the worktrees were not read. A watch
    that the operator interrupts returns 0: ending it is the operator's doing,
    and every frame it printed already said the scan was failing.
    """
    filters = parse_filters(getattr(args, "filter", None))
    sort_key = getattr(args, "sort", "id")
    payload = fetch_fleet(client)
    if not getattr(args, "watch", False):
        render_fleet(
            payload,
            json_mode=args.json,
            filters=filters,
            sort_key=sort_key,
            out=out,
        )
        return 1 if _scan_failed(payload) else 0
    interval = WATCH_INTERVAL_SECONDS
    try:
        while True:
            if not args.json:
                # A table frame redraws itself in place. A JSON frame is one
                # document per line for a reader that parses each one, and the
                # clear-screen escape in front of it is not JSON: piping
                # `--watch --json` into a parser failed on the first frame.
                print("\033[2J\033[H", end="", file=out or sys.stdout)
            render_fleet(
                payload,
                json_mode=args.json,
                filters=filters,
                sort_key=sort_key,
                out=out,
            )
            sleep(interval)
            payload = fetch_fleet(client)
    except KeyboardInterrupt:
        return 0


def normalize_balance(value: Any) -> str:
    """Project any server balance state onto the operator's two-value view."""
    return "ok" if value == "ok" else "low"


def normalized_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The scan with every agent balance collapsed to ``ok``/``low``."""
    agents = payload.get("agents")
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict):
                agent["provider_balance"] = normalize_balance(
                    agent.get("provider_balance")
                )
    return payload


def handle_fleet(args: Any, client: Any) -> dict[str, Any]:
    """One read-only scan with balances collapsed to the operator's ok|low."""
    del args
    return normalized_payload(fetch_fleet(client))


def watch_fleet_envelopes(
    args: Any,
    client: Any,
    *,
    sleep: Any = time.sleep,
    out: Any = None,
) -> int:
    """``--watch --json`` for the CLI: one v1 envelope per refreshed frame.

    ``run_agents_fleet`` keeps its raw one-document-per-frame contract for the
    shell; the operator CLI frames each frame in the same v1 envelope and
    ok|low balance view as a single scan, then stays until interrupted.
    """
    from .output import build_envelope, envelope_json

    stream = out or sys.stdout
    filters = parse_filters(getattr(args, "filter", None))
    sort_key = getattr(args, "sort", "id")
    try:
        while True:
            payload = normalized_payload(fetch_fleet(client))
            frame = dict(payload)
            frame["agents"] = select_agents(
                _agents(payload), filters, sort_key
            )
            failed = _scan_failed(payload)
            errors = (
                [
                    {
                        "code": "nonzero_exit",
                        "message": "command exited with status 1",
                    }
                ]
                if failed
                else None
            )
            envelope = build_envelope(
                "agents fleet",
                data=frame,
                errors=errors,
                exit_code=1 if failed else 0,
            )
            print(envelope_json(envelope), file=stream, flush=True)
            sleep(WATCH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        return 0


def agents_fleet(args: Any, client: Any) -> Any:
    """The ``agents fleet`` handler: a watch owns its own loop and rendering.

    A single scan returns the payload so the caller's envelope/table path
    prints it; ``--watch`` refreshes and redraws in place, returning the
    process status itself. ``--watch --json`` goes through the CLI envelope
    path so a piped reader sees the same contract as a single scan.
    """
    if getattr(args, "watch", False):
        if getattr(args, "json", False):
            return watch_fleet_envelopes(args, client)
        return run_agents_fleet(args, client)
    return handle_fleet(args, client)
