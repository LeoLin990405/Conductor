"""HTTP contract for the memory browse-by-concept endpoints.

The browse page reads two owner-scoped endpoints: the concepts the owner has
memory for, and the memories under one of them. What matters at the HTTP edge
is the shape (flag-shaped, so a switched-off projection answers with empty
lists rather than an error), the owner scoping, and that the two ontology
filters reach the service under the names it uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

#: The marker for "this field was not narrowed on". It is not ``None``:
#: ``None`` asks for the rows that name nothing.
from src.server.contracts.memory_concept import UNSET
from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio

OWNER = "test-user-123"


@pytest.fixture
def memory_projection_on(monkeypatch):
    """The memory projection is switched on for this deployment."""
    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")


@pytest.fixture
def memory_projection_off(monkeypatch):
    """The memory projection is switched off for this deployment.

    The flag is read from the environment at call time, so removing it here
    is enough: the endpoints must answer with empty lists, not with an error.
    """
    monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)


@pytest.fixture
def memory_service(monkeypatch):
    """The concept list and the entry list, as the service answers them."""
    from src.server.services.refactor import memory as memory_svc

    concepts = AsyncMock(
        return_value=[
            {"class": "Preference", "about": "person:leo", "entries": 2},
            {"class": None, "about": None, "entries": 1},
        ]
    )
    entries = AsyncMock(
        return_value=[
            {
                "entry_id": "11111111-1111-4111-8111-111111111111",
                "tier": "user",
                "entry_key": "prefs/language",
                "index_text": "prefers Chinese",
                "status": "active",
                "class": "Preference",
                "about": "person:leo",
            }
        ]
    )
    monkeypatch.setattr(memory_svc, "list_memory_concepts", concepts)
    monkeypatch.setattr(memory_svc, "list_memory", entries)
    return SimpleNamespace(concepts=concepts, entries=entries)


@pytest_asyncio.fixture
async def client():
    from src.server.app.memory import router

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(router)), base_url="http://test"
    ) as http:
        yield http


async def test_concepts_list_the_owner_pairs_with_their_counts(
    client, memory_projection_on, memory_service
):
    response = await client.get("/api/v1/memory/concepts")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "concepts": [
            {"class": "Preference", "about": "person:leo", "entries": 2},
            # A memory that names neither is reported, not dropped: the page
            # must never claim there is nothing left to browse.
            {"class": None, "about": None, "entries": 1},
        ],
    }
    memory_service.concepts.assert_awaited_once_with(
        user_id=OWNER, limit=200, offset=0
    )


async def test_the_concepts_continue_past_the_first_page(
    client, memory_projection_on, memory_service
):
    """The pairs beyond the first page are only reachable by asking for them."""
    response = await client.get("/api/v1/memory/concepts?limit=200&offset=200")

    assert response.status_code == 200
    memory_service.concepts.assert_awaited_once_with(
        user_id=OWNER, limit=200, offset=200
    )


async def test_the_entries_continue_past_the_first_page(
    client, memory_projection_on, memory_service
):
    """The memories beyond the first hundred are only reachable by asking."""
    response = await client.get("/api/v1/memory/entries?limit=100&offset=100")

    assert response.status_code == 200
    kwargs = memory_service.entries.await_args.kwargs
    assert kwargs["limit"] == 100
    assert kwargs["offset"] == 100


async def test_a_negative_offset_is_refused(client, memory_projection_on, memory_service):
    """An offset before the start of the listing is a mistake."""
    assert (await client.get("/api/v1/memory/entries?offset=-1")).status_code == 422
    assert (await client.get("/api/v1/memory/concepts?offset=-1")).status_code == 422
    memory_service.entries.assert_not_awaited()
    memory_service.concepts.assert_not_awaited()


async def test_entries_take_the_class_and_the_about_filter(
    client, memory_projection_on, memory_service
):
    response = await client.get(
        "/api/v1/memory/entries?class=Preference&about=person:leo"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["entries"][0]["about"] == "person:leo"
    # The wire says `class`; the service calls the field `memory_class`, and
    # the filter must arrive under that name or the query narrows nothing.
    memory_service.entries.assert_awaited_once_with(
        user_id=OWNER,
        tier=None,
        limit=100,
        offset=0,
        memory_class="Preference",
        about="person:leo",
    )


async def test_entries_without_a_filter_read_every_row(
    client, memory_projection_on, memory_service
):
    await client.get("/api/v1/memory/entries")

    kwargs = memory_service.entries.await_args.kwargs
    # No narrowing at all: the filter is unset, not null. An unset filter and
    # a null filter ask for different things.
    assert kwargs["memory_class"] is UNSET
    assert kwargs["about"] is UNSET
    assert kwargs["tier"] is None


async def test_entries_ask_for_the_unnamed_group_explicitly(
    client, memory_projection_on, memory_service
):
    """The unnamed group is a selection, not the absence of one.

    Selecting it must read the memories that name no class and no concept,
    and not every memory: the two are told apart on the wire.
    """
    response = await client.get(
        "/api/v1/memory/entries?class_is_null=true&about_is_null=true"
    )

    assert response.status_code == 200
    kwargs = memory_service.entries.await_args.kwargs
    assert kwargs["memory_class"] is None
    assert kwargs["about"] is None


async def test_entries_ask_for_the_partly_unnamed_group_explicitly(
    client, memory_projection_on, memory_service
):
    response = await client.get(
        "/api/v1/memory/entries?class=Preference&about_is_null=true"
    )

    assert response.status_code == 200
    kwargs = memory_service.entries.await_args.kwargs
    assert kwargs["memory_class"] == "Preference"
    assert kwargs["about"] is None


async def test_entries_reject_a_value_and_a_null_flag_together(
    client, memory_projection_on, memory_service
):
    """Naming a class and asking for the unnamed class at once is a mistake."""
    response = await client.get(
        "/api/v1/memory/entries?class=Preference&class_is_null=true"
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid memory filter"}
    memory_service.entries.assert_not_awaited()


async def test_entries_reject_an_unknown_tier(client, memory_projection_on, monkeypatch):
    from src.server.contracts.refactor import ContractError
    from src.server.services.refactor import memory as memory_svc

    list_entries = AsyncMock(side_effect=ContractError("unknown memory tier 'global'"))
    monkeypatch.setattr(memory_svc, "list_memory", list_entries)

    response = await client.get("/api/v1/memory/entries?tier=global")

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid memory filter"}


async def test_the_concepts_are_empty_when_the_projection_is_off(
    client, memory_projection_off, memory_service
):
    response = await client.get("/api/v1/memory/concepts")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "concepts": []}
    memory_service.concepts.assert_not_awaited()


async def test_the_entries_are_empty_when_the_projection_is_off(
    client, memory_projection_off, memory_service
):
    response = await client.get("/api/v1/memory/entries?class=Preference")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "entries": []}
    memory_service.entries.assert_not_awaited()
