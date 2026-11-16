"""N61 U2: the completion-evidence judgment G2 starts before review (shadow).

The judgment runs in the background and only records. These tests pin that
the gate's outcome and the line's state do not depend on it, that ``off``
never builds the port, that a failing port changes nothing, that the code's
scope result (N52) is recorded and never overridden, and that the material
is redacted and bounded. All synthetic; no network, no real HOME.
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.tools.auto_dispatch.completion_judgment as cj
from src.server.services.judgment import (
    COMPLETION_EVIDENCE,
    JudgmentPort,
    JudgmentRequest,
    JudgmentResult,
    JudgmentStatus,
    MemoryRecorder,
)
from src.tools.auto_dispatch.check_modes import (
    ENFORCE,
    SCOPE_CHECK_ENV,
    SHADOW,
    WORKTREE_CHECK_ENV,
)
from src.tools.auto_dispatch.gates import CommandResult, GateRunner, GateSettings
from src.tools.auto_dispatch.types import Dispatch, Line

MODE_ENV = f"CONDUCTOR_JUDGMENT_{COMPLETION_EVIDENCE.upper()}_MODE"
SAMPLE_ENV = f"CONDUCTOR_JUDGMENT_{COMPLETION_EVIDENCE.upper()}_SAMPLE"
SECRET = "sk-" "proj-abcdefghijklmnopqrstuvwxyz123456"
BEARER = "abcdefghijklmnop1234567890QRSTUV"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONDUCTOR_JUDGMENT_RECORDS", str(tmp_path / "records.jsonl"))
    monkeypatch.setenv(WORKTREE_CHECK_ENV, ENFORCE)
    monkeypatch.setenv(SCOPE_CHECK_ENV, ENFORCE)
    monkeypatch.delenv(MODE_ENV, raising=False)
    # Judge every subject (the port's default samples one in ten) and keep
    # the global kill switch out of the way.
    monkeypatch.setenv(SAMPLE_ENV, "1")
    monkeypatch.delenv("CONDUCTOR_JUDGMENT_MODE", raising=False)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


class _Repo:
    def __init__(self, root: Path, changed: Sequence[str]) -> None:
        self.root = root
        root.mkdir(parents=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.name", "Dispatch Test")
        _git(root, "config", "user.email", "dispatch@example.invalid")
        (root / "base.txt").write_text("base\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", "base")
        self.base = _git(root, "rev-parse", "HEAD")
        self.branch = "feature/auto-dispatch-d1/line-line-0"
        _git(root, "checkout", "-q", "-b", self.branch)
        for name in changed:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{name} changed\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", "work")
        self.revision = _git(root, "rev-parse", "HEAD")
        _git(root, "checkout", "-q", "main")


class _Commands:
    def __init__(self, output: bytes = b"3 passed\n", exit_code: int = 0) -> None:
        self.output = output
        self.exit_code = exit_code

    async def run(self, argv, *, cwd, timeout, log_path):
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_bytes(self.output)
        return CommandResult(exit_code=self.exit_code, duration_ms=1)


_FIXED = datetime(2026, 11, 14, tzinfo=timezone.utc)


def _gate(base: Path, repo: _Repo, commands: _Commands) -> GateRunner:
    settings = GateSettings(
        runs_dir=base / "state" / "runs",
        repo_root=repo.root,
        g2_command="pytest -q",
        worktrees_dir=base / "state" / "worktrees",
    )
    return GateRunner(settings, commands=commands, clock=lambda: _FIXED)


def _line(repo: _Repo, scope: Sequence[str], report: str = "All tests pass.") -> Line:
    return Line(
        id="line-0",
        name="line",
        task="Implement the line",
        acceptance=("cmd: pytest -q",),
        branch=repo.branch,
        base_commit=repo.base,
        write_scope=tuple(scope),
        log_tail=report,
    )


def _dispatch(line: Line) -> Dispatch:
    return Dispatch(id="d1", goal="goal", user_id="u", created_at="now", lines=[line])


class _FakeBackend:
    name = "fake"

    def __init__(self, answer: Mapping[str, Mapping[str, float]] | None = None) -> None:
        self.answer_value = answer or {"report_supported": {"true": 0.95}, "in_scope": {"true": 0.95}}
        self.requests: list[JudgmentRequest] = []

    async def answer(self, request: JudgmentRequest):
        self.requests.append(request)
        return self.answer_value


class _RaisingBackend:
    name = "boom"

    async def answer(self, request: JudgmentRequest):
        raise RuntimeError("backend down")


class _SpyPort:
    def __init__(self, inner: JudgmentPort) -> None:
        self.inner = inner
        self.calls: list[dict[str, object]] = []
        self.results: list[JudgmentResult] = []

    async def judge(self, request, **kwargs):
        self.calls.append({"request": request, **kwargs})
        result = await self.inner.judge(request, **kwargs)
        self.results.append(result)
        return result


def _install(monkeypatch: pytest.MonkeyPatch, backend) -> tuple[_SpyPort, MemoryRecorder]:
    recorder = MemoryRecorder()
    spy = _SpyPort(JudgmentPort(backend, recorder=recorder, budget_s=2.0))
    monkeypatch.setattr(cj, "port_factory", lambda: spy)
    return spy, recorder


async def _run_once(
    base: Path, changed: Sequence[str], scope: Sequence[str], *, report: str = "All tests pass.",
    output: bytes = b"3 passed\n", repo: _Repo | None = None,
) -> tuple[object, dict[str, object], dict[str, object], Dispatch]:
    repo = repo or _Repo(base / "repo", changed)
    gate = _gate(base, repo, _Commands(output))
    line = _line(repo, scope, report)
    dispatch = _dispatch(line)
    step = await gate.run_g2(dispatch, line)
    await cj.drain(5.0)
    snapshot = copy.deepcopy(gate._line(dispatch, line.id))
    return step, snapshot, line.to_dict(), dispatch


def _normalise(snapshot: dict[str, object], base: Path) -> str:
    # The two runs use different tmp roots and repos; compare everything else.
    text = json.dumps(snapshot, sort_keys=True, default=str).replace(str(base), "<base>")
    return text


async def _compare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, backend) -> _SpyPort:
    # One repository, two gates: once with the judgment off, once in ``mode``.
    repo = _Repo(tmp_path / "repo", ["in.txt"])
    monkeypatch.setenv(MODE_ENV, "off")
    off_step, off_snap, off_line, _ = await _run_once(
        tmp_path / "a", ["in.txt"], ["in.txt"], repo=repo
    )
    spy, _ = _install(monkeypatch, backend)
    monkeypatch.setenv(MODE_ENV, mode)
    on_step, on_snap, on_line, _ = await _run_once(
        tmp_path / "b", ["in.txt"], ["in.txt"], repo=repo
    )
    assert on_step == off_step
    assert on_line == off_line
    assert _normalise(on_snap, tmp_path / "b") == _normalise(off_snap, tmp_path / "a")
    return spy


@pytest.mark.asyncio
async def test_shadow_leaves_the_g2_outcome_and_state_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = await _compare(tmp_path, monkeypatch, SHADOW, _FakeBackend())
    assert len(spy.calls) == 1
    assert spy.results[0].status == JudgmentStatus.PASS


@pytest.mark.asyncio
async def test_enforce_behaves_as_shadow_in_v1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FakeBackend({"report_supported": {"true": 0.05}, "in_scope": {"true": 0.95}})
    spy = await _compare(tmp_path, monkeypatch, ENFORCE, backend)
    assert spy.results[0].question_status["report_supported"] == JudgmentStatus.FAIL
    assert "enforce=shadow_only" in str(spy.calls[0]["action"])


@pytest.mark.asyncio
async def test_a_raising_backend_changes_nothing_and_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = await _compare(tmp_path, monkeypatch, SHADOW, _RaisingBackend())
    assert spy.results[0].status == JudgmentStatus.ERROR


@pytest.mark.asyncio
async def test_a_port_factory_that_raises_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom():
        raise RuntimeError("no port")

    repo = _Repo(tmp_path / "repo", ["in.txt"])
    monkeypatch.setenv(MODE_ENV, "off")
    off_step, _, off_line, _ = await _run_once(tmp_path / "a", [], ["in.txt"], repo=repo)
    monkeypatch.setattr(cj, "port_factory", boom)
    monkeypatch.setenv(MODE_ENV, SHADOW)
    on_step, _, on_line, _ = await _run_once(tmp_path / "b", [], ["in.txt"], repo=repo)
    assert on_step == off_step and on_line == off_line


@pytest.mark.asyncio
async def test_schedule_that_raises_never_escapes_into_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("hook broke")

    monkeypatch.setattr(cj, "schedule", boom)
    step, _, _, _ = await _run_once(tmp_path, ["in.txt"], ["in.txt"])
    assert step.passed


@pytest.mark.asyncio
async def test_off_never_builds_or_calls_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[int] = []
    monkeypatch.setattr(cj, "port_factory", lambda: built.append(1))
    monkeypatch.setenv(MODE_ENV, "off")
    step, _, _, _ = await _run_once(tmp_path, ["in.txt"], ["in.txt"])
    assert step.passed
    assert built == []
    assert not (tmp_path / "records.jsonl").exists()


@pytest.mark.asyncio
async def test_a_failed_g2_starts_no_judgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy, _ = _install(monkeypatch, _FakeBackend())
    repo = _Repo(tmp_path / "repo", ["in.txt"])
    gate = _gate(tmp_path, repo, _Commands(b"1 failed\n", exit_code=1))
    line = _line(repo, ["in.txt"])
    step = await gate.run_g2(_dispatch(line), line)
    await cj.drain(5.0)
    assert not step.passed
    assert spy.calls == []


@pytest.mark.asyncio
async def test_the_gate_does_not_wait_for_a_slow_judgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    class _Slow:
        name = "slow"

        async def answer(self, request):
            await release.wait()
            return {"report_supported": {"true": 0.9}, "in_scope": {"true": 0.9}}

    recorder = MemoryRecorder()
    port = JudgmentPort(_Slow(), recorder=recorder, budget_s=30.0)
    monkeypatch.setattr(cj, "port_factory", lambda: port)
    repo = _Repo(tmp_path / "repo", ["in.txt"])
    gate = _gate(tmp_path, repo, _Commands())
    line = _line(repo, ["in.txt"])
    step = await asyncio.wait_for(gate.run_g2(_dispatch(line), line), 10.0)
    assert step.passed
    assert recorder.records == []  # still pending: the gate went on without it
    release.set()
    await cj.drain(5.0)
    assert len(recorder.records) == 1


@pytest.mark.asyncio
async def test_the_code_scope_result_is_recorded_and_never_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scope check in shadow lets an out-of-scope line through G2; the
    # judgment says "in scope" with confidence. The code result stands.
    monkeypatch.setenv(SCOPE_CHECK_ENV, SHADOW)
    spy, recorder = _install(monkeypatch, _FakeBackend())
    step, snapshot, _, _ = await _run_once(tmp_path, ["in.txt", "out.txt"], ["in.txt"])
    assert step.passed
    g2 = snapshot["gates"]["G2"]
    assert "scope_conflict" not in g2
    assert [entry["check"] for entry in g2.get("shadow") or []] == ["scope_check"]
    assert spy.results[0].answers["in_scope"] is True
    action = recorder.records[0].action
    assert "code_in_scope=false" in action
    assert "scope_violations=1" in action
    assert "scope_mode=shadow" in action
    assert recorder.records[0].subject == "d1:line-0"


@pytest.mark.asyncio
async def test_the_code_scope_is_true_for_an_in_scope_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, recorder = _install(monkeypatch, _FakeBackend())
    await _run_once(tmp_path, ["in.txt"], ["in.txt"])
    assert "code_in_scope=true;scope_violations=0" in recorder.records[0].action


@pytest.mark.asyncio
async def test_the_material_is_redacted_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FakeBackend()
    _, recorder = _install(monkeypatch, backend)
    noisy = b"".join(b"line %d of noise\n" % i for i in range(5000))
    output = noisy + f"OPENAI_API_KEY={SECRET}\nAuthorization: Bearer {BEARER}\n3 passed\n".encode()
    report = ("chatter\n" * 3000) + f"token: {SECRET}\nAll tests pass.\n"
    many = [f"pkg/file_{i}.txt" for i in range(120)]
    await _run_once(tmp_path, many, ["pkg/**"], report=report, output=output)

    assert len(backend.requests) == 1
    state = backend.requests[0].state
    blob = json.dumps(dict(state))
    assert SECRET not in blob and BEARER not in blob
    assert "[redacted]" in state["g2_output_tails"].lower()
    assert "[redacted]" in state["worker_report"].lower()
    assert "3 passed" in state["g2_output_tails"]
    assert "All tests pass." in state["worker_report"]
    assert len(state["worker_report"].encode()) <= cj.REPORT_BUDGET_BYTES
    assert len(state["g2_output_tails"].encode()) <= cj.OUTPUT_BUDGET_BYTES * cj.MAX_COMMANDS + 64
    assert len(state["diff_files"].encode()) <= cj.FILES_BUDGET_BYTES + 32
    assert "more files" in state["diff_files"]
    assert set(backend.requests[0].questions) == {cj.REPORT_SUPPORTED, cj.IN_SCOPE}
    # The record holds digests only, never the material.
    record = json.dumps(recorder.records[0].to_dict())
    assert SECRET not in record and "noise" not in record and "pkg/file_" not in record


@pytest.mark.asyncio
async def test_a_log_ref_outside_runs_dir_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "secret.log"
    outside.write_text("do not read\n", encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    evidence = cj.Evidence(
        dispatch_id="d1", line_id="l", attempt=0, head="", base="", write_scope=(),
        commands=({"index": 0, "safe_command": "pytest", "exit_code": 0, "log_ref": "../secret.log"},),
        worker_report="", repo_root=tmp_path, runs_dir=runs, scope_mode="enforce",
    )
    state = cj.build_state(evidence, None)
    assert "do not read" not in state["g2_output_tails"]
    assert state["diff_files"] == "(diff unavailable)"


def test_default_port_never_imports_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.delitem(sys.modules, "src.server.app.setup", raising=False)
    port = cj._default_port()
    assert port.backend is None
    assert port.budget_s == cj.BUDGET_SECONDS
    assert "src.server.app.setup" not in sys.modules


# --- whose credentials: the dispatch's owning user, never the platform key ---


@pytest.mark.asyncio
async def test_the_request_carries_the_dispatchs_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _FakeBackend()
    spy, _ = _install(monkeypatch, backend)
    _, _, _, dispatch = await _run_once(tmp_path, ["in.txt"], ["in.txt"])
    assert dispatch.user_id == "u"
    assert spy.calls[0]["request"].user_id == "u"
    assert backend.requests[0].user_id == "u"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["", "   "])
async def test_a_dispatch_without_a_user_sends_none_and_the_port_records_no_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    backend = _FakeBackend()
    spy, recorder = _install(monkeypatch, backend)
    repo = _Repo(tmp_path / "repo", ["in.txt"])
    gate = _gate(tmp_path, repo, _Commands())
    line = _line(repo, ["in.txt"])
    dispatch = Dispatch(id="d1", goal="goal", user_id=owner, created_at="now", lines=[line])
    step = await gate.run_g2(dispatch, line)
    await cj.drain(5.0)
    assert step.passed
    assert len(spy.calls) == 1
    assert spy.calls[0]["request"].user_id is None
    # A real JudgmentPort: no backend call, and the record says why.
    assert backend.requests == []
    assert spy.results[0].reason == "no_user"
    assert len(recorder.records) == 1 and recorder.records[0].reason == "no_user"


def test_build_request_passes_the_user_id_through() -> None:
    assert cj.build_request({"head": "x"}, "user-7").user_id == "user-7"
    assert cj.build_request({"head": "x"}, None).user_id is None
    assert cj.build_request({"head": "x"}, "").user_id is None


# --- drain and the pending cap ---


class _Blocked:
    """A backend that waits until released (or cancelled)."""

    name = "blocked"

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0
        self.cancelled = 0

    async def answer(self, request):
        self.started += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return {"report_supported": {"true": 0.9}, "in_scope": {"true": 0.9}}


def _schedule_direct(tmp_path: Path, n: int, user_id: str = "u") -> None:
    from types import SimpleNamespace

    settings = SimpleNamespace(repo_root=tmp_path, runs_dir=tmp_path / "runs")
    for i in range(n):
        line = Line(id=f"line-{i}", name="l", task="t", acceptance=(), branch="b", base_commit="")
        dispatch = Dispatch(id="d1", goal="g", user_id=user_id, created_at="now", lines=[line])
        cj.schedule(settings, dispatch, line, 0, "", [])


def _blocked_port(monkeypatch: pytest.MonkeyPatch) -> tuple[_Blocked, MemoryRecorder]:
    backend = _Blocked()
    recorder = MemoryRecorder()
    port = JudgmentPort(backend, recorder=recorder, budget_s=60.0)
    monkeypatch.setattr(cj, "port_factory", lambda: port)
    return backend, recorder


@pytest.mark.asyncio
async def test_drain_cancels_what_is_still_pending_after_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, _ = _blocked_port(monkeypatch)
    _schedule_direct(tmp_path, 2)
    tasks = list(cj._PENDING)
    assert len(tasks) == 2
    for _ in range(50):
        if backend.started == 2:
            break
        await asyncio.sleep(0.01)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await cj.drain(0.05)
    assert loop.time() - started < 2.0
    assert all(task.done() for task in tasks)
    assert backend.cancelled == 2
    assert cj._live(loop) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [-1.0, 0.0, float("nan")])
async def test_drain_never_raises_on_odd_timeouts_or_failing_tasks(
    monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    async def boom() -> None:
        raise RuntimeError("judgment task broke")

    task = asyncio.get_running_loop().create_task(boom())
    cj._PENDING.add(task)
    task.add_done_callback(cj._PENDING.discard)
    await asyncio.sleep(0)
    await cj.drain(timeout)
    assert task.done()
    await cj.drain(timeout)  # nothing pending: a no-op


@pytest.mark.asyncio
async def test_drain_with_nothing_pending_returns() -> None:
    await cj.drain()


@pytest.mark.asyncio
async def test_work_past_the_cap_is_dropped_without_a_never_awaited_coroutine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gc
    import warnings

    backend, recorder = _blocked_port(monkeypatch)
    built: list[int] = []
    real_snapshot = cj._snapshot

    def counting(*args, **kwargs):
        built.append(1)
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(cj, "_snapshot", counting)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _schedule_direct(tmp_path, cj.MAX_PENDING + 3)
        assert len(cj._live(asyncio.get_running_loop())) == cj.MAX_PENDING
        assert len(built) == cj.MAX_PENDING  # past the cap nothing is even built
        backend.release.set()
        await cj.drain(5.0)
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]
    assert len(recorder.records) == cj.MAX_PENDING
    assert backend.cancelled == 0
