"""POST /api/v1/threads with a title the caller names.

A thread is born titled: by default with the stamp of its first message, which
the title generator then replaces. A caller may name the title instead, for a
first turn that opens with no message of the user's (the first query is empty,
or is not the user's words): the row stores that title and no generation runs,
so nothing written for the model can become the thread's name.
"""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from src.server.models.conversation import ThreadCreateRequest
from tests.unit.server.app.test_threads_subagents_allowed import _request, _row

CREATE_MOD = "src.server.app.threads.create"


async def _create(body):
    from src.server.services.turn_runtime import TurnRoute

    with (
        patch(f"{CREATE_MOD}.HOST_MODE", "oss"),
        patch(
            f"{CREATE_MOD}.requested_workspace",
            new=AsyncMock(return_value={"workspace_id": "ws-1", "status": "active"}),
        ),
        patch(f"{CREATE_MOD}.turn_route", return_value=TurnRoute("ws-1", "generalist")),
        patch(
            f"{CREATE_MOD}.create_thread", new=AsyncMock(return_value=_row())
        ) as create,
        patch(
            f"{CREATE_MOD}.schedule_title_generation", return_value=None
        ) as generate,
    ):
        resp = await _request("POST", "/api/v1/threads", body)
    return resp, create, generate


@pytest.mark.asyncio
async def test_a_named_title_is_stored_and_nothing_generates_over_it():
    resp, create, generate = await _create({"workspace_id": "ws-1", "title": "Getting started"})

    assert resp.status_code == 201
    assert create.await_args.kwargs["title"] == "Getting started"
    assert resp.json()["title"] == "Getting started"
    generate.assert_not_called()


@pytest.mark.asyncio
async def test_a_named_title_wins_over_the_first_query():
    resp, create, generate = await _create(
        {"workspace_id": "ws-1", "first_query": "hello", "title": "Getting started"}
    )

    assert resp.status_code == 201
    assert create.await_args.kwargs["title"] == "Getting started"
    generate.assert_not_called()


@pytest.mark.asyncio
async def test_without_one_the_first_query_is_stamped_and_generated_from():
    resp, create, generate = await _create({"workspace_id": "ws-1", "first_query": "hello"})

    assert resp.status_code == 201
    assert create.await_args.kwargs["title"] == "hello"
    generate.assert_called_once()
    assert generate.call_args.kwargs["first_query"] == "hello"
    assert generate.call_args.kwargs["expected_title"] == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"workspace_id": "ws-1"},
        {"workspace_id": "ws-1", "first_query": ""},
        {"workspace_id": "ws-1", "first_query": "", "title": ""},
    ],
    ids=["neither", "blank-query", "blank-both"],
)
async def test_a_create_with_nothing_to_title_it_is_refused(body):
    resp, create, _ = await _create(body)

    assert resp.status_code == 422
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_provider_is_bound_in_thread_metadata():
    from src.server.services.native_runtime import NativeRuntimeStatus
    from src.server.services.native_runtime.contracts import NativeCapabilities

    status = NativeRuntimeStatus(
        provider="codex",
        installed=True,
        binary="/usr/bin/codex",
        live_verified=True,
        capabilities=NativeCapabilities(session_resume=True),
    )
    with (
        patch(
            "src.server.services.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(f"{CREATE_MOD}.status_for", return_value=status),
        patch(
            f"{CREATE_MOD}.metadata_for",
            return_value={"native_runtime": {"provider": "codex"}},
        ),
    ):
        resp, create, _ = await _create(
            {
                "workspace_id": "ws-1",
                "first_query": "native task",
                "native_provider": "codex",
            }
        )
    assert resp.status_code == 201
    assert resp.json()["native_provider"] == "codex"
    assert create.await_args.kwargs["metadata"] == {
        "native_runtime": {"provider": "codex"}
    }


def test_a_named_title_is_capped_like_a_rename():
    with pytest.raises(ValidationError):
        ThreadCreateRequest(title="x" * 256)
    assert ThreadCreateRequest(title="x" * 255).first_query == ""


@pytest.mark.asyncio
async def test_auto_provider_is_resolved_and_decision_is_stored():
    from types import SimpleNamespace

    from src.server.services.native_runtime import NativeRuntimeStatus
    from src.server.services.native_runtime.contracts import NativeCapabilities

    status = NativeRuntimeStatus(
        provider="codex",
        installed=True,
        binary="/usr/bin/codex",
        live_verified=True,
        capabilities=NativeCapabilities(approvals=True),
    )
    decision = SimpleNamespace(
        runtime="codex",
        as_dict=lambda: {
            "runtime": "codex",
            "reason": "best eligible",
            "fallbacks": ["ptc"],
            "confidence": 0.9,
            "scores": {"codex": 1.0},
        },
    )
    with (
        patch(
            "src.server.services.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(f"{CREATE_MOD}.status_for", return_value=status),
        patch(
            f"{CREATE_MOD}.metadata_for",
            return_value={"native_runtime": {"provider": "codex"}},
        ),
        patch(
            "src.server.services.native_runtime.router.aresolve_requested_provider",
            new=AsyncMock(return_value=("codex", decision)),
        ),
    ):
        resp, create, _ = await _create(
            {
                "workspace_id": "ws-1",
                "first_query": "refactor code",
                "native_provider": "auto",
            }
        )
    assert resp.status_code == 201
    assert resp.json()["native_provider"] == "codex"
    assert resp.json()["runtime_decision"]["runtime"] == "codex"
    assert create.await_args.kwargs["metadata"]["runtime_decision"]["runtime"] == "codex"
