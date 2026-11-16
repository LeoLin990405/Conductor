from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.conductor_cli.browser.references import ReferenceRegistry, StaleReferenceError


class FakeFrame:
    pass


def _row(frame_id: str, generation: int, node_id: str, handle: AsyncMock, *, text: str | None = None) -> dict:
    return {
        "frame_id": frame_id,
        "document_generation": generation,
        "node_id": node_id,
        "handle": handle,
        "summary": {"role": "button", "text": text or node_id},
    }


@pytest.mark.asyncio
async def test_reference_stays_stable_across_snapshots_of_same_node_and_document() -> None:
    registry = ReferenceRegistry()
    frame_id = registry.frame_id(FakeFrame())
    generation = registry.document_generation(frame_id, "document-token-1")
    handle = AsyncMock()
    handle.evaluate.return_value = True
    row = _row(frame_id, generation, "node-7", handle)

    first = (await registry.replace_snapshot([row]))[0]["ref"]
    second = (await registry.replace_snapshot([row]))[0]["ref"]

    assert second == first
    assert await registry.resolve(first) is handle
    handle.dispose.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_from_omitted_node_is_stale_after_latest_snapshot_replacement() -> None:
    registry = ReferenceRegistry()
    frame_id = registry.frame_id(FakeFrame())
    generation = registry.document_generation(frame_id, "document-token-1")
    handle = AsyncMock()
    handle.evaluate.return_value = True
    ref = (await registry.replace_snapshot([_row(frame_id, generation, "node-7", handle)]))[0]["ref"]

    await registry.replace_snapshot([])

    with pytest.raises(StaleReferenceError):
        await registry.resolve(ref)
    handle.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_document_replacement_invalidates_old_reference_even_for_same_node_id() -> None:
    registry = ReferenceRegistry()
    frame_id = registry.frame_id(FakeFrame())
    first_generation = registry.document_generation(frame_id, "document-token-1")
    handle = AsyncMock()
    handle.evaluate.return_value = True
    first_ref = (await registry.replace_snapshot([_row(frame_id, first_generation, "node-7", handle)]))[0]["ref"]

    second_generation = registry.document_generation(frame_id, "document-token-2")
    second_ref = (await registry.replace_snapshot([_row(frame_id, second_generation, "node-7", handle)]))[0]["ref"]

    assert first_ref != second_ref
    with pytest.raises(StaleReferenceError):
        await registry.resolve(first_ref)


@pytest.mark.asyncio
async def test_frame_ids_are_stable_per_frame_and_references_cannot_cross_frames() -> None:
    registry = ReferenceRegistry()
    top_frame, child_frame = FakeFrame(), FakeFrame()
    top_id = registry.frame_id(top_frame)
    child_id = registry.frame_id(child_frame)
    assert registry.frame_id(top_frame) == top_id
    assert child_id != top_id

    top_gen = registry.document_generation(top_id, "top-doc")
    child_gen = registry.document_generation(child_id, "child-doc")
    top_handle, child_handle = AsyncMock(), AsyncMock()
    top_handle.evaluate.return_value = child_handle.evaluate.return_value = True
    top_ref = f"d{top_gen}.{top_id}.shared-node"
    child_ref = f"d{child_gen}.{child_id}.shared-node"

    await registry.replace_snapshot(
        [
            _row(top_id, top_gen, "shared-node", top_handle),
            _row(child_id, child_gen, "shared-node", child_handle),
        ]
    )

    assert top_ref != child_ref
    assert await registry.resolve(top_ref) is top_handle
    assert await registry.resolve(child_ref) is child_handle


@pytest.mark.asyncio
async def test_disconnected_node_is_removed_and_cannot_be_resolved_again() -> None:
    registry = ReferenceRegistry()
    frame_id = registry.frame_id(FakeFrame())
    generation = registry.document_generation(frame_id, "document-token-1")
    handle = AsyncMock()
    handle.evaluate.return_value = False
    ref = (await registry.replace_snapshot([_row(frame_id, generation, "node-7", handle)]))[0]["ref"]

    with pytest.raises(StaleReferenceError):
        await registry.resolve(ref)
    handle.evaluate.return_value = True
    with pytest.raises(StaleReferenceError):
        await registry.resolve(ref)
