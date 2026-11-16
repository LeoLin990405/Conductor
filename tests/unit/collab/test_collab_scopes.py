"""Scopes are an explicit grant table; every write tool names its own scope."""

from __future__ import annotations

import pytest

from src.collab import scopes
from src.collab.types import ScopeDeniedError


def test_read_is_the_only_default_scope():
    assert scopes.DEFAULT_SCOPES == frozenset({"read"})
    assert scopes.normalize_scopes(None) == frozenset({"read"})
    assert scopes.normalize_scopes("") == frozenset()
    assert scopes.normalize_scopes("read, claim") == frozenset({"read", "claim"})
    assert scopes.normalize_scopes(["READ", "report"]) == frozenset(
        {"read", "report"}
    )


def test_an_unknown_scope_is_refused_instead_of_ignored():
    with pytest.raises(ValueError):
        scopes.normalize_scopes("read, admin")
    assert not scopes.can_write({"admin"})
    assert not scopes.scope_allows({"admin"}, "task_list")


def test_each_tool_maps_to_exactly_one_scope_from_the_prd():
    assert scopes.TOOL_SCOPES == {
        "whoami": "read",
        "task_list": "read",
        "task_get": "read",
        "handoff_list": "read",
        "agent_inbox": "read",
        "review_list": "read",
        "task_create": "propose",
        "task_claim": "claim",
        "task_heartbeat": "claim",
        "task_release": "claim",
        "handoff_request": "claim",
        "handoff_accept": "claim",
        "task_report": "report",
        "review_request": "report",
        "agent_ask": "ask",
        "review_submit": "review",
    }
    assert set(scopes.TOOL_SCOPES.values()) == set(scopes.SCOPES)
    for scope in scopes.SCOPES:
        assert scopes.tools_for_scope(scope) == scopes.SCOPE_TOOLS[scope]
    assert scopes.scope_for_tool("review_submit") == "review"
    with pytest.raises(ValueError):
        scopes.scope_for_tool("not_a_tool")


def test_a_read_grant_cannot_use_a_write_tool():
    assert scopes.scope_allows({"read"}, "task_list")
    assert not scopes.scope_allows({"read"}, "task_claim")
    assert not scopes.scope_allows({"read"}, "task_create")
    assert not scopes.scope_allows({"read"}, "agent_ask")
    with pytest.raises(ScopeDeniedError) as caught:
        scopes.require_scope({"read"}, "task_claim")
    assert caught.value.code == "scope_denied"
    assert caught.value.details["required_scope"] == "claim"
    scopes.require_scope({"read", "claim"}, "task_claim")


def test_can_write_is_about_the_grant_not_file_capability():
    assert not scopes.can_write({"read"})
    assert scopes.can_write({"claim"})
    assert scopes.can_write({"propose"})
    assert scopes.WRITE_SCOPES == frozenset(
        {"propose", "claim", "report", "ask", "review"}
    )
