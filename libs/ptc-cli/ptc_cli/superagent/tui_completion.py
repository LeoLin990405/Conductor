"""Completions and ``@file`` attachments for the Super-agent TUI.

Three completion sources sit behind the composer:

* a slash command at the start of the line (``/jobs``, ``/merge``, ...), with
  a one-line description in the completion menu. The command is found by the
  matcher of the palette (D1): a case-insensitive subsequence of the
  canonical name, the slash name or an alias, so ``/DSP`` finds ``/dispatch``
  and ``/jo`` finds ``/jobs``;
* an argument after a command and a space (``/dispatch 7f``), completed from
  the ids of the command (D2): a unique, case-sensitive prefix, never fuzzy.
  The ids come from an injectable provider, so the composer offers nothing
  until the TUI wires the real rosters in;
* an ``@path`` anywhere a word starts, completed from the *local* filesystem
  relative to the directory the TUI was started in. This is a local
  convenience: the file is read and attached to the prompt, not uploaded on
  its own.

``expand_mentions`` is the attaching half: it turns the ``@path`` tokens into
a prompt suffix the server can read. It refuses missing files, directories,
binaries (NUL byte) and files over the size budget instead of silently
sending a partial prompt.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
)
from prompt_toolkit.document import Document

from .text import plain, strip_controls
from .tui_blocks import collect_mentions
from .tui_palette import COMMANDS as PALETTE_COMMANDS
from .tui_palette import (
    IdProvider,
    PaletteCommand,
    canonical_command,
    command_hint,
    complete_arguments,
    rank_commands,
)

MAX_ATTACHMENT_BYTES = 200_000
MAX_ATTACHMENTS = 8
COMPLETION_LIMIT = 60

_SLASH_RE = re.compile(r"^/(?P<fragment>[^\s]*)$")
_AT_RE = re.compile(r"(?<![\w@])@(?P<fragment>[^\s@]*)$")

# Slash commands, with the hint shown next to each completion.
# The menu and /help share this table. Order is the menu order.
SLASH_COMMANDS: dict[str, str] = {
    # Order matches the own-design command list. Later entries stay available.
    "/worker": "show which worker has the current Ticket",
    "/dispatch": "dispatch roster; /dispatch <id> reads one",
    "/roles": "role registry; /roles set <role> default <name> reason <text>",
    "/jobs": "list Tickets; /jobs <id> shows one; /jobs watch <id> follows it",
    "/attach": "follow a Ticket; /attach <id|index> is /jobs watch",
    "/workspace": "switch the Home workspace",
    "/reply": "answer the Ticket that needs you",
    "/retry": "retry a failed or waiting Ticket",
    "/cancel": "stop a running Ticket (/cancel <id|index>)",
    "/resume": "list saved sessions; /resume <id> reopens one",
    "/status": "reread Supervisor progress for the current Ticket",
    "/help": "show the TUI key map and commands",
    "/quit": "quit the TUI",
    "/skills": "local skills status",
    "/mcp": "local MCP server status",
    "/memory": "browse remembered memory",
    "/agents": "local fleet, native runtimes, daemons",
    "/review": "queue a local cross-review (/review run <line> --by <line>)",
    "/merge": "merge a line locally after its tests (/merge <line> --into <branch>)",
    "/stop": "stop a running Ticket (/stop <id|index>)",
    "/signals": "Sources and job counts",
    "/clear": "clear the conversation",
    "/details": "toggle Worker details (default: Supervisor view)",
    "/model": "show the Router model and configured providers",
    "/compact": "drop activity noise and duplicate rows from the transcript",
    "/expand": "expand every tool block",
    "/collapse": "collapse every tool block",
    "/new": "start a fresh session (persisted)",
    "/exit": "quit the TUI",
}


@dataclass(frozen=True, slots=True)
class Attachment:
    """One local file the prompt carries: its path, its text and its size."""

    path: str
    content: str
    size: int


def _palette_table(commands: Mapping[str, str] | None) -> tuple[PaletteCommand, ...]:
    """The table the slash completer matches against.

    The default is the shared palette of the 28 commands, so the composer
    finds a command the way the palette does. A caller that brings its own
    commands gets a table of its own, one command per slash name.
    """
    if commands is None:
        return PALETTE_COMMANDS
    rows: list[PaletteCommand] = []
    for slash, hint in commands.items():
        rows.append(
            PaletteCommand(
                name=slash.removeprefix("/"),
                slash=slash if slash.startswith("/") else None,
                aliases=(),
                hint=hint,
            )
        )
    return tuple(rows)


class SlashCompleter(Completer):
    """Complete a slash command at the start of the composer line.

    The command is found by the matcher of the palette (D1): a
    case-insensitive subsequence of the canonical name, the slash name or an
    alias, so ``/DSP`` finds ``/dispatch`` and ``/jo`` finds ``/jobs``. The
    completion carries the slash name of the command, or its canonical name
    when the command has no slash name of its own. The description beside it
    is the hint of that command in the locale the TUI is speaking.
    """

    def __init__(self, commands: Mapping[str, str] | None = None) -> None:
        """Take the shipped table, or a table of the caller's own commands."""
        # ``commands`` is the shipped table the callers read; ``table`` is what
        # the matcher runs on, and the palette is the default.
        self.commands = dict(commands or SLASH_COMMANDS)
        self.table = _palette_table(commands)

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        """The commands the line names, best match first."""
        del complete_event
        match = _SLASH_RE.match(document.text_before_cursor)
        if match is None:
            return
        fragment = match.group("fragment")
        for ranked in rank_commands(document.text_before_cursor, self.table):
            command = ranked.command
            text = command.slash or command.name
            yield Completion(
                text,
                start_position=-len(fragment) - 1,
                display=text,
                display_meta=command_hint(command),
            )


class AtFileCompleter(Completer):
    """Complete ``@path`` from the local filesystem, bounded and quiet."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        limit: int = COMPLETION_LIMIT,
        max_entries: int = 5_000,
    ) -> None:
        """Walk the root the TUI was started in, bounded and quiet."""
        self.root = Path(root or Path.cwd()).expanduser().resolve()
        self.limit = max(1, int(limit))
        self.max_entries = max(1, int(max_entries))

    def _walk(self) -> Iterator[str]:
        count = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not name.startswith(".")
                and name not in {"node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
            )
            relative = Path(dirpath).relative_to(self.root)
            for directory in dirnames:
                yield (relative / directory).as_posix() + "/"
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                yield (relative / filename).as_posix()
                count += 1
                if count >= self.max_entries:
                    return
            if count >= self.max_entries:
                return

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        """The local paths the ``@mention`` at the cursor names."""
        del complete_event
        match = _AT_RE.search(document.text_before_cursor)
        if match is None:
            return
        fragment = match.group("fragment")
        directory_part, _, name_part = fragment.rpartition("/")
        prefix = f"{directory_part}/" if directory_part else ""
        emitted = 0
        seen: set[str] = set()
        for candidate in self._walk():
            if not candidate.startswith(prefix):
                continue
            rest = candidate[len(prefix) :]
            if not rest:
                continue
            segments = rest.split("/")
            head = segments[0]
            if not head.startswith(name_part):
                continue
            is_directory = len(segments) > 1 or candidate.endswith("/")
            completion_text = prefix + head + ("/" if is_directory else "")
            if completion_text == fragment or completion_text in seen:
                continue
            seen.add(completion_text)
            yield Completion(
                completion_text,
                start_position=-len(fragment),
                # A local file name is as untrusted as server text (a repository
                # can hold ``notes<RLO>txt.sh``); what is inserted stays the real
                # name so the file still resolves.
                display=plain(completion_text),
            )
            emitted += 1
            if emitted >= self.limit:
                return


def _argument_request(text: str) -> tuple[str, str] | None:
    """The command and the partial argument of a line that names a command.

    A line is an argument request when a command token is followed by a
    space and the token names a command of the palette. A line that names no
    command is not one, so a sentence the operator is writing keeps its
    ``@path`` completions.
    """
    head, separator, tail = text.rpartition(" ")
    if not separator:
        return None
    command = head.strip()
    if not command or " " in command:
        return None
    if canonical_command(command) is None:
        return None
    return command, tail.strip()


class TuiCompleter(Completer):
    """The composer's completer: slash commands, then arguments, then ``@paths``.

    A line that starts with a slash and holds no space is a command query and
    is answered by the matcher of the palette. A line that names a command
    and holds a space after it is an argument query and is answered by the
    ids of that command. Everything else is a ``@path``.
    """

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        id_provider: IdProvider | None = None,
    ) -> None:
        """Take the root the ``@paths`` are walked in and the ids on offer."""
        self.slash = SlashCompleter()
        self.files = AtFileCompleter(root)
        self.id_provider = id_provider

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        """The completion the line asks for: a command, an argument, a path."""
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            yield from self.slash.get_completions(document, complete_event)
            return
        if _AT_RE.search(text) is not None:
            yield from self.files.get_completions(document, complete_event)
            return
        request = _argument_request(text)
        if request is not None:
            command, partial = request
            result = complete_arguments(
                command, partial, provider=self.id_provider
            )
            # A prefix that names more than one id is ambiguous: the menu
            # lists the matches and the completion puts the prefix back, so
            # nothing is inserted until the operator types more of the id.
            ambiguous = bool(partial) and len(result.options) > 1
            for option in result.options:
                yield Completion(
                    partial if ambiguous else option.id,
                    start_position=-len(partial),
                    display=option.id,
                    display_meta=option.subject or option.phase,
                )
            return
        yield from self.files.get_completions(document, complete_event)


class AttachmentError(ValueError):
    """A file that cannot be attached to the prompt.

    The reason lives in the exception, so the shape of the message — the
    mention, then what is wrong with it — is written down once instead of at
    every raise. It is a ``ValueError``, so a caller that catches one for a
    bad attachment keeps catching it.
    """

    def __init__(self, path: str, reason: str) -> None:
        """Name the mention and what is wrong with it."""
        super().__init__(f"@{path} {reason}")


def _read_attachment(path: Path) -> Attachment:
    resolved = path.expanduser()
    if not resolved.is_file():
        raise AttachmentError(path, "is not a file")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise AttachmentError(path, f"cannot be read: {exc}") from exc
    if size > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            path, f"is {size} bytes; the attachment limit is {MAX_ATTACHMENT_BYTES}"
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise AttachmentError(path, f"cannot be read: {exc}") from exc
    if b"\x00" in data:
        raise AttachmentError(path, "looks like a binary file")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttachmentError(path, "is not UTF-8 text") from exc
    return Attachment(path=str(path), content=content, size=size)


def expand_mentions(
    body: str,
    *,
    root: Path | str | None = None,
    max_attachments: int = MAX_ATTACHMENTS,
    reader: Callable[[Path], Attachment] = _read_attachment,
) -> tuple[str, list[Attachment], list[str]]:
    """Attach local ``@files`` to a prompt.

    Returns ``(prompt, attachments, errors)``. The original line is preserved;
    the prompt gets one fenced section per attachment so the server sees the
    file content and its path. Errors are returned rather than raised so the
    TUI can show them without losing the typed line.
    """
    base = Path(root or Path.cwd()).expanduser()
    mentions = collect_mentions(body)
    attachments: list[Attachment] = []
    errors: list[str] = []
    for mention in mentions:
        if len(attachments) >= max_attachments:
            errors.append(f"only {max_attachments} attachments are sent per message")
            break
        candidate = Path(mention)
        target = candidate if candidate.is_absolute() else base / candidate
        try:
            attachment = reader(target)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        attachments.append(
            Attachment(path=mention, content=attachment.content, size=attachment.size)
        )
    if not attachments:
        return body, [], errors
    sections: list[str] = []
    for attachment in attachments:
        language = _language_for(attachment.path)
        fence = f"```{language}" if language else "```"
        sections.append(
            f"Attached file: {strip_controls(attachment.path)}\n{fence}\n"
            f"{strip_controls(attachment.content)}\n```"
        )
    prompt = body + "\n\n" + "\n\n".join(sections)
    return prompt, attachments, errors


def _language_for(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return {
        "py": "python",
        "ts": "typescript",
        "tsx": "tsx",
        "js": "javascript",
        "jsx": "jsx",
        "md": "markdown",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "sh": "bash",
    }.get(suffix, "")


def slash_help_lines() -> list[tuple[str, str]]:
    """The ``/help`` table, shared by the TUI and its tests."""
    return list(SLASH_COMMANDS.items())


__all__: list[Any] = [
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "SLASH_COMMANDS",
    "AtFileCompleter",
    "Attachment",
    "AttachmentError",
    "SlashCompleter",
    "TuiCompleter",
    "expand_mentions",
    "slash_help_lines",
]
