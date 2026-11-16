"""The Ticket memory audit survives a concept list that is too big for one event.

An event carries at most a bounded payload. A valid ontology can be bigger
than that: a memory may name sixteen concepts, and a concept id may be 256
characters. Two such memories were 17,375 bytes against the 16,384-byte cap,
so the audit was rejected whole and the Ticket kept no record of the concepts
it was given.

The audit is split into as many events as it needs instead, and every concept
id survives the split.
"""

from __future__ import annotations

import pytest

from src.server.contracts.memory_concept import (
    MAX_CONCEPT_VALUES,
    MAX_CONCEPT_VALUE,
)
from src.server.services.refactor import events as svc_events
from src.server.services.refactor.memory_context import (
    ContextEntry,
    MemoryContext,
    record_ticket_memory_audit,
)


def _concept(prefix: str, index: int) -> str:
    """A concept id at the length the ontology allows."""
    return f"{prefix}:{index}".ljust(MAX_CONCEPT_VALUE, "x")


def _big_context() -> MemoryContext:
    """Two memories, each naming the most concepts the ontology allows."""
    entries = []
    for memory in range(2):
        concepts = tuple(
            _concept(f"concept{memory}", index) for index in range(MAX_CONCEPT_VALUES)
        )
        entries.append(
            ContextEntry(
                tier="user",
                entry_key=f"memory-{memory}",
                index_text=f"memory {memory}",
                content_hash=f"hash-{memory}",
                tokens=4,
                rank=memory,
                concept_id=concepts[0],
                concepts=concepts,
            )
        )
    return MemoryContext(query="q", budget_tokens=800, entries=tuple(entries))


@pytest.mark.asyncio
async def test_a_large_concept_list_is_audited_in_several_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit is split, and no concept id is lost in the split."""
    context = _big_context()
    written: list[dict] = []

    async def record_event(**kwargs):
        payload = kwargs["payload"]
        # The writer rejects a payload over the cap, so the split has to keep
        # every chunk under it.
        assert svc_events.event_payload_bytes(payload) <= svc_events.MAX_PAYLOAD_BYTES
        written.append(payload)
        return {"event_id": len(written)}

    monkeypatch.setattr(svc_events, "record_event", record_event)

    result = await record_ticket_memory_audit(
        user_id="u1", ticket_id="ticket-1", context=context
    )

    assert result == {"event_id": 1}
    # The audit does not fit in one event, and it is not dropped either.
    assert len(written) > 1
    assert [payload["chunk"] for payload in written] == list(range(len(written)))
    assert all(payload["chunks"] == len(written) for payload in written)

    audited = [record for payload in written for record in payload["audit"]]
    assert len(audited) == len(context.audit_records())
    assert [record["key"] for record in audited] == ["memory-0", "memory-1"]

    # Every concept id the Ticket was given is in the audit.
    concepts = [concept for payload in written for concept in payload["concepts"]]
    assert concepts == list(context.concept_ids)
    assert len(concepts) == 2 * MAX_CONCEPT_VALUES
    assert all(len(concept) == MAX_CONCEPT_VALUE for concept in concepts)
    audited_concepts = [
        concept for record in audited for concept in record["concepts"]
    ]
    assert sorted(audited_concepts) == sorted(context.concept_ids)


@pytest.mark.asyncio
async def test_the_audit_of_one_small_selection_stays_one_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection that fits is not split: the ordinary case is unchanged."""
    context = MemoryContext(
        query="q",
        budget_tokens=800,
        entries=(
            ContextEntry(
                tier="user",
                entry_key="prefs/language",
                index_text="prefers Chinese",
                content_hash="h1",
                tokens=4,
                rank=0,
                concept_id="person:leo",
            ),
        ),
    )
    written: list[dict] = []

    async def record_event(**kwargs):
        written.append(kwargs["payload"])
        return {"event_id": 1}

    monkeypatch.setattr(svc_events, "record_event", record_event)

    await record_ticket_memory_audit(
        user_id="u1", ticket_id="ticket-1", context=context
    )

    assert len(written) == 1
    payload = written[0]
    assert payload["concepts"] == ["person:leo"]
    assert payload["audit"][0]["concept_id"] == "person:leo"
    assert payload["entries"] == 1
    assert payload["chunk"] == 0
    assert payload["chunks"] == 1


@pytest.mark.asyncio
async def test_a_failing_chunk_does_not_cost_the_whole_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One chunk failing to write costs that chunk, not the audit.

    The audit is several events; a write that fails is logged and the rest of
    the audit is still written, so a transient store error cannot lose the
    whole record of what the Ticket knew.
    """
    context = _big_context()
    written: list[dict] = []
    attempts = 0

    async def record_event(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("db is busy")
        written.append(kwargs["payload"])
        return {"event_id": len(written)}

    monkeypatch.setattr(svc_events, "record_event", record_event)

    result = await record_ticket_memory_audit(
        user_id="u1", ticket_id="ticket-1", context=context
    )

    assert result == {"event_id": 1}
    # The rest of the audit is written even though the first chunk failed.
    assert written
    concepts = [concept for payload in written for concept in payload["concepts"]]
    assert set(concepts) <= set(context.concept_ids)


@pytest.mark.asyncio
async def test_the_event_writer_still_rejects_an_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap itself is unchanged: the split is the producer's job."""
    from src.server.contracts.refactor import ContractError

    monkeypatch.setenv("REFACTOR_EVENTS_ENABLED", "1")
    with pytest.raises(ContractError):
        await svc_events.record_event(
            user_id="u1",
            event_type="memory.injected",
            # Redaction bounds one string, so it takes many of them to fill a
            # payload -- which is exactly what the cap is there to stop.
            payload={"blob": ["x" * 500] * 40},
        )


@pytest.mark.asyncio
async def test_the_payload_size_is_measured_after_redaction() -> None:
    """The producer measures the payload the writer will actually store."""
    payload = {"token": "secret", "text": "x" * 100}
    size = svc_events.event_payload_bytes(payload)
    redacted = svc_events.redact_event_payload(payload)
    assert size == svc_events.event_payload_bytes(redacted)
    assert redacted["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_the_chunks_of_one_audit_share_an_audit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two launches of one Ticket write two audits a reader can tell apart."""
    written: list[dict] = []

    async def record_event(**kwargs):
        written.append(kwargs["payload"])
        return {"event_id": len(written)}

    monkeypatch.setattr(svc_events, "record_event", record_event)

    await record_ticket_memory_audit(user_id="u1", ticket_id="t", context=_big_context())
    first = {payload["audit_id"] for payload in written}
    written.clear()
    await record_ticket_memory_audit(user_id="u1", ticket_id="t", context=_big_context())
    second = {payload["audit_id"] for payload in written}

    assert len(first) == 1 and len(second) == 1
    assert first != second and "" not in first


@pytest.mark.asyncio
async def test_a_record_with_oversized_provenance_keeps_its_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unbounded provider provenance must not cost the record its chunk."""
    entry = ContextEntry(
        tier="user",
        entry_key="huge",
        index_text="huge",
        content_hash="h",
        tokens=1,
        rank=0,
        concept_id="project:conductor",
        concepts=("project:conductor",),
        provenance={f"field{index}": f"value {index} " * 20 for index in range(400)},
    )
    context = MemoryContext(query="q", budget_tokens=10, entries=(entry,))
    written: list[dict] = []

    async def record_event(**kwargs):
        payload = kwargs["payload"]
        assert svc_events.event_payload_bytes(payload) <= svc_events.MAX_PAYLOAD_BYTES
        written.append(payload)
        return {"event_id": 1}

    monkeypatch.setattr(svc_events, "record_event", record_event)

    assert await record_ticket_memory_audit(user_id="u1", ticket_id="t", context=context)
    records = [record for payload in written for record in payload["audit"]]
    assert [record["key"] for record in records] == ["huge"]
    assert records[0]["concepts"] == ["project:conductor"]
    assert records[0]["provenance"] == {"omitted": "too_large"}
