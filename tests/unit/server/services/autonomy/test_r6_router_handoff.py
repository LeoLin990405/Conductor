"""R6 L3: the Router can be handed to another model with its context, audited."""

from __future__ import annotations

import json

import pytest

from src.server.services.autonomy import role_types, router_handoff
from src.server.services.autonomy.role_registry import InMemoryRegistryStore, RoleRegistryService
from src.server.services.autonomy.role_types import RoleRegistryError

pytestmark = pytest.mark.asyncio

CONTEXT = {
    "thread_context": {
        "tickets": [{"ticket_id": "t1", "status": "running"}],
        "triggers": [{"trigger_id": "g1"}],
        "spilled": {"tickets": ["t9"], "triggers": []},
        "spill_omitted": {"tickets": 2, "triggers": 0},
        "spill": "read with autonomy_read",
    }
}


@pytest.fixture(autouse=True)
def _reset():
    role_types.set_registry_provider(None)
    router_handoff._reset_for_tests()
    yield
    role_types.set_registry_provider(None)
    router_handoff._reset_for_tests()


async def _service() -> RoleRegistryService:
    service = RoleRegistryService(InMemoryRegistryStore())
    await service.update_role(
        "router",
        {"candidates": ["deepseek-flash", "kimi-router"], "default": "deepseek-flash"},
        actor="user:leo",
        reason="two router candidates",
    )
    service.install()
    return service


async def test_active_model_falls_back_to_config() -> None:
    assert router_handoff.active_router_model(fallback=lambda: "cfg-model") == "cfg-model"
    await _service()
    assert router_handoff.active_router_model(fallback=lambda: "cfg-model") == "deepseek-flash"


async def test_handoff_stores_digests_and_spill_refs_never_inline_rows() -> None:
    """Review fix 1 (fails on 3025c108e): Ticket rows never reach the store."""
    service = await _service()
    secret_row = {"ticket_id": "t1", "status": "running", "text": "SECRET-TICKET-TEXT"}
    payload = {**CONTEXT, "thread_context": {**CONTEXT["thread_context"], "tickets": [secret_row]}}
    router_handoff.remember_context("thread-A", payload)
    router_handoff.remember_context("thread-B", {"thread_context": {"spilled": {"tickets": ["tB"]}}})
    event = await router_handoff.handoff(
        "kimi-router", reason="quota", actor="operator:leo", service=service
    )
    assert (event.from_model, event.to_model) == ("deepseek-flash", "kimi-router")
    assert set(event.sessions) == {"thread-A", "thread-B"}
    assert event.sessions["thread-A"]["spilled"] == {"tickets": ["t9"], "triggers": []}
    assert event.sessions["thread-A"]["inline_rows"] == 2  # one ticket, one trigger
    assert router_handoff.active_router_model() == "kimi-router"
    stored = json.dumps(service.snapshot().to_dict()) + json.dumps(await service.audit())
    assert "SECRET-TICKET-TEXT" not in stored
    audit = await service.audit(role="router")
    assert audit[0]["reason"] == "router_handoff:quota"


async def test_each_session_gets_only_its_own_note_once_without_a_registry_revision() -> None:
    service = await _service()
    router_handoff.remember_context("thread-A", CONTEXT)
    router_handoff.remember_context("thread-B", {"thread_context": {"spilled": {"tickets": ["tB"]}}})
    await router_handoff.handoff("kimi-router", reason="quota", actor="operator:leo", service=service)
    revision = service.snapshot().revision
    audit_rows = len(await service.audit())
    store = router_handoff.InMemoryDeliveryStore()
    note_a = await router_handoff.take_note("thread-A", store=store)
    assert "taking over routing from deepseek-flash" in note_a and "t9" in note_a
    assert "tB" not in note_a and "never execute" in note_a
    assert await router_handoff.take_note("thread-A", store=store) == ""
    # A restart loses process memory; delivery lives in the per-session store.
    router_handoff._reset_for_tests()
    assert await router_handoff.take_note("thread-A", store=store) == ""
    note_b = await router_handoff.take_note("thread-B", store=store)
    assert "tB" in note_b and "t9" not in note_b
    # Fails on eff545ae9: each delivery wrote a registry revision and an audit row.
    assert service.snapshot().revision == revision
    assert len(await service.audit()) == audit_rows


async def test_roles_view_and_audit_redact_handoff_sessions() -> None:
    """Fails on eff545ae9: describe() and the audit returned every session."""
    from src.server.services.autonomy.role_registry import describe

    service = await _service()
    router_handoff.remember_context("thread-A", CONTEXT)
    await router_handoff.handoff("kimi-router", reason="quota", actor="operator:leo", service=service)
    view = json.dumps(describe(service.snapshot()))
    audit = json.dumps(await service.audit())
    for text in (view, audit):
        assert "thread-A" not in text and "t9" not in text
    handoff = describe(service.snapshot())["roles"]["router"]["extra"]["handoff"]
    assert handoff["session_count"] == 1 and "sessions" not in handoff


async def test_handoff_refuses_non_candidates_and_noop() -> None:
    service = await _service()
    with pytest.raises(RoleRegistryError, match="router_not_candidate"):
        await router_handoff.handoff("grok", reason="x", actor="u", service=service)
    with pytest.raises(RoleRegistryError, match="router_already_active"):
        await router_handoff.handoff("deepseek-flash", reason="x", actor="u", service=service)


async def test_quota_handoff_picks_a_candidate_with_headroom() -> None:
    service = await _service()
    assert await router_handoff.handoff_on_quota({"deepseek-flash": 0.5}, service=service) is None
    router_handoff.remember_context("thread-A", CONTEXT)
    event = await router_handoff.handoff_on_quota(
        {"deepseek-flash": 0.0, "kimi-router": 0.7}, service=service
    )
    assert event is not None and event.reason == router_handoff.REASON_QUOTA


def test_carry_context_is_bounded() -> None:
    rows = [{"ticket_id": f"t{i}", "pad": "x" * 500} for i in range(50)]
    carried = router_handoff.carry_context({"thread_context": {"tickets": rows}})
    assert carried["truncated"] is True
    assert 0 < len(carried["inline"]) < 50
    assert router_handoff.carry_context(None)["inline"] == []


def test_the_batch_prompt_no_longer_reads_process_wide_context() -> None:
    from src.server.services.autonomy import router

    prompt = router._router_batch_prompt({"thread_context": {}})
    assert "Router handoff" not in prompt


async def test_put_cannot_plant_a_handoff_note() -> None:
    """Review fix 2: handoff keys are internal; PUT cannot write them."""
    from src.server.services.autonomy.role_types import RoleRegistryError

    service = await _service()
    with pytest.raises(RoleRegistryError, match="extra key not allowed"):
        await service.update_role(
            "router",
            {"extra": {"handoff": {"handoff_id": "x", "sessions": {}}}},
            actor="operator:leo",
            reason="plant",
        )
