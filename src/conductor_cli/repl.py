"""Claude-Code-style interactive REPL over the autonomy Ticket APIs.

Foreground chat streaming is retired; the REPL submits plain-text lines as
manual Tickets and follows them by polling the Ticket detail endpoint.
"""

from __future__ import annotations

import re
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .cli import task_create_body
from .client import APIError, CLIError, ServerError, sanitize_terminal
from .ticket_ids import resolve_ticket_ref

PROMPT = "conductor> "
HISTORY_PATH = Path.home() / ".local" / "state" / "conductor" / "repl_history"
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "closed", "archived"})
INPUT_REQUIRED_STATUSES = frozenset(
    {"waiting", "blocked", "needs_attention", "awaiting_clarification"}
)
ONLINE_WINDOW_SECONDS = 120.0
DAEMON_ONLINE_SECONDS = 180.0
REVIEW_PROVIDER = "codex"

# Marker for "use the session default"; an explicit None means "no override".
_INHERIT = object()
_REPLY_LINE = re.compile(r"/reply[ \t]+(\S+)(?:[ \t](.*))?\Z", re.DOTALL)
_ID_PREFIX = re.compile(r"[0-9a-fA-F-]{1,36}")
# Following has no deadline: it ends when the Ticket is terminal or needs a
# person, or on Ctrl+C. A dropped connection or a 5xx/429/408 is retried with
# exponential backoff up to this ceiling, then the poll pace resumes.
FOLLOW_RETRY_BASE_SECONDS = 2.0
FOLLOW_RETRY_MAX_SECONDS = 30.0
_FOLLOW_RETRYABLE_4XX = frozenset({408, 425, 429})

Emit = Callable[..., None]
ReadLine = Callable[[], str | None]
Sleep = Callable[[float], None]


class ReplClient(Protocol):
    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any: ...

    def post(
        self, path: str, json: dict[str, Any] | list[Any] | None = None
    ) -> Any: ...


def _safe_output(text: str) -> str:
    """One printable line: no escape sequences and no carriage-return overwrite."""
    return sanitize_terminal(str(text).replace("\r", ""))


def _plain_emit(text: str = "", style: str | None = None) -> None:
    del style
    print(_safe_output(text))


def _rich_emit() -> Emit:
    from rich.console import Console

    console = Console(highlight=False)

    def emit(text: str = "", style: str | None = None) -> None:
        console.print(_safe_output(text), style=style, markup=False, highlight=False)

    return emit


def default_emit() -> Emit:
    try:
        return _rich_emit()
    except ImportError:
        return _plain_emit


def _default_reader() -> ReadLine:
    if not sys.stdin.isatty():
        return _plain_reader
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return _plain_reader
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(HISTORY_PATH))
        )
    except OSError:
        return _plain_reader

    def read() -> str | None:
        try:
            return session.prompt(PROMPT)
        except EOFError:
            return None

    return read


def _plain_reader() -> str | None:
    try:
        return input(PROMPT)
    except EOFError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _home_route_missing(exc: CLIError) -> bool:
    """Whether a Home failure means the route is absent, not an auth/server error."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (404, 405)
    text = str(exc)
    return "HTTP 404" in text or "HTTP 405" in text


def _terminal(ticket: dict[str, Any]) -> bool:
    return ticket.get("status") in TERMINAL_STATUSES


def _latest_report_question(ticket: dict[str, Any]) -> str:
    """The newest report question, including one whose attention was cleared."""
    reports = ticket.get("reports")
    if isinstance(reports, list):
        for report in reports:
            if not isinstance(report, dict):
                continue
            question = str(report.get("question") or "").strip()
            if question:
                return question
    return ""


def _attention_question(ticket: dict[str, Any]) -> str:
    """The question a parked Ticket is waiting on, from its attention."""
    attention = ticket.get("attention")
    if isinstance(attention, dict):
        return str(attention.get("question") or "").strip()
    return ""


def _needs_input(ticket: dict[str, Any]) -> bool:
    """Whether the Ticket's *current* state waits on a person.

    Historical report questions are not evidence: a daemon resume clears the
    attention while the old reports stay on the Ticket, so parking is judged
    only from the status and the current attention payload.
    """
    if str(ticket.get("status") or "") in INPUT_REQUIRED_STATUSES:
        return True
    attention = ticket.get("attention")
    return bool(isinstance(attention, dict) and attention)


def _status_style(status: str) -> str | None:
    return {
        "running": "bold",
        "completed": "green",
        "failed": "red",
        "cancelled": "dim",
        "closed": "dim",
        "needs_attention": "yellow",
    }.get(status)


def _emit_status(emit: Emit, ticket: dict[str, Any]) -> None:
    short = str(ticket.get("ticket_id") or "")[:8]
    status = str(ticket.get("status") or "?")
    provider = str(ticket.get("worker_provider") or "")
    model = str(ticket.get("worker_model") or "")
    worker = "/".join(part for part in (provider, model) if part)
    label = f"[{short}] {status}" + (f" ({worker})" if worker else "")
    emit(label, style=_status_style(status))


def follow_ticket(
    client: ReplClient,
    ticket_id: str,
    *,
    poll: float = 2.0,
    sleep: Sleep = time.sleep,
    emit: Emit = _plain_emit,
) -> dict[str, Any]:
    """Poll one Ticket until it ends or needs a person, streaming updates.

    There is no time limit. A server that cannot be reached, or answers
    5xx/408/425/429, is retried with bounded exponential backoff; other 4xx
    answers (the Ticket is gone, access was revoked) end the follow.

    Emits each status transition with the worker provider/model, each new
    report entry (including its question), and only the not-yet-printed suffix
    of the running summary. Returns the Ticket when it reaches a terminal
    state, or as soon as it parks waiting on input/attention so the caller can
    reply instead of polling forever.
    """
    last_status: str | None = None
    seen_reports: set[str] = set()
    seen_questions: set[str] = set()
    printed_summary = ""
    short_id = ticket_id[:8]

    def emit_question(question: str) -> None:
        if question and question not in seen_questions:
            seen_questions.add(question)
            emit(f"question: {question}")

    failures = 0
    while True:
        try:
            ticket = client.get(f"/api/v1/autonomy/tickets/{ticket_id}")
        except (ServerError, APIError) as exc:
            if isinstance(exc, APIError) and exc.status_code not in _FOLLOW_RETRYABLE_4XX:
                raise
            failures += 1
            delay = min(
                FOLLOW_RETRY_BASE_SECONDS * (2 ** (failures - 1)),
                FOLLOW_RETRY_MAX_SECONDS,
            )
            if failures == 1:
                emit(f"lost contact with the server ({exc}); retrying — Ctrl+C to detach")
            sleep(max(delay, poll, 0.05))
            continue
        if failures:
            emit("reconnected")
            failures = 0
        if not isinstance(ticket, dict):
            raise CLIError(f"Unexpected response for ticket {ticket_id}")
        status = str(ticket.get("status") or "")
        for index, report in enumerate(ticket.get("reports") or []):
            if not isinstance(report, dict):
                continue
            key = str(report.get("report_id") or f"#{index}")
            if key in seen_reports:
                continue
            seen_reports.add(key)
            text = str(report.get("summary") or "").strip()
            if text:
                emit(text)
            emit_question(str(report.get("question") or "").strip())
        summary = str(ticket.get("summary") or "")
        if summary != printed_summary:
            delta = (
                summary[len(printed_summary) :]
                if summary.startswith(printed_summary)
                else summary
            )
            printed_summary = summary
            if delta.strip():
                emit(delta.rstrip())
        if status != last_status:
            _emit_status(emit, ticket)
            last_status = status
        if _terminal(ticket):
            error = str(ticket.get("error") or "").strip()
            if error:
                emit(f"error: {error}")
            emit_question(_attention_question(ticket) or _latest_report_question(ticket))
            return ticket
        if _needs_input(ticket):
            emit_question(_attention_question(ticket) or _latest_report_question(ticket))
            if status in {"awaiting_clarification", "needs_attention"}:
                emit(
                    f"waiting for confirmation — confirm with: /confirm {short_id} <text>"
                )
            else:
                emit(f"waiting for input — reply with: /reply {short_id} <text>")
            return ticket
        sleep(max(poll, 0.05))


def resolve_ticket_id(recent: list[dict[str, Any]], prefix: str) -> str:
    """Resolve a short id prefix against the most recent /jobs listing."""
    prefix = (prefix or "").strip()
    if not prefix:
        raise CLIError("A ticket id (or unambiguous prefix) is required")
    ids = [str(ticket.get("ticket_id") or "") for ticket in recent]
    if prefix in ids or not ids:
        return prefix
    matches = [ticket_id for ticket_id in ids if ticket_id.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CLIError(
            f"'{prefix}' is ambiguous: {', '.join(m[:8] for m in matches[:5])}"
        )
    return prefix


_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_BACKUP_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _valid_branch_ref(branch: str) -> bool:
    """Git's ref-name rules, narrow enough to keep the printed command safe.

    Shell quoting is the injection guard; this only refuses inputs that are not
    branch names at all (so the copy-pasted command fails loudly instead of
    quietly doing something else).
    """
    if (
        not branch
        or branch.startswith(("-", "/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or _REF_FORBIDDEN.search(branch)
    ):
        return False
    return all(
        part and part != "." and not part.startswith(".") and part != "@"
        for part in branch.split("/")
    )


def merge_commands(branch: str, *, stamp: str) -> list[str]:
    """The exact local git commands /merge prints; never executed by the REPL.

    ``branch`` is validated as a ref name and shell-quoted so a valid-but-hostile
    ref such as ``feature/$HOME`` or ``feature/x;echo`` stays one argument when
    the operator pastes the command.
    """
    if not _valid_branch_ref(branch):
        raise CLIError(
            f"{branch!r} is not a valid Git branch name; nothing was printed and nothing ran"
        )
    slug = _BACKUP_SLUG.sub("-", branch).strip(".-") or "branch"
    backup = f"backup/pre-merge-{slug}-{stamp}"
    return [
        f"git branch -- {shlex.quote(backup)}",
        f"git merge --no-ff {shlex.quote(branch)}",
        "make lint",
        "uv run pytest tests/unit/ -q",
    ]


def review_prompt(target: dict[str, Any], ticket_id: str) -> str:
    title = str(target.get("title") or "").strip()
    summary = str(target.get("summary") or "").strip() or "(no summary recorded)"
    heading = f"Review the work from ticket {ticket_id}"
    if title:
        heading += f' ("{title}")'
    return (
        f"{heading} in read-only mode. Inspect the diff and the result it produced; "
        "do not modify any files, and do not commit or push anything.\n\n"
        f"Summary of the work under review:\n{summary}\n\n"
        "Assess correctness, look for regressions, and state whether the result "
        "satisfies the original request. Report concrete findings with file paths."
    )


HELP_TEXT = """Commands:
  <text>              create a Ticket for <text> and follow it
  /help               show this help
  /status             server health
  /jobs [n]           list recent Tickets (default 10)
  /show <id>          show one Ticket (summary, error, worker)
  /follow <id>        follow a running Ticket
  /stop <id>          request a Ticket stop
  /reply <id> <text>  reply to a waiting Ticket (body is literal text)
  /confirm <id> <text> confirm a clarification request (draft is shown first)
  /agents             list worker daemons and liveness
  /models             list configured models
  /review <id>        open a read-only codex review Ticket in the target's workspace/worktree
  /merge <branch>     print the local git commands to merge <branch> (never runs them)
  /use provider[/model]  change the default worker for new Tickets
  /exit               leave the REPL

Ids accept unambiguous prefixes from the most recent /jobs listing.
Ctrl+C while following stops following (the Ticket keeps running); at the
prompt, Ctrl+C or Ctrl+D exits."""


class Repl:
    def __init__(
        self,
        client: ReplClient,
        *,
        workspace: str | None = None,
        provider: str | None = "codex",
        model: str | None = None,
        daemon: str | None = None,
        cwd: str | None = None,
        worktree_root: str | None = None,
        poll: float = 2.0,
        read_line: ReadLine | None = None,
        emit: Emit | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self.client = client
        self.workspace_arg = workspace
        self.provider = provider or "codex"
        self.model = model
        self.daemon = daemon
        self.cwd = cwd
        self.worktree_root = worktree_root
        self.poll = poll
        self.recent_jobs: list[dict[str, Any]] = []
        self._workspace_id: str | None = None
        self._read = read_line if read_line is not None else _default_reader()
        self._emit = emit if emit is not None else default_emit()
        self._sleep = sleep

    # -- main loop ---------------------------------------------------------

    def run(self) -> int:
        self._emit(
            f"Conductor REPL — worker {self._worker_label()}. Type /help for commands."
        )
        while True:
            try:
                line = self._read()
            except KeyboardInterrupt:
                break
            if line is None:
                break
            # Only the line terminator is removed: a /reply body keeps its
            # spacing. Command detection ignores leading indentation.
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            try:
                if line.lstrip().startswith("/"):
                    if self._slash(line.lstrip()) is False:
                        break
                else:
                    self._submit(line.strip())
            except CLIError as exc:
                self._emit(f"error: {exc}")
            except KeyboardInterrupt:
                self._emit("interrupted")
        self._emit("bye")
        return 0

    def _worker_label(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    # -- tasks -------------------------------------------------------------

    def _workspace(self) -> str:
        if self.workspace_arg:
            return self.workspace_arg
        if self._workspace_id:
            return self._workspace_id
        workspace_id = ""
        try:
            home = self.client.post("/api/v1/workspaces/home")
            if isinstance(home, dict):
                workspace_id = str(
                    home.get("workspace_id")
                    or home.get("workspace", {}).get("workspace_id")
                    or ""
                )
        except CLIError as exc:
            # Only a deployment without the Home route may fall back. Auth,
            # server and connection failures must surface instead of silently
            # running the Ticket in a different workspace.
            if not _home_route_missing(exc):
                raise
            workspace_id = ""
        if not workspace_id:
            listing = self.client.get("/api/v1/workspaces")
            workspaces = (
                listing.get("workspaces") or [] if isinstance(listing, dict) else []
            )
            if not workspaces:
                raise CLIError(
                    "No workspace available (Home missing and /workspaces empty)"
                )
            workspace_id = str(workspaces[0].get("workspace_id") or "")
        self._workspace_id = workspace_id
        return workspace_id

    # -- daemons -----------------------------------------------------------

    def _daemons(self) -> list[dict[str, Any]]:
        data = self.client.get("/api/v1/autonomy/daemons")
        if isinstance(data, dict):
            rows = data.get("daemons") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        return [row for row in rows if isinstance(row, dict)]

    def _select_daemon(self, provider: str) -> str:
        """Pick a registered daemon that can run this provider.

        Cloud Workers are disabled on this deployment, so a Ticket created
        without a daemon fails closed after submission. Choose locally first
        and refuse to submit instead.
        """
        provider = (provider or "").strip().lower()
        daemons = self._daemons()
        compatible = [
            row
            for row in daemons
            if str(row.get("provider") or "").strip().lower() == provider
        ]
        if not compatible:
            registered = sorted(
                {
                    str(row.get("provider") or "").strip()
                    for row in daemons
                    if str(row.get("provider") or "").strip()
                }
            )
            if registered:
                raise CLIError(
                    f"No {provider} daemon is registered (registered providers: "
                    f"{', '.join(registered)}). Start one or switch worker with "
                    f"/use {registered[0]}."
                )
            raise CLIError(
                "No local daemon is registered, and cloud Workers are disabled on "
                "this deployment. Register and start a daemon before creating Tickets."
            )
        now = datetime.now(timezone.utc)
        live = [
            row
            for row in compatible
            if (seen := _parse_datetime(row.get("last_seen_at"))) is not None
            and (now - seen).total_seconds() <= DAEMON_ONLINE_SECONDS
        ]
        pool = live or compatible
        picked = max(pool, key=lambda row: str(row.get("last_seen_at") or ""))
        daemon_id = str(picked.get("daemon_id") or "").strip()
        if not daemon_id:
            raise CLIError("Daemon response did not include a daemon id")
        if not live:
            self._emit(
                f"warning: no {provider} daemon was seen in the last "
                f"{int(DAEMON_ONLINE_SECONDS)}s; the Ticket may stay queued until one starts"
            )
        return daemon_id

    def _daemon_provider(self, daemon_id: str) -> str:
        """The provider of a registered session daemon, or '' when unknown."""
        for row in self._daemons():
            if str(row.get("daemon_id") or "") == daemon_id:
                return str(row.get("provider") or "").strip().lower()
        return ""

    def _create_ticket(
        self,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None | object = _INHERIT,
        daemon: str | None | object = _INHERIT,
        workspace: str | None | object = _INHERIT,
        cwd: str | None | object = _INHERIT,
    ) -> str:
        """Create a Ticket; ``_INHERIT`` keeps a session default, ``None`` clears it."""
        title = prompt.splitlines()[0].strip()[:60] if prompt.strip() else ""
        worker_provider = self.provider if provider is None else provider
        if daemon is _INHERIT:
            daemon_id: str | None = self.daemon
        else:
            daemon_id = daemon
        if daemon_id is None:
            # Tickets must bind a daemon: cloud Workers fail closed. Select a
            # compatible registered one rather than submitting a failing Ticket.
            daemon_id = self._select_daemon(worker_provider)
        if model is _INHERIT:
            model_value = self.model or ""
        else:
            model_value = model or ""
        workspace_id = self._workspace() if workspace is _INHERIT else workspace
        if not workspace_id:
            workspace_id = self._workspace()
        cwd_value = self.cwd if cwd is _INHERIT else cwd
        body = task_create_body(
            str(workspace_id),
            prompt,
            provider=worker_provider,
            model=str(model_value),
            daemon=daemon_id,
            cwd=None if cwd_value is None else str(cwd_value),
            worktree_root=self.worktree_root,
            title=title,
        )
        created = self.client.post("/api/v1/autonomy/tasks", body)
        if isinstance(created, dict):
            ticket_id = str(
                created.get("ticket_id")
                or created.get("ticket", {}).get("ticket_id")
                or ""
            )
        else:
            ticket_id = ""
        if not ticket_id:
            raise CLIError("Server did not return a ticket id")
        # The follow hint prints the short id, so the prefix cache must know it.
        self.recent_jobs = [
            job
            for job in self.recent_jobs
            if str(job.get("ticket_id") or "") != ticket_id
        ]
        self.recent_jobs.insert(
            0,
            {
                "ticket_id": ticket_id,
                "title": title,
                "status": "queued",
                "worker_provider": worker_provider,
            },
        )
        return ticket_id

    def _submit(
        self,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None | object = _INHERIT,
        daemon: str | None | object = _INHERIT,
        workspace: str | None | object = _INHERIT,
        cwd: str | None | object = _INHERIT,
    ) -> None:
        ticket_id = self._create_ticket(
            prompt,
            provider=provider,
            model=model,
            daemon=daemon,
            workspace=workspace,
            cwd=cwd,
        )
        self._emit(f"ticket {ticket_id[:8]} created — following (Ctrl+C to detach)")
        try:
            self._follow(ticket_id)
        except KeyboardInterrupt:
            self._emit(f"stopped following {ticket_id[:8]} (ticket keeps running)")

    def _follow(self, ticket_id: str) -> dict[str, Any]:
        return follow_ticket(
            self.client,
            ticket_id,
            poll=self.poll,
            sleep=self._sleep,
            emit=self._emit,
        )

    def _detail(self, ticket_id: str) -> dict[str, Any]:
        detail = self.client.get(f"/api/v1/autonomy/tickets/{ticket_id}")
        if not isinstance(detail, dict):
            raise CLIError(f"Unexpected response for ticket {ticket_id}")
        return detail

    def _resolve_ticket(self, prefix: str) -> str:
        """Resolve an id prefix, refreshing the recent list once when needed."""
        if not self.recent_jobs:
            try:
                data = self.client.get(
                    "/api/v1/autonomy/tickets", params={"limit": 200}
                )
            except CLIError:
                data = None
            tickets = data.get("tickets") or [] if isinstance(data, dict) else []
            self.recent_jobs = [t for t in tickets if isinstance(t, dict)]
        resolved = resolve_ticket_id(self.recent_jobs, prefix)
        if resolved == prefix.strip() and _ID_PREFIX.fullmatch(resolved):
            # Not in the recent listing: ask the server, which knows every
            # Ticket (a full UUID comes straight back).
            return resolve_ticket_ref(self.client, resolved)
        return resolved

    # -- slash commands ----------------------------------------------------

    def _slash(self, line: str) -> bool | None:
        """Parse one slash command; ``/reply`` keeps its body as literal text."""
        text = line.strip()
        name = text.split(None, 1)[0] if text else ""
        if name == "/reply":
            match = _REPLY_LINE.match(line)
            if match is None:
                rest = []
            else:
                # Exactly one separator after the id; the remainder is raw.
                rest = [match.group(1), match.group(2) or ""]
        else:
            try:
                tokens = shlex.split(text)
            except ValueError as exc:
                self._emit(f"error: could not parse command: {exc}")
                return None
            if not tokens:
                return None
            name, rest = tokens[0], tokens[1:]
        method = getattr(self, f"_cmd_{name.lstrip('/').replace('-', '_')}", None)
        if method is None:
            self._emit(f"error: unknown command {name} — try /help")
            return None
        return method(rest, line)

    def _cmd_help(self, rest: list[str], line: str) -> None:
        del rest, line
        self._emit(HELP_TEXT)

    def _cmd_exit(self, rest: list[str], line: str) -> bool:
        del rest, line
        return False

    def _cmd_status(self, rest: list[str], line: str) -> None:
        del rest, line
        # The health route is unversioned at /health (the API has no /api/v1/health).
        health = self.client.get("/health")
        if not isinstance(health, dict):
            raise CLIError("Unexpected health response")
        status = health.get("status") or "?"
        service = health.get("service") or "?"
        version = health.get("version") or "?"
        self._emit(f"status: {status}  service: {service}  version: {version}")

    def _cmd_jobs(self, rest: list[str], line: str) -> None:
        del line
        limit = 10
        if rest:
            try:
                limit = max(1, min(int(rest[0]), 200))
            except ValueError:
                raise CLIError("/jobs expects a number") from None
        data = self.client.get("/api/v1/autonomy/tickets", params={"limit": limit})
        tickets = data.get("tickets") or [] if isinstance(data, dict) else []
        self.recent_jobs = [t for t in tickets if isinstance(t, dict)]
        self._emit(f"{'id':<8} {'status':<10} {'provider':<12} title")
        for ticket in self.recent_jobs:
            short = str(ticket.get("ticket_id") or "")[:8]
            status = str(ticket.get("status") or "")
            provider = str(ticket.get("worker_provider") or "")
            title = str(ticket.get("title") or "").replace("\n", " ")
            if len(title) > 44:
                title = title[:41] + "..."
            self._emit(f"{short:<8} {status:<10} {provider:<12} {title}")

    def _cmd_show(self, rest: list[str], line: str) -> None:
        del line
        ticket_id = self._resolve_ticket(self._arg(rest))
        detail = self._detail(ticket_id)
        self._emit(f"ticket: {detail.get('ticket_id')}")
        self._emit(f"title: {detail.get('title') or ''}")
        self._emit(
            f"workspace: {detail.get('workspace_id')}  scope: {detail.get('scope')}"
        )
        self._emit(
            f"status: {detail.get('status')}  worker: {detail.get('worker_provider')}/{detail.get('worker_model')}"
        )
        summary = str(detail.get("summary") or "").strip()
        if summary:
            self._emit(f"summary:\n{summary}")
        question = _attention_question(detail)
        if question:
            self._emit(f"question: {question}")
        report_question = _latest_report_question(detail)
        if report_question and report_question != question:
            self._emit(f"report question: {report_question}")
        error = str(detail.get("error") or "").strip()
        if error:
            self._emit(f"error: {error}", style="red")

    def _cmd_follow(self, rest: list[str], line: str) -> None:
        del line
        ticket_id = self._resolve_ticket(self._arg(rest))
        try:
            self._follow(ticket_id)
        except KeyboardInterrupt:
            self._emit(f"stopped following {ticket_id[:8]} (ticket keeps running)")

    def _cmd_stop(self, rest: list[str], line: str) -> None:
        del line
        ticket_id = self._resolve_ticket(self._arg(rest))
        self.client.post(f"/api/v1/autonomy/tickets/{ticket_id}/stop")
        self._emit(f"stop requested for {ticket_id[:8]}")

    def _cmd_reply(self, rest: list[str], line: str) -> None:
        del line
        if len(rest) < 2:
            raise CLIError("usage: /reply <id> <text>")
        ticket_id = self._resolve_ticket(rest[0])
        # The body is literal text, not shell syntax: quotes, apostrophes and
        # surrounding whitespace in an answer must reach the Ticket unchanged.
        text = rest[1]
        if not text.strip():
            raise CLIError("usage: /reply <id> <text>")
        self.client.post(f"/api/v1/autonomy/tickets/{ticket_id}/reply", {"text": text})
        self._emit(f"reply sent to {ticket_id[:8]}")

    def _cmd_confirm(self, rest: list[str], line: str) -> None:
        del line
        ticket_id = self._resolve_ticket(self._arg(rest))
        draft = self.client.get(
            f"/api/v1/autonomy/tickets/{ticket_id}/clarification"
        )
        if not isinstance(draft, dict):
            raise CLIError("Unexpected clarification response")
        expanded = str(draft.get("expanded_request") or "").strip()
        if expanded:
            self._emit(f"clarified request:\n{expanded}")
        assumptions = [str(item) for item in draft.get("assumptions") or [] if str(item)]
        if assumptions:
            self._emit("assumptions:\n" + "\n".join(f"  - {item}" for item in assumptions))
        text = " ".join(rest[1:]).strip()
        if not text:
            raise CLIError(
                "the draft above is what a bare confirmation would accept; "
                "confirm with: /confirm <id> <text>"
            )
        self.client.post(
            f"/api/v1/autonomy/tickets/{ticket_id}/clarification/confirm",
            {"text": text},
        )
        self._emit(f"confirmation sent to {ticket_id[:8]}")

    def _cmd_agents(self, rest: list[str], line: str) -> None:
        del rest, line
        data = self.client.get("/api/v1/autonomy/daemons")
        daemons = (
            data
            if isinstance(data, list)
            else (data.get("daemons") or [] if isinstance(data, dict) else [])
        )
        now = datetime.now(timezone.utc)
        self._emit(f"{'name':<16} {'provider':<12} liveness")
        for daemon in daemons:
            if not isinstance(daemon, dict):
                continue
            name = str(daemon.get("name") or daemon.get("daemon_id") or "")[:16]
            provider = str(daemon.get("provider") or "")[:12]
            seen = _parse_datetime(daemon.get("last_seen_at"))
            if seen is None:
                state = "never seen"
            elif (now - seen).total_seconds() < ONLINE_WINDOW_SECONDS:
                state = f"online (seen {(now - seen).total_seconds():.0f}s ago)"
            else:
                state = f"offline (seen {(now - seen).total_seconds():.0f}s ago)"
            self._emit(f"{name:<16} {provider:<12} {state}")

    def _cmd_models(self, rest: list[str], line: str) -> None:
        del rest, line
        data = self.client.get("/api/v1/models")
        models = data.get("models") or {} if isinstance(data, dict) else {}
        if not models:
            self._emit("no models configured")
            return
        for provider, entry in models.items():
            names = entry.get("models") if isinstance(entry, dict) else entry
            names = [str(name) for name in (names or [])]
            self._emit(f"{provider}:")
            for name in names:
                self._emit(f"  {name}")

    def _cmd_review(self, rest: list[str], line: str) -> None:
        del line
        target_id = self._resolve_ticket(self._arg(rest))
        target = self._detail(target_id)
        target_provider = str(target.get("worker_provider") or "").strip().lower()
        target_model = str(target.get("worker_model") or "").strip()
        target_daemon = str(target.get("worker_daemon_id") or "").strip() or None
        mandate = target.get("mandate")
        target_cwd = (
            str(mandate.get("cwd") or "").strip()
            if isinstance(mandate, dict)
            else ""
        ) or None
        workspace_id = str(target.get("workspace_id") or "").strip() or None
        # Review with a compatible worker in the target's own workspace/worktree.
        # Only reuse the target's model/daemon when it already runs the review
        # provider; a foreign model (e.g. opus) or daemon would be rejected or
        # silently switch context.
        model = target_model if target_provider == REVIEW_PROVIDER else ""
        daemon = target_daemon if target_provider == REVIEW_PROVIDER else None
        if daemon is None:
            session_provider = (
                self._daemon_provider(self.daemon) if self.daemon else ""
            )
            if session_provider == REVIEW_PROVIDER:
                daemon = self.daemon
            else:
                # Never reuse the session's daemon or checkout for a review.
                daemon = self._select_daemon(REVIEW_PROVIDER)
        self._submit(
            review_prompt(target, target_id),
            provider=REVIEW_PROVIDER,
            model=model,
            daemon=daemon,
            workspace=workspace_id,
            cwd=target_cwd,
        )

    def _cmd_merge(self, rest: list[str], line: str) -> None:
        del line
        branch = self._arg(rest)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self._emit(
            f"To merge '{branch}' locally, run (nothing has been executed or pushed):"
        )
        for command in merge_commands(branch, stamp=stamp):
            self._emit(f"  {command}")

    def _cmd_use(self, rest: list[str], line: str) -> None:
        del line
        spec = self._arg(rest)
        provider, _, model = spec.partition("/")
        provider = provider.strip()
        if not provider:
            raise CLIError("usage: /use provider[/model]")
        # Resolve any pinned daemon's provider before changing session state: a
        # failed lookup must not leave the new provider paired with the old
        # daemon, or the next submission is rejected by the backend.
        unpin = False
        if self.daemon:
            try:
                pinned_provider = self._daemon_provider(self.daemon)
            except CLIError:
                pinned_provider = ""
            if pinned_provider != provider.lower():
                unpin = True
        self.provider = provider
        self.model = model.strip() or None
        if unpin and self.daemon:
            # A pinned daemon only serves its own provider; keep them paired.
            self._emit(
                f"unpinning daemon {self.daemon[:8]}: it does not serve {provider}"
            )
            self.daemon = None
        self._emit(f"default worker is now {self._worker_label()}")

    @staticmethod
    def _arg(rest: list[str]) -> str:
        if not rest:
            raise CLIError("missing argument — try /help")
        return rest[0]


def register_parser(commands: Any) -> None:
    repl = commands.add_parser("repl", help="start an interactive Ticket REPL")
    repl.add_argument(
        "--workspace",
        help="workspace id for new tasks (default: Home, else the first workspace)",
    )
    repl.add_argument(
        "--provider", default="codex", help="default worker provider for new tasks"
    )
    repl.add_argument("--model", help="default worker model for new tasks")
    repl.add_argument("--daemon", help="worker daemon id for new tasks")
    repl.add_argument(
        "--cwd",
        "--worktree",
        dest="cwd",
        help="existing worktree under --worktree-root",
    )
    repl.add_argument(
        "--worktree-root",
        default="~/.conductor/worktrees",
        help="allowed root for Ticket worktrees",
    )
    repl.add_argument(
        "--poll",
        type=_positive_poll,
        default=2.0,
        help="seconds between ticket polls while following",
    )
    repl.set_defaults(handler=run_repl)


def _positive_poll(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed < float("inf"):
        raise ValueError(f"invalid poll value: {value!r}")
    return parsed


def run_repl(args: Any, client: ReplClient) -> int:
    session = Repl(
        client,
        workspace=getattr(args, "workspace", None),
        provider=getattr(args, "provider", None) or "codex",
        model=getattr(args, "model", None),
        daemon=getattr(args, "daemon", None),
        cwd=getattr(args, "cwd", None),
        worktree_root=getattr(args, "worktree_root", None),
        poll=getattr(args, "poll", None) or 2.0,
    )
    try:
        return session.run()
    except CLIError as exc:
        print(f"conductor: {exc}", file=sys.stderr)
        return 1
