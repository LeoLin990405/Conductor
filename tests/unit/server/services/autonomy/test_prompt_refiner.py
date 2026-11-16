"""The prompt refiner: mode switch, rewrite-and-confirm, hold, confirm/reject.

Covers the whole feature surface: ``AUTONOMY_PROMPT_REFINE``, the one-shot
refiner call contract (no tools, no repository path, no write access), the
rewrite that parks a Ticket until a human confirms it, the unattended hold
used by channel and schedule entries, ``/raw`` / ``no_refine``, the confirm and
reject endpoints, and the intake/scheduler wiring.

The shell's own ``/raw`` cases sit beside the shell, in
``libs/ptc-cli/tests/unit_tests/test_shell_raw_prefix.py``: ``libs/ptc-cli`` is
its own uv project, with TUI dependencies the server environment deliberately
lacks.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.autonomy import intake
from src.server.services.autonomy import manual as manual_module
from src.server.services.autonomy import scheduler as scheduler_module
from src.server.services.autonomy import schedules as schedules_module
from src.server.services.autonomy import prompt_refiner
from src.server.services.autonomy.prompt_refiner import RefinedPrompt
from tests.conftest import create_test_app

_MOD = "src.server.services.autonomy.prompt_refiner"
_TICKETS_DB = "src.server.database.autonomy.tickets"
_CLARIFICATIONS_DB = "src.server.database.autonomy.clarifications"

_CLEAR = (
    "Fix the flaky `test_dispatch_keys` in src/server/services/autonomy: "
    "it times out after 30s when Redis is slow."
)
_VAGUE = "do something with the stuff maybe"


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "queued",
        "title": "do something",
        "scope": "do something",
        "mandate": {"source_id": "s-1", "strength": "explicit"},
        "origin_thread_key": "thread-1",
    }
    ticket.update(overrides)
    return ticket


class FakeRefiner:
    def __init__(self, refined: RefinedPrompt | None = None, *, error: Exception | None = None):
        self.refined = refined or RefinedPrompt(
            refined_prompt="Change the button label to Save across the editor.",
            assumptions=["The button is in the toolbar"],
            open_questions=["Which toolbar?"],
            acceptance_criteria=["The label reads Save"],
        )
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def refine(self, *, user_id: str, prompt: str) -> RefinedPrompt:
        self.calls.append((user_id, prompt))
        if self.error is not None:
            raise self.error
        return self.refined


class _GateLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


def _stub_gate_lock(stack: ExitStack) -> None:
    stack.enter_context(patch(f"{_MOD}._ticket_gate_lock", return_value=_GateLock()))


# --------------------------------------------------------------------- config


def test_mode_defaults_to_off():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AUTONOMY_PROMPT_REFINE", None)
        assert prompt_refiner.refine_mode() == "off"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("off", "off"),
        ("vague", "vague"),
        ("always", "always"),
        ("ALWAYS", "always"),
        ("  Vague  ", "vague"),
    ],
)
def test_mode_reads_the_env(value, expected):
    with patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": value}):
        assert prompt_refiner.refine_mode() == expected


def test_an_unknown_mode_falls_back_to_off():
    with patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "sometimes"}):
        assert prompt_refiner.refine_mode() == "off"


# ------------------------------------------------------------------- /raw


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("/raw fix the save button", ("fix the save button", True)),
        ("/raw   keep   spaces ", ("keep   spaces", True)),
        ("fix the save button", ("fix the save button", False)),
        ("/rawfix the save button", ("/rawfix the save button", False)),
        ("/raw", ("", True)),
    ],
)
def test_strip_raw_prefix(line, expected):
    assert prompt_refiner.strip_raw_prefix(line) == expected


# ------------------------------------------------------------------ refiner


@pytest.mark.asyncio
async def test_no_refiner_without_a_server_llm():
    with patch("src.server.app.setup.llm_service", None):
        assert await prompt_refiner.default_refiner() is None


@pytest.mark.asyncio
async def test_the_refiner_call_carries_only_the_request():
    llm = SimpleNamespace(complete=AsyncMock(return_value=RefinedPrompt(refined_prompt="rewritten")))
    with patch("src.server.app.setup.llm_service", llm):
        refiner = await prompt_refiner.default_refiner()
        assert refiner is not None
        result = await refiner.refine(user_id="u-1", prompt="fix the save button")
    assert result.refined_prompt == "rewritten"
    kwargs = llm.complete.await_args.kwargs
    # One structured completion: the request text is the whole user prompt,
    # with no repository path, no tools and no write surface.
    assert kwargs["user_prompt"] == "fix the save button"
    assert kwargs["response_schema"] is RefinedPrompt
    assert kwargs["user_id"] == "u-1"
    # Exactly these arguments: nothing that could carry tools, a repository
    # path, a working directory or a write surface reaches the model.
    assert set(kwargs) == {
        "user_id",
        "system_prompt",
        "user_prompt",
        "response_schema",
        "slot",
    }


def test_an_empty_rewrite_keeps_the_original_request():
    coerced = prompt_refiner._coerce(
        SimpleNamespace(
            refined_prompt="   ",
            assumptions=[" a ", ""],
            open_questions=["q1", "q2", "q3", "q4"],
            acceptance_criteria=["c1"],
        ),
        "  original request  ",
    )
    assert coerced.refined_prompt == "original request"
    assert coerced.assumptions == ["a"]
    assert coerced.open_questions == ["q1", "q2", "q3"]
    assert coerced.acceptance_criteria == ["c1"]


# -------------------------------------------------------------- refine gate


@pytest.mark.asyncio
async def test_gate_off_passes_without_touching_the_db():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "off"}))
        active = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.get_active_clarification", AsyncMock())
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        update = stack.enter_context(patch(f"{_TICKETS_DB}.get_ticket", AsyncMock()))
        outcome = await prompt_refiner.refine_gate(ticket=_ticket())
    assert outcome == "passed"
    active.assert_not_awaited()
    create.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_always_refines_once_and_parks_with_the_rewrite():
    refiner = FakeRefiner()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(
                    side_effect=[
                        _ticket(),
                        _ticket(status="awaiting_clarification"),
                    ]
                ),
            )
        )
        active = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        update = stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        outcome = await prompt_refiner.refine_gate(ticket=_ticket(), refiner=refiner)

    assert outcome == "awaiting"
    assert refiner.calls == [("u-1", "do something")]
    active.assert_awaited_once_with("t-1")
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["original_prompt"] == "do something"
    assert kwargs["expanded_request"] == (
        "Change the button label to Save across the editor."
    )
    assert kwargs["assumptions"] == ["The button is in the toolbar"]
    assert kwargs["open_questions"] == ["Which toolbar?"]
    # Parked before the model call, so a queued Ticket is never observable
    # while the refiner decides.
    first = update.await_args_list[0]
    assert first.kwargs["expected_status"] == "queued"
    assert first.kwargs["status"] == "awaiting_clarification"
    second = update.await_args_list[1]
    assert second.kwargs["expected_status"] == "awaiting_clarification"
    assert second.kwargs["attention"] == {
        "question": "Please confirm the refined request before this Ticket starts."
    }
    assert second.kwargs["mandate"]["refine"] == {
        "mode": "always",
        "degraded": False,
        "acceptance_criteria": ["The label reads Save"],
    }


@pytest.mark.asyncio
async def test_vague_mode_passes_a_clear_prompt_without_a_model_call():
    refiner = FakeRefiner()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "vague"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(f"{_TICKETS_DB}.get_ticket", AsyncMock(return_value=_ticket(scope=_CLEAR)))
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        outcome = await prompt_refiner.refine_gate(
            ticket=_ticket(scope=_CLEAR), refiner=refiner
        )
    assert outcome == "passed"
    assert refiner.calls == []
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_vague_mode_refines_a_vague_prompt():
    refiner = FakeRefiner()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "vague"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(
                    side_effect=[
                        _ticket(scope=_VAGUE),
                        _ticket(scope=_VAGUE, status="awaiting_clarification"),
                    ]
                ),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        outcome = await prompt_refiner.refine_gate(
            ticket=_ticket(scope=_VAGUE), refiner=refiner
        )
    assert outcome == "awaiting"
    assert refiner.calls == [("u-1", _VAGUE)]


@pytest.mark.asyncio
async def test_gate_passes_a_ticket_that_already_moved():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(return_value=_ticket(status="running")),
            )
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        outcome = await prompt_refiner.refine_gate(
            ticket=_ticket(status="running"), refiner=FakeRefiner()
        )
    assert outcome == "passed"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_is_idempotent_once_a_draft_exists():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        outcome = await prompt_refiner.refine_gate(ticket=_ticket(), refiner=FakeRefiner())
    assert outcome == "awaiting"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_refiner_degrades_to_the_original_request():
    refiner = FakeRefiner(error=RuntimeError("model down"))
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(
                    side_effect=[
                        _ticket(),
                        _ticket(status="awaiting_clarification"),
                    ]
                ),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        update = stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        outcome = await prompt_refiner.refine_gate(ticket=_ticket(), refiner=refiner)
    assert outcome == "awaiting"
    assert refiner.calls == [("u-1", "do something")]
    assert create.await_args.kwargs["expanded_request"] == "do something"
    assert update.await_args_list[1].kwargs["mandate"]["refine"]["degraded"] is True


@pytest.mark.asyncio
async def test_a_timed_out_refiner_degrades_and_never_blocks():
    class SlowRefiner:
        def __init__(self):
            self.calls = 0

        async def refine(self, *, user_id: str, prompt: str) -> RefinedPrompt:
            self.calls += 1
            await asyncio.sleep(0.05)
            return RefinedPrompt(refined_prompt="late")

    slow = SlowRefiner()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        stack.enter_context(patch(f"{_MOD}.REFINE_TIMEOUT_SECONDS", 0.01))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(
                    side_effect=[
                        _ticket(),
                        _ticket(status="awaiting_clarification"),
                    ]
                ),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        outcome = await prompt_refiner.refine_gate(ticket=_ticket(), refiner=slow)
    assert outcome == "awaiting"
    assert slow.calls == 1
    assert create.await_args.kwargs["expanded_request"] == "do something"


@pytest.mark.asyncio
async def test_a_ticket_that_answers_mid_refinement_passes():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(side_effect=[_ticket(), _ticket(status="queued")]),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        outcome = await prompt_refiner.refine_gate(ticket=_ticket(), refiner=FakeRefiner())
    assert outcome == "passed"
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("released", "expected"),
    [
        # Queued but never confirmed: the caller still owes the launch.
        ({"status": "queued"}, "passed"),
        # A confirm or reject released it and enqueued its own launch: the
        # gate must not let the caller launch a second time.
        (
            {
                "status": "queued",
                "mandate": {"source_id": "s-1", "clarified": True},
            },
            "handled",
        ),
        ({"status": "awaiting_clarification"}, "awaiting"),
    ],
)
async def test_a_draft_that_cannot_be_stored_settles_on_the_latest_state(
    released, expected
):
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(
                    side_effect=[
                        _ticket(),
                        _ticket(status="awaiting_clarification"),
                        _ticket(**released),
                    ]
                ),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        outcome = await prompt_refiner.refine_gate(ticket=_ticket(), refiner=FakeRefiner())
    assert outcome == expected


# ----------------------------------------------------------------- hold gate


@pytest.mark.asyncio
async def test_hold_off_passes():
    with patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "off"}):
        outcome = await prompt_refiner.hold_gate(ticket=_ticket())
    assert outcome == "passed"


@pytest.mark.asyncio
async def test_hold_parks_the_request_without_calling_the_refiner():
    default_refiner = AsyncMock()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(patch(f"{_MOD}.default_refiner", default_refiner))
        stack.enter_context(
            patch(f"{_TICKETS_DB}.get_ticket", AsyncMock(return_value=_ticket()))
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        update = stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        outcome = await prompt_refiner.hold_gate(ticket=_ticket())
    assert outcome == "awaiting"
    default_refiner.assert_not_awaited()
    kwargs = create.await_args.kwargs
    assert kwargs["original_prompt"] == "do something"
    assert kwargs["expanded_request"] == "do something"
    held = update.await_args.kwargs
    assert held["expected_status"] == "queued"
    assert held["status"] == "awaiting_clarification"
    assert held["mandate"]["refine"]["held"] is True


@pytest.mark.asyncio
async def test_hold_passes_a_ticket_that_is_not_queued():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(return_value=_ticket(status="running")),
            )
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        outcome = await prompt_refiner.hold_gate(ticket=_ticket(status="running"))
    assert outcome == "passed"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_hold_in_vague_mode_passes_a_clear_request():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "vague"}))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(f"{_TICKETS_DB}.get_ticket", AsyncMock(return_value=_ticket(scope=_CLEAR)))
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(f"{_CLARIFICATIONS_DB}.create_clarification", AsyncMock())
        )
        outcome = await prompt_refiner.hold_gate(ticket=_ticket(scope=_CLEAR))
    assert outcome == "passed"
    create.assert_not_awaited()


# ---------------------------------------------------------- confirm / reject


@pytest.mark.asyncio
async def test_confirm_with_explicit_text_becomes_the_goal():
    confirm = AsyncMock(return_value={"ticket_id": "t-1", "status": "queued"})
    with patch(f"{_MOD}.confirm_clarification", confirm):
        result = await prompt_refiner.confirm_refinement(
            ticket_id="t-1", text="Change the button label to Save."
        )
    assert result["status"] == "queued"
    confirm.assert_awaited_once_with(
        ticket_id="t-1", text="Change the button label to Save."
    )


@pytest.mark.asyncio
async def test_confirm_without_text_adopts_the_stored_rewrite():
    active = AsyncMock(
        return_value={
            "clarification_id": "c-1",
            "expanded_request": "Change the button label to Save across the editor.",
        }
    )
    confirm = AsyncMock(return_value={"ticket_id": "t-1", "status": "queued"})
    with (
        patch(f"{_CLARIFICATIONS_DB}.get_active_clarification", active),
        patch(f"{_MOD}.confirm_clarification", confirm),
    ):
        await prompt_refiner.confirm_refinement(ticket_id="t-1")
    confirm.assert_awaited_once_with(
        ticket_id="t-1",
        text="Change the button label to Save across the editor.",
    )


@pytest.mark.asyncio
async def test_confirm_without_an_active_refinement_raises():
    active = AsyncMock(return_value=None)
    confirm = AsyncMock()
    with (
        patch(f"{_CLARIFICATIONS_DB}.get_active_clarification", active),
        patch(f"{_MOD}.confirm_clarification", confirm),
    ):
        with pytest.raises(ValueError, match="no active refinement"):
            await prompt_refiner.confirm_refinement(ticket_id="t-1")
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_keeps_the_original_request_and_launches():
    reject = AsyncMock(
        return_value={
            "ticket": {"ticket_id": "t-1", "status": "queued"},
            "clarification": {"clarification_id": "c-1"},
        }
    )
    with patch(f"{_CLARIFICATIONS_DB}.reject_ticket_clarification", reject):
        result = await prompt_refiner.reject_refinement(ticket_id="t-1")
    assert result["status"] == "queued"
    reject.assert_awaited_once_with(ticket_id="t-1", enqueue_launch=True)


@pytest.mark.asyncio
async def test_reject_without_a_ticket_raises_not_found():
    reject = AsyncMock(return_value=None)
    with (
        patch(f"{_CLARIFICATIONS_DB}.reject_ticket_clarification", reject),
        patch(f"{_TICKETS_DB}.get_ticket", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ValueError, match="ticket not found"):
            await prompt_refiner.reject_refinement(ticket_id="t-1")


@pytest.mark.asyncio
async def test_reject_of_a_changed_ticket_raises():
    reject = AsyncMock(return_value=None)
    with (
        patch(f"{_CLARIFICATIONS_DB}.reject_ticket_clarification", reject),
        patch(f"{_TICKETS_DB}.get_ticket", AsyncMock(return_value=_ticket())),
    ):
        with pytest.raises(ValueError, match="ticket changed"):
            await prompt_refiner.reject_refinement(ticket_id="t-1")


# ------------------------------------------------------------ manual entry

_MANUAL = "src.server.services.autonomy.manual"


def _enter_manual_mocks(stack: ExitStack, ticket_status: str = "queued") -> dict:
    """Mocks for one create_manual_task call with the clarify gate off."""
    stack.enter_context(patch.dict(os.environ, {}, clear=False))
    os.environ.pop("AUTONOMY_CLARIFY_ENABLED", None)
    return {
        "sources": stack.enter_context(
            patch(
                f"{_MANUAL}.source_db.list_sources",
                AsyncMock(return_value=[{"source_id": "s-1", "kind": "manual"}]),
            )
        ),
        "insert_trigger": stack.enter_context(
            patch(
                f"{_MANUAL}.trigger_db.insert_trigger",
                AsyncMock(return_value={"trigger_id": "trg-1"}),
            )
        ),
        "ensure_thread": stack.enter_context(
            patch(f"{_MANUAL}.trigger_db.ensure_thread", AsyncMock())
        ),
        "policy": stack.enter_context(
            patch(
                f"{_MANUAL}.policy.current_policy",
                AsyncMock(return_value={"version": "v1"}),
            )
        ),
        "create_ticket": stack.enter_context(
            patch(
                f"{_MANUAL}.ticket_db.create_ticket",
                AsyncMock(return_value=_ticket(status=ticket_status)),
            )
        ),
        "add_subscription": stack.enter_context(
            patch(f"{_MANUAL}.ticket_db.add_subscription", AsyncMock())
        ),
        "get_ticket": stack.enter_context(
            patch(
                f"{_MANUAL}.ticket_db.get_ticket",
                AsyncMock(return_value=_ticket(status=ticket_status)),
            )
        ),
        "effect": stack.enter_context(
            patch(f"{_MANUAL}.enqueue_effect", AsyncMock(return_value=True))
        ),
    }


@pytest.mark.asyncio
async def test_manual_entry_launches_unchanged_with_the_refiner_off():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "off"}))
        mocks = _enter_manual_mocks(stack)
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        await manual_module.create_manual_task(
            user_id="u-1", workspace_id="w-1", prompt="Fix the save button"
        )
    refine_gate.assert_not_awaited()
    assert mocks["create_ticket"].await_args.kwargs["scope"] == "Fix the save button"
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"


@pytest.mark.asyncio
async def test_manual_entry_parks_on_the_refined_rewrite():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_manual_mocks(stack, ticket_status="awaiting_clarification")
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        ticket = await manual_module.create_manual_task(
            user_id="u-1", workspace_id="w-1", prompt="fix the save thing"
        )
    refine_gate.assert_awaited_once()
    assert refine_gate.await_args.kwargs["mode"] == "always"
    assert ticket["status"] == "awaiting_clarification"
    for call in mocks["effect"].await_args_list:
        assert call.kwargs["kind"] != "launch_worker"


@pytest.mark.asyncio
async def test_manual_entry_launches_once_the_rewrite_is_confirmed():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_manual_mocks(stack)
        stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="passed"))
        )
        await manual_module.create_manual_task(
            user_id="u-1", workspace_id="w-1", prompt="fix the save thing"
        )
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [{}, {"AUTONOMY_PROMPT_REFINE": "off"}, {"AUTONOMY_PROMPT_REFINE": "bogus"}],
)
@pytest.mark.parametrize(
    "prompt",
    [
        "/raw fix the save button",
        "fix the save button\nsecond line",
        "  padded request  ",
        "/rawfix",
    ],
)
async def test_off_creates_the_ticket_exactly_as_before(env, prompt):
    """Default off: the Ticket is built from the prompt verbatim, as on main."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=False))
        if not env:
            os.environ.pop("AUTONOMY_PROMPT_REFINE", None)
        mocks = _enter_manual_mocks(stack)
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        hold_gate = stack.enter_context(
            patch(f"{_MOD}.hold_gate", AsyncMock(return_value="awaiting"))
        )
        await manual_module.create_manual_task(
            user_id="u-1",
            workspace_id="w-1",
            prompt=prompt,
            hold_for_confirmation=prompt.startswith("/rawfix"),
        )
    refine_gate.assert_not_awaited()
    hold_gate.assert_not_awaited()
    created = mocks["create_ticket"].await_args.kwargs
    assert created["title"] == prompt.strip().splitlines()[0][:120]
    assert created["scope"] == prompt[:20000]
    assert created["mandate"]["scope"] == prompt[:1500]
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vague", "always"])
async def test_a_bare_raw_prefix_is_refused_before_anything_is_stored(mode):
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": mode}))
        mocks = _enter_manual_mocks(stack)
        with pytest.raises(ValueError, match="/raw"):
            await manual_module.create_manual_task(
                user_id="u-1", workspace_id="w-1", prompt="/raw   "
            )
    mocks["insert_trigger"].assert_not_awaited()
    mocks["create_ticket"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["vague", "always"])
async def test_a_raw_prefix_runs_the_request_as_written(mode):
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": mode}))
        mocks = _enter_manual_mocks(stack)
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        await manual_module.create_manual_task(
            user_id="u-1", workspace_id="w-1", prompt="/raw fix the save button"
        )
    refine_gate.assert_not_awaited()
    created = mocks["create_ticket"].await_args.kwargs
    assert created["scope"] == "fix the save button"
    assert created["mandate"]["scope"] == "fix the save button"
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"


@pytest.mark.asyncio
async def test_the_no_refine_flag_runs_the_request_as_written():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_manual_mocks(stack)
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        await manual_module.create_manual_task(
            user_id="u-1",
            workspace_id="w-1",
            prompt="fix the save button",
            no_refine=True,
        )
    refine_gate.assert_not_awaited()
    assert mocks["create_ticket"].await_args.kwargs["scope"] == "fix the save button"
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"


@pytest.mark.asyncio
async def test_a_held_manual_entry_never_calls_the_refiner():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_manual_mocks(stack, ticket_status="awaiting_clarification")
        hold_gate = stack.enter_context(
            patch(f"{_MOD}.hold_gate", AsyncMock(return_value="awaiting"))
        )
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        ticket = await manual_module.create_manual_task(
            user_id="u-1",
            workspace_id="w-1",
            prompt="summarize the repo",
            hold_for_confirmation=True,
        )
    hold_gate.assert_awaited_once()
    assert hold_gate.await_args.kwargs["mode"] == "always"
    refine_gate.assert_not_awaited()
    assert ticket["status"] == "awaiting_clarification"
    for call in mocks["effect"].await_args_list:
        assert call.kwargs["kind"] != "launch_worker"


@pytest.mark.asyncio
async def test_a_held_manual_entry_launches_with_the_refiner_off():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "off"}))
        mocks = _enter_manual_mocks(stack)
        hold_gate = stack.enter_context(
            patch(f"{_MOD}.hold_gate", AsyncMock(return_value="awaiting"))
        )
        await manual_module.create_manual_task(
            user_id="u-1",
            workspace_id="w-1",
            prompt="summarize the repo",
            hold_for_confirmation=True,
        )
    hold_gate.assert_not_awaited()
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"


@pytest.mark.asyncio
async def test_manual_entry_end_to_end_parks_with_the_model_rewrite():
    llm = SimpleNamespace(
        complete=AsyncMock(
            return_value=RefinedPrompt(
                refined_prompt="Change the toolbar button label to Save.",
                acceptance_criteria=["The label reads Save"],
            )
        )
    )
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_manual_mocks(stack)
        stack.enter_context(patch("src.server.app.setup.llm_service", llm))
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(
                    side_effect=[
                        _ticket(scope="fix the save thing"),
                        _ticket(scope="fix the save thing", status="awaiting_clarification"),
                        _ticket(scope="fix the save thing", status="awaiting_clarification"),
                    ]
                ),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        ticket = await manual_module.create_manual_task(
            user_id="u-1", workspace_id="w-1", prompt="fix the save thing"
        )
    assert ticket["status"] == "awaiting_clarification"
    assert create.await_args.kwargs["expanded_request"] == (
        "Change the toolbar button label to Save."
    )
    assert llm.complete.await_args.kwargs["user_prompt"] == "fix the save thing"
    for call in mocks["effect"].await_args_list:
        assert call.kwargs["kind"] != "launch_worker"


# ------------------------------------------------------------------- intake

_INTAKE = "src.server.services.autonomy.intake"


@asynccontextmanager
async def _null_routing_lock(thread_key: str, *, timeout_seconds: float = 30.0):
    yield


@pytest.fixture(autouse=True)
def _routing_lock():
    with patch(f"{_INTAKE}.ticket_db.thread_routing_lock", MagicMock(side_effect=_null_routing_lock)):
        yield


def _source() -> dict:
    return {
        "source_id": "s1",
        "user_id": "u1",
        "kind": "github",
        "config": {"allowed_logins": ["alice"], "repository": "acme/widgets"},
    }


def _draft(text: str = "please fix the save button") -> dict:
    return {
        "trigger_id": "trg1",
        "external_id": "m1",
        "kind": "comment",
        "thread_key": "github:acme/widgets#12",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": text,
        "url": "",
        "payload": {},
    }


def _enter_intake_mocks(stack: ExitStack, ticket_status: str = "queued") -> dict:
    return {
        "insert": stack.enter_context(
            patch(
                f"{_INTAKE}.trigger_db.insert_trigger",
                AsyncMock(return_value={"trigger_id": "trg1"}),
            )
        ),
        "ensure_thread": stack.enter_context(
            patch(f"{_INTAKE}.trigger_db.ensure_thread", AsyncMock())
        ),
        "list_for_thread": stack.enter_context(
            patch(
                f"{_INTAKE}.ticket_db.list_tickets_for_thread",
                AsyncMock(
                    return_value=[
                        {
                            "ticket_id": "t-1",
                            "status": ticket_status,
                            "origin_thread_key": "github:acme/widgets#12",
                            "subscription_kind": "own",
                        }
                    ]
                ),
            )
        ),
        "add_input": stack.enter_context(
            patch(
                f"{_INTAKE}.ticket_db.add_input",
                AsyncMock(return_value={"input_id": "in1"}),
            )
        ),
        "context_subscribers": stack.enter_context(
            patch(f"{_INTAKE}.ticket_db.context_subscribers", AsyncMock(return_value=[]))
        ),
        "effect": stack.enter_context(
            patch(f"{_INTAKE}.enqueue_effect", AsyncMock(return_value=True))
        ),
    }


@pytest.mark.asyncio
async def test_a_channel_request_is_held_instead_of_auto_refined():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_intake_mocks(stack)
        hold_gate = stack.enter_context(
            patch(f"{_INTAKE}.hold_gate", AsyncMock(return_value="awaiting"))
        )
        assert await intake.receive_trigger(source=_source(), draft=_draft())
    hold_gate.assert_awaited_once()
    mocks["effect"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_channel_request_launches_with_the_refiner_off():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "off"}))
        mocks = _enter_intake_mocks(stack)
        assert await intake.receive_trigger(source=_source(), draft=_draft())
    mocks["effect"].assert_awaited_once()
    assert mocks["effect"].await_args.kwargs["kind"] == "launch_worker"
    assert mocks["effect"].await_args.kwargs["idempotency_key"] == "autonomy:launch:t-1"


@pytest.mark.asyncio
async def test_a_parked_ticket_still_answers_on_its_channel():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_intake_mocks(stack, ticket_status="awaiting_clarification")
        answer = stack.enter_context(
            patch(f"{_INTAKE}.answer_via_channel", AsyncMock(return_value={"ticket_id": "t-1"}))
        )
        assert await intake.receive_trigger(source=_source(), draft=_draft("yes, save"))
    answer.assert_awaited_once()
    mocks["effect"].assert_not_awaited()


# ---------------------------------------------------------------- schedules


@pytest.mark.asyncio
async def test_a_due_schedule_holds_its_request(monkeypatch):
    from src.server.database.autonomy import schedules as schedule_db

    due = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    row = {
        "schedule_id": "11111111-1111-4111-8111-111111111111",
        "user_id": "u1",
        "workspace_id": "w1",
        "title": "Daily",
        "prompt": "Summarize",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": "d1",
        "next_run_at": due,
    }
    created: list[dict] = []

    async def due_rows(*, limit=50):
        return [row]

    async def make_task(**kwargs):
        created.append(kwargs)
        return {"ticket_id": "t1"}

    async def update(user_id, schedule_id, **fields):
        return {**row, **fields}

    monkeypatch.setattr(schedule_db, "due_schedules", due_rows)
    monkeypatch.setattr(schedules_module, "create_manual_task", make_task)
    monkeypatch.setattr(schedule_db, "update_schedule", update)

    assert await schedules_module.run_due_schedules(hold_for_confirmation=True) == 1
    assert created[0]["hold_for_confirmation"] is True


@pytest.mark.asyncio
async def test_the_scheduler_pass_holds_due_schedules(monkeypatch):
    for name in (
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "enqueue_due_polls",
        "sweep_worktree_cleanups",
    ):
        monkeypatch.setattr(scheduler_module, name, AsyncMock(return_value=0))
    monkeypatch.setattr(scheduler_module, "watchdog_sweep", AsyncMock(return_value=None))
    from src.server.database.autonomy import daemons, stops

    monkeypatch.setattr(daemons, "recover_expired_daemon_leases", AsyncMock(return_value=None))
    monkeypatch.setattr(stops, "expire_stop_requests", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.server.services.refactor.memory_consolidation.run_due_memory_consolidations",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.daemon_output.expire_old_logs",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.run_waker_pass", AsyncMock(return_value=None)
    )
    instance = scheduler_module.AutonomyScheduler()
    instance.memory_store = None
    instance.main_agent_orchestrator = None

    await instance._run_pass()

    scheduler_module.run_due_schedules.assert_awaited_once_with(
        hold_for_confirmation=True
    )


# ---------------------------------------------------------------------- API

_API_USER = "test-user-123"
_API_TICKET = "22222222-2222-4222-8222-222222222222"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _api_ticket(status: str = "awaiting_clarification", **overrides) -> dict:
    now = datetime.now(timezone.utc)
    ticket = {
        "ticket_id": _API_TICKET,
        "user_id": _API_USER,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label to Save",
        "status": status,
        "mandate": {
            "strength": "explicit",
            "source_id": "s1",
            "refine": {"mode": "always", "degraded": False},
        },
        "origin_trigger_id": "trg_1",
        "origin_thread_key": "github:acme/widgets#12",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "attempts": 0,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    ticket.update(overrides)
    return ticket


def _feature_on():
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_confirm_endpoint_adopts_the_text(client):
    confirm = AsyncMock(return_value=_api_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket()),
        ),
        patch("src.server.app.autonomy.confirm_refinement", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/confirm",
            json={"text": "Change the toolbar label to Save."},
        )
    assert resp.status_code == 200
    confirm.assert_awaited_once_with(
        ticket_id=_API_TICKET, text="Change the toolbar label to Save."
    )
    assert resp.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_confirm_endpoint_without_text_adopts_the_rewrite(client):
    confirm = AsyncMock(return_value=_api_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket()),
        ),
        patch("src.server.app.autonomy.confirm_refinement", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/confirm",
            json={},
        )
    assert resp.status_code == 200
    confirm.assert_awaited_once_with(ticket_id=_API_TICKET, text=None)


@pytest.mark.asyncio
async def test_confirm_endpoint_409_when_not_awaiting(client):
    confirm = AsyncMock()
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket(status="queued")),
        ),
        patch("src.server.app.autonomy.confirm_refinement", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/confirm",
            json={"text": "hello"},
        )
    assert resp.status_code == 409
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_endpoint_404_for_unowned_ticket(client):
    confirm = AsyncMock()
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket(user_id="someone-else")),
        ),
        patch("src.server.app.autonomy.confirm_refinement", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/confirm",
            json={"text": "hello"},
        )
    assert resp.status_code == 404
    confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_endpoint_maps_value_error_to_409(client):
    confirm = AsyncMock(side_effect=ValueError("no active refinement"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket()),
        ),
        patch("src.server.app.autonomy.confirm_refinement", new=confirm),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/confirm",
            json={"text": "hello"},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no active refinement"


@pytest.mark.asyncio
async def test_reject_endpoint_keeps_the_original_request(client):
    reject = AsyncMock(return_value=_api_ticket(status="queued"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket()),
        ),
        patch("src.server.app.autonomy.reject_refinement", new=reject),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/reject"
        )
    assert resp.status_code == 200
    reject.assert_awaited_once_with(ticket_id=_API_TICKET)
    assert resp.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_reject_endpoint_409_when_not_awaiting(client):
    reject = AsyncMock()
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket(status="queued")),
        ),
        patch("src.server.app.autonomy.reject_refinement", new=reject),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/reject"
        )
    assert resp.status_code == 409
    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_endpoint_404_for_unowned_ticket(client):
    reject = AsyncMock()
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket(user_id="someone-else")),
        ),
        patch("src.server.app.autonomy.reject_refinement", new=reject),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/reject"
        )
    assert resp.status_code == 404
    reject.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_endpoint_maps_value_error_to_409(client):
    reject = AsyncMock(side_effect=ValueError("ticket changed or no active refinement"))
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_api_ticket()),
        ),
        patch("src.server.app.autonomy.reject_refinement", new=reject),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/reject"
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "ticket changed or no active refinement"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_refinement_endpoints_refuse_a_clarification_gate_ticket(client, action):
    """A Ticket the clarify gate parked is answered on its own endpoint, not here."""
    confirm = AsyncMock()
    reject = AsyncMock()
    with (
        _feature_on(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(
                return_value=_api_ticket(
                    mandate={"strength": "explicit", "source_id": "s1"}
                )
            ),
        ),
        patch("src.server.app.autonomy.confirm_refinement", new=confirm),
        patch("src.server.app.autonomy.reject_refinement", new=reject),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{_API_TICKET}/refinement/{action}", json={}
        )
    assert resp.status_code == 409
    confirm.assert_not_awaited()
    reject.assert_not_awaited()


# ------------------------------------------------- Router accept (new tickets)

_HOST = "src.server.services.autonomy.host"


def _host_accept_patches(stack: ExitStack, *, enqueue: AsyncMock) -> None:
    stack.enter_context(
        patch(
            f"{_HOST}.eval_db.active_eval_for_trigger",
            AsyncMock(return_value={"eval_id": "eval-1"}),
        )
    )
    stack.enter_context(
        patch(
            f"{_HOST}.trigger_db.get_trigger",
            AsyncMock(
                return_value={
                    "trigger_id": "tr-1",
                    "source_id": "s-1",
                    "user_id": "u-1",
                    "thread_key": "github:acme/widgets#12",
                    "actor_login": "alice",
                    "actor_is_bot": False,
                    "addressed": True,
                    "text": "@bot fix the save button",
                }
            ),
        )
    )
    stack.enter_context(
        patch(
            f"{_HOST}.source_db.get_source_any_user",
            AsyncMock(
                return_value={
                    "source_id": "s-1",
                    "user_id": "u-1",
                    "kind": "github",
                    "config": {"workspace_id": "w-1", "allowed_logins": ["alice"]},
                }
            ),
        )
    )
    stack.enter_context(
        patch(
            f"{_HOST}.policy.current_policy",
            AsyncMock(return_value={"accept_mode": "explicit", "version": "v1"}),
        )
    )
    stack.enter_context(
        patch(
            f"{_HOST}.eval_db.decide_eval",
            AsyncMock(return_value={"eval_id": "eval-1", "ticket_id": "ticket-9"}),
        )
    )
    stack.enter_context(
        patch(f"{_HOST}.router_db.record_router_route_decision", AsyncMock())
    )
    stack.enter_context(patch(f"{_HOST}.clarify_enabled", return_value=False))
    stack.enter_context(patch(f"{_HOST}.gate", AsyncMock(return_value="passed")))
    stack.enter_context(patch(f"{_HOST}.enqueue_effect", enqueue))


@pytest.mark.asyncio
async def test_a_new_github_ticket_is_held_never_refined_when_refine_is_on():
    from src.server.services.autonomy.host import AutonomyToolHost

    enqueue = AsyncMock(return_value=True)
    refiner = FakeRefiner()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        _host_accept_patches(stack, enqueue=enqueue)
        _stub_gate_lock(stack)
        stack.enter_context(
            patch(
                f"{_TICKETS_DB}.get_ticket",
                AsyncMock(return_value=_ticket(ticket_id="ticket-9")),
            )
        )
        stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        update = stack.enter_context(
            patch(
                f"{_TICKETS_DB}.update_ticket",
                AsyncMock(return_value=_ticket(status="awaiting_clarification")),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_CLARIFICATIONS_DB}.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        default_refiner = stack.enter_context(
            patch(f"{_MOD}.default_refiner", AsyncMock(return_value=refiner))
        )
        result = await AutonomyToolHost(user_id="u-1", thread_id="router").route(
            trigger_id="tr-1", decision="accept", title="Fix", scope="fix the save button"
        )
    assert result["decision"] == "accept"
    enqueue.assert_not_awaited()
    assert update.await_args.kwargs["status"] == "awaiting_clarification"
    assert create.await_args.kwargs["expanded_request"] == "do something"
    default_refiner.assert_not_awaited()
    assert refiner.calls == []


@pytest.mark.asyncio
async def test_a_new_github_ticket_launches_unchanged_when_refine_is_off():
    from src.server.services.autonomy.host import AutonomyToolHost

    enqueue = AsyncMock(return_value=True)
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {}, clear=False))
        os.environ.pop("AUTONOMY_PROMPT_REFINE", None)
        _host_accept_patches(stack, enqueue=enqueue)
        get_ticket = stack.enter_context(
            patch(f"{_TICKETS_DB}.get_ticket", AsyncMock())
        )
        await AutonomyToolHost(user_id="u-1", thread_id="router").route(
            trigger_id="tr-1", decision="accept", title="Fix", scope="fix the save button"
        )
    get_ticket.assert_not_awaited()
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["idempotency_key"] == "autonomy:launch:ticket-9"


@pytest.mark.asyncio
async def test_a_raw_schedule_prompt_is_still_held():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AUTONOMY_PROMPT_REFINE": "always"}))
        mocks = _enter_manual_mocks(stack)
        hold_gate = stack.enter_context(
            patch(f"{_MOD}.hold_gate", AsyncMock(return_value="awaiting"))
        )
        refine_gate = stack.enter_context(
            patch(f"{_MOD}.refine_gate", AsyncMock(return_value="awaiting"))
        )
        await manual_module.create_manual_task(
            user_id="u-1",
            workspace_id="w-1",
            prompt="/raw nightly dependency audit",
            no_refine=True,
            hold_for_confirmation=True,
        )
    hold_gate.assert_awaited_once()
    refine_gate.assert_not_awaited()
    mocks["effect"].assert_not_awaited()
