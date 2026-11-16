"""Offline dev-adapter rehearsal for dispatch step 2.

The process environment is never changed. ``CONDUCTOR_DISPATCH_*`` is set
only on the child environment that builds the run. Status goes through the
CLI handler with an API client that answers 404, so this process does not
open a socket and does not start the backend.

``DispatchService`` drops the gate runner unless the adapter is ``live``.
This rehearsal still honours ``CONDUCTOR_DISPATCH_GATES=on`` by attaching
``GateRunner`` to the dev collaborators. That gap is reported; it is not
patched in ``src/``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import-time diagnostics belong on stderr. The redirect starts before the
# project imports, and only when this file is the process entry point.
_EARLY_STDOUT = None
if __name__ == "__main__":
    _EARLY_STDOUT = contextlib.redirect_stdout(sys.stderr)
    _EARLY_STDOUT.__enter__()

from src.tools.auto_dispatch import (  # noqa: E402
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    FileApprovalGate,
    DispatchState,
    FixedPlanner,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    MachineConfig,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
    progress_pct,
)
from src.tools.auto_dispatch.gates import GateRunner, GateSettings  # noqa: E402

GOAL_APPROVE = "Add a one-line rehearsal note"
GOAL_REJECT = "Add a second rehearsal note"
# ``child_env`` pins ``LC_ALL=C.UTF-8``, so status renders the English labels
# (``dispatch_timeline.display_lang``: only ``zh*`` renders Chinese).
GATE_LABELS = (
    "G0 SPEC",
    "D1 DAG",
    "implementing",
    "G2 focused tests",
    "primary review",
    "secondary review",
    "G5 integration",
    "G6 human",
)
_GATE_ARGV = [sys.executable, "-c", "import sys; sys.exit(0)"]
GATE_COMMAND = shlex.join(_GATE_ARGV)
PUBLIC_GATE_COMMAND = ["python", "-c", "import sys; sys.exit(0)"]

_SESSIONS: dict[str, "_Session"] = {}
_GATE_REPORTS: dict[str, dict[str, Any]] = {}
# One loop for the whole run. Each machine binds its actor queue to the loop
# that first drives it, so start and decide must share that loop.
_RUNNER: asyncio.Runner | None = None


class StepFailure(Exception):
    """One rehearsal step did not meet its assertion."""


class _Session:
    """One dev machine, kept so approve and reject resume the same run."""

    def __init__(self, env: Mapping[str, str]) -> None:
        repo = Path(env["CONDUCTOR_DISPATCH_REPO"])
        runs = Path(env["CONDUCTOR_DISPATCH_RUNS_DIR"])
        proposed = Path(env["CONDUCTOR_DISPATCH_PROPOSED_DIR"])
        self.store = InMemoryDispatchStore()
        self.gate = FileApprovalGate(proposed)
        self.machine = DispatchMachine(
            Collaborators(
                planner=FixedPlanner(),
                selector=RoundRobinSelector(),
                launcher=RecordingLauncher(),
                monitor=ScriptedMonitor(),
                reviewer=AlwaysPassReviewer(),
                merger=NoopMerger(),
                gate=self.gate,
                outcomes=InMemoryOutcomeStore(),
                store=self.store,
                gates=GateRunner(
                    GateSettings(
                        runs_dir=runs,
                        repo_root=repo,
                        g2_command=env.get("CONDUCTOR_DISPATCH_G2_COMMAND"),
                        g5_command=env.get("CONDUCTOR_DISPATCH_G5_COMMAND"),
                        g5_timeout_seconds=3600.0,
                    )
                ),
            ),
            config=MachineConfig(
                poll_interval_seconds=float(env["CONDUCTOR_DISPATCH_POLL_SECONDS"])
            ),
        )

    async def start(self, dispatch_id: str, goal: str) -> Dispatch:
        created = await self.store.create(
            Dispatch(
                id=dispatch_id,
                goal=goal,
                user_id="rehearsal",
                created_at="2026-11-06T00:00:00+00:00",
            )
        )
        return await self.machine.run(created)

    async def decide(self, dispatch_id: str, approved: bool) -> Dispatch:
        parked = await self.store.get(dispatch_id)
        if parked is None:
            raise StepFailure("dispatch disappeared before the human gate")
        await self.gate.resolve_dispatch(dispatch_id, approved, "")
        return await self.machine.run(parked)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the rehearsal. ``0`` passed, ``1`` a step failed, ``2`` bad root."""
    parser = argparse.ArgumentParser(prog="e2e_rehearsal")
    parser.add_argument("--root", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).expanduser().resolve()
    if not _root_is_usable(root):
        print("root must be absent or an empty directory", file=sys.stderr)
        return 2
    evidence: dict[str, Any] = {
        "proof": {
            "adapter": "dev",
            "gates": "on",
            "api": "not-contacted",
            "launcher": "recording",
        },
        "steps": [],
    }
    caller = dict(os.environ)
    global _RUNNER
    _SESSIONS.clear()
    _GATE_REPORTS.clear()
    prepare_error: BaseException | None = None
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sys.stderr), asyncio.Runner() as runner:
            _RUNNER = runner
            env = child_env(root)
            try:
                layout(root)
                seed_repo(root / "repo", env)
                approve_id = "e2e" + uuid.uuid4().hex[:8]
                reject_id = "e2e" + uuid.uuid4().hex[:8]
                _prepare_refs(root / "repo", approve_id, env)
                _prepare_refs(root / "repo", reject_id, env)
            except BaseException as exc:
                _record_prepare(evidence, exc, root, started)
                prepare_error = exc
            if prepare_error is None:
                parked = _run_step(
                    evidence,
                    "start",
                    ["drive_dev", approve_id],
                    "in_process:drive_dev",
                    approve_id,
                    lambda: _start_checked(env, approve_id, GOAL_APPROVE),
                )
                if isinstance(parked, Dispatch):
                    _run_step(
                        evidence,
                        "status",
                        ["conductor", "dispatch", "status", approve_id],
                        "cli",
                        approve_id,
                        lambda: _status_step(root, approve_id, parked),
                    )
                    _run_step(
                        evidence,
                        "approve",
                        ["decide", approve_id, "approve"],
                        "in_process:decide",
                        approve_id,
                        lambda: _approve_step(root, approve_id),
                    )
                rejected = _run_step(
                    evidence,
                    "start-reject",
                    ["drive_dev", reject_id],
                    "in_process:drive_dev",
                    reject_id,
                    lambda: _start_checked(env, reject_id, GOAL_REJECT),
                )
                if isinstance(rejected, Dispatch):
                    _run_step(
                        evidence,
                        "reject",
                        ["decide", reject_id, "reject"],
                        "in_process:decide",
                        reject_id,
                        lambda: _reject_step(root, reject_id),
                    )
    except StepFailure:
        pass
    finally:
        _RUNNER = None
        _SESSIONS.clear()
        if dict(os.environ) != caller:
            os.environ.clear()
            os.environ.update(caller)
    _release_early_stdout()
    _scrub(evidence, root)
    _write_evidence(root, evidence, json_stdout=bool(args.json))
    if isinstance(prepare_error, (KeyboardInterrupt, SystemExit)):
        raise prepare_error
    if prepare_error is not None:
        return 1
    failed = [step for step in evidence["steps"] if not step["assertion"]["ok"]]
    return 1 if failed or not evidence["steps"] else 0


def child_env(root: Path) -> dict[str, str]:
    """Dispatch variables for the child only. The caller env is not copied."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(root / "home"),
        "CONDUCTOR_DISPATCH_ADAPTERS": "dev",
        "CONDUCTOR_DISPATCH_GATES": "on",
        "CONDUCTOR_DISPATCH_POLL_SECONDS": "2",
        "CONDUCTOR_DISPATCH_G2_COMMAND": GATE_COMMAND,
        "CONDUCTOR_DISPATCH_G5_COMMAND": GATE_COMMAND,
        "CONDUCTOR_DISPATCH_RUNS_DIR": str(root / "runs"),
        "CONDUCTOR_DISPATCH_PROPOSED_DIR": str(root / "proposed"),
        "CONDUCTOR_DISPATCH_HOME": str(root / "home"),
        "CONDUCTOR_DISPATCH_REPO": str(root / "repo"),
    }


def layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("runs", "proposed", "home", "repo"):
        (root / name).mkdir(parents=True, exist_ok=True)


def seed_repo(repo: Path, env: Mapping[str, str]) -> None:
    """One commit with a single Alembic head, so G5 can check the worktree."""
    if (repo / ".git").exists():
        return
    versions = repo / "migrations" / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "140_rehearsal.py").write_text(
        'revision = "140"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (repo / "README").write_text("rehearsal\n", encoding="utf-8")
    _git(repo, env, ["init", "-b", "main"])
    _git(repo, env, ["add", "migrations", "README"])
    _git(repo, env, ["commit", "-m", "seed the rehearsal repo"])


def _start_checked(env: Mapping[str, str], dispatch_id: str, goal: str) -> Dispatch:
    parked = drive_dev(env, dispatch_id, goal)
    if parked.state is not DispatchState.APPROVAL:
        raise StepFailure("dev run did not reach approval")
    _GATE_REPORTS[dispatch_id] = _gate_report(parked)
    return parked


def _gate_report(dispatch: Dispatch) -> dict[str, Any]:
    """Exit codes copied from the gate record, plus the public command form.

    The process runs the current interpreter. Evidence keeps the basename
    ``python`` so the interpreter's absolute path stays out of the file.
    """
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    lines = gates.get("lines") if isinstance(gates.get("lines"), dict) else {}
    recorded: list[dict[str, Any]] = []
    if not lines:
        raise StepFailure("G2 did not run a command")
    for line_id in sorted(lines):
        snap = lines[line_id]
        gate = snap.get("gates") if isinstance(snap, dict) and isinstance(snap.get("gates"), dict) else {}
        focused = gate.get("G2") if isinstance(gate.get("G2"), dict) else {}
        commands = focused.get("commands") if isinstance(focused.get("commands"), list) else []
        if not commands:
            raise StepFailure("G2 did not run a command")
        for item in commands:
            if not isinstance(item, dict) or item.get("exit_code") != 0:
                raise StepFailure("G2 command failed")
            recorded.append({"line": str(line_id), "exit_code": 0})
    integration = gates.get("integration") if isinstance(gates.get("integration"), dict) else {}
    checks = integration.get("checks") if isinstance(integration.get("checks"), dict) else {}
    full = checks.get("full_suite") if isinstance(checks.get("full_suite"), dict) else {}
    if full.get("exit_code") != 0:
        raise StepFailure("G5 command failed")
    return {"command": list(PUBLIC_GATE_COMMAND), "g2": recorded, "g5_exit_code": 0}


def drive_dev(env: Mapping[str, str], dispatch_id: str, goal: str) -> Dispatch:
    """Start one dev run and remember its machine for a later human decision."""
    session = _Session(env)
    _SESSIONS[dispatch_id] = session
    return _run_on_loop(session.start(dispatch_id, goal))


def _run_on_loop(coro: Coroutine[Any, Any, Dispatch]) -> Dispatch:
    """Run one step on the loop shared by every step of this rehearsal."""
    if _RUNNER is None:
        coro.close()
        raise StepFailure("rehearsal event loop is not running")
    return _RUNNER.run(coro)


def present_status(home: Path, dispatch_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Human status in a child process whose env holds the dispatch variables.

    The child calls the CLI handler with an API client that only raises 404.
    The parent process environment is not modified, and no socket is opened.
    """
    home.mkdir(parents=True, exist_ok=True)
    record = home / f"{dispatch_id}.json"
    record.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    out = home / f"{dispatch_id}.status.json"
    completed = subprocess.run(
        [sys.executable, "-c", _STATUS_SNIPPET, dispatch_id, str(out)],
        cwd=home.parent,
        env=child_env(home.parent),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not out.is_file():
        raise StepFailure("status child failed")
    presented = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    if not isinstance(presented, dict):
        raise StepFailure("status did not return a record")
    return presented


_STATUS_SNIPPET = """
import json
import sys
from pathlib import Path

from conductor_cli.client import APIError
from conductor_cli.dispatch import _status


class Absent:
    def get(self, _path):
        raise APIError("HTTP 404 from Conductor", 404)


presented = _status(sys.argv[1], Absent(), as_json=False)
Path(sys.argv[2]).write_text(json.dumps(presented), encoding="utf-8")
"""


def summary_of(dispatch: Dispatch) -> dict[str, Any]:
    return {
        "id": dispatch.id,
        "goal": dispatch.goal,
        "state": dispatch.state.value,
        "progress_pct": progress_pct(dispatch),
        "integrate_branch": dispatch.integrate_branch,
        "approval_token": dispatch.approval_token,
        "message": dispatch.message,
        "lines": [line.to_dict() for line in dispatch.lines],
        "outcomes": {key: value.to_dict() for key, value in dispatch.outcomes.items()},
        "gates": dict(dispatch.gates) if isinstance(dispatch.gates, dict) else {},
    }


def assert_gate_order(rendered: list[str]) -> None:
    """The eight named positions, in order, on every line that reached G0.

    The status child runs in the C locale, so the labels are the English
    ones (``implementing``, ``secondary review``). The rehearsal brief names
    the secondary position ``次审``.
    """
    blocks = "\n".join(rendered).split("G0 SPEC")[1:]
    if not blocks:
        raise StepFailure("status did not render G0 SPEC")
    for block in blocks:
        cursor = 0
        for label in GATE_LABELS[1:]:
            found = block.find(label)
            if found < cursor:
                raise StepFailure("gate order is not G0 through G6")
            cursor = found + len(label)


def assert_record_and_rendered(summary: dict[str, Any], presented: dict[str, Any]) -> None:
    for key, value in summary.items():
        if presented.get(key) != value:
            raise StepFailure("human status dropped or changed a record field")
    rendered = presented.get("rendered")
    if not isinstance(rendered, list) or not rendered:
        raise StepFailure("human status has no rendered lines")
    if not all(isinstance(line, str) for line in rendered):
        raise StepFailure("human status has no rendered lines")


def assert_g6_rejected(rendered: list[str]) -> None:
    g6 = [line for line in rendered if "G6" in line]
    if not g6:
        raise StepFailure("reject status has no G6 line")
    blob = "\n".join(line.strip() for line in g6)
    needles = ("human_rejected", "cancelled", "blocked")
    if not any(word in blob for word in needles):
        raise StepFailure("G6 line does not show a rejection or cancellation: " + blob)


def _status_step(root: Path, dispatch_id: str, parked: Dispatch) -> dict[str, Any]:
    presented = present_status(root / "home", dispatch_id, summary_of(parked))
    rendered = presented.get("rendered")
    if not isinstance(rendered, list):
        raise StepFailure("status did not render lines")
    assert_gate_order(rendered)
    _reject_token(rendered, parked.approval_token)
    return presented


def _approve_step(root: Path, dispatch_id: str) -> dict[str, Any]:
    finished = _run_on_loop(_SESSIONS[dispatch_id].decide(dispatch_id, True))
    summary = summary_of(finished)
    presented = present_status(root / "home", dispatch_id, summary)
    assert_record_and_rendered(summary, presented)
    rendered = presented["rendered"]
    _reject_token(rendered, finished.approval_token)
    return presented


def _reject_step(root: Path, dispatch_id: str) -> dict[str, Any]:
    finished = _run_on_loop(_SESSIONS[dispatch_id].decide(dispatch_id, False))
    summary = summary_of(finished)
    presented = present_status(root / "home", dispatch_id, summary)
    rendered = presented.get("rendered")
    if not isinstance(rendered, list):
        raise StepFailure("reject status did not render lines")
    assert_g6_rejected(rendered)
    _reject_token(rendered, finished.approval_token)
    return presented


def _reject_token(rendered: list[str], token: str | None) -> None:
    if token and token in "\n".join(rendered):
        raise StepFailure("rendered status leaked the approval token")


def _run_step(
    evidence: dict[str, Any],
    name: str,
    command: list[str],
    invocation: str,
    run_id: str,
    action: Callable[[], Any],
) -> Any:
    started = time.perf_counter()
    try:
        value = action()
    except StepFailure as exc:
        _record_step(
            evidence, name, command, invocation, run_id, started, ok=False, detail=str(exc)
        )
        raise
    except Exception as exc:
        _record_step(
            evidence,
            name,
            command,
            invocation,
            run_id,
            started,
            ok=False,
            detail=type(exc).__name__,
        )
        raise StepFailure(name) from None
    _record_step(evidence, name, command, invocation, run_id, started, ok=True, detail="ok")
    return value


def _record_step(
    evidence: dict[str, Any],
    name: str,
    command: list[str],
    invocation: str,
    run_id: str,
    started: float,
    *,
    ok: bool,
    detail: str,
) -> None:
    step: dict[str, Any] = {
        "name": name,
        "invocation": invocation,
        "command": command,
        "exit_code": 0 if ok else 1,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "run_id": run_id,
        "assertion": {"ok": ok, "detail": detail},
    }
    report = _GATE_REPORTS.get(run_id)
    if report is not None and name in {"start", "start-reject"}:
        step["gates"] = report
    evidence["steps"].append(step)


def _prepare_refs(repo: Path, dispatch_id: str, env: Mapping[str, str]) -> None:
    """The integrate branch and the three dev line branches, before the machine runs."""
    _git(repo, env, ["branch", f"integrate/auto-dispatch-{dispatch_id}"])
    for index in range(3):
        _git(repo, env, ["branch", f"feature/auto-dispatch-{dispatch_id}/line-line-{index}"])


def _git(repo: Path, env: Mapping[str, str], args: list[str]) -> None:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=rehearsal",
            "-c",
            "user.email=rehearsal@invalid",
            "-C",
            str(repo),
            *args,
        ],
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise StepFailure("git failed")


def _root_is_usable(root: Path) -> bool:
    if root.exists() and not root.is_dir():
        return False
    if root.is_dir() and any(root.iterdir()):
        return False
    return True


def _approval_tokens(root: Path) -> list[str]:
    found: list[str] = []
    for path in (root / "home").glob("*.json") if (root / "home").is_dir() else []:
        token = _token_at(path, "approval_token")
        if token:
            found.append(token)
    proposed = root / "proposed"
    if proposed.is_dir():
        for path in proposed.glob("*/gate.json"):
            token = _token_at(path, "token")
            if token:
                found.append(token)
    return found


def _token_at(path: Path, key: str) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    token = data.get(key)
    if isinstance(token, str) and len(token) >= 8:
        return token
    return ""


def _redact(value: Any, tokens: list[str]) -> Any:
    if isinstance(value, str):
        for token in tokens:
            value = value.replace(token, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, tokens) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item, tokens) for key, item in value.items()}
    return value


def _scrub(evidence: dict[str, Any], root: Path) -> None:
    """Redact approval tokens, then fail if a home path or an email remains."""
    redacted = _redact(evidence, _approval_tokens(root))
    evidence.clear()
    evidence.update(redacted)
    home = str(Path.home())
    blob = json.dumps(evidence, ensure_ascii=False)
    if "@" in blob or (home and home in blob):
        evidence["steps"].append(
            {
                "name": "scrub",
                "invocation": "in_process:_scrub",
                "command": ["scrub"],
                "exit_code": 1,
                "duration_ms": 0,
                "run_id": "",
                "assertion": {"ok": False, "detail": "evidence held a secret or a home path"},
            }
        )


def _release_early_stdout() -> None:
    global _EARLY_STDOUT
    redirect = _EARLY_STDOUT
    if redirect is None:
        return
    _EARLY_STDOUT = None
    redirect.__exit__(None, None, None)


def _record_prepare(
    evidence: dict[str, Any],
    exc: BaseException,
    root: Path,
    started: float,
) -> None:
    """One failed prepare step. The message has been through ``_scrub``."""
    message = _scrubbed_message(str(exc), root)
    evidence["steps"].append(
        {
            "name": "prepare",
            "invocation": "in_process:prepare",
            "command": ["prepare"],
            "exit_code": 1,
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "run_id": "",
            "error_type": type(exc).__name__,
            "message": message,
            "assertion": {"ok": False, "prepare_ok": False, "detail": message},
        }
    )


def _scrubbed_message(text: str, root: Path) -> str:
    """Run the exception text through ``_scrub``, then drop home paths and emails."""
    payload: dict[str, Any] = {"steps": [], "message": text}
    _scrub(payload, root)
    message = payload.get("message")
    if not isinstance(message, str):
        message = ""
    home = str(Path.home())
    if home:
        message = message.replace(home, "[home]")
    if "@" in message:
        message = " ".join("[redacted]" if "@" in part else part for part in message.split())
    return message


def _write_evidence(root: Path, evidence: dict[str, Any], *, json_stdout: bool) -> None:
    text = json.dumps(evidence, ensure_ascii=False, indent=2)
    if root.is_dir():
        try:
            (root / "evidence.json").write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass
    if json_stdout:
        print(text)


if __name__ == "__main__":
    raise SystemExit(main())
