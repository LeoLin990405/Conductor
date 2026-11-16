"""P0 privacy: a Ticket prompt carries only private-free, confident, relevant memory.

The Ticket prompt is sent to other vendors' models. Before this gate the
memory block carried an entry titled ``[private]``, course memories and other
projects' memories. These tests drive the real selection
(``build_turn_memory_block`` -> ``select_memory_context``) with an in-memory
store, so the gate is checked where the rows actually flow.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import ticket_memory_policy as policy_mod
from src.server.services.autonomy import worker
from src.server.services.refactor import memory_context

USER = "user-a"
WORKSPACE = "ws-conductor"
THREAD = "thread-1"

TICKET = {
    "ticket_id": "ticket-1",
    "user_id": USER,
    "workspace_id": WORKSPACE,
    "worker_thread_id": THREAD,
    "title": "Filter memory injection in Conductor ticket prompts",
    "scope": "Drop private and unrelated memfw entries from the worker memory block",
    "mandate": {"repo": "github.com/leo/conductor"},
}

USER_META = {"origin": "user", "confidence": 0.9, "sensitivity": "public"}


def _row(key: str, text: str, *, meta: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "entry_key": key,
        "index_text": text,
        "content_hash": f"hash-{key}",
        "status": "active",
        "metadata": dict(USER_META if meta is None else meta),
        **extra,
    }


RELEVANT = _row("conductor-memory-filter", "Conductor worker memory block must filter memfw entries")
PRIVATE_TITLE = _row("m-private", "[private] Conductor worker memory block memfw notes")
PRIVATE_TAG = _row(
    "m-tag",
    "Conductor worker memory block memfw tagged",
    meta={**USER_META, "tags": ["personal", "memfw"]},
)
PRIVATE_FIELD = _row(
    "m-vis",
    "Conductor worker memory block memfw visibility",
    meta={**USER_META, "memory_metadata": {"visibility": "private"}},
)
COURSE = _row("ant7511-week7", "ANT7511 course notes week 7 reading log rules")
OTHER_PROJECT = _row(
    "agos-memory",
    "Filter memfw entries in the worker block in agos",
    meta={**USER_META, "project": "agos"},
)
LOW_CONFIDENCE = _row(
    "m-low",
    "Filter memfw entries in the worker block low",
    meta={"origin": "user", "confidence": 0.4, "sensitivity": "public"},
)
INFERENCE = _row(
    "m-infer",
    "Filter memfw entries in the worker block guess",
    meta={"origin": "assistant_inference", "confidence": 0.95, "sensitivity": "public"},
)
SCOPED = _row(
    "ws-fact",
    "Uses the validated base",
    namespace=["owner", "workspaces", WORKSPACE, "memory"],
)

ALL_ROWS = [
    RELEVANT,
    PRIVATE_TITLE,
    PRIVATE_TAG,
    PRIVATE_FIELD,
    COURSE,
    OTHER_PROJECT,
    LOW_CONFIDENCE,
    INFERENCE,
]


def _search(rows_by_tier: dict[str, list[dict[str, Any]]]):
    async def search(user_id, tier, query, limit, thread_id, workspace_id):
        return list(rows_by_tier.get(tier, []))

    return search


async def _ticket_block(rows_by_tier, ticket=TICKET, env: dict[str, str] | None = None) -> str:
    from src.server.services.refactor import memory_runtime_controls

    with (
        patch.dict("os.environ", env or {}, clear=False),
        patch.object(
            memory_runtime_controls, "automatic_memory_enabled", AsyncMock(return_value=True)
        ),
        patch.object(memory_context, "refactor_flag_enabled", return_value=True),
        patch.object(memory_context, "_default_search", _search(rows_by_tier)),
        patch("src.config.settings.get_memory_federation_binding", return_value=None),
        patch(
            "src.server.services.refactor.events.record_event",
            new=AsyncMock(return_value={"event_id": 1}),
        ),
    ):
        return await worker.worker_memory_context(dict(ticket))


@pytest.mark.asyncio
async def test_ticket_prompt_never_carries_private_or_unrelated_entries():
    block = await _ticket_block({"user": ALL_ROWS})

    assert "conductor-memory-filter" in block
    for leaked in (
        "[private]",
        "m-private",
        "m-tag",
        "m-vis",
        "ANT7511",
        "agos",
        "m-low",
        "m-infer",
    ):
        assert leaked not in block, leaked

    prompt = worker.worker_prompt(TICKET, {}, native=True, memory_context=block)
    assert "[private]" not in prompt
    assert "ANT7511" not in prompt
    assert "agos" not in prompt


@pytest.mark.asyncio
async def test_an_entry_scoped_to_the_ticket_workspace_is_kept_without_overlap():
    block = await _ticket_block({"workspace": [SCOPED]})
    assert "ws-fact" in block


@pytest.mark.asyncio
async def test_an_entry_from_another_workspace_namespace_needs_real_relevance():
    foreign = {**SCOPED, "namespace": ["owner", "workspaces", "ws-other", "memory"]}
    assert await _ticket_block({"workspace": [foreign]}) == ""


@pytest.mark.asyncio
async def test_a_ticket_with_no_determinable_relevance_gets_no_memory():
    ticket = {"ticket_id": "t", "user_id": USER, "title": "do it", "scope": "now"}
    assert await _ticket_block({"user": ALL_ROWS + [SCOPED]}, ticket=ticket) == ""


@pytest.mark.asyncio
async def test_an_invalid_policy_value_fails_closed():
    block = await _ticket_block(
        {"user": [RELEVANT]}, env={policy_mod.ENV_MIN_CONFIDENCE: "very"}
    )
    assert block == ""


@pytest.mark.asyncio
async def test_the_operator_can_switch_ticket_memory_off():
    assert await _ticket_block({"user": [RELEVANT]}, env={policy_mod.ENV_ENABLED: "off"}) == ""


@pytest.mark.asyncio
async def test_count_and_byte_caps_bound_the_block():
    rows = [
        _row(f"conductor-memfw-{i}", f"Filter memfw entries in the worker block {i}")
        for i in range(20)
    ]
    block = await _ticket_block({"user": rows}, env={policy_mod.ENV_MAX_ENTRIES: "3"})
    assert len(block.splitlines()) == 3

    block = await _ticket_block({"user": rows}, env={policy_mod.ENV_MAX_BYTES: "200"})
    assert 0 < len(block.encode()) <= 200


def test_default_policy_is_fail_closed():
    policy = policy_mod.TicketMemoryPolicy.from_env({})
    assert policy.enabled
    assert not policy.allow_inference
    assert policy.min_confidence >= 0.5
    assert policy.max_entries > 0 and policy.max_bytes > 0


def test_a_raising_row_check_drops_the_row():
    scope = policy_mod.ticket_scope(TICKET)
    admit = policy_mod.make_row_filter(scope, policy_mod.TicketMemoryPolicy())

    class Broken(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert admit("user", Broken()) is False


def test_reviewers_and_tickets_are_told_not_to_do_memory_lookups():
    from src.tools.auto_dispatch.delivery import _LINE_DELIVERY_CONTRACT
    from src.tools.auto_dispatch.execution import _REVIEW_SYSTEM

    assert "do not search or read Fleet Memory" in _REVIEW_SYSTEM
    assert "memory retrieval" not in _REVIEW_SYSTEM
    # The Ticket and line contracts say the same thing, and they override a
    # repository AGENTS.md copy of the operator's memory rules.
    assert worker._NATIVE_TICKET_CONTRACT == _LINE_DELIVERY_CONTRACT
    assert "repository AGENTS.md" in _LINE_DELIVERY_CONTRACT
    assert "do not write memory or change-memory records" in _LINE_DELIVERY_CONTRACT


def test_remote_memfw_rows_keep_their_privacy_and_project_labels():
    from src.server.services.refactor.memory_federation.remote import _policy_labels

    labels = _policy_labels(
        {"tags": ["private", 3], "visibility": "private", "project": "agos", "confidence": 0.4}
    )
    assert labels == {
        "tags": ["private"],
        "visibility": "private",
        "project": "agos",
        "confidence": 0.4,
    }
    assert policy_mod.row_is_private({"metadata": {"memory_metadata": labels}})


@pytest.mark.parametrize(
    "text",
    [
        "[private] resume notes",
        "［private］ resume notes",
        "［ＰＲＩＶＡＴＥ］ resume notes",
        "<private> resume notes",
        "private: resume notes",
        "Private : resume notes",
        "resume notes #private",
        "私密：简历",
        "【私密】简历",
        "[sensitive] payment debugging",
        "(Confidential) recommendation letter",
        "[pri\u200bvate] resume notes",
        "[\u2060private\u2060] resume notes",
        "pri\u200dvate: resume notes",
        "【個人】履歷",
        "私人：推薦信",
        "[隱私] 付款",
    ],
)
def test_private_markers_are_caught_after_normalization(text):
    assert policy_mod.row_is_private({"entry_key": "k", "index_text": text})


@pytest.mark.parametrize(
    "meta",
    [
        {"private": True},
        {"is_private": "true"},
        {"is_private": 1},
        {"a": {"b": {"c": {"visibility": "PRIVATE"}}}},
        {"nested": [{"sensitivity": "secret"}]},
        {"wrapper": {"privacy": "personal"}},
        {"deep": {"labels": ["#Private"]}},
    ],
)
def test_private_metadata_is_found_at_any_depth(meta):
    assert policy_mod.row_is_private({"entry_key": "k", "index_text": "ok", "metadata": meta})


def test_plain_text_and_public_metadata_are_not_private():
    row = {"entry_key": "k", "index_text": "privately funded parser", "metadata": {"private": False}}
    assert not policy_mod.row_is_private(row)


@pytest.mark.asyncio
async def test_missing_sensitivity_is_private_unless_scoped_to_the_ticket():
    unlabelled = _row(
        "unlabelled",
        "Filter memfw entries in the worker block",
        meta={"origin": "user", "confidence": 0.9},
    )
    assert await _ticket_block({"user": [unlabelled]}) == ""
    # Explicit scope admits it: the Ticket's workspace namespace, or a
    # declared project the Ticket names.
    scoped = {**unlabelled, "namespace": ["owner", "workspaces", WORKSPACE, "memory"]}
    assert "unlabelled" in await _ticket_block({"workspace": [scoped]})
    # A declared project the Ticket names is not enough with sensitivity
    # missing; it needs public too, and then skips the overlap check.
    declared = {**unlabelled, "metadata": {**unlabelled["metadata"], "repo": "leo/conductor"}}
    assert await _ticket_block({"user": [declared]}) == ""
    public = {
        **unlabelled,
        "entry_key": "declared-public",
        "index_text": "Uses squash merges",
        "metadata": {**declared["metadata"], "sensitivity": "public"},
    }
    assert "declared-public" in await _ticket_block({"user": [public]})


@pytest.mark.asyncio
async def test_course_notes_and_personal_kinds_are_dropped():
    rows = [
        _row(f"kind-{kind}", "Filter memfw entries in the worker block", meta={**USER_META, "kind": kind})
        for kind in ("course", "notes", "personal")
    ] + [
        _row("cat", "Filter memfw entries in the worker block", meta={**USER_META, "category": "Course"}),
    ]
    assert await _ticket_block({"user": rows}) == ""


@pytest.mark.asyncio
async def test_generic_project_words_do_not_make_an_entry_relevant():
    generic = _row("generic", "Agent Memory Challenge for conductor dispatch review tests")
    assert await _ticket_block({"user": [generic]}) == ""


@pytest.mark.asyncio
async def test_two_shared_terms_are_not_enough():
    two = _row("two", "memfw block layout notes for gardening")
    assert await _ticket_block({"user": [two]}) == ""


@pytest.mark.asyncio
async def test_the_gate_logs_drop_counts_without_text(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger=policy_mod.__name__):
        await _ticket_block({"user": ALL_ROWS})
    lines = [r.getMessage() for r in caplog.records if "[memory-gate]" in r.getMessage()]
    assert lines and "ticket=ticket-1" in lines[0]
    assert "private=" in lines[0] and "kept=1" in lines[0]
    assert "ANT7511" not in lines[0] and "Filter" not in lines[0]


def test_generic_project_words_are_stopwords():
    assert policy_mod.terms_of("memory conductor agent dispatch ticket line review test") == frozenset()
    assert policy_mod.TicketMemoryPolicy().min_overlap >= 3


def test_worker_turns_get_no_conductor_memory_server():
    from src.server.handlers.chat.native_run import native_capability_servers
    from src.server.services.native_runtime.mcp import NativeMCPServer

    memory = NativeMCPServer(name="conductor_memory", command="uv", args=())
    skills = NativeMCPServer(name="conductor_skills", command="uv", args=())
    worker = native_capability_servers(worker_role=True, memory_server=memory, skills_server=skills)
    router = native_capability_servers(worker_role=False, memory_server=memory, skills_server=skills)
    assert [s["name"] for s in worker] == ["conductor_skills"]
    assert [s["name"] for s in router] == ["conductor_memory", "conductor_skills"]


def test_top_level_row_visibility_is_honoured():
    assert policy_mod.row_is_private({"entry_key": "k", "index_text": "ok", "visibility": "private"})
    assert policy_mod.row_is_private({"entry_key": "k", "index_text": "ok", "tags": ["personal"]})
    assert not policy_mod.row_is_private({"entry_key": "k", "index_text": "ok", "visibility": "public"})
