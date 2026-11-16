"""A planner ``complete`` callable that runs Claude Code in the dispatch repo.

A plain LLM call cannot see the repository, so the paths it names are guesses.
:class:`ClaudeCodeComplete` runs the Claude Code CLI non-interactively in the
dispatch repo root, read-only and isolated, and hands the final result text back
to :class:`~src.tools.auto_dispatch.planning.LLMPlanner`, which validates it
exactly as it validates any other planner reply.

The CLI is started as::

    claude -p --output-format json --permission-mode plan --model <model>
        --restricted --safe-mode --strict-mcp-config --disable-slash-commands
        --settings '{"disableAllHooks": true}'
        --tools Read,Grep,Glob --allowedTools Read,Grep,Glob
        --disallowedTools <write/exec/network tools>
        --no-session-persistence --append-system-prompt <system>

with the planning prompt on stdin. Plan mode is advice the model follows, not
an isolation boundary: a hook in the repo's ``.claude/settings.json`` would run
under it. What isolates the run (the same boundary as the read-only reviewer in
``src/llms/native_cli/claude.py``, Claude Code 2.1.309 ``--help``):

- ``--restricted`` ignores user, project and local settings files (so their
  hooks), removes the code-running tools and confines file tools to the cwd;
- ``--safe-mode`` disables CLAUDE.md, skills, installed plugins, hooks, MCP
  servers, custom commands and agents;
- ``--strict-mcp-config`` (no ``--mcp-config``) loads no MCP server;
- ``--disable-slash-commands`` disables skills and commands;
- ``--settings '{"disableAllHooks": true}'`` turns hooks off in the one
  settings layer ``--restricted`` still reads;
- ``--tools`` makes Read, Grep and Glob the only built-in tools that exist.

Every failure raises :class:`ClaudePlannerError` whose message is a fixed
reason code (and an exit code at most): neither the model's output nor the
CLI's stderr reaches the exception text or a log line, so nothing of it can
land in a dispatch message. The error also carries the fixed vocabulary
reason the machine parks the dispatch with
(:mod:`src.tools.auto_dispatch.planner_reason`): ``planner_failed`` on its own
would say that planning failed, not why. Raw or failed model output is never
stored as a plan: only a reply that ``LLMPlanner`` has validated becomes lines
and a SPEC archive.

The CLI runs in its own process group. The group is SIGKILLed on timeout, on
cancellation and after a normal exit, even when the CLI itself has already
exited, so no descendant it left behind outlives the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Sequence
from pathlib import Path

from .planner_reason import (
    INVALID_JSON,
    NO_JSON,
    PLANNER_ERROR,
    PLANNER_TIMEOUT,
    PlannerFailure,
)

logger = logging.getLogger(__name__)

DEFAULT_PLANNER_BIN = "claude"
DEFAULT_PLANNER_MODEL = "claude-opus-5-5"
DEFAULT_PLANNER_TIMEOUT_SECONDS = 600.0

# The only built-in tools the planner run has: reading and searching the repo.
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")
# Tools that write, run code or reach the network are refused outright as well,
# in case a later CLI widens what --tools or --restricted leave in place.
DENIED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "MultiEdit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
)
# The one settings layer --restricted still reads; hooks are off in it too.
ISOLATION_SETTINGS = json.dumps({"disableAllHooks": True}, sort_keys=True)
# Flags that switch off user/project/local settings, hooks, plugins, CLAUDE.md,
# skills, slash commands and MCP servers while keeping repo-file reading.
ISOLATION_FLAGS: tuple[str, ...] = (
    "--restricted",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--settings",
    ISOLATION_SETTINGS,
)

_REAP_SECONDS = 5.0


async def _spawn(*argv: str, **kwargs) -> asyncio.subprocess.Process:
    """Start the CLI. A module seam, so the unit suite can refuse the real binary."""
    return await asyncio.create_subprocess_exec(*argv, **kwargs)


class ClaudePlannerError(PlannerFailure):
    """A Claude Code planner run failed. The message is a reason code only.

    The message is the CLI's own reason code; ``reason`` is the fixed
    vocabulary the dispatch is parked with, so the machine never has to read
    the message to know why the planner failed.
    """

    def __init__(self, message: str, *, reason: str = PLANNER_ERROR) -> None:
        super().__init__(reason, message)


class ClaudeCodeComplete:
    """``complete(prompt, system) -> str`` backed by an isolated, read-only Claude Code run."""

    def __init__(
        self,
        *,
        cwd: Path | str,
        binary: str = DEFAULT_PLANNER_BIN,
        model: str = DEFAULT_PLANNER_MODEL,
        timeout_seconds: float = DEFAULT_PLANNER_TIMEOUT_SECONDS,
        allowed_tools: Sequence[str] = READ_ONLY_TOOLS,
        denied_tools: Sequence[str] = DENIED_TOOLS,
        json_schema: dict | None = None,
    ) -> None:
        self.cwd = Path(cwd)
        self.binary = binary or DEFAULT_PLANNER_BIN
        self.model = model or DEFAULT_PLANNER_MODEL
        self.timeout_seconds = float(timeout_seconds)
        self.allowed_tools = tuple(allowed_tools)
        self.denied_tools = tuple(denied_tools)
        # When set, the CLI enforces structured output (--json-schema) and the
        # envelope's structured_output is returned as JSON text.
        self.json_schema = json_schema

    def argv(self, system: str) -> list[str]:
        """The CLI argv for one run. The prompt itself goes on stdin."""
        tools = ",".join(self.allowed_tools)
        return [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--model",
            self.model,
            *ISOLATION_FLAGS,
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--disallowedTools",
            ",".join(self.denied_tools),
            "--no-session-persistence",
            *(
                ("--json-schema", json.dumps(self.json_schema, separators=(",", ":")))
                if self.json_schema is not None
                else ()
            ),
            "--append-system-prompt",
            system,
        ]

    async def __call__(self, prompt: str, system: str) -> str:
        argv = self.argv(system)
        try:
            proc = await _spawn(
                *argv,
                cwd=str(self.cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            logger.warning("Claude Code planner could not start: %s", type(exc).__name__)
            raise ClaudePlannerError(
                "claude_planner_unavailable", reason=PLANNER_ERROR
            ) from None
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            await _kill_group(proc)
            logger.warning(
                "Claude Code planner timed out after %.0fs", self.timeout_seconds
            )
            raise ClaudePlannerError(
                "claude_planner_timeout", reason=PLANNER_TIMEOUT
            ) from None
        except BaseException:
            # Cancellation (or anything else): no descendant outlives the call.
            await _kill_group(proc)
            raise
        # A clean exit can still leave a descendant that let go of the pipes.
        await _kill_group(proc)
        if proc.returncode != 0:
            # stderr is deliberately dropped: it can quote the model or a credential.
            logger.warning("Claude Code planner exited %s", proc.returncode)
            raise ClaudePlannerError(
                f"claude_planner_exit:{proc.returncode}", reason=PLANNER_ERROR
            )
        return parse_result_envelope(stdout)


def parse_result_envelope(stdout: bytes | str) -> str:
    """The ``result`` text of a ``--output-format json`` envelope, or raise.

    The envelope must be one JSON object of ``type`` ``result`` that is not an
    error and carries a string ``result``. A rejected envelope is dropped: its
    text is neither logged nor returned.
    """
    try:
        text = stdout.decode("utf-8") if isinstance(stdout, bytes) else stdout
        envelope = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        logger.warning("Claude Code planner printed a reply that is not JSON")
        raise ClaudePlannerError(
            "claude_planner_bad_json", reason=INVALID_JSON
        ) from None
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        logger.warning("Claude Code planner reply is not a result envelope")
        raise ClaudePlannerError("claude_planner_bad_envelope", reason=INVALID_JSON)
    if envelope.get("is_error") or envelope.get("subtype", "success") != "success":
        logger.warning("Claude Code planner reported an error result")
        raise ClaudePlannerError("claude_planner_error_result", reason=PLANNER_ERROR)
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        # Enforced by --json-schema: already one object of the plan's shape.
        return json.dumps(structured)
    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        logger.warning("Claude Code planner result is missing or empty")
        raise ClaudePlannerError("claude_planner_empty_result", reason=NO_JSON)
    return result


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the CLI's whole process group, then reap the CLI.

    The group is signalled whether or not the CLI has exited: a descendant can
    outlive its parent and hold the output pipes open. The group id is the
    CLI's pid (``start_new_session``), and a pid is not reused while a group of
    that id still has members, so the signal cannot reach an unrelated process.
    Only when the group cannot be signalled is the CLI killed on its own.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # the group is already empty
    except OSError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    try:
        # Shielded so a second cancellation cannot leave the CLI unreaped.
        await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=_REAP_SECONDS))
    except TimeoutError:
        logger.warning("Claude Code planner process did not exit after SIGKILL")
