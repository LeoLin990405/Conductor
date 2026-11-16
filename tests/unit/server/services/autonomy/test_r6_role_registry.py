"""R6 L1: one role registry with fallbacks, validation and audit."""

from __future__ import annotations

import pytest

from src.server.services.autonomy import role_types
from src.server.services.autonomy.role_registry import (
    InMemoryRegistryStore,
    RegistryConflictError,
    RoleRegistryService,
    describe,
)
from src.server.services.autonomy.role_types import (
    HARD_RULES,
    SOURCE_FALLBACK,
    SOURCE_STORE,
    RoleRegistryError,
    fallback_registry,
    validate_registry,
)
from src.server.services.autonomy.roles import ROLE_POLICY_ENV, load_role_policy

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv(ROLE_POLICY_ENV, raising=False)
    monkeypatch.delenv("CONDUCTOR_DISPATCH_PLANNER", raising=False)
    role_types.set_registry_provider(None)
    yield
    role_types.set_registry_provider(None)


def _service() -> RoleRegistryService:
    return RoleRegistryService(InMemoryRegistryStore())


async def test_no_stored_row_reads_the_three_legacy_places(tmp_path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text('{"developer_pool": ["stepfun", "kimi"]}', encoding="utf-8")
    reg = fallback_registry(
        {ROLE_POLICY_ENV: str(policy), "CONDUCTOR_DISPATCH_PLANNER": "llm"},
        router_model="deepseek-flash-account",
    )
    assert reg.source == SOURCE_FALLBACK
    assert reg.default_name("router") == "deepseek-flash-account"
    assert reg.entry("planner").extra["planner_cli"] == "llm"
    assert reg.entry("worker").extra["developer_pool"] == ["stepfun", "kimi"]
    assert reg.default_name("reviewer") == "codex"
    assert validate_registry(reg) == []
    service = _service()
    assert (await service.get()).source == SOURCE_FALLBACK
    assert service.snapshot() is None


async def test_an_edit_is_validated_stored_and_audited() -> None:
    service = _service()
    reg = await service.update_role(
        "worker",
        {"candidates": ["stepfun", "kimi", "glm"], "default": "stepfun"},
        actor="user:leo",
        reason="stepfun is best on web",
    )
    assert reg.source == SOURCE_STORE and reg.revision == 1
    assert reg.entry("worker").candidate_names == ("stepfun", "kimi", "glm")
    audit = await service.audit()
    assert audit[0]["role"] == "worker" and audit[0]["actor"] == "user:leo"
    assert audit[0]["reason"] == "stepfun is best on web"
    assert audit[0]["before"]["source"] == SOURCE_FALLBACK
    assert audit[0]["after"]["default"] == "stepfun"
    # A second edit compares and swaps on the revision.
    reg2 = await service.update_role(
        "worker", {"default": "kimi"}, actor="user:leo", reason="swap", expected_revision=1
    )
    assert reg2.revision == 2
    with pytest.raises(RegistryConflictError):
        await service.update_role(
            "worker", {"default": "glm"}, actor="user:leo", reason="stale", expected_revision=1
        )


@pytest.mark.parametrize(
    ("role", "changes", "rule"),
    [
        ("router", {"constraints": {"may_execute": True}}, "router_routes_never_executes"),
        ("router", {"constraints": {"may_write": True}}, "router_routes_never_executes"),
        ("reviewer", {"constraints": {"may_write": True}}, "review_is_read_only"),
        ("reviewer", {"candidates": ["codex", "kimi"]}, "review_ineligible:kimi"),
        ("reviewer", {"extra": {"secondary_pool": ["deepseek"]}}, "review_ineligible:deepseek"),
        ("worker", {"constraints": {"read_only": True}}, "worker_must_write"),
    ],
)
async def test_hard_rules_cannot_be_configured_off(role, changes, rule) -> None:
    service = _service()
    with pytest.raises(RoleRegistryError, match=rule):
        await service.update_role(role, changes, actor="user:leo", reason="try")
    assert await service.audit() == []


async def test_edits_need_reason_actor_and_known_fields() -> None:
    service = _service()
    with pytest.raises(RoleRegistryError, match="reason"):
        await service.update_role("worker", {"default": "kimi"}, actor="u", reason=" ")
    with pytest.raises(RoleRegistryError, match="actor"):
        await service.update_role("worker", {"default": "kimi"}, actor="", reason="r")
    with pytest.raises(RoleRegistryError, match="not editable"):
        await service.update_role("worker", {"hard_rules": []}, actor="u", reason="r")
    with pytest.raises(RoleRegistryError, match="planner_cli"):
        await service.update_role(
            "planner", {"extra": {"planner_cli": "bash"}}, actor="u", reason="r"
        )


async def test_installed_registry_drives_role_policy_and_planner() -> None:
    from src.server.services.dispatch import _registry_planner_cli

    service = _service()
    await service.update_role(
        "worker", {"candidates": ["stepfun", "kimi"], "extra": {"developer_pool": ["stepfun", "kimi"]}},
        actor="u", reason="narrow",
    )
    await service.update_role(
        "planner", {"extra": {"planner_cli": "llm"}}, actor="u", reason="llm planner"
    )
    assert load_role_policy().developer_pool != ("stepfun", "kimi")
    assert _registry_planner_cli() is None
    service.install()
    # Fails on old code: the policy came only from the file / built-in roster.
    assert load_role_policy().developer_pool == ("stepfun", "kimi")
    assert _registry_planner_cli() == "llm"
    # An explicit env still reads the legacy file path.
    assert load_role_policy({}).developer_pool[0] == "kimi"


async def test_describe_lists_hard_rules() -> None:
    view = describe(fallback_registry({}, router_model="r"))
    assert view["hard_rules"] == list(HARD_RULES)
    assert set(view["roles"]) == {"router", "planner", "reviewer", "worker"}
