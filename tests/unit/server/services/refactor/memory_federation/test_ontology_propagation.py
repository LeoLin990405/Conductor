"""The ontology of a federated memory survives the trip to the Ticket audit.

A memory names what kind of claim it is (``class``) and the concepts it is
about (``about``). A federated source declares that ontology on its own
memories, so the adapters have to carry it: a memory that names a concept
must still name it when the Worker reads it, and the Ticket audit has to
report it. An adapter that drops the ontology makes an annotated memory look
unnamed, and the audit then answers "what did this Ticket know about?" with
nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.services.refactor.memory_federation.integration import (
    _as_context_rows,
)
from src.server.services.refactor.memory_federation.models import MemoryHit


def _annotated_hit() -> MemoryHit:
    """A provider hit as the MemFW adapters build it."""
    return MemoryHit(
        source="memfw",
        key="memfw-pref",
        text="MemFW preference: prefers Chinese",
        score=1.0,
        tier="user",
        provenance={
            "memory_id": "memfw-pref",
            "layer": "current",
            "document_hash": "sha256:document",
            "memory_metadata": {
                "layer": "current",
                "tier": "hot",
                "class": "Preference",
                "about": ["project:conductor", "person:leo"],
            },
        },
    )


@pytest.mark.asyncio
async def test_the_ontology_of_a_federated_memory_reaches_the_ticket_audit(
    monkeypatch: pytest.MonkeyPatch,
):
    """The provider's own annotation is what the audit has to report."""
    from src.server.services.refactor import events as svc_events
    from src.server.services.refactor.memory_context import (
        record_ticket_memory_audit,
        select_memory_context,
    )

    rows = _as_context_rows([_annotated_hit()])

    async def search(
        user_id, tier, query, limit, thread_id=None, workspace_id=None
    ):
        return rows if tier == "user" else []

    context = await select_memory_context(
        user_id="u1", query="preference", budget_tokens=400, search=search
    )
    assert context.entries[0].memory_class == "Preference"
    assert context.entries[0].concepts == ("project:conductor", "person:leo")

    audit = AsyncMock(return_value={"event_id": 1})
    monkeypatch.setattr(svc_events, "record_event", audit)
    written = await record_ticket_memory_audit(
        user_id="u1", ticket_id="ticket-1", context=context
    )

    assert written == {"event_id": 1}
    payload = audit.await_args.kwargs["payload"]
    assert payload["concepts"] == ["project:conductor", "person:leo"]
    record = payload["audit"][0]
    assert record["concept_id"] == "project:conductor"
    assert record["concepts"] == ["project:conductor", "person:leo"]


@pytest.mark.asyncio
async def test_a_federated_memory_without_an_ontology_names_nothing():
    """The unnamed case stays unnamed: the audit must not invent a concept."""
    from src.server.services.refactor.memory_context import select_memory_context

    hit = MemoryHit(
        source="memfw",
        key="memfw-plain",
        text="MemFW note: no ontology",
        score=1.0,
        tier="user",
        provenance={"memory_metadata": {"layer": "current", "tier": "hot"}},
    )
    rows = _as_context_rows([hit])

    async def search(
        user_id, tier, query, limit, thread_id=None, workspace_id=None
    ):
        return rows if tier == "user" else []

    context = await select_memory_context(
        user_id="u1", query="note", budget_tokens=400, search=search
    )
    assert context.entries[0].concepts == ()
    assert context.concept_ids == ()
