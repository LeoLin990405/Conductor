"""The clarification gate: score a prompt, park unclear Tickets, confirm."""

import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.clarify import (
    ClarityAssessment,
    build_fallback_assessment,
    clarity_threshold,
    clarify_enabled,
    confirm_clarification,
    gate,
    heuristic_clarity_score,
)

_MOD = "src.server.services.autonomy.clarify"
_ENABLED = {"AUTONOMY_CLARIFY_ENABLED": "1"}
_DISABLED = {"AUTONOMY_CLARIFY_ENABLED": "0"}
_UNSET = object()


class FakeClarifier:
    def __init__(self, assessment: ClarityAssessment):
        self.assessment = assessment
        self.calls: list[tuple[str, str]] = []

    async def assess(self, *, user_id: str, prompt: str) -> ClarityAssessment:
        self.calls.append((user_id, prompt))
        return self.assessment


class _GateLock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


def _stub_gate_lock(stack: ExitStack, fresh_ticket: dict | None = None) -> None:
    stack.enter_context(patch(f"{_MOD}._ticket_gate_lock", return_value=_GateLock()))
    stack.enter_context(
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value=fresh_ticket or _ticket(status="awaiting_clarification")),
        )
    )


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


def _enter_gate_mocks(stack: ExitStack, source_kind: str) -> dict:
    """Mocks for one gate call that parks the Ticket; asserts run inside stack."""
    return {
        "active": stack.enter_context(
            patch(
                f"{_MOD}.clarification_db.get_active_clarification",
                AsyncMock(return_value=None),
            )
        ),
        "create": stack.enter_context(
            patch(
                f"{_MOD}.clarification_db.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        ),
        "update": stack.enter_context(
            patch(
                f"{_MOD}.ticket_db.update_ticket",
                AsyncMock(
                    return_value={
                        "ticket_id": "t-1",
                        "status": "awaiting_clarification",
                    }
                ),
            )
        ),
        "source": stack.enter_context(
            patch(
                f"{_MOD}.source_db.get_source_any_user",
                AsyncMock(return_value={"source_id": "s-1", "kind": source_kind}),
            )
        ),
        "outbox": stack.enter_context(
            patch(
                f"{_MOD}.outbox_db.enqueue_outbox",
                AsyncMock(return_value={"outbox_id": "ob-1"}),
            )
        ),
        "effect": stack.enter_context(
            patch(f"{_MOD}.enqueue_effect", AsyncMock(return_value=True))
        ),
    }


# ---------------------------------------------------------------- heuristic


def test_very_short_prompt_scores_low():
    assert heuristic_clarity_score("do it") == 0.45
    assert heuristic_clarity_score("do it") < 0.5


def test_detailed_prompt_scores_high():
    prompt = (
        "Fix the flaky `test_dispatch_keys` in src/server/services/autonomy: "
        "it times out after 30s when Redis is slow."
    )
    assert heuristic_clarity_score(prompt) >= 0.95


def test_vague_tokens_lower_the_score():
    plain = heuristic_clarity_score("fix the thing")
    vague = heuristic_clarity_score("fix the thing asap maybe somehow")
    assert vague < plain


def test_heuristic_is_deterministic():
    prompt = "maybe update something in the config etc"
    assert heuristic_clarity_score(prompt) == heuristic_clarity_score(prompt)


# ------------------------------------------------------------------- config


def test_clarify_enabled_reads_env():
    with patch.dict(os.environ, _ENABLED):
        assert clarify_enabled()
    with patch.dict(os.environ, _DISABLED):
        assert not clarify_enabled()


def test_threshold_defaults_and_clamps():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AUTONOMY_CLARIFY_THRESHOLD", None)
        assert clarity_threshold() == 0.6
    with patch.dict(os.environ, {"AUTONOMY_CLARIFY_THRESHOLD": "2"}):
        assert clarity_threshold() == 1.0
    with patch.dict(os.environ, {"AUTONOMY_CLARIFY_THRESHOLD": "-1"}):
        assert clarity_threshold() == 0.0


# --------------------------------------------------------------------- gate


@pytest.mark.asyncio
async def test_gate_disabled_passes_without_touching_the_db():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _DISABLED))
        active = stack.enter_context(
            patch(f"{_MOD}.clarification_db.get_active_clarification", AsyncMock())
        )
        create = stack.enter_context(
            patch(f"{_MOD}.clarification_db.create_clarification", AsyncMock())
        )
        update = stack.enter_context(
            patch(f"{_MOD}.ticket_db.update_ticket", AsyncMock())
        )
        outcome = await gate(ticket=_ticket())
    assert outcome == "passed"
    active.assert_not_awaited()
    create.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_passes_a_clear_prompt_without_a_clarification_row():
    clarifier = FakeClarifier(ClarityAssessment(score=0.1))
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(
            stack,
            _ticket(
                status="awaiting_clarification",
                scope="Fix the flaky `test_dispatch_keys` under src/server: "
                "it times out after 30s when Redis is slow.",
                title="Fix flaky test",
            ),
        )
        active = stack.enter_context(
            patch(
                f"{_MOD}.clarification_db.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(f"{_MOD}.clarification_db.create_clarification", AsyncMock())
        )
        stack.enter_context(
            patch(
                f"{_MOD}.ticket_db.update_ticket",
                AsyncMock(return_value={"ticket_id": "t-1", "status": "queued"}),
            )
        )
        outcome = await gate(
            ticket=_ticket(
                scope="Fix the flaky `test_dispatch_keys` under src/server: "
                "it times out after 30s when Redis is slow.",
                title="Fix flaky test",
            ),
            clarifier=clarifier,
        )
    assert outcome == "passed"
    assert clarifier.calls == []
    active.assert_awaited_once_with("t-1")
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_noops_when_the_ticket_is_not_queued():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack, _ticket(status="running"))
        active = stack.enter_context(
            patch(f"{_MOD}.clarification_db.get_active_clarification", AsyncMock())
        )
        create = stack.enter_context(
            patch(f"{_MOD}.clarification_db.create_clarification", AsyncMock())
        )
        outcome = await gate(ticket=_ticket(status="running"))
    assert outcome == "passed"
    active.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_parks_an_ambiguous_ticket_and_asks_on_the_source_channel():
    clarifier = FakeClarifier(
        ClarityAssessment(
            score=0.2,
            expanded_request="Fix the login redirect loop.",
            assumptions=["Targets the web app"],
            open_questions=["Which environment?"],
        )
    )
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack)
        mocks = _enter_gate_mocks(stack, "github")
        outcome = await gate(ticket=_ticket(), clarifier=clarifier)
        assert outcome == "awaiting"
        assert clarifier.calls == [("u-1", "do something")]

        create = mocks["create"]
        assert create.await_count == 1
        kwargs = create.await_args.kwargs
        assert kwargs["ticket_id"] == "t-1"
        assert kwargs["original_prompt"] == "do something"
        assert kwargs["expanded_request"] == "Fix the login redirect loop."
        assert kwargs["assumptions"] == ["Targets the web app"]
        assert kwargs["open_questions"] == ["Which environment?"]
        # The model score is floored at the heuristic value.
        assert kwargs["clarity_score"] == max(0.35, 0.2)

        update = mocks["update"]
        update.assert_awaited_once()
        assert update.await_args.kwargs["expected_status"] == "awaiting_clarification"
        assert "status" not in update.await_args.kwargs
        assert update.await_args.kwargs["attention"] == {
            "question": "Please confirm the clarified request before this Ticket starts."
        }

        outbox = mocks["outbox"]
        outbox.assert_awaited_once()
        assert outbox.await_args.kwargs["idempotency_key"] == (
            "autonomy:clarify-request:t-1"
        )
        assert "Fix the login redirect loop." in outbox.await_args.kwargs["body"]
        assert "1. Which environment?" in outbox.await_args.kwargs["body"]

        effect = mocks["effect"]
        effect.assert_awaited_once()
        assert effect.await_args.kwargs["kind"] == "reply"
        assert effect.await_args.kwargs["idempotency_key"] == (
            "autonomy:reply-effect:ob-1"
        )
        assert effect.await_args.kwargs["payload"] == {"outbox_id": "ob-1"}


@pytest.mark.asyncio
async def test_gate_never_enqueues_a_launch_effect():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack)
        mocks = _enter_gate_mocks(stack, "github")
        outcome = await gate(
            ticket=_ticket(), clarifier=FakeClarifier(ClarityAssessment(score=0.1))
        )
        effect = mocks["effect"]
        for call in effect.await_args_list:
            assert call.kwargs["kind"] != "launch_worker"
    assert outcome == "awaiting"


@pytest.mark.asyncio
async def test_gate_skips_the_outbox_for_a_manual_source():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack)
        mocks = _enter_gate_mocks(stack, "manual")
        outcome = await gate(
            ticket=_ticket(), clarifier=FakeClarifier(ClarityAssessment(score=0.1))
        )
        mocks["create"].assert_awaited_once()
        mocks["update"].assert_awaited_once()
        mocks["outbox"].assert_not_awaited()
        mocks["effect"].assert_not_awaited()
    assert outcome == "awaiting"


@pytest.mark.asyncio
async def test_gate_is_idempotent_once_a_clarification_exists():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack)
        mocks = _enter_gate_mocks(stack, "github")
        clarifier = FakeClarifier(ClarityAssessment(score=0.1))
        assert await gate(ticket=_ticket(), clarifier=clarifier) == "awaiting"
        create_calls = mocks["create"].await_count
        outbox_calls = mocks["outbox"].await_count
        update_calls = mocks["update"].await_count
        mocks["active"].return_value = {"clarification_id": "c-1"}
        second = await gate(ticket=_ticket(), clarifier=clarifier)
        assert second == "awaiting"
        assert mocks["create"].await_count == create_calls
        assert mocks["outbox"].await_count == outbox_calls
        assert mocks["update"].await_count == update_calls


@pytest.mark.asyncio
async def test_gate_treats_a_moved_ticket_as_passed():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack)
        mocks = _enter_gate_mocks(stack, "github")
        mocks["update"].return_value = None
        stack.enter_context(
            patch(
                f"{_MOD}.ticket_db.get_ticket",
                AsyncMock(return_value={"ticket_id": "t-1", "status": "running"}),
            )
        )
        outcome = await gate(
            ticket=_ticket(), clarifier=FakeClarifier(ClarityAssessment(score=0.1))
        )
        mocks["source"].assert_not_awaited()
        mocks["outbox"].assert_not_awaited()
    assert outcome == "passed"


@pytest.mark.asyncio
async def test_gate_falls_back_to_the_template_draft_without_an_llm():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        _stub_gate_lock(stack)
        stack.enter_context(patch("src.server.app.setup.llm_service", None))
        stack.enter_context(
            patch(
                f"{_MOD}.clarification_db.get_active_clarification",
                AsyncMock(return_value=None),
            )
        )
        create = stack.enter_context(
            patch(
                f"{_MOD}.clarification_db.create_clarification",
                AsyncMock(return_value={"clarification_id": "c-1"}),
            )
        )
        stack.enter_context(
            patch(f"{_MOD}.ticket_db.update_ticket", AsyncMock(return_value={"ticket_id": "t-1"}))
        )
        stack.enter_context(
            patch(f"{_MOD}.source_db.get_source_any_user", AsyncMock(return_value=None))
        )
        outcome = await gate(ticket=_ticket())
        kwargs = create.await_args.kwargs
        assert kwargs["expanded_request"].startswith("Goal: do something")
        assert kwargs["assumptions"] == [
            "The request targets the workspace this Ticket was filed in."
        ]
        assert "What is the exact goal or desired outcome?" in kwargs["open_questions"]
    assert outcome == "awaiting"


def test_build_fallback_assessment_asks_about_missing_goal_cues():
    assessment = build_fallback_assessment("stuff")
    assert assessment.score == heuristic_clarity_score("stuff")
    assert "What action should be taken?" in assessment.open_questions
    assert "What is the exact goal or desired outcome?" in assessment.open_questions


# ------------------------------------------------------------------ confirm


def _enter_confirm_mocks(stack: ExitStack, update_return=_UNSET) -> dict:
    return {
        "confirm": stack.enter_context(
            patch(
                f"{_MOD}.clarification_db.confirm_ticket_clarification",
                AsyncMock(
                    return_value=(
                        None
                        if update_return is None
                        else {
                            "ticket": update_return
                            if update_return is not _UNSET
                            else {"ticket_id": "t-1", "user_id": "u-1", "status": "queued", "mandate": {"goal": "Do the thing properly, using the v2 API.", "clarified": True, "strength": "explicit"}},
                            "clarification": {"clarification_id": "c-1"},
                        }
                    )
                ),
            )
        ),
        "effect": stack.enter_context(
            patch(f"{_MOD}.enqueue_effect", AsyncMock(return_value=True))
        ),
    }


@pytest.mark.asyncio
async def test_confirm_clarification_sets_the_goal_and_launches():
    with ExitStack() as stack:
        mocks = _enter_confirm_mocks(stack)
        result = await confirm_clarification(
            ticket_id="t-1", text="Do the thing properly, using the v2 API."
        )
        assert result["status"] == "queued"

        mocks["confirm"].assert_awaited_once_with(
            ticket_id="t-1",
            text="Do the thing properly, using the v2 API.",
            enqueue_launch=True,
        )

        effect = mocks["effect"]
        effect.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_can_advance_a_root_without_enqueuing_a_worker():
    with ExitStack() as stack:
        mocks = _enter_confirm_mocks(stack)
        await confirm_clarification(
            ticket_id="t-1", text="Revised goal", enqueue_launch=False
        )
        mocks["confirm"].assert_awaited_once_with(
            ticket_id="t-1", text="Revised goal", enqueue_launch=False
        )
        mocks["effect"].assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_override_does_not_read_env_or_gate_when_false():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, _ENABLED))
        active = stack.enter_context(
            patch(f"{_MOD}.clarification_db.get_active_clarification", AsyncMock())
        )
        outcome = await gate(ticket=_ticket(), enabled=False)
    assert outcome == "passed"
    active.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_clarification_rejects_empty_text():
    with ExitStack() as stack:
        get = stack.enter_context(
            patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock())
        )
        with pytest.raises(ValueError):
            await confirm_clarification(ticket_id="t-1", text="")
        get.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_clarification_without_an_active_one_raises():
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value={"ticket_id": "t-1", "user_id": "u-1"})))
        active = stack.enter_context(patch(f"{_MOD}.clarification_db.confirm_ticket_clarification", AsyncMock(return_value=None)))
        with pytest.raises(ValueError, match="no active clarification"):
            await confirm_clarification(ticket_id="t-1", text="clarified goal")
        active.assert_awaited_once_with(ticket_id="t-1", text="clarified goal", enqueue_launch=True)


@pytest.mark.asyncio
async def test_confirm_clarification_raises_when_the_ticket_changed():
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value={"ticket_id": "t-1"})))
        mocks = _enter_confirm_mocks(stack, update_return=None)
        with pytest.raises(ValueError, match="ticket changed or no active clarification"):
            await confirm_clarification(ticket_id="t-1", text="clarified goal")
        mocks["confirm"].assert_awaited_once()
        mocks["effect"].assert_not_awaited()
