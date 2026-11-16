"""N61 U1 (watchdog progress) and U3 (Router preselect) hooks.

The flow-comparison tests record the Router/watchdog action sequence with
and without the hook and require them to be identical in off and shadow
mode, and whenever the judgment fails. A fake port is injected through
``judgment_hooks.set_port``; ``_build_request`` is replaced so these tests
do not need the judgment package. Tests that use the real types skip until
that package exists.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import warnings
from types import SimpleNamespace
from typing import Any

import pytest

from src.server.services.autonomy import judgment_hooks as hooks
from src.server.services.autonomy import router as router_service
from src.server.services.autonomy import watchdog_session

_REAL_BUILD_REQUEST = hooks._build_request


class FakePort:
    def __init__(
        self,
        mode: str = "shadow",
        *,
        result: Any = None,
        raises: BaseException | None = None,
        mode_raises: bool = False,
        hang: bool = False,
    ) -> None:
        self.mode = mode
        self.result = result
        self.raises = raises
        self.mode_raises = mode_raises
        self.hang = hang
        self.mode_calls: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def mode_for(self, use: str) -> str:
        self.mode_calls.append(use)
        if self.mode_raises:
            raise RuntimeError("mode lookup broke")
        return self.mode

    async def judge(
        self, request, *, action: str = "", sample_key: str = "", subject: str = ""
    ):
        self.calls.append(
            {"request": request, "action": action, "sample_key": sample_key, "subject": subject}
        )
        if self.hang:
            await asyncio.Event().wait()
        if self.raises is not None:
            raise self.raises
        return self.result


def _result(status: str, answers: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, answers=answers or {}, reason="")


PASS_PROGRESSING = _result("pass", {"progress": "progressing"})
PASS_STALLED = _result("pass", {"progress": "stalled"})
INCONCLUSIVE = _result("inconclusive", {"progress": None})
ERROR = _result("error", {"progress": None})


def _fake_request(use, state, prompt, options, question_id, *, user_id=None):
    return SimpleNamespace(
        use=use,
        state=dict(state),
        prompt=prompt,
        options=options,
        question_id=question_id,
        user_id=user_id,
    )


def _clear_ticket_state() -> None:
    for name in ("_skip_counts", "_report_marks"):
        getattr(hooks, name, {}).clear()


@pytest.fixture(autouse=True)
def _isolated_hooks(monkeypatch):
    # No test reaches the real default port, so nothing is recorded to HOME.
    monkeypatch.setattr(hooks, "_get_port", lambda: hooks._port_override)
    monkeypatch.setattr(hooks, "_build_request", _fake_request)
    # Enforce tests opt in; the guard tests below remove the ack.
    monkeypatch.setenv("CONDUCTOR_JUDGMENT_U1_ENFORCE_ACK", "1")
    monkeypatch.setattr(hooks, "_enforce_ack_warned", False, raising=False)
    _clear_ticket_state()
    hooks.set_port(None)
    yield
    hooks.set_port(None)
    _clear_ticket_state()
    for task in list(hooks._pending):
        task.cancel()


class _Dispatcher:
    def __init__(self, events: list) -> None:
        self.events = events

    async def create_thread(self, **kwargs):
        self.events.append(("create_thread", kwargs))
        return "thread"

    async def dispatch_turn(self, **kwargs):
        self.events.append(("dispatch_turn", kwargs))
        return {"status": "started"}


CANDIDATES = [
    {"trigger_id": "tr-1", "source": "github", "text": "please fix the build", "addressed": True},
    {"trigger_id": "tr-2", "source": "github", "text": "nice work", "addressed": False},
]


async def _router_flow(monkeypatch, port, candidates=CANDIDATES):
    events: list = []

    async def ensure(user_id, *, dispatcher, shard=0):
        events.append(("ensure_router_session", user_id, shard))
        return {"user_id": user_id, "thread_id": "t", "workspace_id": "ws", "shard": shard}

    async def prompt(_user_id, payload):
        events.append(("prompt", [c["trigger_id"] for c in payload["candidates"]]))
        return "prompt"

    monkeypatch.setattr(router_service, "ensure_router_session", ensure)
    monkeypatch.setattr(router_service, "_candidate_batch_prompt", prompt)
    monkeypatch.setattr(router_service, "router_snapshot_enabled", lambda: False)
    monkeypatch.setattr(router_service, "get_autonomy_router_model", lambda: "m")
    hooks.set_port(port)
    result = await router_service.dispatch_candidate_batch(
        user_id="u1",
        eval_id="eval-1",
        candidates=[dict(c) for c in candidates],
        dispatcher=_Dispatcher(events),
    )
    events.append(("result", result))
    return events


TICKET = {
    "ticket_id": "T1",
    "user_id": "u1",
    "title": "Fix build",
    "status": "running",
    "summary": "running tests",
    "scope": "fix the build",
    "started_at": "2026-11-14T00:00:00+00:00",
    "updated_at": "2026-11-14T02:30:00+00:00",
    "mandate": {},
}
REPORTS = [
    {"report_id": f"r{i}", "summary": f"step {i}", "created_at": f"2026-11-14T0{i}:00:00+00:00"}
    for i in range(1, 6)
]


async def _watchdog_flow(monkeypatch, port, *, ticket=None, reports=None):
    events: list = []

    async def ensure(user_id, *, dispatcher):
        events.append(("ensure_watchdog_session", user_id))
        return {"thread_id": "wt", "workspace_id": "ws"}

    monkeypatch.setattr(watchdog_session, "ensure_watchdog_session", ensure)
    hooks.set_port(port)
    result = await router_service.dispatch_watchdog_review(
        ticket=dict(ticket or TICKET),
        reports=[dict(r) for r in (REPORTS if reports is None else reports)],
        dispatcher=_Dispatcher(events),
    )
    # The prompt embeds reviewed_at = now; compare everything else.
    for event in events:
        if event[0] == "dispatch_turn":
            event[1]["prompt"] = event[1]["prompt"].split('"reviewed_at"')[0]
    events.append(("result", result))
    return events


# -- U3 Router preselect ------------------------------------------------------


ROUTER_VARIANTS = {
    "off": lambda: FakePort("off", result=_result("pass", {"candidate": "tr-1"})),
    "shadow_pass": lambda: FakePort("shadow", result=_result("pass", {"candidate": "tr-1"})),
    "shadow_inconclusive": lambda: FakePort("shadow", result=INCONCLUSIVE),
    "shadow_error": lambda: FakePort("shadow", result=ERROR),
    "shadow_raises": lambda: FakePort("shadow", raises=RuntimeError("backend down")),
    "mode_raises": lambda: FakePort("shadow", mode_raises=True),
    "enforce_pass_not_implemented": lambda: FakePort(
        "enforce", result=_result("pass", {"candidate": "tr-1"})
    ),
    "garbage_result": lambda: FakePort("shadow", result=object()),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", sorted(ROUTER_VARIANTS))
async def test_router_flow_is_identical_with_the_hook(monkeypatch, variant):
    baseline = await _router_flow(monkeypatch, None)
    port = ROUTER_VARIANTS[variant]()
    with_hook = await _router_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert with_hook == baseline
    assert [e[0] for e in baseline] == [
        "ensure_router_session",
        "prompt",
        "dispatch_turn",
        "result",
    ]


@pytest.mark.asyncio
async def test_router_off_never_calls_the_port_or_builds_material(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("off must not build a request")

    monkeypatch.setattr(hooks, "_build_request", explode)
    port = FakePort("off")
    await _router_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert port.calls == []
    assert port.mode_calls == ["router_preselect"]


@pytest.mark.asyncio
async def test_router_shadow_records_one_choice_over_eligible_candidates(monkeypatch):
    port = FakePort("shadow", result=_result("pass", {"candidate": "tr-1"}))
    await _router_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert len(port.calls) == 1
    call = port.calls[0]
    request = call["request"]
    assert request.use == "router_preselect"
    assert request.question_id == "candidate"
    assert request.options == ("tr-1", "tr-2", "none")
    assert call["action"] == "router_dispatched:eval-1"
    assert call["sample_key"] == "u1:eval-1"
    assert all(isinstance(v, str) for v in request.state.values())


@pytest.mark.asyncio
async def test_router_shadow_does_not_wait_for_a_slow_judgment(monkeypatch):
    port = FakePort("shadow", hang=True)
    events = await asyncio.wait_for(_router_flow(monkeypatch, port), timeout=2)
    assert events[-1] == ("result", {"status": "started"})
    assert len(hooks._pending) == 1  # still running in the background


@pytest.mark.asyncio
async def test_router_hook_swallows_request_build_failures(monkeypatch):
    baseline = await _router_flow(monkeypatch, None)

    def broken(*_args, **_kwargs):
        raise ValueError("judgment types missing")

    monkeypatch.setattr(hooks, "_build_request", broken)
    port = FakePort("shadow")
    assert await _router_flow(monkeypatch, port) == baseline
    assert port.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, hooks.PRESELECT_MAX_CANDIDATES + 1])
async def test_router_preselect_skips_empty_or_oversized_batches(monkeypatch, count):
    candidates = [{"trigger_id": f"tr-{i}", "text": "x"} for i in range(count)]
    port = FakePort("shadow", result=ERROR)
    await _router_flow(monkeypatch, port, candidates=candidates)
    await hooks.drain_pending()
    assert port.calls == []


def test_router_preselect_state_is_bounded():
    huge = [{"trigger_id": f"tr-{i}", "text": "x" * 50_000, "actor": "a" * 5_000} for i in range(16)]
    state = hooks.router_preselect_state(huge, accept_mode="explicit")
    assert len(state["candidates"]) <= hooks.CANDIDATES_MAX_CHARS
    assert state["candidate_count"] == "16"
    assert all(isinstance(v, str) for v in state.values())


# -- U1 watchdog progress -----------------------------------------------------


WATCHDOG_VARIANTS = {
    "off": lambda: FakePort("off", result=PASS_PROGRESSING),
    "shadow_progressing_pass": lambda: FakePort("shadow", result=PASS_PROGRESSING),
    "shadow_error": lambda: FakePort("shadow", result=ERROR),
    "shadow_raises": lambda: FakePort("shadow", raises=RuntimeError("backend down")),
    "mode_raises": lambda: FakePort("enforce", mode_raises=True),
    "enforce_stalled_pass": lambda: FakePort("enforce", result=PASS_STALLED),
    "enforce_inconclusive": lambda: FakePort("enforce", result=INCONCLUSIVE),
    "enforce_error": lambda: FakePort("enforce", result=ERROR),
    "enforce_raises": lambda: FakePort("enforce", raises=RuntimeError("backend down")),
    "enforce_garbage": lambda: FakePort("enforce", result=object()),
    "enforce_progressing_but_inconclusive": lambda: FakePort(
        "enforce", result=_result("inconclusive", {"progress": "progressing"})
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", sorted(WATCHDOG_VARIANTS))
async def test_watchdog_review_flow_is_identical_unless_enforce_says_progressing(
    monkeypatch, variant
):
    baseline = await _watchdog_flow(monkeypatch, None)
    with_hook = await _watchdog_flow(monkeypatch, WATCHDOG_VARIANTS[variant]())
    await hooks.drain_pending()
    assert with_hook == baseline
    assert [e[0] for e in baseline] == ["ensure_watchdog_session", "dispatch_turn", "result"]


@pytest.mark.asyncio
async def test_watchdog_off_never_calls_the_port(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("off must not build a request")

    monkeypatch.setattr(hooks, "_build_request", explode)
    port = FakePort("off", result=PASS_PROGRESSING)
    await _watchdog_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert port.calls == []


@pytest.mark.asyncio
async def test_watchdog_shadow_records_the_progress_choice(monkeypatch):
    port = FakePort("shadow", result=PASS_PROGRESSING)
    events = await _watchdog_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert [e[0] for e in events] == ["ensure_watchdog_session", "dispatch_turn", "result"]
    assert len(port.calls) == 1
    call = port.calls[0]
    assert call["request"].use == "watchdog_progress"
    assert call["request"].options == (
        "progressing",
        "stalled",
        "off_track",
        "needs_verification",
    )
    assert call["action"] == "watchdog_review_dispatched:T1"
    assert call["sample_key"] == "T1:2026-11-14T02:30:00+00:00"
    state = call["request"].state
    assert state["last_report_at"] == "2026-11-14T05:00:00+00:00"
    assert state["report_count"] == "5"
    assert '"r5"' in state["recent_reports"] and '"r2"' not in state["recent_reports"]


@pytest.mark.asyncio
async def test_watchdog_shadow_does_not_wait_for_a_slow_judgment(monkeypatch):
    port = FakePort("shadow", hang=True)
    events = await asyncio.wait_for(_watchdog_flow(monkeypatch, port), timeout=2)
    assert events[-1] == ("result", {"status": "started"})


@pytest.mark.asyncio
async def test_watchdog_enforce_skips_review_only_for_progressing_pass(monkeypatch):
    port = FakePort("enforce", result=PASS_PROGRESSING)
    events = await _watchdog_flow(monkeypatch, port)
    assert events == [("result", {"status": "skipped", "reason": "judgment_progressing"})]
    assert port.calls[0]["action"] == "watchdog_review_gate:T1"


@pytest.mark.asyncio
async def test_watchdog_enforce_timeout_escalates_to_the_review(monkeypatch):
    baseline = await _watchdog_flow(monkeypatch, None)
    monkeypatch.setattr(hooks, "ENFORCE_WAIT_SECONDS", 0.05)
    port = FakePort("enforce", hang=True)
    assert await _watchdog_flow(monkeypatch, port) == baseline


@pytest.mark.asyncio
async def test_watchdog_gate_never_raises_on_a_broken_ticket():
    hooks.set_port(FakePort("enforce", result=PASS_PROGRESSING))
    # A mandate that is not a mapping and reports that are not dicts.
    skip = await hooks.watchdog_review_gate(
        ticket={"ticket_id": "T", "mandate": "bad"}, reports=["not a dict"]
    )
    assert skip is False


def test_watchdog_progress_state_is_bounded():
    ticket = dict(TICKET, summary="s" * 50_000, scope="c" * 50_000, status="x" * 5_000)
    reports = [{"report_id": f"r{i}", "text": "y" * 50_000} for i in range(20)]
    state = hooks.watchdog_progress_state(ticket, reports)
    assert len(state["summary"]) == hooks.SUMMARY_MAX_CHARS
    assert len(state["scope"]) == hooks.SCOPE_MAX_CHARS
    assert len(state["ticket_status"]) == hooks.FIELD_MAX_CHARS
    assert len(state["recent_reports"]) <= hooks.REPORTS_MAX_CHARS
    assert all(isinstance(v, str) for v in state.values())


# -- with the real judgment package (skips until L1 lands) --------------------


def test_real_request_types(monkeypatch):
    judgment = pytest.importorskip("src.server.services.judgment")
    request = _REAL_BUILD_REQUEST(
        hooks.WATCHDOG_PROGRESS,
        {"a": "b"},
        "prompt",
        hooks.PROGRESS_OPTIONS,
        hooks.PROGRESS_QUESTION_ID,
    )
    assert isinstance(request, judgment.JudgmentRequest)
    assert request.use == judgment.WATCHDOG_PROGRESS == hooks.WATCHDOG_PROGRESS
    assert hooks.ROUTER_PRESELECT == judgment.ROUTER_PRESELECT
    assert request.version == judgment.get_use(hooks.WATCHDOG_PROGRESS).version
    question = request.questions[hooks.PROGRESS_QUESTION_ID]
    assert isinstance(question, judgment.ChoiceQuestion)
    assert tuple(question.options) == hooks.PROGRESS_OPTIONS


@pytest.mark.asyncio
async def test_real_port_shadow_records_without_changing_the_review(monkeypatch):
    judgment = pytest.importorskip("src.server.services.judgment")

    class Backend:
        name = "fake"

        async def answer(self, request):
            return {
                "progress": {
                    "progressing": 0.9,
                    "stalled": 0.05,
                    "off_track": 0.03,
                    "needs_verification": 0.02,
                }
            }

    baseline = await _watchdog_flow(monkeypatch, None)
    monkeypatch.setattr(hooks, "_build_request", _REAL_BUILD_REQUEST)
    recorder = judgment.MemoryRecorder()
    # Sample every key (the v1 default samples one in ten).
    environ = {"CONDUCTOR_JUDGMENT_WATCHDOG_PROGRESS_SAMPLE": "1"}
    port = judgment.JudgmentPort(Backend(), recorder=recorder, environ=environ)
    events = await _watchdog_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert events == baseline
    assert len(recorder.records) == 1
    record = recorder.records[0]
    # The Ticket's user reached the port, so it judged instead of no_user.
    assert record.reason != "no_user"
    assert record.answers == {"progress": "progressing"}
    assert record.use == hooks.WATCHDOG_PROGRESS
    assert record.action == "watchdog_review_dispatched:T1"
    assert "running tests" not in str(record.to_dict())


# -- review fixes: user id, enforce guards, bounded background work ----------


SKIPPED = [("result", {"status": "skipped", "reason": "judgment_progressing"})]


def _reports(n: int) -> list[dict[str, Any]]:
    return [
        {"report_id": f"r{i}", "summary": f"step {i}", "created_at": f"2026-11-14T{i:02d}:00:00+00:00"}
        for i in range(1, n + 1)
    ]


@pytest.mark.asyncio
async def test_watchdog_request_carries_the_ticket_user(monkeypatch):
    port = FakePort("shadow", result=PASS_PROGRESSING)
    await _watchdog_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert port.calls[0]["request"].user_id == "u1"


@pytest.mark.asyncio
async def test_watchdog_request_without_a_user_passes_none():
    port = FakePort("shadow", result=PASS_PROGRESSING)
    hooks.set_port(port)
    ticket = {k: v for k, v in TICKET.items() if k != "user_id"}
    assert await hooks.watchdog_review_gate(ticket=ticket, reports=_reports(2)) is False
    await hooks.drain_pending()
    assert port.calls[0]["request"].user_id is None


@pytest.mark.asyncio
async def test_router_request_carries_the_user(monkeypatch):
    port = FakePort("shadow", result=_result("pass", {"candidate": "tr-1"}))
    await _router_flow(monkeypatch, port)
    await hooks.drain_pending()
    assert port.calls[0]["request"].user_id == "u1"


def test_real_request_carries_user_id():
    judgment = pytest.importorskip("src.server.services.judgment")
    args = (hooks.WATCHDOG_PROGRESS, {"a": "b"}, "p", hooks.PROGRESS_OPTIONS, hooks.PROGRESS_QUESTION_ID)
    request = _REAL_BUILD_REQUEST(*args, user_id="u1")
    assert isinstance(request, judgment.JudgmentRequest)
    assert request.user_id == "u1"
    assert _REAL_BUILD_REQUEST(*args, user_id="").user_id is None


@pytest.mark.asyncio
async def test_enforce_without_ack_runs_the_review_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv(hooks.ENFORCE_ACK_ENV)
    baseline = await _watchdog_flow(monkeypatch, None)
    port = FakePort("enforce", result=PASS_PROGRESSING)
    with caplog.at_level(logging.WARNING, logger=hooks.__name__):
        first = await _watchdog_flow(monkeypatch, port, reports=_reports(1))
        second = await _watchdog_flow(monkeypatch, port, reports=_reports(2))
    await hooks.drain_pending()
    base = baseline[:-1]
    assert [e[0] for e in first] == [e[0] for e in second] == [e[0] for e in baseline]
    assert first[-1] == second[-1] == baseline[-1] and base
    # It still records, as shadow does.
    assert [c["action"] for c in port.calls] == ["watchdog_review_dispatched:T1"] * 2
    warnings_seen = [r for r in caplog.records if hooks.ENFORCE_ACK_ENV in r.getMessage()]
    assert len(warnings_seen) == 1


@pytest.mark.asyncio
async def test_enforce_skips_at_most_two_reviews_in_a_row(monkeypatch):
    port = FakePort("enforce", result=PASS_PROGRESSING)
    results = [
        await _watchdog_flow(monkeypatch, port, reports=_reports(n)) for n in range(1, 6)
    ]
    await hooks.drain_pending()
    # skip, skip, review (cap), then the count starts again.
    assert results[0] == SKIPPED and results[1] == SKIPPED
    assert results[2][0][0] == "ensure_watchdog_session"
    assert results[3] == SKIPPED and results[4] == SKIPPED


@pytest.mark.asyncio
async def test_enforce_reviews_when_no_new_report_arrived(monkeypatch):
    port = FakePort("enforce", result=PASS_PROGRESSING)
    first = await _watchdog_flow(monkeypatch, port, reports=_reports(2))
    again = await _watchdog_flow(monkeypatch, port, reports=_reports(2))
    await hooks.drain_pending()
    assert first == SKIPPED
    assert again[0][0] == "ensure_watchdog_session"
    # The repeat sweep was only recorded, not used to skip.
    assert port.calls[-1]["action"] == "watchdog_review_dispatched:T1"


@pytest.mark.asyncio
async def test_enforce_reviews_a_ticket_with_no_reports(monkeypatch):
    port = FakePort("enforce", result=PASS_PROGRESSING)
    events = await _watchdog_flow(monkeypatch, port, reports=[])
    await hooks.drain_pending()
    assert events[0][0] == "ensure_watchdog_session"


@pytest.mark.asyncio
async def test_a_review_resets_the_skip_count(monkeypatch):
    port = FakePort("enforce", result=PASS_PROGRESSING)
    assert await _watchdog_flow(monkeypatch, port, reports=_reports(1)) == SKIPPED
    port.result = PASS_STALLED
    assert (await _watchdog_flow(monkeypatch, port, reports=_reports(2)))[0][0] == (
        "ensure_watchdog_session"
    )
    port.result = PASS_PROGRESSING
    assert await _watchdog_flow(monkeypatch, port, reports=_reports(3)) == SKIPPED
    assert await _watchdog_flow(monkeypatch, port, reports=_reports(4)) == SKIPPED


@pytest.mark.asyncio
async def test_per_ticket_state_is_bounded():
    hooks.set_port(FakePort("enforce", result=PASS_PROGRESSING))
    for i in range(hooks.TICKET_STATE_MAX + 50):
        await hooks.watchdog_review_gate(
            ticket=dict(TICKET, ticket_id=f"T{i}"), reports=_reports(1)
        )
    assert len(hooks._report_marks) == hooks.TICKET_STATE_MAX
    assert len(hooks._skip_counts) == hooks.TICKET_STATE_MAX
    assert "T0" not in hooks._report_marks


@pytest.mark.asyncio
async def test_background_judgments_are_capped(monkeypatch):
    assert hooks.MAX_PENDING == 8
    port = FakePort("shadow", hang=True)
    hooks.set_port(port)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(hooks.MAX_PENDING + 4):
            await hooks.router_preselect(
                user_id="u1", eval_id="e", candidates=CANDIDATES, accept_mode="explicit"
            )
        await asyncio.sleep(0)
        gc.collect()
    assert len(hooks._pending) == hooks.MAX_PENDING
    assert len(port.calls) == hooks.MAX_PENDING
    assert not [w for w in caught if "never awaited" in str(w.message)]


@pytest.mark.asyncio
async def test_drain_pending_honours_its_timeout():
    hooks.set_port(FakePort("shadow", hang=True))
    await hooks.router_preselect(
        user_id="u1", eval_id="e", candidates=CANDIDATES, accept_mode="explicit"
    )
    await asyncio.wait_for(hooks.drain_pending(0.05), timeout=2)
    assert len(hooks._pending) == 1
