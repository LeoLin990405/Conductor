from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.services.refactor import memory_sources

USER = "00000000-0000-0000-0000-000000000001"
CURRENT_ID = "00000000-0000-0000-0000-000000000002"
PREVIOUS_ID = "00000000-0000-0000-0000-000000000003"
NAMESPACE = (USER, "memory")


class FakeStore:
    def __init__(self, values):
        self.values = {key: {"content": value} for key, value in values.items()}
        self.fail_once_key = None

    async def aget(self, namespace, key):
        assert namespace == NAMESPACE
        value = self.values.get(key)
        return SimpleNamespace(value=value) if value is not None else None

    async def aput(self, namespace, key, value):
        assert namespace == NAMESPACE
        if self.fail_once_key == key:
            self.fail_once_key = None
            raise RuntimeError("injected Store write failure")
        self.values[key] = dict(value)

    async def adelete(self, namespace, key):
        assert namespace == NAMESPACE
        self.values.pop(key, None)


@pytest.fixture(autouse=True)
def _projection_snapshot_db(monkeypatch):
    """Keep the revert's projection snapshot/restore off the database."""
    from unittest.mock import AsyncMock

    from src.server.database.refactor import memory as db_memory
    from src.server.database.refactor import memory_tool_policy as db_policy

    monkeypatch.setattr(db_memory, "get_projection_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        db_memory, "restore_projection_rows", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        db_policy, "get_denials_snapshot", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        db_policy, "restore_denials_snapshot", AsyncMock(return_value=None)
    )
    yield db_memory


@pytest.fixture
def revisions(monkeypatch):
    current = {
        "entry_id": CURRENT_ID,
        "user_id": USER,
        "tier": "user",
        "namespace": list(NAMESPACE),
        "entry_key": "current.md",
        "status": "active",
        "index_text": "current hook",
        "metadata": {"lifecycle": "active"},
        "source_entry_id": PREVIOUS_ID,
    }
    previous = {
        "entry_id": PREVIOUS_ID,
        "user_id": USER,
        "tier": "user",
        "namespace": list(NAMESPACE),
        "entry_key": "previous.md",
        "status": "superseded",
        "index_text": "previous hook",
        "metadata": {"lifecycle": "wrong"},
        "source_entry_id": None,
    }

    async def get_revision(*, user_id, entry_id):
        assert user_id == USER
        return {CURRENT_ID: current, PREVIOUS_ID: previous}.get(entry_id)

    @asynccontextmanager
    async def lock(user_id):
        assert user_id == USER
        yield

    monkeypatch.setattr(memory_sources.memory_revisions, "get_revision", get_revision)
    monkeypatch.setattr(memory_sources.memory_revisions, "owner_memory_lock", lock)
    return current, previous


@pytest.mark.asyncio
async def test_source_lookup_reports_unavailable_without_exact_verified_offsets(
    monkeypatch, revisions
):
    current, _ = revisions
    current["source_thread_id"] = "00000000-0000-0000-0000-000000000010"
    current["source_response_id"] = "00000000-0000-0000-0000-000000000011"

    result = await memory_sources.lookup_memory_source(user_id=USER, entry_id=CURRENT_ID)

    assert result == {
        "status": "unavailable",
        "entry_id": CURRENT_ID,
        "reason": "exact_message_offset_reference_unavailable",
    }


@pytest.mark.asyncio
async def test_source_lookup_validates_owner_and_returns_exact_message_slice(
    monkeypatch, revisions
):
    current, _ = revisions
    thread_id = "00000000-0000-0000-0000-000000000010"
    response_id = "00000000-0000-0000-0000-000000000011"
    current["source_thread_id"] = thread_id
    current["source_response_id"] = None
    current["metadata"]["source_response_id"] = response_id
    current["metadata"]["evidence_quote_ref"] = {
        "message_id": "msg-user-1",
        "start": 1,
        "end": 4,
        "unit": "unicode_codepoint",
    }
    monkeypatch.setattr(
        "src.server.database.conversation.get_thread_auth_meta",
        lambda _thread: _async_value({"user_id": USER}),
    )
    monkeypatch.setattr(
        memory_sources.memory_revisions,
        "get_source_turn_slice",
        lambda **_kwargs: _async_value(
            {"thread_id": thread_id, "response_id": response_id, "slice_codec": "fake", "slice": b"slice"}
        ),
    )
    messages = [
        SimpleNamespace(id="msg-user-1", type="human", content="我😀在上海"),
        SimpleNamespace(id="assistant-1", type="ai", content="Understood."),
    ]
    monkeypatch.setattr(
        "src.server.services.history.slices.decode",
        lambda *_args: SimpleNamespace(messages=messages),
    )
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: SimpleNamespace(serde=object()),
    )

    result = await memory_sources.lookup_memory_source(user_id=USER, entry_id=CURRENT_ID)

    assert result == {
        "status": "available",
        "entry_id": CURRENT_ID,
        "source": {
            "thread_id": thread_id,
            "response_id": response_id,
            "message_id": "msg-user-1",
            "role": "human",
            "origin": "assistant_inference",
            "supports_user_evidence": False,
            "start": 1,
            "end": 4,
            "quote": "😀在上",
            "unit": "unicode_codepoint",
        },
    }


@pytest.mark.asyncio
async def test_source_lookup_does_not_quote_wrong_message_or_role(monkeypatch, revisions):
    current, _ = revisions
    thread_id = "00000000-0000-0000-0000-000000000010"
    response_id = "00000000-0000-0000-0000-000000000011"
    current["source_thread_id"] = thread_id
    current["source_response_id"] = response_id
    current["metadata"]["evidence_quote_ref"] = {
        "message_id": "not-in-turn",
        "start": 0,
        "end": 5,
        "unit": "unicode_codepoint",
    }
    monkeypatch.setattr(
        "src.server.database.conversation.get_thread_auth_meta",
        lambda _thread: _async_value({"user_id": USER}),
    )
    monkeypatch.setattr(
        memory_sources.memory_revisions,
        "get_source_turn_slice",
        lambda **_kwargs: _async_value(
            {"thread_id": thread_id, "response_id": response_id, "slice_codec": "fake", "slice": b"slice"}
        ),
    )
    monkeypatch.setattr(
        "src.server.services.history.slices.decode",
        lambda *_args: SimpleNamespace(
            messages=[SimpleNamespace(id="assistant-1", type="ai", content="Paris")]
        ),
    )
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: SimpleNamespace(serde=object()),
    )

    result = await memory_sources.lookup_memory_source(user_id=USER, entry_id=CURRENT_ID)

    assert result["status"] == "unavailable"
    assert "source" not in result


@pytest.mark.asyncio
async def test_source_lookup_includes_role_without_calling_ai_text_user_evidence(
    monkeypatch, revisions
):
    current, _ = revisions
    thread_id = "00000000-0000-0000-0000-000000000010"
    response_id = "00000000-0000-0000-0000-000000000011"
    current["source_thread_id"] = thread_id
    current["source_response_id"] = response_id
    current["metadata"].update(
        {
            "origin": "user",
            "evidence_quote_ref": {
                "message_id": "assistant-1",
                "start": 0,
                "end": 5,
                "unit": "unicode_codepoint",
            },
        }
    )
    monkeypatch.setattr(
        "src.server.database.conversation.get_thread_auth_meta",
        lambda _thread: _async_value({"user_id": USER}),
    )
    monkeypatch.setattr(
        memory_sources.memory_revisions,
        "get_source_turn_slice",
        lambda **_kwargs: _async_value(
            {"thread_id": thread_id, "response_id": response_id, "slice_codec": "fake", "slice": b"slice"}
        ),
    )
    monkeypatch.setattr(
        "src.server.services.history.slices.decode",
        lambda *_args: SimpleNamespace(
            messages=[SimpleNamespace(id="assistant-1", type="ai", content="Paris")]
        ),
    )
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: SimpleNamespace(serde=object()),
    )

    result = await memory_sources.lookup_memory_source(user_id=USER, entry_id=CURRENT_ID)

    assert result["status"] == "available"
    assert result["source"]["role"] == "ai"
    assert result["source"]["origin"] == "user"
    assert result["source"]["supports_user_evidence"] is False


@pytest.mark.asyncio
async def test_source_lookup_resolves_task_run_to_distinct_parent_response(
    monkeypatch, revisions
):
    current, _ = revisions
    thread_id = "00000000-0000-0000-0000-000000000010"
    response_id = "00000000-0000-0000-0000-000000000011"
    task_run_id = "00000000-0000-0000-0000-000000000012"
    current["source_thread_id"] = thread_id
    current["source_response_id"] = None
    current["metadata"].update(
        {
            "source_task_run_id": task_run_id,
            "evidence_quote_ref": {
                "message_id": "msg-user-1",
                "start": 0,
                "end": 5,
                "unit": "unicode_codepoint",
            },
        }
    )
    monkeypatch.setattr(
        "src.server.database.conversation.get_thread_auth_meta",
        lambda _thread: _async_value({"user_id": USER}),
    )

    async def parent_response(*, thread_id: str, task_run_id: str):
        assert thread_id == thread_id_value
        assert task_run_id == task_run_value
        return response_value

    thread_id_value = thread_id
    task_run_value = task_run_id
    response_value = response_id
    monkeypatch.setattr(
        memory_sources.memory_revisions, "get_parent_response_id", parent_response
    )

    async def source_slice(*, thread_id: str, response_id: str):
        assert response_id != task_run_id
        return {
            "thread_id": thread_id,
            "response_id": response_id,
            "slice_codec": "fake",
            "slice": b"slice",
        }

    monkeypatch.setattr(
        memory_sources.memory_revisions, "get_source_turn_slice", source_slice
    )
    monkeypatch.setattr(
        "src.server.services.history.slices.decode",
        lambda *_args: SimpleNamespace(
            messages=[SimpleNamespace(id="msg-user-1", type="human", content="Paris here")]
        ),
    )
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: SimpleNamespace(serde=object()),
    )

    result = await memory_sources.lookup_memory_source(user_id=USER, entry_id=CURRENT_ID)

    assert result["status"] == "available"
    assert result["source"]["response_id"] == response_id


@pytest.mark.parametrize("case", ["wrong_owner", "wrong_response", "bad_offsets", "bad_decode"])
@pytest.mark.asyncio
async def test_source_lookup_fails_closed_on_invalid_source_boundary(
    monkeypatch, revisions, case
):
    current, _ = revisions
    thread_id = "00000000-0000-0000-0000-000000000010"
    response_id = "00000000-0000-0000-0000-000000000011"
    current["source_thread_id"] = thread_id
    current["source_response_id"] = response_id
    current["metadata"]["evidence_quote_ref"] = {
        "message_id": "msg-user-1",
        "start": 0,
        "end": 999,
        "unit": "unicode_codepoint",
    }
    auth_user = "00000000-0000-0000-0000-000000000099" if case == "wrong_owner" else USER
    monkeypatch.setattr(
        "src.server.database.conversation.get_thread_auth_meta",
        lambda _thread: _async_value({"user_id": auth_user}),
    )
    source_response = "00000000-0000-0000-0000-000000000098" if case == "wrong_response" else response_id
    monkeypatch.setattr(
        memory_sources.memory_revisions,
        "get_source_turn_slice",
        lambda **_kwargs: _async_value(
            {"thread_id": thread_id, "response_id": source_response, "slice_codec": "fake", "slice": b"slice"}
        ),
    )
    if case == "bad_decode":
        def decode(*_args):
            raise ValueError("bad slice")
    else:
        def decode(*_args):
            return SimpleNamespace(
                messages=[SimpleNamespace(id="msg-user-1", type="human", content="short")]
            )
    monkeypatch.setattr("src.server.services.history.slices.decode", decode)
    monkeypatch.setattr(
        "src.server.services.history.reader.CheckpointHistoryReader.get_instance",
        lambda: SimpleNamespace(serde=object()),
    )

    result = await memory_sources.lookup_memory_source(user_id=USER, entry_id=CURRENT_ID)

    assert result["status"] == "unavailable"
    assert "source" not in result


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_revert_preserves_predecessor_body_and_creates_forward_revision(
    monkeypatch, revisions
):
    current, _ = revisions
    predecessor_body = "\r\n  - indented detail  \r\n\r\n"
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\r\nlifecycle: wrong\r\nreason: original correction\r\n---\r\n" + predecessor_body,
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])
    projected = []

    async def project_memory_write(**kwargs):
        projected.append(kwargs["key"])
        return {"entry_key": kwargs["key"]}

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        project_memory_write,
    )
    policy_sync = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        policy_sync,
    )

    result = await memory_sources.revert_memory_revision(
        user_id=USER, entry_id=CURRENT_ID, store=store, reason="Restore the previous wording"
    )

    assert result["status"] == "complete"
    new_key = result["new_entry_key"]
    new_content = store.values[new_key]["content"]
    header, body = memory_sources._frontmatter(new_content)
    assert body == predecessor_body
    assert header["lifecycle"] == "active"
    assert header["reason"] == "original correction"
    assert header["restore_reason"] == "Restore the previous wording"
    assert header["source_entry_id"] == CURRENT_ID
    assert store.values["previous.md"]["content"].endswith(predecessor_body)
    assert "lifecycle: superseded" in store.values["current.md"]["content"]
    assert f"]({new_key})" in store.values["memory.md"]["content"]
    assert "](current.md)" not in store.values["memory.md"]["content"]
    assert projected == [new_key, "current.md", "memory.md"]
    assert current["source_entry_id"] == PREVIOUS_ID
    assert policy_sync.await_args.kwargs == {
        "user_id": USER,
        "restored_entry_key": new_key,
        "restored_from_entry_id": PREVIOUS_ID,
        "replaced_entry_id": CURRENT_ID,
    }


@pytest.mark.asyncio
async def test_revert_projection_failure_fails_closed_and_retry_completes(
    monkeypatch, revisions
):
    """Durable lineage is the authorization record forget walks, so a revert
    that cannot project its revision must not report success."""
    current, _previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: wrong\n---\nOriginal body\n",
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])

    async def failing_projection(**kwargs):
        raise RuntimeError("projection backend down")

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        failing_projection,
    )
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        AsyncMock(return_value=False),
    )

    with pytest.raises(memory_sources.MemoryRevisionError, match="projection_failed"):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    # A failed revert rolls its Store materialization back: no restored detail
    # and the original current revision/index remain untouched.
    assert not any("--restore-" in key for key in store.values)
    assert "lifecycle: active" in store.values["current.md"]["content"]
    assert store.values["memory.md"]["content"] == "- [Current](current.md) — current hook\n"

    # The deterministic revision key makes the retry idempotent.
    async def working_projection(**kwargs):
        return {"entry_key": kwargs["key"]}

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        working_projection,
    )
    result = await memory_sources.revert_memory_revision(
        user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
    )
    assert result["status"] == "complete"


@pytest.mark.asyncio
async def test_revert_partial_projection_failure_rolls_projection_back(
    monkeypatch, revisions, _projection_snapshot_db
):
    """A projection that committed before a later one failed must be undone."""
    current, _previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: wrong\n---\nOriginal body\n",
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])
    calls = []

    async def partial_projection(**kwargs):
        calls.append((kwargs["key"], kwargs.get("value")))
        if kwargs["key"] == "memory.md":
            raise RuntimeError("projection backend down")
        return {"entry_key": kwargs["key"]}

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        partial_projection,
    )
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        AsyncMock(return_value=False),
    )

    with pytest.raises(memory_sources.MemoryRevisionError, match="projection_failed"):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    # The index projection fails after the revision/current projections
    # committed; the rollback restores the projection snapshot and Store.
    assert not any("--restore-" in key for key in store.values)
    _projection_snapshot_db.restore_projection_rows.assert_awaited_once()
    kwargs = _projection_snapshot_db.restore_projection_rows.await_args.kwargs
    assert kwargs["rows"] == []
    assert any(key.startswith("previous--restore-") for key in kwargs["entry_keys"])
    assert "current.md" in kwargs["entry_keys"]

@pytest.mark.asyncio
async def test_revert_policy_sync_failure_rolls_activation_back(
    monkeypatch, revisions, _projection_snapshot_db
):
    """A failed authority copy must not leave the restored revision active."""
    current, _previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: wrong\n---\nOriginal body\n",
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])

    async def working_projection(**kwargs):
        return {"entry_key": kwargs["key"]}

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        working_projection,
    )
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        AsyncMock(side_effect=RuntimeError("policy copy failed")),
    )

    with pytest.raises(memory_sources.MemoryRevisionError, match="tool_policy_sync_failed"):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert not any("--restore-" in key for key in store.values)
    assert "lifecycle: active" in store.values["current.md"]["content"]
    assert store.values["memory.md"]["content"] == "- [Current](current.md) — current hook\n"
    _projection_snapshot_db.restore_projection_rows.assert_awaited()


@pytest.mark.asyncio
async def test_revert_cancellation_during_policy_sync_rolls_back(
    monkeypatch, revisions, _projection_snapshot_db
):
    """Request cancellation during the authority copy must not leave the
    restored revision active without its enforcement."""
    current, _previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: wrong\n---\nOriginal body\n",
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])

    async def working_projection(**kwargs):
        return {"entry_key": kwargs["key"]}

    async def cancelled_policy(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        working_projection,
    )
    from src.server.database.refactor import memory_tool_policy as db_policy

    committed = [{"entry_id": CURRENT_ID, "active": True}]
    db_policy.get_denials_snapshot.return_value = committed
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        cancelled_policy,
    )

    with pytest.raises(asyncio.CancelledError):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert not any("--restore-" in key for key in store.values)
    assert "lifecycle: active" in store.values["current.md"]["content"]
    _projection_snapshot_db.restore_projection_rows.assert_awaited()
    db_policy.restore_denials_snapshot.assert_awaited()
    assert db_policy.restore_denials_snapshot.await_args.kwargs["rows"] == committed
    assert CURRENT_ID in db_policy.restore_denials_snapshot.await_args.kwargs["entry_ids"]


@pytest.mark.asyncio
async def test_revert_retry_finishes_after_index_switch_and_is_idempotent(
    monkeypatch, revisions
):
    current, previous = revisions
    predecessor_body = "Original body\n"
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: superseded\n---\n" + predecessor_body,
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    previous["content_hash"] = content_hash(store.values["previous.md"]["content"])
    store.fail_once_key = "current.md"

    with pytest.raises(RuntimeError, match="injected Store write failure"):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    # Compensation restores the pre-revert Store; the retry starts clean.
    assert not any("--restore-" in key for key in store.values)
    assert store.values["memory.md"]["content"] == "- [Current](current.md) — current hook\n"
    assert "lifecycle: active" in store.values["current.md"]["content"]

    projected = []

    async def project_memory_write(**kwargs):
        projected.append(kwargs["key"])
        return {"entry_key": kwargs["key"]}

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        project_memory_write,
    )
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        AsyncMock(return_value=False),
    )
    result = await memory_sources.revert_memory_revision(
        user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
    )

    new_key = result["new_entry_key"]
    assert "lifecycle: active" in store.values[new_key]["content"]
    assert "lifecycle: superseded" in store.values["current.md"]["content"]
    assert projected == [new_key, "current.md", "memory.md"]
    retry_result = await memory_sources.revert_memory_revision(
        user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
    )
    assert retry_result["new_entry_key"] == new_key
    assert store.values["memory.md"]["content"].count(f"]({new_key})") == 1


@pytest.mark.asyncio
async def test_revert_rejects_pending_predecessor(monkeypatch, revisions):
    _, previous = revisions
    previous["metadata"] = {"lifecycle": "pending"}
    store = FakeStore({})

    with pytest.raises(memory_sources.MemoryRevisionError, match="predecessor_not_restorable"):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert store.values == {}


@pytest.mark.asyncio
async def test_revert_refuses_expired_predecessor(monkeypatch, revisions):
    _, previous = revisions
    previous["metadata"] = {
        "lifecycle": "superseded",
        "valid_until": "2000-01-01T00:00:00Z",
    }
    store = FakeStore({})

    with pytest.raises(
        memory_sources.MemoryRevisionError,
        match="predecessor_outside_validity_window",
    ):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert store.values == {}


@pytest.mark.asyncio
async def test_revert_refuses_store_content_that_differs_from_projection(
    monkeypatch, revisions
):
    current, previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nchanged after projection\n",
            "previous.md": "---\nlifecycle: superseded\n---\nOriginal body\n",
        }
    )
    store_values_before = {key: dict(value) for key, value in store.values.items()}
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash("---\nlifecycle: active\n---\noriginal projected body\n")
    previous["content_hash"] = content_hash(store.values["previous.md"]["content"])

    with pytest.raises(memory_sources.MemoryRevisionError, match="store_projection_mismatch"):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert store.values == store_values_before


@pytest.mark.asyncio
async def test_revert_rejects_entry_outside_owner_namespace(monkeypatch, revisions):
    current, _ = revisions
    current["namespace"] = [USER, "workspaces", "other", "memory"]
    store = FakeStore({})

    with pytest.raises(
        memory_sources.MemoryRevisionError, match="entry_outside_user_namespace"
    ):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert store.values == {}


@pytest.mark.asyncio
async def test_revert_cancellation_during_projection_rolls_back(
    monkeypatch, revisions, _projection_snapshot_db
):
    current, _previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: wrong\n---\nOriginal body\n",
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])

    async def cancelling_projection(**kwargs):
        if kwargs["key"] == "memory.md":
            raise asyncio.CancelledError()
        return {"entry_key": kwargs["key"]}

    monkeypatch.setattr(
        "src.server.services.refactor.memory_dual_write.project_memory_write",
        cancelling_projection,
    )
    monkeypatch.setattr(
        "src.server.services.refactor.memory_tool_policy_approval.sync_reverted_memory_policy",
        AsyncMock(return_value=False),
    )

    with pytest.raises(asyncio.CancelledError):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert not any("--restore-" in key for key in store.values)
    assert "lifecycle: active" in store.values["current.md"]["content"]
    _projection_snapshot_db.restore_projection_rows.assert_awaited()


@pytest.mark.asyncio
async def test_revert_cancellation_during_store_writes_rolls_back(
    monkeypatch, revisions, _projection_snapshot_db
):
    current, _previous = revisions
    store = FakeStore(
        {
            "memory.md": "- [Current](current.md) — current hook\n",
            "current.md": "---\nlifecycle: active\n---\nCurrent body\n",
            "previous.md": "---\nlifecycle: wrong\n---\nOriginal body\n",
        }
    )
    from src.server.services.refactor.memory_projection import content_hash

    current["content_hash"] = content_hash(store.values["current.md"]["content"])
    revisions[1]["content_hash"] = content_hash(store.values["previous.md"]["content"])
    original_put = store.aput

    cancelled_once = False

    async def cancelling_put(namespace_arg, key, value):
        nonlocal cancelled_once
        if key == "memory.md" and not cancelled_once:
            cancelled_once = True
            raise asyncio.CancelledError()
        return await original_put(namespace_arg, key, value)

    monkeypatch.setattr(store, "aput", cancelling_put)

    with pytest.raises(asyncio.CancelledError):
        await memory_sources.revert_memory_revision(
            user_id=USER, entry_id=CURRENT_ID, store=store, reason="undo"
        )

    assert not any("--restore-" in key for key in store.values)
    assert "lifecycle: active" in store.values["current.md"]["content"]
    assert store.values["memory.md"]["content"] == "- [Current](current.md) — current hook\n"
    _projection_snapshot_db.restore_projection_rows.assert_awaited()
