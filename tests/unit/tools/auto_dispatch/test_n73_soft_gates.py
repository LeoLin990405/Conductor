"""N73 L3: soft gates open; quota and pool are inputs, structural checks record only."""

from __future__ import annotations

import json
import logging

import pytest

from src.server.services.autonomy.roles import AgentState, RolePolicy, RolePolicyError
from src.tools.auto_dispatch.check_modes import ENFORCE, OFF, SHADOW
from src.tools.auto_dispatch.gates import shadow_findings
from src.tools.auto_dispatch.planning import LLMPlanner
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.soft_gates import QUOTA_ADVISORY_ENV, quota_advisory, record_only

POLICY = RolePolicy(developer_pool=("kimi",))


class _Empty:
    async def remaining(self, model: str) -> float:
        return 0.0


def _plan_json() -> str:
    def line(name: str, deps: list[str]) -> dict:
        return {
            "name": name,
            "task": f"Change src/{name}.py.",
            "depends_on": deps,
            "write_scope": [f"src/{name}.py", f"tests/unit/test_{name}.py"],
            "declared_paths": [f"src/{name}.py", f"tests/unit/test_{name}.py"],
            "acceptance": [f"cmd: pytest -q tests/unit/test_{name}.py"],
            "risk_bearing": False,
        }

    return json.dumps(
        {
            "schema_version": 1,
            "spec": {"title": "t", "body": "## Goal\nx\n\n## Acceptance\n- y"},
            "lines": [line("one", []), line("two", ["one"])],
        }
    )


def _planner() -> LLMPlanner:
    async def complete(prompt: str, system: str) -> str:
        return _plan_json()

    return LLMPlanner(
        complete,
        policy=POLICY,
        agents=(AgentState(provider="kimi", capacity=2),),
        headroom=_Empty(),
    )


@pytest.mark.asyncio
async def test_quota_no_longer_fails_the_plan(monkeypatch) -> None:
    """S3: the out-of-quota developer's lines are left for launch-time assignment."""
    monkeypatch.delenv(QUOTA_ADVISORY_ENV, raising=False)
    lines = await _planner().plan("goal")
    assert [line.name for line in lines] == ["one", "two"]
    # Fails on old code: the planner refused UNASSIGNED quota_exhausted.
    assert all(not line.assigned_model for line in lines)


@pytest.mark.asyncio
async def test_quota_advisory_off_restores_the_old_refusal(monkeypatch) -> None:
    monkeypatch.setenv(QUOTA_ADVISORY_ENV, "0")
    with pytest.raises(DispatchError):
        await _planner().plan("goal")
    assert quota_advisory({QUOTA_ADVISORY_ENV: "off"}) is False
    assert quota_advisory({}) is True


def test_record_only_turns_enforce_into_shadow(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert record_only(ENFORCE, check="scope_check_test") == SHADOW
    assert "record-only" in caplog.text
    assert record_only(SHADOW) == SHADOW and record_only(OFF) == OFF


def test_avoid_and_hints_are_advisory_inputs() -> None:
    old = RolePolicy.from_mapping({"developer_pool": ["kimi", "deepseek"]}, strict=True)
    new = RolePolicy.from_mapping(
        {"developer_pool": ["kimi", "deepseek"], "avoid": {"deepseek": "uncommitted exits"}},
        strict=True,
    )
    # Old policy files still load, and avoid does not change the pools.
    assert old == new and old.avoid == {}
    hints = new.assignment_hints()
    assert hints["preferred"][:2] == ["kimi", "deepseek"]
    assert hints["avoid"] == {"deepseek": "uncommitted exits"}
    assert RolePolicy.from_mapping({"avoid": "grok, glm"}).avoid == {"grok": "", "glm": ""}
    with pytest.raises(RolePolicyError):
        RolePolicy.from_mapping({"avoid": 3}, strict=True)


def test_advisory_roster_reaches_past_the_pool_but_never_reserved_roles() -> None:
    roster = RolePolicy(developer_pool=("kimi",)).advisory_work_roster()
    assert "kimi" in roster and "stepfun" in roster and "doubao" in roster
    assert "claude" not in roster and "codex" not in roster


def test_shadow_findings_are_review_evidence_only() -> None:
    snapshot = {
        "primary_verdict": "SECRET",
        "gates": {
            "G2": {
                "shadow": [
                    {
                        "check": "scope_check",
                        "would_refuse": "g2_scope_conflict",
                        "finding": "scope: out_of_scope.txt",
                        "record": {"files": ["out_of_scope.txt"]},
                    },
                    "junk",
                ]
            }
        },
    }
    assert shadow_findings(snapshot) == [
        {
            "check": "scope_check",
            "would_refuse": "g2_scope_conflict",
            "finding": "scope: out_of_scope.txt",
            "files": ["out_of_scope.txt"],
        }
    ]
    assert shadow_findings({}) == [] and shadow_findings(None) == []


def test_record_only_has_a_rollback_switch() -> None:
    from src.tools.auto_dispatch.soft_gates import RECORD_ONLY_ENV

    assert record_only(ENFORCE, environ={RECORD_ONLY_ENV: "0"}) == ENFORCE
    assert record_only(ENFORCE, environ={}) == SHADOW
