"""N61 U2: a fast judgment on a line's completion evidence, in shadow.

When a line has passed G2 and is about to go to review, the gate hands this
module a snapshot of what it saw. The module asks the judgment port two
yes/no questions:

* ``report_supported``: does the G2 output on the reviewed HEAD support the
  worker report's claim that the tests pass?
* ``in_scope``: are all the diff's files inside the line's write scope?

``in_scope`` is only a comparison (Planner ruling Q4): the gate's own file
comparison (N52 ``scope_violations``) is authoritative. That code result is
recorded next to the judgment, in the record's ``action``, so the offline
eval can compare the two. The judgment never overrides it.

Shadow only. ``enforce`` behaves as ``shadow`` in v1 (PRD section 3, non-goals:
no use switches to enforce), and ``off`` never builds the port. The judgment
runs as a background task, so the gate does not wait for it, and nothing it
does (or fails to do) reaches the gate's outcome or the line's state.

The material is bounded and redacted before the port sees it: command
displays are the redacted ones G2 already records, output tails and the
worker report go through the gate's N27 ``redacted_output_tail``, and every
field has a byte cap. The port records hashes and lengths, never the text.

Whose credentials: the request carries the dispatch's owning ``user_id``
(``Dispatch.user_id``), so the backend uses that user's keys and model
preferences. A dispatch without one sends ``user_id=None`` and the port
records ``no_user`` without calling the backend; this module never falls
back to the platform key.

Where it can run: only the server process has ``src.server.app.setup``
loaded, and only there does the port get an LLM backend. The CLI
auto-dispatch process (``conductor auto-dispatch ...``) never loads it, so
every U2 judgment there records ERROR ``backend_unavailable``. That is
expected in v1; this module does not wire another backend.

Shutdown: the server lifespan calls :func:`drain`, which waits for the
pending judgments within a timeout, cancels the rest, and never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The port's time budget for this use. The gate never waits on it; the
#: budget only bounds how long a background judgment lives.
BUDGET_SECONDS = 10.0
#: At most this many judgments run at once; more are skipped, not queued.
MAX_PENDING = 8

#: Byte caps on the material.
OUTPUT_BUDGET_BYTES = 1_500
REPORT_BUDGET_BYTES = 2_000
COMMANDS_BUDGET_BYTES = 2_000
FILES_BUDGET_BYTES = 3_000
SCOPE_BUDGET_BYTES = 1_500
MAX_COMMANDS = 4
MAX_FILES = 60

REPORT_SUPPORTED = "report_supported"
IN_SCOPE = "in_scope"

_SETUP_MODULE = "src.server.app.setup"
_PENDING: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class Evidence:
    """What the gate saw, copied at the hook so the line can move on."""

    dispatch_id: str
    line_id: str
    attempt: int
    head: str
    base: str
    write_scope: tuple[str, ...]
    commands: tuple[dict[str, object], ...]
    worker_report: str
    repo_root: Path
    runs_dir: Path
    scope_mode: str
    #: The dispatch's owning user; ``None`` when the dispatch has none.
    user_id: str | None = None


def _default_port() -> Any:
    """The production port, without importing the server into a CLI or test.

    The server process has ``src.server.app.setup`` loaded and gets
    ``default_port()`` (its LLM backend and the JSONL recorder). Anywhere
    else, importing the server would cost seconds and change the process
    environment, so the port has no backend: each judgment records ERROR
    ``backend_unavailable``.
    """
    from src.server.services.judgment import JudgmentPort, default_port, default_recorder

    if _SETUP_MODULE in sys.modules:
        port = default_port()
    else:
        port = JudgmentPort(None, recorder=default_recorder())
    port.budget_s = BUDGET_SECONDS
    return port


#: The seam tests patch: returns a ``JudgmentPort``-like object.
port_factory: Callable[[], Any] = _default_port


def _cap(text: str, budget: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", "ignore") + "\n[truncated]"


def _output_tail(log_ref: object, runs_dir: Path) -> str:
    """The redacted tail of one G2 command log; a ref outside ``runs_dir`` is not read."""
    from .gates import _window_size, read_log_tail, redacted_output_tail

    if not isinstance(log_ref, str) or not log_ref:
        return ""
    root = Path(runs_dir).resolve()
    path = (root / log_ref).resolve()
    if not path.is_relative_to(root):
        return ""
    try:
        raw, cut = read_log_tail(path, _window_size(OUTPUT_BUDGET_BYTES))
    except OSError:
        return ""
    return redacted_output_tail(raw, OUTPUT_BUDGET_BYTES, cut=cut)


def code_scope(evidence: Evidence) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
    """The diff's file names and the code's scope violations (N52), authoritative.

    The names are ``None`` when Git cannot produce the diff.
    """
    from .gates import git_changed_paths, scope_violations

    if not evidence.head:
        return None, ()
    changed = git_changed_paths(evidence.repo_root, evidence.head, evidence.base or "HEAD")
    if changed is None:
        return None, ()
    return changed, scope_violations(changed, evidence.write_scope)


def build_state(evidence: Evidence, changed: Sequence[str] | None) -> dict[str, str]:
    """The bounded, redacted material the port sees."""
    from .gates import redacted_output_tail

    commands: list[str] = []
    outputs: list[str] = []
    for item in evidence.commands[:MAX_COMMANDS]:
        baseline = item.get("lint_baseline")
        entry: dict[str, object] = {
            "index": item.get("index"),
            "command": str(item.get("safe_command") or "unknown"),
            "exit_code": item.get("exit_code"),
        }
        if isinstance(baseline, dict):
            entry["lint_baseline_passed"] = baseline.get("passed") is True
        commands.append(json.dumps(entry, sort_keys=True))
        tail = _output_tail(item.get("log_ref"), evidence.runs_dir)
        outputs.append(f"--- command {item.get('index')} ---\n{tail or '(no output)'}")
    if len(evidence.commands) > MAX_COMMANDS:
        commands.append(f"... {len(evidence.commands) - MAX_COMMANDS} more commands")
    if changed is None:
        files = "(diff unavailable)"
    else:
        names = list(changed[:MAX_FILES])
        if len(changed) > MAX_FILES:
            names.append(f"... {len(changed) - MAX_FILES} more files")
        files = "\n".join(names) or "(no files changed)"
    report = redacted_output_tail(
        evidence.worker_report.encode("utf-8", "replace"), REPORT_BUDGET_BYTES
    )
    return {
        "head": evidence.head[:64],
        "g2_commands": _cap("\n".join(commands) or "(none)", COMMANDS_BUDGET_BYTES),
        "g2_output_tails": _cap(
            "\n".join(outputs) or "(none)", OUTPUT_BUDGET_BYTES * MAX_COMMANDS
        ),
        "diff_files": _cap(files, FILES_BUDGET_BYTES),
        "write_scope": _cap("\n".join(evidence.write_scope) or "(none)", SCOPE_BUDGET_BYTES),
        "worker_report": report or "(no report)",
    }


def build_request(state: Mapping[str, str], user_id: str | None = None) -> Any:
    """The port request; ``user_id`` is passed through as is (``None`` -> ``no_user``)."""
    from src.server.services.judgment import (
        COMPLETION_EVIDENCE,
        BoolQuestion,
        JudgmentRequest,
        get_use,
    )

    return JudgmentRequest(
        use=COMPLETION_EVIDENCE,
        version=get_use(COMPLETION_EVIDENCE).version,
        state=dict(state),
        questions={
            REPORT_SUPPORTED: BoolQuestion(
                "Do the G2 commands, exit codes and output tails on this HEAD support "
                "the worker report's claim that the tests pass?"
            ),
            IN_SCOPE: BoolQuestion(
                "Are all the files in diff_files inside write_scope (path globs)?"
            ),
        },
        user_id=user_id or None,
    )


def action_text(
    mode: str, changed: Sequence[str] | None, violations: Sequence[str], evidence: Evidence
) -> str:
    """The action the flow took, plus the code's scope result for the eval."""
    if changed is None or not evidence.write_scope:
        in_scope = "unknown"
    else:
        in_scope = "false" if violations else "true"
    parts = [
        "g2_pass->review",
        f"code_in_scope={in_scope}",
        f"scope_violations={len(violations)}",
        f"scope_mode={evidence.scope_mode}",
    ]
    if mode == "enforce":
        parts.append("enforce=shadow_only")
    return ";".join(parts)


async def judge(evidence: Evidence) -> Any:
    """Gather, ask, record. Never raises; returns the result or ``None``."""
    try:
        from src.server.services.judgment import COMPLETION_EVIDENCE, use_mode

        mode = use_mode(COMPLETION_EVIDENCE)
        if mode == "off":
            return None
        changed, violations = await asyncio.to_thread(code_scope, evidence)
        state = await asyncio.to_thread(build_state, evidence, changed)
        port = port_factory()
        subject = f"{evidence.dispatch_id}:{evidence.line_id}"
        return await port.judge(
            build_request(state, evidence.user_id),
            action=action_text(mode, changed, violations, evidence),
            sample_key=f"{subject}:{evidence.attempt}:{evidence.head}",
            subject=subject,
        )
    except Exception:
        logger.warning("[judgment] U2 completion judgment failed", exc_info=True)
        return None


def _snapshot(
    settings: Any,
    dispatch: Any,
    line: Any,
    attempt: int,
    head: str | None,
    recorded: Sequence[Mapping[str, object]],
) -> Evidence:
    from .check_modes import SCOPE_CHECK_ENV, check_mode

    return Evidence(
        dispatch_id=str(dispatch.id),
        line_id=str(line.id),
        attempt=int(attempt),
        head=str(head or ""),
        base=str(line.base_commit or ""),
        write_scope=tuple(str(p) for p in line.write_scope if p),
        commands=tuple(dict(item) for item in recorded if isinstance(item, Mapping)),
        worker_report=str(line.log_tail or ""),
        repo_root=Path(settings.repo_root),
        runs_dir=Path(settings.runs_dir),
        scope_mode=str(check_mode(SCOPE_CHECK_ENV)),
        user_id=_owner(dispatch),
    )


def _owner(dispatch: Any) -> str | None:
    """The dispatch's owning user id, or ``None``; never a platform stand-in."""
    value = getattr(dispatch, "user_id", None)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def schedule(
    settings: Any,
    dispatch: Any,
    line: Any,
    attempt: int,
    head: str | None,
    recorded: Sequence[Mapping[str, object]],
) -> None:
    """The gate's hook: start the shadow judgment in the background. Never raises.

    ``off`` returns before anything is built. The gate does not wait for the
    task and reads nothing from it.
    """
    try:
        from src.server.services.judgment import COMPLETION_EVIDENCE, use_mode

        if use_mode(COMPLETION_EVIDENCE) == "off":
            return
        loop = asyncio.get_running_loop()
        live = _live(loop)
        if len(live) >= MAX_PENDING:
            logger.info("[judgment] U2 skipped: %d judgments pending", len(live))
            return
        evidence = _snapshot(settings, dispatch, line, attempt, head, recorded)
        task = loop.create_task(_run(evidence))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception:
        logger.warning("[judgment] U2 could not start", exc_info=True)


def _live(loop: asyncio.AbstractEventLoop) -> list[asyncio.Task[None]]:
    """This loop's unfinished judgments; tasks of a closed loop are forgotten."""
    for task in list(_PENDING):
        if task.done() or task.get_loop().is_closed():
            _PENDING.discard(task)
    return [task for task in _PENDING if task.get_loop() is loop]


async def _run(evidence: Evidence) -> None:
    await judge(evidence)


#: After the drain's timeout, how long cancelled judgments get to unwind.
_CANCEL_GRACE_SECONDS = 0.5


async def drain(timeout: float = 5.0) -> None:
    """Wait up to ``timeout`` for this loop's background judgments, then cancel the rest.

    For tests and the server lifespan at shutdown. Never raises (a cancelled
    or failed judgment is swallowed; only cancelling ``drain`` itself
    propagates). Tasks that ignore cancellation are left after a short grace.
    """
    try:
        pending = _live(asyncio.get_running_loop())
        if not pending:
            return
        budget = float(timeout)
        if not budget >= 0.0:  # negative or NaN
            budget = 0.0
        _, still = await asyncio.wait(pending, timeout=budget)
        if still:
            logger.info("[judgment] U2 drain: cancelling %d pending judgments", len(still))
            for task in still:
                task.cancel()
            await asyncio.wait(still, timeout=_CANCEL_GRACE_SECONDS)
        for task in pending:
            if task.done() and not task.cancelled():
                task.exception()  # mark retrieved; ``_run`` already never raises
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("[judgment] U2 drain failed", exc_info=True)
