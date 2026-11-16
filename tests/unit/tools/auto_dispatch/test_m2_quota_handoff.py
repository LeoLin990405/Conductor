"""M2c: a quota move carries a handoff pack (m2-conversation-store.md §4.3, §8).

A Worker that reports ``quota_exhausted`` moves to another developer
through N25(a) (``_move_off_limited_provider``). M2c adds a pack of what the
previous Worker was doing: from memfw (behind a port, faked here) or, when
memfw has nothing, from Conductor's own line state. Shadow records it,
enforce also puts it in the new Worker's first prompt, off does nothing.
The move itself, and who gets the line, never change.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.llms.native_cli.failure import FailureClass
from src.server.services.autonomy.roles import RolePolicy
from src.tools.auto_dispatch import LineStatus, handoff_pack
from src.tools.auto_dispatch.machine import DispatchMachine
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector
from tests.unit.tools.auto_dispatch.test_parallel_lines_n31 import (
    FailFirst,
    _assigned,
    _drive,
    _limited,
    _setup,
)

pytestmark = pytest.mark.asyncio

POLICY = RolePolicy(developer_pool=("kimi", "glm", "stepfun"))
TASK = "TASK-MARKER implement the thing"
HEADER = "Handoff pack from the previous developer of this line."
_REAL_SELECT = DispatchMachine._select_model


def _quota(run_id: str):
    return _limited(
        run_id,
        failure_class=FailureClass.QUOTA_EXHAUSTED,
        reason="worker_quota_exhausted",
        retry_after=3600,
    )


def _memfw_pack(session: str) -> dict:
    return {
        "schema": handoff_pack.SCHEMA,
        "notice": handoff_pack.NOTICE,
        "for_agent": "glm",
        "source": {
            "agent": "kimi",
            "session_id": session,
            "origin": "worker",
            "started_at": "2026-11-14T00:00:00Z",
            "updated_at": "2026-11-14T00:10:00Z",
            "turns": 3,
            "withheld": 0,
            "parser_version": "kimi/1",
        },
        "system": {
            "label": handoff_pack.SYSTEM_LABEL,
            "goal": "the goal",
            "goal_source": "task_file",
            "files_touched": ["src/a/x.py"],
            "git": None,
            "recent_turns": [
                {
                    "offset": 1,
                    "role": "assistant",
                    "ts": "2026-11-14T00:09:00Z",
                    "text": "PREVIOUS-TURN-MARKER I was halfway through x.py",
                    "withheld": False,
                    "tool_calls": [],
                }
            ],
            "lineage": {"parent_session_id": None, "previous_handoffs": []},
        },
        "truncated": False,
        "max_bytes": 16384,
    }


class FakePort:
    """A memfw stand-in: a scripted answer per call, every request kept."""

    def __init__(self, *answers: handoff_pack.PortResult) -> None:
        self.answers = list(answers)
        self.requests: list[handoff_pack.PackRequest] = []

    def build(self, request: handoff_pack.PackRequest) -> handoff_pack.PortResult:
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        return handoff_pack.PortResult.ok(_memfw_pack(request.session_id))


class DiffLauncher(FailFirst):
    """FailFirst that also reports an uncommitted diff for the relaunch."""

    async def uncommitted_diff(self, line, *, max_bytes: int = 16_000):
        return "DIFF-MARKER +changed line"


async def _run(tmp_path: Path, monkeypatch, mode: str | None, *, port=None, launcher=None):
    if mode is None:
        monkeypatch.delenv(handoff_pack.MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(handoff_pack.MODE_ENV, mode)
    launcher = launcher or FailFirst({"a": _quota})
    line = _assigned("a", "kimi", ("src/a/",))
    line.task = TASK
    store, machine, gates, service, row = await _setup(
        tmp_path, [line], launcher=launcher, parallel=1
    )
    machine._c.selector = QuotaAwareModelSelector(policy=POLICY)
    machine._c.handoff_pack_port = port
    # The previous Worker's session, as dispatch records it.
    seeded = await store.get(row.id)
    seeded.gates.setdefault("worker_recovery", {})["a"] = {
        "attempts": {},
        "session_id": "sess-kimi-1",
        "session_mode": "resume",
        "session_author": "kimi",
        "session_origin": "cli",
    }
    await store.save(seeded)
    calls: list[tuple] = []
    real = _REAL_SELECT

    async def counting(self, dispatch, line, used, **kwargs):
        chosen = await real(self, dispatch, line, used, **kwargs)
        calls.append((line.id, tuple(used), kwargs.get("role"), tuple(kwargs.get("never") or ()), chosen))
        return chosen

    monkeypatch.setattr(DispatchMachine, "_select_model", counting)
    await _drive(service, row.id)
    after = await store.get(row.id)
    return after, launcher, calls, gates


def _pack_record(after) -> dict | None:
    return after.gates["lines"]["a"].get(handoff_pack.GATE_KEY)


def _lines(dispatch) -> list[dict]:
    """The lines as stored, minus the random run-id suffix."""
    out = []
    for line in dispatch.lines:
        raw = line.to_dict()
        raw.pop("current_run", None)
        raw.pop("log_tail", None)
        out.append(raw)
    return out


async def test_shadow_is_the_default_records_the_pack_and_keeps_it_out_of_the_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    port = FakePort()
    after, launcher, _calls, _gates = await _run(tmp_path, monkeypatch, None, port=port)
    models = [model for line_id, model in launcher.calls if line_id == "a"]
    assert models[0] == "kimi" and len(models) == 2 and models[1] != "kimi"
    record = _pack_record(after)
    assert record is not None
    assert record["mode"] == "shadow" and record["injected"] is False
    assert record["from_agent"] == "kimi" and record["from_session"] == "sess-kimi-1"
    assert record["status"] == "ok" and record["source"] == "memfw" and record["bytes"] > 0
    assert Path(record["path"]).is_file()
    assert oct(Path(record["path"]).stat().st_mode & 0o777) == "0o600"
    # memfw was asked for the old session, for the new agent, with no model.
    request = port.requests[0]
    assert (request.agent, request.session_id, request.for_agent) == ("kimi", "sess-kimi-1", models[1])
    assert request.max_bytes == 16384 and request.timeout == 30.0
    assert HEADER not in launcher.tasks[-1]
    assert "PREVIOUS-TURN-MARKER" not in launcher.tasks[-1]
    assert after.line("a").status is LineStatus.PASSED


async def test_enforce_puts_the_pack_between_the_diff_and_the_task(
    tmp_path: Path, monkeypatch
) -> None:
    launcher = DiffLauncher({"a": _quota})
    after, launcher, _calls, _gates = await _run(
        tmp_path, monkeypatch, "enforce", port=FakePort(), launcher=launcher
    )
    record = _pack_record(after)
    assert record["mode"] == "enforce" and record["injected"] is True
    prompt = launcher.tasks[-1]
    assert HEADER in prompt and handoff_pack.NOTICE in prompt
    assert "\n[system]\n" in prompt
    assert "PREVIOUS-TURN-MARKER" in prompt
    diff_at = prompt.index("DIFF-MARKER")
    pack_at = prompt.index(HEADER)
    task_at = prompt.index("TASK-MARKER")
    assert diff_at < pack_at < task_at
    # The diff is shown once, not again among the blockers.
    assert prompt.count("DIFF-MARKER") == 1
    # One relaunch carried it (the failed first launch never reached a task).
    assert sum(HEADER in task for task in launcher.tasks) == 1


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (None, "memfw_not_installed"),
        (handoff_pack.PortResult.unavailable("timeout"), "timeout"),
        (handoff_pack.PortResult.unavailable("session_not_indexed"), "session_not_indexed"),
        (handoff_pack.PortResult.ok({"schema": "memfw.handoff/v1"}), "invalid_output"),
        (handoff_pack.PortResult.unavailable("store_unavailable"), "store_unavailable"),
    ],
)
async def test_memfw_unavailable_falls_back_and_the_move_is_unchanged(
    tmp_path: Path, monkeypatch, answer, code
) -> None:
    port = None if answer is None else FakePort(answer)
    after, launcher, calls, _gates = await _run(
        tmp_path / "shadow", monkeypatch, "enforce", port=port
    )
    record = _pack_record(after)
    assert record["status"] == f"unavailable:{code}"
    assert record["source"] == "conductor"
    # The Conductor-built pack has the same system section and is injected.
    pack = json.loads(Path(record["path"]).read_text())
    assert handoff_pack.pack_problem(pack) is None
    assert pack["system"]["conductor"]["predecessor"] == "kimi"
    assert pack["system"]["goal"].startswith("TASK-MARKER")
    assert HEADER in launcher.tasks[-1]
    off, off_launcher, off_calls, _ = await _run(tmp_path / "off", monkeypatch, "off")
    # Same moves, same picks, same end state as off: the pack never blocks.
    assert launcher.calls == off_launcher.calls
    assert [c[2:] for c in calls] == [c[2:] for c in off_calls]
    assert _lines(after) == _lines(off)


async def test_not_indexed_tries_the_older_session_next(tmp_path: Path, monkeypatch) -> None:
    class Manager:
        def current(self, key):
            return None

        def archived_session_ids(self, key):
            assert key.endswith(":line:a:author:kimi")
            return ("sess-kimi-0",)

    port = FakePort(handoff_pack.PortResult.unavailable("session_not_indexed"))
    monkeypatch.setattr(DispatchMachine, "_session_manager", lambda self: Manager())
    after, _launcher, _calls, _gates = await _run(tmp_path, monkeypatch, "shadow", port=port)
    assert [r.session_id for r in port.requests] == ["sess-kimi-1", "sess-kimi-0"]
    record = _pack_record(after)
    assert record["status"] == "ok" and record["from_session"] == "sess-kimi-0"


async def test_off_is_main_exactly(tmp_path: Path, monkeypatch) -> None:
    port = FakePort()
    off, off_launcher, off_calls, _ = await _run(tmp_path / "off", monkeypatch, "off", port=port)
    assert port.requests == []
    assert _pack_record(off) is None
    assert not (tmp_path / "off" / "runs" / "recovery-sessions" / handoff_pack.PACK_SUBDIR).exists()

    async def main_has_no_hook(self, *args, **kwargs):
        return None

    monkeypatch.setattr(DispatchMachine, "_quota_handoff_pack", main_has_no_hook)
    main, main_launcher, main_calls, _ = await _run(tmp_path / "main", monkeypatch, "enforce")
    assert off_launcher.calls == main_launcher.calls
    assert off_launcher.tasks == main_launcher.tasks
    assert [c[2:] for c in off_calls] == [c[2:] for c in main_calls]
    assert _lines(off) == _lines(main)
    assert sorted(off.gates) == sorted(main.gates)
    assert sorted(off.gates["lines"]["a"]) == sorted(main.gates["lines"]["a"])


async def test_m2c_adds_no_selection_logic(tmp_path: Path, monkeypatch) -> None:
    """Who takes the line is ``_select_model``'s answer, call for call."""
    off, off_launcher, off_calls, _ = await _run(tmp_path / "off", monkeypatch, "off")
    on, on_launcher, on_calls, _ = await _run(
        tmp_path / "on", monkeypatch, "enforce", port=FakePort()
    )
    assert [c[2:] for c in on_calls] == [c[2:] for c in off_calls]
    replacement = [c[-1] for c in on_calls if c[0] == "a"]
    assert on.line("a").assigned_model == replacement[0]
    assert on_launcher.calls == off_launcher.calls
    # The only thing a selector may read is "a pack, from kimi".
    assert handoff_pack.selection_input(on.gates["lines"]["a"]) == {
        "handoff_pack": True,
        "predecessor": "kimi",
    }
    # And the module itself names no selector.
    source = Path(handoff_pack.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "``"))
    )
    for name in ("_select_model(", "select(", "superagent_assign", "import memfw", "from memfw"):
        assert name not in code


async def test_a_rate_limit_move_builds_no_pack(tmp_path: Path, monkeypatch) -> None:
    """M2c is the quota path only; a rate limit moves exactly as before."""
    monkeypatch.setenv(handoff_pack.MODE_ENV, "enforce")
    port = FakePort()
    launcher = FailFirst({"a": _limited})
    line = _assigned("a", "kimi", ("src/a/",))
    store, machine, _gates, service, row = await _setup(tmp_path, [line], launcher=launcher, parallel=1)
    machine._c.selector = QuotaAwareModelSelector(policy=POLICY)
    machine._c.handoff_pack_port = port
    await _drive(service, row.id)
    after = await store.get(row.id)
    assert port.requests == []
    assert handoff_pack.GATE_KEY not in after.gates["lines"]["a"]


class SlowPort(FakePort):
    """A memfw that takes its time, as a real one may (up to 30 s)."""

    def build(self, request):
        time.sleep(0.3)
        return super().build(request)


async def test_nothing_waits_between_the_reassign_and_its_save(tmp_path: Path, monkeypatch) -> None:
    """The slow pack build happens before the move; the move and its record save together."""
    from src.tools.auto_dispatch import machine as machine_module

    events: list[str] = []
    real_reassign = machine_module.apply_human_reassign
    real_leave = DispatchMachine._leave
    real_save = DispatchMachine._save_locked

    async def reassign(*args, **kwargs):
        result = await real_reassign(*args, **kwargs)
        events.append("reassign")
        return result

    async def leave(self, awaitable):
        events.append("leave")
        return await real_leave(self, awaitable)

    async def save(self, dispatch):
        events.append("save")
        return await real_save(self, dispatch)

    monkeypatch.setattr(machine_module, "apply_human_reassign", reassign)
    monkeypatch.setattr(DispatchMachine, "_leave", leave)
    monkeypatch.setattr(DispatchMachine, "_save_locked", save)
    port = SlowPort()
    after, launcher, _calls, _gates = await _run(tmp_path, monkeypatch, "enforce", port=port)
    # The probe and the real move are the two reassign calls of the quota move.
    first = events.index("reassign")
    real = events.index("reassign", first + 1)
    following = [event for event in events[real + 1 :] if event in ("leave", "save")]
    assert following and following[0] == "save", events[first - 3 : real + 4]
    assert port.requests, "the pack was built"
    record = _pack_record(after)
    assert record["injected"] is True and record["status"] == "ok"
    assert HEADER in launcher.tasks[-1]
    assert sum(HEADER in task for task in launcher.tasks) == 1
