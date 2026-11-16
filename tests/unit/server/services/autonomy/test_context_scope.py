"""Explicit context scopes: policy validation, membership and read filters."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import context_scope

_MOD = "src.server.services.autonomy.context_scope"


def _scope(kind: str = "shared", **over) -> dict:
    base = {
        "scope_id": "scope-1",
        "user_id": "u-1",
        "name": "team",
        "kind": kind,
        "shared_with_user_ids": [],
        "policy": {
            "data_classes": ["triggers", "reports", "tickets", "a2a_messages"],
            "allow_a2a": True,
            "allow_cross_user": False,
            "max_participants": 10,
            "retention_days": 90,
        },
    }
    base.update(over)
    return base


def _ticket(**over) -> dict:
    base = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "context_scope_id": "scope-1",
        "origin_thread_key": "thread-1",
        "status": "running",
    }
    base.update(over)
    return base


def test_isolated_policy_cannot_enable_sharing():
    with pytest.raises(context_scope.ContextScopeError, match="cannot allow"):
        context_scope.validate_policy(
            "isolated", {"allow_a2a": True, "max_participants": 1}
        )
    with pytest.raises(context_scope.ContextScopeError, match="exactly one"):
        context_scope.validate_policy("isolated", {"max_participants": 2})
    normalized = context_scope.validate_policy("isolated", {})
    assert normalized["allow_a2a"] is False
    assert normalized["max_participants"] == 1


def test_shared_policy_rejects_unknown_and_duplicate_fields():
    with pytest.raises(context_scope.ContextScopeError, match="invalid"):
        context_scope.validate_policy("shared", {"unknown": True})
    with pytest.raises(context_scope.ContextScopeError, match="invalid"):
        context_scope.validate_policy(
            "shared", {"data_classes": ["tickets", "tickets"]}
        )


@pytest.mark.asyncio
async def test_ensure_scope_uses_the_bound_scope():
    get_scope = AsyncMock(return_value=_scope())
    with patch(f"{_MOD}.scope_db.get_scope", get_scope):
        assert await context_scope.ensure_scope(_ticket()) == _scope()
    get_scope.assert_awaited_once_with("scope-1")


@pytest.mark.asyncio
async def test_ensure_scope_creates_an_isolated_scope_when_missing():
    isolated = _scope("isolated", scope_id="new-scope")
    create = AsyncMock(return_value=isolated)
    with patch(f"{_MOD}.scope_db.create_isolated_scope", create):
        result = await context_scope.ensure_scope(
            _ticket(context_scope_id=None, ticket_id="t-9")
        )
    assert result == isolated
    create.assert_awaited_once_with(user_id="u-1", ticket_id="t-9")


@pytest.mark.asyncio
async def test_assert_can_share_requires_one_shared_scope():
    other = _ticket(ticket_id="t-2")
    assert await context_scope.assert_can_share(_ticket(), other, scope=_scope())
    with pytest.raises(context_scope.ContextScopeError, match="different"):
        await context_scope.assert_can_share(
            _ticket(),
            _ticket(ticket_id="t-2", context_scope_id="scope-2"),
            scope=_scope(),
        )
    with pytest.raises(context_scope.ContextScopeError, match="isolated"):
        await context_scope.assert_can_share(
            _ticket(), other, scope=_scope("isolated", scope_id="scope-1")
        )


@pytest.mark.asyncio
async def test_cross_user_sharing_needs_a_mapped_peer():
    other = _ticket(ticket_id="t-2", user_id="u-2")
    with pytest.raises(context_scope.ContextScopeError, match="cross-user"):
        await context_scope.assert_can_share(_ticket(), other, scope=_scope())
    mapped = _scope(
        shared_with_user_ids=["u-2"],
        policy={
            "data_classes": ["triggers", "reports", "tickets", "a2a_messages"],
            "allow_a2a": True,
            "allow_cross_user": True,
            "max_participants": 10,
            "retention_days": 90,
        },
    )
    assert await context_scope.assert_can_share(_ticket(), other, scope=mapped)
    # A mapped peer may answer the owner too; ownership is part of the mapping.
    peer = _ticket(user_id="u-2")
    assert await context_scope.assert_can_share(
        peer, _ticket(ticket_id="t-2", user_id="u-1"), scope=mapped
    )
    with pytest.raises(context_scope.ContextScopeError, match="not mapped"):
        await context_scope.assert_can_share(
            _ticket(),
            _ticket(ticket_id="t-3", user_id="u-3"),
            scope=mapped,
        )


@pytest.mark.asyncio
async def test_following_a_context_thread_requires_a_peer_in_scope():
    with (
        patch(
            f"{_MOD}.scope_db.list_scope_ticket_ids",
            AsyncMock(return_value=["t-1", "t-2"]),
        ),
        patch(
            f"{_MOD}.scope_db.ticket_ids_for_thread",
            AsyncMock(return_value=["t-2"]),
        ),
    ):
        assert await context_scope.assert_can_follow_thread(
            _ticket(), "github:acme#2", scope=_scope()
        )
    with (
        patch(
            f"{_MOD}.scope_db.list_scope_ticket_ids",
            AsyncMock(return_value=["t-1"]),
        ),
        patch(
            f"{_MOD}.scope_db.ticket_ids_for_thread",
            AsyncMock(return_value=["t-9"]),
        ),
    ):
        with pytest.raises(context_scope.ContextScopeError, match="no Ticket"):
            await context_scope.assert_can_follow_thread(
                _ticket(), "github:acme#9", scope=_scope()
            )


@pytest.mark.asyncio
async def test_following_is_refused_in_an_isolated_scope():
    with pytest.raises(context_scope.ContextScopeError, match="shared scope"):
        await context_scope.assert_can_follow_thread(
            _ticket(), "other-thread", scope=_scope("isolated", scope_id="scope-1")
        )


@pytest.mark.asyncio
async def test_a_ticket_may_always_follow_its_own_origin_thread():
    assert await context_scope.assert_can_follow_thread(
        _ticket(), "thread-1", scope=_scope("isolated", scope_id="scope-1")
    )


@pytest.mark.asyncio
async def test_readable_ticket_ids_follow_the_data_policy():
    with patch(
        f"{_MOD}.scope_db.list_scope_ticket_ids",
        AsyncMock(return_value=["t-1", "t-2"]),
    ) as peers:
        assert await context_scope.readable_ticket_ids(_ticket(), scope=_scope()) == {
            "t-1",
            "t-2",
        }
    with patch(
        f"{_MOD}.scope_db.list_scope_ticket_ids",
        AsyncMock(return_value=["t-1", "t-2"]),
    ) as peers:
        assert await context_scope.readable_ticket_ids(
            _ticket(),
            scope=_scope(
                policy={
                    "data_classes": ["a2a_messages"],
                    "allow_a2a": True,
                    "max_participants": 10,
                }
            ),
        ) == {"t-1"}
    peers.assert_not_awaited()


@pytest.mark.asyncio
async def test_readable_ticket_ids_are_per_data_class():
    scope = _scope(policy={"data_classes": ["reports"], "allow_a2a": False})
    with patch(
        f"{_MOD}.scope_db.list_scope_ticket_ids",
        AsyncMock(return_value=["t-1", "t-2"]),
    ):
        assert await context_scope.readable_ticket_ids(
            _ticket(), scope=scope, data_class="tickets"
        ) == {"t-1"}
        assert await context_scope.readable_ticket_ids(
            _ticket(), scope=scope, data_class="reports"
        ) == {"t-1", "t-2"}

    scope = _scope(policy={"data_classes": ["tickets"], "allow_a2a": False})
    with patch(
        f"{_MOD}.scope_db.list_scope_ticket_ids",
        AsyncMock(return_value=["t-1", "t-2"]),
    ):
        assert await context_scope.readable_ticket_ids(
            _ticket(), scope=scope, data_class="tickets"
        ) == {"t-1", "t-2"}
        assert await context_scope.readable_ticket_ids(
            _ticket(), scope=scope, data_class="reports"
        ) == {"t-1"}


@pytest.mark.asyncio
async def test_scope_members_are_policy_independent():
    with patch(
        f"{_MOD}.scope_db.list_scope_ticket_ids",
        AsyncMock(return_value=["t-1", "t-2"]),
    ) as members:
        assert await context_scope.scope_member_ids(
            _ticket(),
            scope=_scope(policy={"data_classes": ["a2a_messages"], "allow_a2a": True}),
        ) == {"t-1", "t-2"}
    members.assert_awaited_once_with("scope-1")
    assert await context_scope.scope_member_ids(
        _ticket(), scope=_scope("isolated", scope_id="scope-1")
    ) == {"t-1"}


def test_ticket_summary_is_report_text_and_needs_the_reports_class():
    rows = [
        {"ticket_id": "t-1", "title": "mine", "summary": "internal finding"},
    ]
    hidden = context_scope.hide_unpublished_report_text(
        rows, scope=_scope(policy={"data_classes": ["tickets"]})
    )
    assert hidden == [{"ticket_id": "t-1", "title": "mine", "summary": ""}]
    published = context_scope.hide_unpublished_report_text(
        rows, scope=_scope(policy={"data_classes": ["reports", "tickets"]})
    )
    assert published[0]["summary"] == "internal finding"


def test_a_blocked_reports_attention_question_is_report_text_too():
    """A blocked or failed report puts its own question, or its summary, in
    the Ticket's attention slot, so a peer that may not read Reports must not
    read it either. The slot itself stays: the peer still sees that the
    Ticket waits for a person."""
    rows = [
        {
            "ticket_id": "t-2",
            "title": "theirs",
            "summary": "internal finding",
            "attention": {"question": "internal finding"},
        },
    ]
    hidden = context_scope.hide_unpublished_report_text(
        rows, scope=_scope(policy={"data_classes": ["tickets"]})
    )
    assert hidden == [
        {
            "ticket_id": "t-2",
            "title": "theirs",
            "summary": "",
            "attention": {"question": ""},
        }
    ]
    published = context_scope.hide_unpublished_report_text(
        rows, scope=_scope(policy={"data_classes": ["reports", "tickets"]})
    )
    assert published[0]["attention"]["question"] == "internal finding"


def test_a_ticket_without_attention_keeps_its_shape():
    rows = [{"ticket_id": "t-2", "title": "theirs", "summary": "internal finding"}]
    hidden = context_scope.hide_unpublished_report_text(
        rows, scope=_scope(policy={"data_classes": ["tickets"]})
    )
    assert hidden == [{"ticket_id": "t-2", "title": "theirs", "summary": ""}]
