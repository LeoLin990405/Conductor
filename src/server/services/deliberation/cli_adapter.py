"""Local CLI participant for deliberation.

Runs the same non-interactive argv a daemon would (`codex exec`, `kimi -p`,
`grok -p`) and records ``daemon_id`` for a later parallel hand-off. It does
not claim tickets and it does not call the daemon HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from src.llms.native_cli.process import kill_child_group, spawn_group
from src.server.services.deliberation.errors import DeliberationError

# Critique prompts include the prior transcript. Keep the latest lines when
# that transcript would push the prompt to this size.
_PROMPT_LIMIT = 12000

_BALLOT = re.compile(r"(?im)^BALLOT:\s*(agree|dissent)\s*$")
_COMMENT = re.compile(r"(?im)^COMMENT:\s*(.*)$")
_CRITIQUE = re.compile(r"(?im)^CRITIQUE:\s*(.*)$")
_BODY = re.compile(r"(?im)^BODY:\s*(.*)$")

_BIN_ENV = {
    "codex": "NATIVE_CLI_CODEX_BIN",
    "kimi": "NATIVE_CLI_KIMI_BIN",
    "grok": "NATIVE_CLI_GROK_BIN",
}

CliRunner = Callable[[list[str], str | None, str | None, float], Awaitable[Any]]


def command_argv(
    provider: str, binary: str, prompt: str
) -> tuple[list[str], str | None]:
    """Argv for one non-interactive turn.

    Codex reads the prompt on stdin (``codex exec ... -``) so the prompt is not
    an argv element. Kimi and grok take ``-p``.
    """
    if provider == "codex":
        return [
            binary,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-",
        ], prompt
    if provider == "kimi":
        return [binary, "-p", prompt], None
    if provider == "grok":
        return [binary, "-p", prompt], None
    raise DeliberationError("unknown_provider", f"unknown provider {provider}")


def _unwrap_cli_text(text: str) -> str:
    """Use a grok JSON document's ``text`` (or ``result``) string when present."""
    stripped = text.strip().lstrip("\ufeff")
    if not stripped.startswith("{"):
        return stripped
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if not isinstance(payload, dict):
        return stripped
    inner = payload.get("text")
    if not isinstance(inner, str):
        inner = payload.get("result")
    if isinstance(inner, str):
        return inner.strip()
    return stripped


def _matched_object(text: str, start: int) -> str | None:
    """Slice the JSON object that begins at ``start``, respecting strings."""
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _extract_plan(text: str) -> dict[str, Any] | None:
    marker = text.find("PLAN_JSON:")
    if marker < 0:
        return None
    brace = text.find("{", marker + len("PLAN_JSON:"))
    if brace < 0:
        return None
    raw = _matched_object(text, brace)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
        return parsed
    return None


def _fallback_plan(body: str, agent_key: str) -> dict[str, Any]:
    return {
        "summary": body[:200],
        "steps": [
            {
                "step_key": agent_key or "work",
                "title": f"{agent_key} step",
                "prompt": body[:500],
                "assigned_agent": agent_key or "codex",
                "depends_on": [],
            }
        ],
    }


def parse_participant_output(text: str, *, agent_key: str) -> dict[str, Any]:
    """Split one CLI reply into body, plan, critique, ballot, and comment."""
    working = _unwrap_cli_text(str(text or ""))
    body_match = _BODY.search(working)
    if body_match:
        body = body_match.group(1).strip()
    else:
        body = working.strip()[:2000]
    critique_match = _CRITIQUE.search(working)
    critique = critique_match.group(1).strip() if critique_match else ""
    comment_match = _COMMENT.search(working)
    comment = comment_match.group(1).strip() if comment_match else ""
    ballot_match = _BALLOT.search(working)
    ballot = ballot_match.group(1).lower() if ballot_match else None
    plan = _extract_plan(working) or _fallback_plan(body, agent_key)
    return {
        "body": body,
        "plan": plan,
        "critique": critique,
        "ballot": ballot,
        "comment": comment,
    }


def _objective_clause(objective: str) -> str:
    clause = " ".join(objective.split())
    if not clause:
        clause = "the objective"
    if len(clause) > 160:
        clause = clause[:157].rstrip() + "..."
    return clause


def _example_plan_json(objective: str) -> str:
    """Canonical three-step plan. Spec and impl are independent; review waits."""
    clause = _objective_clause(objective)
    plan = {
        "summary": f"Deliberate on {clause}",
        "steps": [
            {
                "step_key": "spec",
                "title": "Write the spec",
                "prompt": f"Write the spec for {clause}",
                "assigned_agent": "codex",
                "depends_on": [],
            },
            {
                "step_key": "impl",
                "title": "Implement",
                "prompt": f"Implement {clause}",
                "assigned_agent": "kimi",
                "depends_on": [],
            },
            {
                "step_key": "review",
                "title": "Review",
                "prompt": f"Review {clause}",
                "assigned_agent": "grok",
                "depends_on": ["spec", "impl"],
            },
        ],
    }
    return json.dumps(plan, ensure_ascii=False, separators=(",", ":"))


def _prompt_propose(agent_key: str, objective: str) -> str:
    return (
        f"You are {agent_key}, one participant in a multi-agent deliberation.\n"
        "Do not modify files. Do not call tools. Reply with exactly these lines and nothing else:\n"
        "BODY: <one sentence>\n"
        f"PLAN_JSON: {_example_plan_json(objective)}\n"
        "\n"
        "Objective:\n"
        f"{objective}\n"
    )


def _render_transcript(transcript: object) -> str:
    if not isinstance(transcript, (list, tuple)):
        return ""
    lines: list[str] = []
    for item in transcript:
        if isinstance(item, dict):
            agent_key = str(item.get("agent_key") or "")
            kind = str(item.get("kind") or "")
            body = str(item.get("body") or "")
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            agent_key, kind, body = str(item[0]), str(item[1]), str(item[2])
        else:
            continue
        lines.append(f"{agent_key} {kind}: {body}")
    return "\n".join(lines)


def _prompt_critique(agent_key: str, ctx: dict) -> str:
    objective = str(ctx.get("objective") or "")[:2000]
    transcript = _render_transcript(ctx.get("transcript"))
    head = (
        f"You are {agent_key}, one participant in a multi-agent deliberation.\n"
        "Do not modify files. Do not call tools. Reply with exactly these lines and nothing else:\n"
        "BODY: <one sentence>\n"
        "CRITIQUE: <one sentence>\n"
        f"PLAN_JSON: {_example_plan_json(objective)}\n"
        "\n"
        "Objective:\n"
        f"{objective}\n"
        "\n"
        "Transcript:\n"
    )
    room = (_PROMPT_LIMIT - 1) - len(head)
    if room <= 0:
        return head[: _PROMPT_LIMIT - 1]
    if len(transcript) > room:
        transcript = transcript[-room:]
    return head + transcript


def _prompt_vote(agent_key: str, synthesis: dict) -> str:
    return (
        f"You are {agent_key}. Vote on the synthesis. Do not modify files. Reply with exactly:\n"
        "BALLOT: agree\n"
        "COMMENT: <short reason>\n"
        "Use BALLOT: agree when the plan has steps spec, impl, and review, and review depends on spec and impl. Otherwise BALLOT: dissent.\n"
        "\n"
        "Synthesis:\n"
        f"{json.dumps(synthesis)}\n"
    )


async def _kill_process(process: asyncio.subprocess.Process) -> None:
    """Kill the participant CLI's whole group and reap it.

    The group is killed even when the leader already exited: a tool or MCP
    server it started can still hold the pipes open, and that is what keeps
    ``communicate()`` from returning.
    """
    kill_child_group(process)
    await process.wait()


class LocalCliParticipantAdapter:
    """Participant that runs a local non-interactive CLI.

    ``daemon_id`` is recorded for a later parallel hand-off. This adapter does
    not claim tickets and does not synthesize; the merging planner does.
    """

    def __init__(
        self,
        *,
        agent_key: str,
        binary: str | None = None,
        cwd: str | None = None,
        timeout: float = 180,
        runner: CliRunner | None = None,
        daemon_id: str | None = None,
    ) -> None:
        self.agent_key = agent_key
        self.provider = agent_key
        self.daemon_id = daemon_id
        self.cwd = cwd
        self.timeout = timeout
        self._binary = binary
        self._runner = runner

    def binary(self) -> str:
        if self._binary:
            return self._binary
        env = _BIN_ENV.get(self.provider)
        if env and os.environ.get(env):
            return os.environ[env]
        found = shutil.which(self.provider)
        if not found:
            raise DeliberationError(
                "cli_unavailable",
                f"{self.provider} CLI not found",
            )
        return found

    async def _exec(self, argv: list[str], stdin: str | None) -> str:
        # Before the start: an encoding error after it would leave a tool-running
        # CLI that nothing is left to kill.
        payload = None if stdin is None else stdin.encode("utf-8")
        try:
            process = await spawn_group(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise DeliberationError(
                "cli_unavailable",
                f"{self.provider} CLI not found",
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            await _kill_process(process)
            raise DeliberationError(
                "cli_timeout",
                f"{self.provider} CLI timed out after {self.timeout}s",
            ) from exc
        except BaseException:
            await _kill_process(process)
            raise
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace")[:500]
            raise DeliberationError("cli_failed", detail)
        return stdout.decode("utf-8", errors="replace")

    async def _complete(self, prompt: str) -> str:
        argv, stdin = command_argv(self.provider, self.binary(), prompt)
        if self._runner is not None:
            return str(await self._runner(argv, stdin, self.cwd, self.timeout))
        print(f"deliberation cli: {self.provider}", file=sys.stderr, flush=True)
        return await self._exec(argv, stdin)

    async def propose(self, ctx: dict) -> dict[str, Any]:
        prompt = _prompt_propose(self.agent_key, str(ctx.get("objective") or ""))
        parsed = parse_participant_output(
            await self._complete(prompt),
            agent_key=self.agent_key,
        )
        return {
            "body": parsed["body"],
            "plan": parsed["plan"],
            "critique": parsed["critique"],
        }

    async def critique(self, ctx: dict) -> dict[str, Any]:
        parsed = parse_participant_output(
            await self._complete(_prompt_critique(self.agent_key, ctx)),
            agent_key=self.agent_key,
        )
        return {
            "body": parsed["body"],
            "plan": parsed["plan"],
            "critique": parsed["critique"],
        }

    async def vote(self, ctx: dict, synthesis: dict) -> dict[str, str]:
        del ctx
        parsed = parse_participant_output(
            await self._complete(_prompt_vote(self.agent_key, synthesis)),
            agent_key=self.agent_key,
        )
        ballot = parsed["ballot"] or "dissent"
        comment = parsed["comment"] or (
            "no ballot line" if parsed["ballot"] is None else ""
        )
        return {"ballot": ballot, "comment": comment}

    async def synthesize(self, *args: object, **kwargs: object) -> dict[str, Any]:
        del args, kwargs
        raise DeliberationError(
            "bad_role",
            "the local CLI participant does not synthesize",
        )
