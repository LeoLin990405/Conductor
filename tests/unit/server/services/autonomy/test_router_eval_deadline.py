"""Router eval dispatch stops at that eval's deadline and cancels only its run."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.evals import (
    ROUTER_EVAL_DEADLINE_SECONDS,
    _cancel_expired_router_batches,
    recover_stale_evals,
    route_eval_effect,
)
from src.server.services.autonomy.models import EffectRecord
from src.server.services.projects.dispatch import stable_request_key

EVAL_ID = "eval-1"
RUN_ID = "run-1"
THREAD_ID = "router-thread-1"


class _NoDb:
    async def __aenter__(self):
        raise AssertionError("live database must not be opened")

    async def __aexit__(self, *exc):
        return False


def _no_db():
    return _NoDb()


def _batch_request_key(eval_ids: list[str] | None = None) -> str:
    identity = ",".join(eval_ids or [EVAL_ID])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return f"autonomy:router-batch:{digest}"


def _effect() -> EffectRecord:
    return EffectRecord(
        effect_id="eff-1",
        user_id="user-1",
        kind="router_turn",
        idempotency_key=f"autonomy:router-turn:{EVAL_ID}",
        payload={
            "eval_id": EVAL_ID,
            "thread_key": "github:acme/widgets#1",
            "user_id": "user-1",
        },
        attempts=1,
    )


def _eval_row(deadline):
    return {
        "eval_id": EVAL_ID,
        "thread_key": "github:acme/widgets#1",
        "trigger_ids": ["trg-1"],
        "status": "queued",
        "deadline_at": deadline,
    }


class _Route:
    def __init__(self, deadline):
        self.deadline = deadline
        self.dispatch = AsyncMock()
        self.expire_one = AsyncMock(return_value=True)
        self.mark_delivered = AsyncMock()
        self.cancel = AsyncMock()
        self.find_run = AsyncMock(
            return_value={
                "conversation_response_id": RUN_ID,
                "conversation_thread_id": THREAD_ID,
            }
        )
        self._stack = ExitStack()

    def __enter__(self):
        stack = self._stack
        for target in (
            "src.server.database.pool.get_db_connection",
            "src.server.database.autonomy.evals.get_db_connection",
            "src.server.database.autonomy.triggers.get_db_connection",
            "src.server.database.autonomy.sources.get_db_connection",
            "src.server.database.autonomy.tickets.get_db_connection",
            "src.server.services.autonomy.policy.get_db_connection",
        ):
            stack.enter_context(patch(target, _no_db))
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.eval_db.get_eval",
                AsyncMock(return_value=_eval_row(self.deadline)),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.eval_db.router_effect_batches",
                AsyncMock(return_value=[]),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.eval_db.expire_one",
                self.expire_one,
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.eval_db.mark_delivered",
                self.mark_delivered,
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.trigger_db.get_trigger",
                AsyncMock(
                    return_value={
                        "trigger_id": "trg-1",
                        "source_id": "src-1",
                        "thread_key": "github:acme/widgets#1",
                        "actor_login": "alice",
                        "addressed": True,
                        "text": "please fix",
                        "url": "",
                    }
                ),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.trigger_db.list_triggers_for_thread",
                AsyncMock(return_value=[]),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.ticket_db.list_tickets_for_thread",
                AsyncMock(return_value=[]),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.source_db.get_source_any_user",
                AsyncMock(
                    return_value={
                        "source_id": "src-1",
                        "kind": "github",
                        "config": {
                            "repository": "acme/widgets",
                            "allowed_logins": ["alice"],
                        },
                    }
                ),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.policy.current_policy",
                AsyncMock(
                    return_value={
                        "accept_mode": "explicit",
                        "admission": {},
                        "version": "v1",
                    }
                ),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.autonomy.evals.router.dispatch_candidate_batch",
                self.dispatch,
            )
        )
        stack.enter_context(
            patch(
                "src.server.database.runs.lifecycle.find_run_by_request_key",
                self.find_run,
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.cancel_dispatch.cancel_workflow",
                self.cancel,
            )
        )
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


async def _hang(**_kwargs):
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_route_eval_effect_times_out_at_the_remaining_deadline():
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=400)
    with _Route(deadline) as route:
        route.dispatch.side_effect = _hang
        await asyncio.wait_for(route_eval_effect(_effect()), timeout=2)

    route.dispatch.assert_awaited()
    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.mark_delivered.assert_not_awaited()
    route.find_run.assert_awaited_once_with(
        stable_request_key(_batch_request_key())
    )
    route.cancel.assert_awaited_once_with(THREAD_ID, RUN_ID, by_user=False)
    assert route.cancel.await_args.args[1] == RUN_ID


@pytest.mark.asyncio
async def test_route_eval_effect_caps_the_wait_at_the_constant():
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    with (
        _Route(deadline) as route,
        patch(
            "src.server.services.autonomy.evals.ROUTER_EVAL_DEADLINE_SECONDS",
            0.05,
        ),
    ):
        route.dispatch.side_effect = _hang
        await asyncio.wait_for(route_eval_effect(_effect()), timeout=2)

    route.dispatch.assert_awaited()
    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.mark_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_deadline_waits_the_cap_and_does_not_mark_delivered():
    seen: list[float] = []

    async def spy(awaitable, timeout):
        seen.append(timeout)
        awaitable.close()
        raise TimeoutError

    with (
        _Route(None) as route,
        patch("src.server.services.autonomy.evals.asyncio.wait_for", spy),
    ):
        await route_eval_effect(_effect())

    assert seen == [float(ROUTER_EVAL_DEADLINE_SECONDS)]
    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.mark_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_past_deadline_eval_is_not_dispatched():
    deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    with _Route(deadline) as route:
        await route_eval_effect(_effect())

    route.dispatch.assert_not_awaited()
    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.mark_delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_uses_only_that_eval_run_and_a_failure_still_returns():
    deadline = datetime.now(timezone.utc) - timedelta(seconds=5)

    async def boom(thread_id, run_id=None, *, by_user=True):
        assert thread_id == THREAD_ID
        assert run_id == RUN_ID
        assert by_user is False
        raise RuntimeError("cancel failed")

    with _Route(deadline) as route:
        route.cancel.side_effect = boom
        await route_eval_effect(_effect())

    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.mark_delivered.assert_not_awaited()
    route.find_run.assert_awaited_once_with(
        stable_request_key(_batch_request_key())
    )
    route.cancel.assert_awaited_once_with(THREAD_ID, RUN_ID, by_user=False)
    assert route.cancel.await_args.args[1] is not None


@pytest.mark.asyncio
async def test_cancel_is_not_called_without_a_run_id():
    deadline = datetime.now(timezone.utc) - timedelta(seconds=5)
    with _Route(deadline) as route:
        route.find_run.return_value = {"conversation_thread_id": THREAD_ID}
        await route_eval_effect(_effect())

    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.cancel.assert_not_awaited()

    with _Route(deadline) as route:
        route.find_run.side_effect = RuntimeError("lookup failed")
        await route_eval_effect(_effect())

    route.expire_one.assert_awaited_once_with(EVAL_ID)
    route.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_dispatch_still_marks_delivered():
    deadline = datetime.now(timezone.utc) + timedelta(minutes=4)
    with _Route(deadline) as route:
        await route_eval_effect(_effect())

    route.dispatch.assert_awaited()
    route.mark_delivered.assert_awaited_once_with(EVAL_ID)
    route.expire_one.assert_not_awaited()
    route.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_stale_evals_expires_and_cancels_only_those_runs():
    find_run = AsyncMock(
        return_value={
            "conversation_response_id": RUN_ID,
            "conversation_thread_id": THREAD_ID,
        }
    )
    cancel = AsyncMock()
    with (
        patch("src.server.database.pool.get_db_connection", _no_db),
        patch(
            "src.server.database.autonomy.evals.get_db_connection",
            _no_db,
        ),
        patch(
            "src.server.services.autonomy.evals.eval_db.expire_stale_evals",
            new_callable=AsyncMock,
            return_value=["e1", "e2"],
        ) as expire,
        patch(
            "src.server.services.autonomy.evals.eval_db.router_effect_batches",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.server.database.runs.lifecycle.find_run_by_request_key",
            find_run,
        ),
        patch(
            "src.server.services.cancel_dispatch.cancel_workflow",
            cancel,
        ),
    ):
        assert await recover_stale_evals() == 2
    expire.assert_awaited_once_with()
    assert find_run.await_args_list[0].args == (stable_request_key("autonomy:router:e1"),)
    assert find_run.await_args_list[1].args == (stable_request_key("autonomy:router:e2"),)
    assert cancel.await_count == 2
    for call in cancel.await_args_list:
        assert call.args == (THREAD_ID, RUN_ID)
        assert call.kwargs == {"by_user": False}


@pytest.mark.asyncio
async def test_mixed_batch_expiry_keeps_the_shared_run_when_a_peer_is_live():
    find_run = AsyncMock()
    cancel = AsyncMock()
    batch = {
        "idempotency_key": "autonomy:router-batch:abc",
        "eval_ids": ["expired", "live"],
    }
    with (
        patch(
            "src.server.services.autonomy.evals.eval_db.router_effect_batches",
            AsyncMock(return_value=[batch]),
        ),
        patch(
            "src.server.services.autonomy.evals.eval_db.get_eval",
            AsyncMock(side_effect=[{"status": "unavailable"}, {"status": "delivered"}]),
        ),
        patch("src.server.database.runs.lifecycle.find_run_by_request_key", find_run),
        patch("src.server.services.cancel_dispatch.cancel_workflow", cancel),
    ):
        await _cancel_expired_router_batches(["expired"])

    find_run.assert_not_awaited()
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_recovery_cancels_once_by_the_persisted_batch_key():
    find_run = AsyncMock(
        return_value={
            "conversation_response_id": RUN_ID,
            "conversation_thread_id": THREAD_ID,
        }
    )
    cancel = AsyncMock()
    batch = {
        "idempotency_key": "autonomy:router-batch:abc",
        "eval_ids": ["expired-1", "expired-2"],
    }
    with (
        patch(
            "src.server.services.autonomy.evals.eval_db.router_effect_batches",
            AsyncMock(return_value=[batch]),
        ),
        patch(
            "src.server.services.autonomy.evals.eval_db.get_eval",
            AsyncMock(return_value={"status": "unavailable"}),
        ),
        patch("src.server.database.runs.lifecycle.find_run_by_request_key", find_run),
        patch("src.server.services.cancel_dispatch.cancel_workflow", cancel),
    ):
        await _cancel_expired_router_batches(["expired-1", "expired-2"])

    find_run.assert_awaited_once_with(stable_request_key("autonomy:router-batch:abc"))
    cancel.assert_awaited_once_with(THREAD_ID, RUN_ID, by_user=False)
