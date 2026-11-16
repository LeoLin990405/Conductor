"""Planner DAG: the schema contract, every rejection reason, and the archive."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.server.services.autonomy.roles import DEFAULT_PLANNER, AgentState, RolePolicy
from src.tools.auto_dispatch.planner_prompt import PLANNER_SYSTEM_PROMPT
from src.tools.auto_dispatch.planning import (
    MAX_GOAL_BYTES,
    FileSpecArchive,
    LLMPlanner,
    PlanRejection,
    Spec,
    SpecRecord,
    _record,
    validate_plan,
)
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.types import Line

pytestmark = pytest.mark.asyncio

FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "planner_output_v1.json"
)


def _planner(reply: str, **kwargs: object):
    calls: list[tuple[str, str]] = []

    async def complete(prompt: str, system: str) -> str:
        calls.append((prompt, system))
        return reply

    return LLMPlanner(complete, **kwargs), calls


def _payload(*lines: dict[str, object], **top: object) -> dict[str, object]:
    """A planner payload in the v1 schema, as a dict for the pure validator."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "spec": {"title": "Do the work", "body": "## Goal\nShip it."},
        "lines": list(lines),
    }
    payload.update(top)
    return payload


def _reply(*lines: dict[str, object], **top: object) -> str:
    """The same payload as the model would emit it."""
    return json.dumps(_payload(*lines, **top))


def _line(name: str, task: str = "do the work", **over: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "task": task,
        "depends_on": [],
        "write_scope": [f"src/{name}.py"],
        "declared_paths": [f"src/{name}.py"],
        "acceptance": [f"cmd: pytest -q tests/unit/{name}.py"],
        "risk_bearing": False,
    }
    entry.update(over)
    return entry


def _fixture_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestRolePolicyFileEntry:
    """A policy file feeds the default LLMPlanner/DAG assignment directly."""

    @staticmethod
    def _policy(tmp_path, monkeypatch, *pool: str):
        import json
        import os

        path = tmp_path / "policy.json"
        path.write_text(json.dumps({"developer_pool": list(pool)}), encoding="utf-8")
        monkeypatch.setenv("CONDUCTOR_ROLE_POLICY_FILE", str(path))
        return os.environ["CONDUCTOR_ROLE_POLICY_FILE"]

    async def test_an_alias_cannot_take_a_write_line(self, tmp_path, monkeypatch):
        """arkcli is doubao: text-only, so a write line has no eligible Worker."""
        self._policy(tmp_path, monkeypatch, "arkcli")
        planner, _ = _planner(_reply(_line("edit")))
        with pytest.raises(DispatchError) as excinfo:
            await planner.plan("ship it")
        assert "line-0:no_eligible_worker" in str(excinfo.value)

    async def test_the_doubao_spelling_gets_the_same_result(self, tmp_path, monkeypatch):
        self._policy(tmp_path, monkeypatch, "doubao")
        planner, _ = _planner(_reply(_line("edit")))
        with pytest.raises(DispatchError) as excinfo:
            await planner.plan("ship it")
        assert "line-0:no_eligible_worker" in str(excinfo.value)

    async def test_an_unknown_name_reports_unknown_worker(self, tmp_path, monkeypatch):
        self._policy(tmp_path, monkeypatch, "no-such-worker")
        planner, _ = _planner(_reply(_line("edit")))
        with pytest.raises(DispatchError) as excinfo:
            await planner.plan("ship it")
        assert "line-0:unknown_worker" in str(excinfo.value)


class TestCanonicalSample:
    async def test_the_sample_plans_end_to_end(self):
        planner, calls = _planner(FIXTURE.read_text(encoding="utf-8"))
        lines = await planner.plan("add a dispatch status summary endpoint")

        assert [line.id for line in lines] == ["line-0", "line-1"]
        assert [line.name for line in lines] == ["summary_model", "summary_route"]
        # the planner's dependency names become line ids
        assert lines[0].depends_on == ()
        assert lines[1].depends_on == ("line-0",)
        assert lines[1].write_scope == (
            "src/server/app/dispatch_summary.py",
            "tests/unit/server/app/test_dispatch_summary_api.py",
        )
        assert lines[1].acceptance == (
            "cmd: pytest -q tests/unit/server/app/test_dispatch_summary_api.py",
            "the route requires the same auth as GET /dispatch/{id}",
        )
        assert lines[0].risk_bearing is False
        assert lines[1].risk_bearing is True
        assert len(calls) == 1

    async def test_the_sample_assignment_is_written_back(self):
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"))
        lines = await planner.plan("add a dispatch status summary endpoint")

        pool = RolePolicy.default().developer_pool
        assert lines[0].assigned_model in pool
        assert lines[1].assigned_model in pool
        # the two lines write disjoint scopes, so both are placed
        assert len(planner.last_result.assignment.assignments) == 2
        assert planner.last_result.assignment.unassigned == ()

    async def test_the_plan_result_carries_the_spec_and_the_risk_set(self):
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"))
        result = await planner.plan_dag("add a dispatch status summary endpoint")

        assert result.schema_version == 1
        assert result.spec.title == "Add a dispatch status summary endpoint"
        assert "## Goal" in result.spec.body
        assert result.risk_bearing == frozenset({"line-1"})
        assert result.prompt_version == "planner_prompt_v2"
        assert result.spec_ref == ""

    async def test_the_sample_is_the_fixture_the_tests_load(self):
        payload = _fixture_payload()
        outcome = validate_plan(payload)

        assert not isinstance(outcome, PlanRejection)
        assert [line.name for line in outcome.lines] == [
            "summary_model",
            "summary_route",
        ]


class TestRejectionReasons:
    @pytest.mark.parametrize(
        "payload, expected",
        [
            ({"schema_version": 2, "spec": {"title": "t", "body": "b"}, "lines": [_line("a")]}, "schema_version_unsupported:2"),
            ({"schema_version": "1", "spec": {"title": "t", "body": "b"}, "lines": [_line("a")]}, "schema_version_unsupported:'1'"),
            ({"schema_version": 1, "lines": [_line("a")]}, "missing_spec:None"),
            ({"schema_version": 1, "spec": {"title": " ", "body": "b"}, "lines": [_line("a")]}, "missing_spec:title"),
            ({"schema_version": 1, "spec": {"title": "t", "body": ""}, "lines": [_line("a")]}, "missing_spec:body"),
            ({"schema_version": 1, "spec": {"title": "t", "body": "x" * 16_001}, "lines": [_line("a")]}, "spec_too_large:body"),
            ({"schema_version": 1, "spec": {"title": "t", "body": "b"}}, "no_lines:None"),
            ({"schema_version": 1, "spec": {"title": "t", "body": "b"}, "lines": []}, "no_lines:[]"),
            (_payload(*[_line(f"n{i}") for i in range(9)]), "too_many_lines:9"),
            (_payload(_line("a", "x" * 8_001)), "line_too_large:task"),
            (_payload(_line("a", task="ok", write_scope=["y" * 513])), "line_too_large:a"),
            (_payload(_line("a", task="ok", acceptance=["z" * 513])), "line_too_large:acceptance"),
            (_payload(_line("a"), _line("a")), "duplicate_task_id:a"),
            (_payload(_line("a", depends_on=["ghost"])), "missing_dependency:ghost"),
            (_payload(_line("a", depends_on=["a"])), "self_dependency:a"),
            (_payload(_line("a", depends_on=["b"]), _line("b", depends_on=["a"])), "dependency_cycle"),
            (_payload(_line("a", write_scope=[])), "empty_write_scope:a"),
            (_payload(_line("a", write_scope=["  "])), "empty_write_scope:a"),
            (_payload(_line("a", write_scope=["/etc/passwd"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["../outside"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["~/secrets"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["C:/win"])), "invalid_write_scope:a"),
            # A Windows-style absolute path only shows its leading separator
            # once the backslashes are unified, so normalisation has to come
            # before the boundary check.
            (_payload(_line("a", write_scope=["\\etc\\secret"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["..\\..\\etc"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["C:\\win"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["~\\keys"])), "invalid_write_scope:a"),
            (_payload(_line("a", write_scope=["src/a"]), _line("b", write_scope=["src/a/b.py"])), "write_scope_conflict:a:b"),
            (_payload(_line("a", acceptance=[])), "empty_acceptance:a"),
            (_payload(_line("a", risk_bearing="yes")), "invalid_field:risk_bearing:a"),
            (_payload(_line("a", task=7)), "invalid_field:task:a"),
            (_payload(_line("a", depends_on="b")), "invalid_field:depends_on:a"),
            (_payload(_line("a"), notes="hello"), "unknown_field:notes"),
            (_payload(_line("a", extra=1)), "unknown_field:extra"),
            (["not", "an", "object"], "invalid_field:payload"),
            ("a string", "invalid_field:payload"),
        ],
    )
    def test_the_reason_is_the_stable_string(self, payload, expected):
        outcome = validate_plan(payload)

        assert isinstance(outcome, PlanRejection)
        assert outcome.code == expected

    async def test_the_rejection_becomes_a_dispatch_error(self):
        planner, _ = _planner(_reply(_line("a", write_scope=["/abs"])))
        with pytest.raises(DispatchError, match="invalid_write_scope:a"):
            await planner.plan("goal")

    def test_the_first_error_in_order_wins(self):
        # an unknown field is last in the order, so the duplicate name wins
        payload = _payload(_line("a"), _line("a"), junk=1)

        assert validate_plan(payload).code == "duplicate_task_id:a"

    def test_a_size_error_wins_over_a_later_type_error(self):
        payload = _payload(_line("a", task="x" * 8_001, risk_bearing="yes"))

        assert validate_plan(payload).code == "line_too_large:task"

    def test_a_nul_or_lone_surrogate_is_its_own_reason(self):
        for payload in (
            _payload(_line("a", task="do a\x00b")),
            _payload(_line("a", task="do \ud800 it")),
        ):
            assert validate_plan(payload).code == "invalid_text:task"

    def test_a_blank_task_is_refused(self):
        # the baseline refused a line with no task text; the strict validator
        # keeps that refusal rather than accepting an empty work lane
        for blank in ("", "   ", "\t\n"):
            payload = _payload(_line("a", task=blank))

            assert validate_plan(payload).code == "invalid_field:task:a", repr(blank)

    @pytest.mark.parametrize("field", ["title", "body"])
    def test_a_spec_no_utf8_can_carry_is_refused(self, field):
        spec = {"title": "t", "body": "b"}
        spec[field] = "lone \ud800 surrogate"
        payload = {
            "schema_version": 1,
            "spec": spec,
            "lines": [_line("a")],
        }

        assert validate_plan(payload).code == f"invalid_text:spec_{field}"

    def test_a_line_that_is_not_an_object_is_refused(self):
        payload = _payload(_line("a"))
        payload["lines"][0] = "a string"

        assert validate_plan(payload).code == "invalid_field:line:0"


class TestPromptSchema:
    def test_the_prompt_allows_an_empty_dependency_list(self):
        # A root line has nothing to wait on, so the template must not read as
        # forbidding "depends_on": [] while also requiring the field.
        assert "depends_on" in PLANNER_SYSTEM_PROMPT
        assert "[] for a root line" in PLANNER_SYSTEM_PROMPT
        assert "an empty list is a legal root line" in PLANNER_SYSTEM_PROMPT

    def test_a_root_line_validates(self):
        payload = _payload(_line("root", depends_on=[]))

        outcome = validate_plan(payload)

        assert not isinstance(outcome, PlanRejection)
        assert outcome.lines[0].depends_on == ()

    def test_the_canonical_sample_root_lines_are_accepted(self):
        # The sample's first line is a root: if an empty list were refused, the
        # normative fixture could not be planned at all.
        payload = _fixture_payload()

        assert payload["lines"][0]["depends_on"] == []
        assert not isinstance(validate_plan(payload), PlanRejection)


class TestGoalLimit:
    async def test_a_goal_past_the_limit_never_calls_the_model(self):
        planner, calls = _planner(_reply(_line("a")))

        with pytest.raises(DispatchError, match="goal_too_large"):
            await planner.plan("x" * (MAX_GOAL_BYTES + 1))

        assert calls == []

    async def test_a_goal_at_the_limit_is_planned(self):
        planner, calls = _planner(_reply(_line("a")))
        lines = await planner.plan("x" * MAX_GOAL_BYTES)

        assert len(lines) == 1
        assert len(calls) == 1


class TestWriteScopeOverlap:
    @pytest.mark.parametrize(
        "first, second",
        [
            ("src/a.py", "src/a.py"),
            ("src/a", "src/a/b.py"),
            ("src/a/b.py", "src/a"),
            ("src/**", "src/x.py"),
            ("tests/**", "tests/unit/a.py"),
            ("./src/a.py", "src/a.py"),
            ("src\\a\\b.py", "src/a/b.py"),
        ],
    )
    def test_overlapping_scopes_are_refused(self, first, second):
        payload = _payload(
            _line("first", write_scope=[first]), _line("second", write_scope=[second])
        )

        assert validate_plan(payload).code == "write_scope_conflict:first:second"

    @pytest.mark.parametrize(
        "first, second",
        [
            ("src/a.py", "src/b.py"),
            ("src/ab", "src/a/b.py"),
            ("src/**", "tests/**"),
            ("src/a.py", "src/a/b.py"),
        ],
    )
    def test_scopes_that_do_not_overlap_are_accepted(self, first, second):
        payload = _payload(
            _line("first", write_scope=[first]), _line("second", write_scope=[second])
        )

        assert not isinstance(validate_plan(payload), PlanRejection)

    @pytest.mark.parametrize("root", ["**", "*", ".", "./"])
    def test_a_root_scope_conflicts_with_any_path(self, root):
        # A root scope claims the whole repository, so a second line naming any
        # path at all conflicts with it.
        payload = _payload(
            _line("first", write_scope=[root]),
            _line("second", write_scope=["src/server/app/x.py"]),
        )

        assert validate_plan(payload).code == "write_scope_conflict:first:second"

    def test_a_lone_root_scope_line_is_accepted(self):
        payload = _payload(_line("whole_repo", write_scope=["**"]))

        assert not isinstance(validate_plan(payload), PlanRejection)

    @pytest.mark.parametrize(
        "root_glob, concrete",
        [
            ("*.py", "foo.py"),
            ("foo?.py", "foo1.py"),
            ("[ab].py", "a.py"),
            ("././", "src/a.py"),
        ],
    )
    def test_a_root_level_glob_or_dotted_root_conflicts(self, root_glob, concrete):
        # Codex's probe pairs: a glob that starts at the root, and a dotted
        # root spelling, both conflict with a concrete path.
        payload = _payload(
            _line("first", write_scope=[root_glob]),
            _line("second", write_scope=[concrete]),
        )

        assert validate_plan(payload).code == "write_scope_conflict:first:second"

    def test_the_control_pair_still_conflicts(self):
        payload = _payload(
            _line("first", write_scope=["./"]),
            _line("second", write_scope=["./src/a.py"]),
        )

        assert validate_plan(payload).code == "write_scope_conflict:first:second"

    def test_a_root_level_glob_stays_apart_from_a_nested_path(self):
        payload = _payload(
            _line("first", write_scope=["*.py"]),
            _line("second", write_scope=["src/server/app/x.py"]),
        )

        assert not isinstance(validate_plan(payload), PlanRejection)

    def test_a_dependent_pair_may_not_overlap(self):
        payload = _payload(
            _line("first", write_scope=["src/a"]),
            _line("second", write_scope=["src/a/b.py"], depends_on=["first"]),
        )

        assert validate_plan(payload).code == "write_scope_conflict:first:second"


class TestAssignment:
    async def test_a_reserved_role_never_receives_a_work_line(self):
        policy = RolePolicy.default()
        planner, _ = _planner(
            _reply(_line("a"), _line("b")),
            policy=policy,
        )
        result = await planner.plan_dag("goal")

        reserved = set(policy.planner) | set(policy.primary_reviewer)
        for item in result.assignment.assignments:
            assert item.provider not in reserved

    async def test_the_archive_matches_the_returned_plan(self, tmp_path):
        # The assignment is written back before the archive is taken, so the
        # archived plan, the returned lines and the recomputed spec_id agree.
        # Pin the built-in roster: an operator CONDUCTOR_ROLE_POLICY_FILE must
        # not change which developers the fixture expects.
        archive = FileSpecArchive(tmp_path)
        planner, _ = _planner(
            FIXTURE.read_text(encoding="utf-8"),
            archive=archive,
            policy=RolePolicy.default(),
        )
        lines = await planner.plan("add a dispatch status summary endpoint")

        returned = {line.id: line.assigned_model for line in lines}
        assert returned == {"line-0": "kimi", "line-1": "glm"}

        target = tmp_path / planner.last_result.spec_ref.removeprefix("spec:")
        stored = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        assert {
            item["id"]: item["assigned_model"] for item in stored["lines"]
        } == returned

        # the reference is the one the caller can recompute from the plan
        record = _record(planner.last_result)
        assert archive.spec_id(record) == planner.last_result.spec_ref.removeprefix(
            "spec:"
        )

    async def test_an_unassigned_plan_is_kept_and_archived_before_it_fails(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        policy = RolePolicy(developer_pool=("kimi",))
        # A dry pool: the plan is not refused for its size (N28), so the
        # assignment is what names the breakage.
        planner, _ = _planner(
            _reply(*[_line(f"n{i}") for i in range(3)]),
            archive=archive,
            policy=policy,
            agents=(AgentState(provider="kimi", capacity=0),),
        )

        with pytest.raises(DispatchError, match="line-2:no_capacity"):
            await planner.plan("goal")

        # the failed plan is still readable, with its full unassigned list
        kept = planner.last_result
        assert kept is not None
        assert [item.task_id for item in kept.assignment.unassigned] == ["line-0", "line-1", "line-2"]
        assert kept.unassigned_reasons() == (
            "line-0:no_capacity",
            "line-1:no_capacity",
            "line-2:no_capacity",
        )
        assert kept.spec_ref.startswith("spec:")

        stored = json.loads((tmp_path / kept.spec_ref.removeprefix("spec:") / "plan.json").read_text())
        assert [item["task_id"] for item in stored["assignment"]["unassigned"]] == [
            "line-0",
            "line-1",
            "line-2",
        ]

    async def test_an_unassigned_plan_is_kept_even_without_an_archive(self):
        policy = RolePolicy(developer_pool=("kimi",))
        planner, _ = _planner(
            _reply(*[_line(f"n{i}") for i in range(3)]), policy=policy, agents=(AgentState(provider="kimi", capacity=0),)
        )

        with pytest.raises(DispatchError, match="no_capacity"):
            await planner.plan("goal")

        assert planner.last_result is not None
        assert planner.last_result.spec_ref == ""
        assert len(planner.last_result.assignment.unassigned) == 3

    async def test_the_reserved_override_is_audited_on_the_line_and_in_the_archive(
        self, tmp_path, caplog
    ):
        import logging

        archive = FileSpecArchive(tmp_path)
        policy = replace(
            RolePolicy.default(),
            allow_reserved_code=True,
            developer_pool=(DEFAULT_PLANNER, "kimi"),
        )
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"), archive=archive, policy=policy)
        with caplog.at_level(logging.INFO):
            lines = await planner.plan("add a dispatch status summary endpoint")

        audited = [line for line in lines if line.assignment_audit]
        assert audited, "the reserved override was used but not audited"
        for line in audited:
            assert line.assigned_model == DEFAULT_PLANNER
            assert "allow_reserved_code" in line.assignment_audit
            assert DEFAULT_PLANNER in line.assignment_audit
        assert any("dispatch assignment audit" in record.message for record in caplog.records)

        target = tmp_path / planner.last_result.spec_ref.removeprefix("spec:")
        stored = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        archived = {
            item["name"]: item["assigned_model"]
            for item in stored["lines"]
            if item["assigned_model"] == DEFAULT_PLANNER
        }
        assert archived, "the archive does not show which line used the override"
        assert all(
            "allow_reserved_code" in item["assignment_audit"]
            for item in stored["lines"]
            if item["assigned_model"] == DEFAULT_PLANNER
        )

    async def test_a_default_policy_audits_nothing(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"), archive=archive)
        lines = await planner.plan("add a dispatch status summary endpoint")

        assert all(line.assignment_audit is None for line in lines)
        target = tmp_path / planner.last_result.spec_ref.removeprefix("spec:")
        stored = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        assert all(item["assigned_model"] != "claude" for item in stored["lines"])

    async def test_an_unassigned_line_fails_the_plan_with_every_reason(self):
        policy = RolePolicy(developer_pool=("kimi",))
        reply = _reply(*[_line(f"n{i}") for i in range(7)])
        planner, _ = _planner(reply, policy=policy)
        with pytest.raises(DispatchError) as caught:
            await planner.plan("goal")

        # N28: seven lines for one developer place are refused before any
        # assignment, with the fixed code.
        message = str(caught.value)
        assert "exceeds_developer_pool:7>1" in message
        assert caught.value.reason == "exceeds_developer_pool"

    async def test_an_unassigned_dependency_blocks_its_child(self):
        policy = RolePolicy(developer_pool=("kimi",))
        reply = _reply(
            _line("first", write_scope=["src/a.py"]),
            _line("second", write_scope=["src/b.py"], depends_on=["first"]),
            _line("third", write_scope=["src/c.py"], depends_on=["second"]),
        )
        # Every line holds a place up front, so a chain of three lines does
        # not fit one developer (N28); before, line-2 came back
        # dependency_unassigned.
        planner, _ = _planner(reply, policy=policy)
        with pytest.raises(DispatchError, match="exceeds_developer_pool:3>1"):
            await planner.plan("goal")

    async def test_the_validator_reports_the_unassigned_list(self):
        policy = RolePolicy(developer_pool=("kimi",))
        outcome = validate_plan(
            json.loads(_reply(*[_line(f"n{i}") for i in range(3)])),
            policy=policy,
            agents=(AgentState(provider="kimi", capacity=0),),
        )

        assert not isinstance(outcome, PlanRejection)
        assert [item.task_id for item in outcome.assignment.unassigned] == [
            "line-0",
            "line-1",
            "line-2",
        ]
        assert {item.reason for item in outcome.assignment.unassigned} == {"no_capacity"}

    async def test_a_large_low_risk_line_reaches_the_widest_headroom(self):
        # The planner's estimated_size is a task-size hint independent of
        # risk: a large, non-risk-bearing line must still prefer the provider
        # with the most remaining quota. Without the field flowing through the
        # Line, only risk_bearing made a line large, so roster order (kimi)
        # would win a tie that a large task should send to cursor (0.95 over
        # 0.60).
        class _FakeHeadroom:
            def __init__(self, fractions):
                self._fractions = fractions

            async def remaining(self, model):
                return self._fractions.get(model, 1.0)

        policy = RolePolicy(developer_pool=("kimi", "cursor"))
        agents = [
            AgentState(provider="kimi", capacity=1, success_rate=0.9),
            AgentState(provider="cursor", capacity=1, success_rate=0.1),
        ]
        headroom = _FakeHeadroom({"kimi": 0.60, "cursor": 0.95})

        large, _ = _planner(
            _reply(_line("big", estimated_size="large")),
            policy=policy,
            agents=agents,
            headroom=headroom,
        )
        result = await large.plan_dag("goal")
        assert result.assignment.provider_of("line-0") == "cursor"

        # The mirror: the same line with no estimate is an ordinary medium
        # task, where a proven developer (kimi) still wins the healthy-tier
        # tie -- so it is the "large" estimate, not headroom alone, that sends
        # the work to the widest quota.
        medium, _ = _planner(
            _reply(_line("big")),
            policy=policy,
            agents=agents,
            headroom=headroom,
        )
        result = await medium.plan_dag("goal")
        assert result.assignment.provider_of("line-0") == "kimi"


class _FailingArchive:
    async def save(self, record: SpecRecord) -> str:
        raise OSError("disk is full")


class TestSpecArchive:
    async def test_the_archive_holds_the_spec_and_the_plan_only(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"), archive=archive)
        await planner.plan("add a dispatch status summary endpoint")

        target = tmp_path / planner.last_result.spec_ref.removeprefix("spec:")
        spec_markdown = (target / "SPEC.md").read_text(encoding="utf-8")
        plan_text = (target / "plan.json").read_text(encoding="utf-8")
        plan_json = json.loads(plan_text)

        assert spec_markdown.startswith("# Add a dispatch status summary endpoint")
        assert "REPLY-SECRET" not in plan_text
        assert set(plan_json) == {
            "schema_version",
            "prompt_version",
            "spec",
            "lines",
            "assignment",
            "risk_bearing",
        }
        assert set(plan_json["lines"][0]) == {
            "id",
            "name",
            "task",
            "depends_on",
            "write_scope",
            "declared_paths",
            "acceptance",
            "risk_bearing",
            "estimated_size",
            "assigned_model",
            "assignment_audit",
        }
        assert plan_json["risk_bearing"] == ["line-1"]

    async def test_nothing_else_is_archived(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"), archive=archive)
        goal = "add a dispatch status summary endpoint"
        await planner.plan(goal)

        stored = "".join(
            path.read_text(encoding="utf-8") for path in sorted(tmp_path.rglob("*")) if path.is_file()
        )
        for forbidden in (
            "You are the planner",  # the system prompt
            "Decompose this goal",  # the user prompt
            goal,  # the goal itself
            "reasoning",
            "usage",
            "token_count",
        ):
            assert forbidden not in stored, forbidden

    async def test_archiving_the_same_plan_twice_is_idempotent(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        first, _ = _planner(FIXTURE.read_text(encoding="utf-8"), archive=archive)
        second, _ = _planner(FIXTURE.read_text(encoding="utf-8"), archive=archive)

        await first.plan("goal")
        reference = first.last_result.spec_ref
        await second.plan("goal")

        assert second.last_result.spec_ref == reference
        assert len(list(tmp_path.iterdir())) == 1
        assert not list(tmp_path.rglob("*.tmp"))

    def test_two_threads_archiving_the_same_plan_both_succeed(self, tmp_path):
        # Each save gets its own temporary file, so two concurrent writers of
        # the same plan cannot rename each other's file away.
        import asyncio
        import threading

        from src.tools.auto_dispatch.planning import Spec

        archive = FileSpecArchive(tmp_path)
        payload = _payload(_line("a"))
        result = validate_plan(payload)
        record = SpecRecord(
            spec=Spec(title="t", body="b"),
            lines=result.lines,
            assignment=result.assignment,
            risk_bearing=result.risk_bearing,
            schema_version=result.schema_version,
        )
        references: list[str] = []
        errors: list[BaseException] = []

        def save_once() -> None:
            async def main() -> None:
                references.append(await archive.save(record))

            try:
                asyncio.run(main())
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=save_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(references) == 2
        assert references[0] == references[1]
        target = tmp_path / references[0].removeprefix("spec:")
        assert (target / "SPEC.md").read_text(encoding="utf-8").startswith("# t")
        assert json.loads((target / "plan.json").read_text(encoding="utf-8"))["lines"]
        # no temporary file is left behind
        assert [path.name for path in target.iterdir() if path.name.endswith(".tmp")] == []

    async def test_an_archive_failure_is_a_plan_failure(self):
        planner, _ = _planner(
            FIXTURE.read_text(encoding="utf-8"), archive=_FailingArchive()
        )

        with pytest.raises(DispatchError, match="spec_archive_failed"):
            await planner.plan("goal")

    async def test_no_archive_means_no_disk_writes(self, tmp_path):
        planner, _ = _planner(FIXTURE.read_text(encoding="utf-8"))
        await planner.plan("goal")

        assert list(tmp_path.iterdir()) == []
        assert planner.last_result.spec_ref == ""


class TestDeterminism:
    async def test_the_same_reply_plans_identically(self):
        reply = FIXTURE.read_text(encoding="utf-8")
        first, _ = _planner(reply)
        second, _ = _planner(reply)

        left = await first.plan("goal")
        right = await second.plan("goal")

        assert [line.to_dict() for line in left] == [line.to_dict() for line in right]
        assert first.last_result.spec_ref == second.last_result.spec_ref

    async def test_the_spec_id_depends_only_on_the_plan(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        record = SpecRecord(
            spec=Spec(title="t", body="b"),
            lines=(),
            assignment=validate_plan(_payload(_line("a"))).assignment,
            risk_bearing=frozenset(),
            schema_version=1,
        )

        assert archive.spec_id(record) == archive.spec_id(record)
        assert len(archive.spec_id(record)) == 16


class _EventLauncher:
    """Records every launch with the model it was handed, in order."""

    def __init__(self, events: list[tuple[str, ...]]) -> None:
        self._events = events
        self._counts: dict[str, int] = {}

    async def launch(self, line, model: str, skills: list[str]) -> str:
        seen = self._counts.get(line.id, 0)
        self._counts[line.id] = seen + 1
        self._events.append(("launch", line.id, model))
        return f"run-{line.id}-{seen + 1}"


class _EventMonitor:
    """Records a completion event for every run it reports done."""

    def __init__(self, events: list[tuple[str, ...]]) -> None:
        self._events = events

    async def poll(self, run_id: str):
        from src.tools.auto_dispatch.types import RunStatus

        self._events.append(("poll", run_id))
        return RunStatus(output="done", complete=True, tokens=1)

    async def cancel(self, run_id: str) -> None:
        self._events.append(("cancel", run_id))


class _EventLauncher:
    """Records every launch with the model it was handed, in order."""

    def __init__(self, events: list[tuple[str, ...]]) -> None:
        self._events = events
        self._counts: dict[str, int] = {}

    async def launch(self, line, model: str, skills: list[str]) -> str:
        seen = self._counts.get(line.id, 0)
        self._counts[line.id] = seen + 1
        self._events.append(("launch", line.id, model))
        return f"run-{line.id}-{seen + 1}"


class _EventMonitor:
    """Records a poll for every run, so completion order is observable."""

    def __init__(self, events: list[tuple[str, ...]]) -> None:
        self._events = events

    async def poll(self, run_id: str):
        from src.tools.auto_dispatch.types import RunStatus

        self._events.append(("poll", run_id))
        return RunStatus(output="done", complete=True, tokens=1)

    async def cancel(self, run_id: str) -> None:
        self._events.append(("cancel", run_id))


def _assert_launched_in_dag_order(events: list[tuple[str, ...]]) -> None:
    """The planner-to-machine linkage contract, over a launch/poll event stream.

    Every line the planner placed is started, with exactly the model the role
    policy chose for it, and a dependent line starts only after the run it
    waits on has been polled to completion.
    """
    launches = [event for event in events if event[0] == "launch"]
    polls = [event for event in events if event[0] == "poll"]
    models = {event[1]: event[2] for event in launches}
    assert models == {"line-0": "kimi", "line-1": "glm"}, models
    assert [event[1] for event in launches] == ["line-0", "line-1"]
    assert polls, "no run was ever polled, so nothing completed"
    assert events.index(polls[0]) < events.index(("launch", "line-1", "glm")), (
        "line-1 depends on line-0 and must not start before line-0 finished"
    )


class TestPlannerMachineLink:
    """The planner's pre-assigned lines have to reach the launcher, in DAG order.

    W3 owns the machine's state advance and dependency order (blocker B1):
    ``_plan`` resets every line to PENDING and ``_launch`` only starts lines
    that are ASSIGNED, so a line the planner already placed is never launched,
    and every line is started in one pass regardless of its dependencies.

    What W4 guarantees on its own -- that ``plan()`` returns lines carrying the
    model the role policy chose -- is asserted in ``TestCanonicalSample`` and
    ``TestSpecArchive``; those pass. Everything here needs W3, so this is one
    test marked ``xfail(strict=False)``:

    * ``strict=False`` so that when W3 lands the DAG order the test turns into a
      plain pass instead of an XPASS(strict) failure;
    * the reason names the marker for removal.

    The body runs either way, which is what makes it a real contract: under the
    mutation Codex used (every line pre-set to ASSIGNED with model ``minimax``)
    it fails on the exact-model assertion rather than on "nothing launched", so
    the test does distinguish the two.
    """

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "W3 blocker B1: machine._plan resets a pre-assigned line to PENDING "
            "and machine._launch only starts ASSIGNED lines, so a planner-assigned "
            "line is never launched and every line starts in one pass regardless "
            "of its dependencies. Remove this marker once W3 lands the state "
            "advance and the DAG order."
        ),
    )
    async def test_a_pre_assigned_line_is_launched_in_dag_order(self, tmp_path):
        from src.tools.auto_dispatch import (
            AlwaysPassReviewer,
            Collaborators,
            Dispatch,
            DispatchMachine,
            FileApprovalGate,
            InMemoryDispatchStore,
            InMemoryOutcomeStore,
            NoopMerger,
            RoundRobinSelector,
        )

        events: list[tuple[str, ...]] = []

        async def complete(prompt: str, system: str) -> str:
            return FIXTURE.read_text(encoding="utf-8")

        collaborators = Collaborators(
            planner=LLMPlanner(complete),
            selector=RoundRobinSelector(),
            launcher=_EventLauncher(events),
            monitor=_EventMonitor(events),
            reviewer=AlwaysPassReviewer(),
            merger=NoopMerger(),
            gate=FileApprovalGate(tmp_path / "proposed"),
            outcomes=InMemoryOutcomeStore(),
            store=InMemoryDispatchStore(),
        )

        async def _no_sleep(_seconds: float) -> None:
            return None

        machine = DispatchMachine(collaborators, sleep=_no_sleep)
        await machine.run(
            Dispatch(id="d1", goal="add a status endpoint", user_id="u", created_at="now")
        )

        _assert_launched_in_dag_order(events)


class TestProtocol:
    async def test_plan_still_satisfies_the_planner_protocol(self):
        from src.tools.auto_dispatch.protocols import Planner

        planner, _ = _planner(_reply(_line("a")))

        assert isinstance(planner, Planner)
        lines = await planner.plan("goal")
        assert isinstance(lines, list)
        assert all(isinstance(line, Line) for line in lines)


VECTORS = json.loads(
    (Path(__file__).resolve().parents[3] / "fixtures" / "write_scope_overlap_vectors_v1.json").read_text(
        encoding="utf-8"
    )
)


class TestWriteScopeVectors:
    """The shared vector file, exercised through the planner validator.

    The same file drives the assignment stage (``test_selection_dag.py``), so
    the validator and ``assign_dag`` cannot drift apart on what overlaps.
    """

    @pytest.mark.parametrize("first, second", VECTORS["overlap"])
    def test_an_overlapping_pair_is_refused(self, first, second):
        payload = _payload(
            _line("first", write_scope=[first]),
            _line("second", write_scope=[second]),
        )

        assert validate_plan(payload).code == "write_scope_conflict:first:second"

    @pytest.mark.parametrize("first, second", VECTORS["disjoint"])
    def test_a_disjoint_pair_is_accepted(self, first, second):
        payload = _payload(
            _line("first", write_scope=[first]),
            _line("second", write_scope=[second]),
        )

        assert not isinstance(validate_plan(payload), PlanRejection)

    @pytest.mark.parametrize("scope", VECTORS["rejected"])
    def test_a_rejected_scope_is_refused(self, scope):
        payload = _payload(_line("one", write_scope=[scope]))

        # An empty or whitespace-only entry is reported as an empty scope
        # before the boundary check, which is the PRD's order; everything else
        # is an invalid scope.
        expected = "empty_write_scope" if not scope.strip() else "invalid_write_scope"
        assert validate_plan(payload).code == f"{expected}:one"

    def test_an_empty_write_scope_list_is_refused(self):
        payload = _payload(_line("one", write_scope=[]))

        assert validate_plan(payload).code == "empty_write_scope:one"

    @pytest.mark.parametrize(
        "scopes",
        [
            ["src/a.py", ""],
            ["", "src/a.py"],
            ["src/a.py", " ", "src/b.py"],
            ["src/a.py", "\t"],
            ["src/a.py", "src/\x00b.py"],
        ],
    )
    def test_a_blank_or_invalid_member_refuses_the_whole_line(self, scopes):
        # No item is silently dropped: the first failing member refuses the
        # line, and the reason is that member's.
        payload = _payload(_line("one", write_scope=scopes))

        expected = (
            "empty_write_scope"
            if all(not str(item).strip() for item in scopes if isinstance(item, str))
            or any(isinstance(item, str) and not item.strip() for item in scopes)
            else "invalid_write_scope"
        )
        if any(isinstance(item, str) and "\x00" in item for item in scopes):
            # a NUL is uncarriable text, reported against the line
            assert validate_plan(payload).code == "invalid_text:one"
            return
        assert validate_plan(payload).code == f"{expected}:one"

    def test_a_lone_surrogate_in_a_write_scope_is_refused(self):
        # The archive writes the scope as UTF-8, so text no encoder can carry
        # must be refused here rather than surfacing as an archive failure --
        # and as uncarriable text, not as an oversized one.
        from src.tools.auto_dispatch.planning import check_write_scope

        values, reason = check_write_scope(["src/\ud800x.py"])

        assert values is None
        assert reason == "invalid_text"
        assert validate_plan(
            _payload(_line("one", write_scope=["src/\ud800x.py"]))
        ).code == "invalid_text:one"

    def test_a_non_string_scope_item_is_refused(self):
        from src.tools.auto_dispatch.planning import check_write_scope

        for raw in (7, None, True, ["src/a.py"], {"a": 1}):
            values, reason = check_write_scope([raw])

            assert values is None, raw
            assert reason == "invalid_text", raw

    @pytest.mark.parametrize("scope", VECTORS["rejected_items"])
    def test_every_rejected_item_is_refused(self, scope):
        payload = _payload(_line("one", write_scope=[scope]))

        if "\x00" in scope:
            # a NUL is uncarriable text, reported against the line
            assert validate_plan(payload).code == "invalid_text:one"
            return
        expected = "empty_write_scope" if not scope.strip() else "invalid_write_scope"
        assert validate_plan(payload).code == f"{expected}:one"

    @pytest.mark.parametrize("scope", VECTORS["rejected_whitespace"])
    def test_a_scope_invalid_only_for_its_whitespace_is_refused(self, scope):
        # The trimmed spelling is the value that gets stored, so it is the value
        # that must be checked: a leading space cannot launder an absolute path
        # or a ".." segment past the validator.
        payload = _payload(_line("one", write_scope=[scope]))

        assert validate_plan(payload).code == "invalid_write_scope:one"

    @pytest.mark.parametrize("padding", [" ", "\t", "\n", " \t\n "])
    @pytest.mark.parametrize("body", ["src/../x.py", "/etc/x", "C:\\x", "~/secret"])
    def test_padding_never_launders_a_scope(self, padding, body):
        for padded in (f"{padding}{body}", f"{body}{padding}", f"{padding}{body}{padding}"):
            payload = _payload(_line("one", write_scope=[padded]))

            assert validate_plan(payload).code == "invalid_write_scope:one", padded

    @pytest.mark.parametrize("padding", [" ", "\t", "\n"])
    def test_a_padded_valid_scope_is_stored_trimmed(self, padding):
        payload = _payload(_line("one", write_scope=[f"{padding}src/a.py{padding}"]))

        outcome = validate_plan(payload)

        assert not isinstance(outcome, PlanRejection)
        assert outcome.lines[0].write_scope == ("src/a.py",)

    def test_the_vector_file_covers_every_row_of_the_spec(self):
        assert len(VECTORS["overlap"]) == 12
        assert len(VECTORS["disjoint"]) == 4
        assert len(VECTORS["rejected"]) == 8


class TestStoredScopeIsTheValidatedOne:
    """The third layer: what a model reply becomes once it is a Line.

    A padded scope is trimmed before it is checked, and the Line carries that
    trimmed spelling, so the archive and the assignment claim see exactly what
    was validated.
    """

    async def test_a_padded_scope_is_stored_trimmed_and_assigned(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        reply = _reply(
            _line("one", write_scope=["  src/server/app/x.py\t"]),
            _line("two", write_scope=["tests/unit/server/app/x.py"]),
        )
        planner, _ = _planner(reply, archive=archive)
        lines = await planner.plan("goal")

        assert [line.write_scope for line in lines] == [
            ("src/server/app/x.py",),
            ("tests/unit/server/app/x.py",),
        ]
        target = tmp_path / planner.last_result.spec_ref.removeprefix("spec:")
        stored = json.loads((target / "plan.json").read_text(encoding="utf-8"))
        assert [item["write_scope"] for item in stored["lines"]] == [
            ["src/server/app/x.py"],
            ["tests/unit/server/app/x.py"],
        ]

    @pytest.mark.parametrize(
        "scopes",
        [[], ["src/a.py", ""], ["", "src/a.py"], ["src/a.py", " ", "src/b.py"], ["src/\x00x.py"]],
    )
    async def test_a_line_with_a_bad_scope_is_never_archived(self, tmp_path, scopes):
        archive = FileSpecArchive(tmp_path)
        reply = _reply(_line("one", write_scope=scopes))
        planner, _ = _planner(reply, archive=archive)

        with pytest.raises(DispatchError):
            await planner.plan("goal")

        assert planner.last_result is None
        assert list(tmp_path.iterdir()) == []

    async def test_a_padded_escaping_scope_never_reaches_a_line(self, tmp_path):
        archive = FileSpecArchive(tmp_path)
        reply = _reply(_line("one", write_scope=[" ../outside.py"]))
        planner, _ = _planner(reply, archive=archive)

        with pytest.raises(DispatchError, match="invalid_write_scope:one"):
            await planner.plan("goal")

        # nothing was archived, and nothing is readable as a stored plan
        assert planner.last_result is None
        assert list(tmp_path.iterdir()) == []


class TestDagLaunchContract:
    """The linkage assertions themselves, against a synthetic event stream.

    The end-to-end test above needs W3's machine before it can run, so the
    contract is proven here instead: the same assertions accept a correct
    stream and reject the substitution Codex probed with (every line launched
    with ``minimax``), and reject a stream that starts the dependent line
    before its prerequisite finished.
    """

    def test_a_correct_stream_passes(self):
        _assert_launched_in_dag_order(
            [
                ("launch", "line-0", "kimi"),
                ("poll", "run-line-0-1"),
                ("launch", "line-1", "glm"),
                ("poll", "run-line-1-1"),
            ]
        )

    def test_a_substituted_model_is_rejected(self):
        with pytest.raises(AssertionError, match="minimax"):
            _assert_launched_in_dag_order(
                [
                    ("launch", "line-0", "minimax"),
                    ("poll", "run-line-0-1"),
                    ("launch", "line-1", "minimax"),
                    ("poll", "run-line-1-1"),
                ]
            )

    def test_a_dependent_line_started_too_early_is_rejected(self):
        with pytest.raises(AssertionError, match="must not start before"):
            _assert_launched_in_dag_order(
                [
                    ("launch", "line-0", "kimi"),
                    ("launch", "line-1", "glm"),
                    ("poll", "run-line-0-1"),
                    ("poll", "run-line-1-1"),
                ]
            )


#: The first-failure order the Planner's algorithm pins. Every layer must give
#: the same reason for the same list.
FIRST_FAILURE_VECTORS: tuple[tuple[list[object], str], ...] = (
    (["", "a\x00"], "empty_write_scope"),
    (["../x", "a\x00"], "invalid_write_scope"),
    (["", "\ud800"], "empty_write_scope"),
    (["a\x00", ""], "invalid_text"),
    (["\ud800"], "invalid_text"),
    ([f"item-{index}" for index in range(33)], "line_too_large"),
    (["y" * 513], "line_too_large"),
)


class TestFirstFailureOrder:
    """One scope check, one first-failure reason, at every layer.

    A later item's ``invalid_text`` must not overtake an earlier item's reason:
    the whole list is judged in order and the first failing item decides.
    """

    @pytest.mark.parametrize("items, expected", FIRST_FAILURE_VECTORS)
    def test_the_validator_reports_the_first_failing_item(self, items, expected):
        payload = _payload(_line("one", write_scope=items))

        assert validate_plan(payload).code == f"{expected}:one"

    @pytest.mark.parametrize("items, expected", FIRST_FAILURE_VECTORS)
    def test_the_direct_assignment_reports_the_same_reason(self, items, expected):
        from src.tools.auto_dispatch.selection import assign_dag, dag_agents_from_policy
        from src.tools.auto_dispatch.types import Line

        plan = assign_dag(
            [Line(id="line-0", name="one", task="do", write_scope=tuple(items))],
            dag_agents_from_policy(),
        )

        assert plan.assignments == ()
        assert [(item.task_id, item.reason) for item in plan.unassigned] == [
            ("line-0", f"{expected}:line-0")
        ]

    @pytest.mark.parametrize("items, expected", FIRST_FAILURE_VECTORS)
    async def test_the_fake_planner_refuses_and_archives_nothing(
        self, tmp_path, items, expected
    ):
        archive = FileSpecArchive(tmp_path)
        reply = _reply(_line("one", write_scope=items))
        planner, _ = _planner(reply, archive=archive)

        with pytest.raises(DispatchError, match=f"{expected}:one"):
            await planner.plan("goal")

        assert planner.last_result is None
        assert list(tmp_path.iterdir()) == []

    def test_the_three_layers_agree_on_every_vector(self):
        from src.tools.auto_dispatch.planning import check_write_scope
        from src.tools.auto_dispatch.selection import assign_dag, dag_agents_from_policy
        from src.tools.auto_dispatch.types import Line

        for items, expected in FIRST_FAILURE_VECTORS:
            _values, reason = check_write_scope(items)
            assert reason == expected, items
            plan = assign_dag(
                [Line(id="line-0", name="one", task="do", write_scope=tuple(items))],
                dag_agents_from_policy(),
            )
            assert plan.unassigned[0].reason == f"{expected}:line-0", items
            assert validate_plan(_payload(_line("one", write_scope=items))).code == (
                f"{expected}:one"
            ), items


def _providers_by_name(outcome: object) -> dict[str, str]:
    names = {line.id: line.name for line in outcome.lines}
    return {
        names[item.task_id]: item.provider for item in outcome.assignment.assignments
    }


def _reasons_by_name(outcome: object) -> dict[str, str]:
    names = {line.id: line.name for line in outcome.lines}
    return {
        names[item.task_id]: item.reason for item in outcome.assignment.unassigned
    }


# name -> ("invalid_write_scope", None) or ("missing_dependency", parent name)
_FilteredReason = tuple[str, str | None]


def _filtered_graphs() -> list[dict[str, object]]:
    """The five graphs, as planner payloads. ``keep`` is the unrelated lines."""
    illegal = ["/etc/passwd"]
    other = ["../outside"]
    return [
        {
            "name": "single parent",
            "lines": [
                _line("p", write_scope=illegal),
                _line("c", depends_on=["p"]),
                _line("u"),
            ],
            "expected": {
                "p": ("invalid_write_scope", None),
                "c": ("missing_dependency", "p"),
            },
            "keep": ("u",),
        },
        {
            "name": "chain",
            "lines": [
                _line("p", write_scope=illegal),
                _line("c", depends_on=["p"]),
                _line("d", depends_on=["c"]),
                _line("u"),
            ],
            "expected": {
                "p": ("invalid_write_scope", None),
                "c": ("missing_dependency", "p"),
                "d": ("missing_dependency", "p"),
            },
            "keep": ("u",),
        },
        {
            "name": "diamond",
            "lines": [
                _line("p", write_scope=illegal),
                _line("a", depends_on=["p"]),
                _line("b", depends_on=["p"]),
                _line("d", depends_on=["a", "b"]),
                _line("u"),
            ],
            "expected": {
                "p": ("invalid_write_scope", None),
                "a": ("missing_dependency", "p"),
                "b": ("missing_dependency", "p"),
                "d": ("missing_dependency", "p"),
            },
            "keep": ("u",),
        },
        {
            "name": "two independent parents",
            "lines": [
                _line("p1", write_scope=illegal),
                _line("p2", write_scope=other),
                _line("c1", depends_on=["p1"]),
                _line("c2", depends_on=["p2"]),
                _line("m", depends_on=["p1", "p2"]),
                _line("n", depends_on=["p2", "p1"]),
                _line("u"),
            ],
            "expected": {
                "p1": ("invalid_write_scope", None),
                "p2": ("invalid_write_scope", None),
                "c1": ("missing_dependency", "p1"),
                "c2": ("missing_dependency", "p2"),
                "m": ("missing_dependency", "p1"),
                "n": ("missing_dependency", "p2"),
            },
            "keep": ("u",),
        },
        {
            "name": "unrelated lines",
            "lines": [
                _line("p", write_scope=illegal),
                _line("c", depends_on=["p"]),
                _line("u"),
                _line("v"),
            ],
            "expected": {
                "p": ("invalid_write_scope", None),
                "c": ("missing_dependency", "p"),
            },
            "keep": ("u", "v"),
            # Two places for the two kept lines: a filtered family takes none.
            "policy": RolePolicy(developer_pool=("kimi", "grok")),
        },
    ]


def _assert_filtered_graph(shape: dict[str, object]) -> object:
    """U matches the plan that never contained the filtered family."""
    lines = shape["lines"]
    expected: dict[str, _FilteredReason] = shape["expected"]  # type: ignore[assignment]
    keep: tuple[str, ...] = shape["keep"]  # type: ignore[assignment]
    policy = shape.get("policy") or RolePolicy.default()
    outcome = validate_plan(_payload(*lines), policy=policy)  # type: ignore[arg-type]
    assert not isinstance(outcome, PlanRejection)
    assert validate_plan(_payload(*lines), policy=policy).assignment == outcome.assignment  # type: ignore[arg-type]

    payload_ids = {
        entry["name"]: f"line-{index}" for index, entry in enumerate(lines)  # type: ignore[index, arg-type]
    }
    present = {line.name for line in outcome.lines}
    reasons = {item.task_id: item.reason for item in outcome.assignment.unassigned}
    for name, (kind, parent) in expected.items():
        line_id = payload_ids[name]
        if kind == "invalid_write_scope":
            # A filtered line is not a Line. Only its id and reason remain.
            assert name not in present
            assert reasons[line_id] == f"invalid_write_scope:{line_id}"
        else:
            assert parent is not None
            assert name in present
            assert reasons[line_id] == f"missing_dependency:{payload_ids[parent]}"
    kept = [line for line in lines if line["name"] in keep]  # type: ignore[index, union-attr]
    alone = validate_plan(_payload(*kept), policy=policy)  # type: ignore[arg-type]
    assert not isinstance(alone, PlanRejection)
    assert _providers_by_name(outcome) == _providers_by_name(alone)
    alone_reasons = _reasons_by_name(alone)
    present_reasons = {
        line.name: reasons[line.id] for line in outcome.lines if line.id in reasons
    }
    for name in keep:
        assert present_reasons.get(name) == alone_reasons.get(name)
    extra = set(reasons) - {payload_ids[name] for name in expected} - {
        payload_ids[name] for name in keep
    }
    assert extra == set()
    return outcome


class TestFilteredDependencyDoesNotPoisonAnUnrelatedLine:
    """Validator and fake planner: the same five graphs as assign_dag.

    A plan that is only an illegal scope is still a ``PlanRejection`` (see the
    cases above). These payloads each have a legal line, so the illegal parent
    is filtered instead of taking the siblings with it.
    """

    @pytest.mark.parametrize("shape", _filtered_graphs(), ids=lambda shape: shape["name"])
    async def test_the_validator_keeps_an_unrelated_line(
        self, shape: dict[str, object]
    ) -> None:
        _assert_filtered_graph(shape)

    @pytest.mark.parametrize("shape", _filtered_graphs(), ids=lambda shape: shape["name"])
    async def test_the_fake_planner_keeps_an_unrelated_line(
        self, shape: dict[str, object]
    ) -> None:
        outcome = _assert_filtered_graph(shape)
        policy = shape.get("policy") or RolePolicy.default()
        planner, _calls = _planner(_reply(*shape["lines"]), policy=policy)  # type: ignore[arg-type]
        planned = await planner.plan_dag("goal")

        assert planned.assignment == outcome.assignment

    @pytest.mark.parametrize("shape", _filtered_graphs(), ids=lambda shape: shape["name"])
    async def test_plan_names_the_filtered_lines_and_not_an_extra_one(
        self, shape: dict[str, object]
    ) -> None:
        outcome = _assert_filtered_graph(shape)
        policy = shape.get("policy") or RolePolicy.default()
        planner, _calls = _planner(_reply(*shape["lines"]), policy=policy)  # type: ignore[arg-type]

        with pytest.raises(DispatchError) as caught:
            await planner.plan("goal")

        assert planner.last_result is not None
        assert planner.last_result.assignment == outcome.assignment
        assert str(caught.value) == (
            "Planner could not assign every line: "
            + ", ".join(planner.last_result.unassigned_reasons())
            + "; refine the goal"
        )


def _parent_with(defect: str) -> dict[str, object]:
    """An illegal scope plus one structural defect. The scope fails first."""
    parent = _line("p", write_scope=["/etc/passwd"])
    if defect == "missing task":
        del parent["task"]
    elif defect == "task type":
        parent["task"] = 7
    elif defect == "risk":
        parent["risk_bearing"] = "yes"
    elif defect == "unknown":
        parent["note"] = "not a field"
    elif defect == "acceptance":
        parent["acceptance"] = []
    else:
        raise AssertionError(defect)
    return parent


class TestFilteredLineKeepsOnlyItsIdAndReason:
    """B1: a filtered line's other fields are not read, and U is still assigned."""

    @pytest.mark.parametrize(
        "defect",
        ["missing task", "task type", "risk", "unknown", "acceptance"],
    )
    async def test_the_validator_does_not_raise_on_a_broken_filtered_line(
        self, defect: str
    ) -> None:
        outcome = validate_plan(_payload(_parent_with(defect), _line("u")))

        assert not isinstance(outcome, PlanRejection)
        assert [line.name for line in outcome.lines] == ["u"]
        assert [(item.task_id, item.reason) for item in outcome.assignment.unassigned] == [
            ("line-0", "invalid_write_scope:line-0")
        ]
        assert [item.task_id for item in outcome.assignment.assignments] == ["line-1"]

    async def test_a_normal_illegal_scope_is_the_same_shape(self) -> None:
        outcome = validate_plan(
            _payload(_line("p", write_scope=["/etc/passwd"]), _line("u"))
        )

        assert not isinstance(outcome, PlanRejection)
        assert [line.name for line in outcome.lines] == ["u"]
        assert [(item.task_id, item.reason) for item in outcome.assignment.unassigned] == [
            ("line-0", "invalid_write_scope:line-0")
        ]
        assert [item.task_id for item in outcome.assignment.assignments] == ["line-1"]

    @pytest.mark.parametrize(
        "defect",
        ["missing task", "task type", "risk", "unknown", "acceptance"],
    )
    async def test_the_fake_planner_returns_the_scope_reason(
        self, defect: str
    ) -> None:
        planner, _calls = _planner(_reply(_parent_with(defect), _line("u")))

        with pytest.raises(DispatchError, match="invalid_write_scope:line-0"):
            await planner.plan("goal")

        assert planner.last_result is not None
        assert [line.name for line in planner.last_result.lines] == ["u"]
        assert [item.task_id for item in planner.last_result.assignment.assignments] == [
            "line-1"
        ]

    async def test_the_fake_planner_control_matches(self) -> None:
        planner, _calls = _planner(
            _reply(_line("p", write_scope=["/etc/passwd"]), _line("u"))
        )

        with pytest.raises(DispatchError, match="invalid_write_scope:line-0"):
            await planner.plan("goal")

        assert planner.last_result is not None
        assert [item.task_id for item in planner.last_result.assignment.assignments] == [
            "line-1"
        ]


class TestFilteredScopeDoesNotEnterTheArchive:
    """B2: a lone surrogate in a filtered scope never reaches the archive hash."""

    async def test_a_surrogate_scope_archives_invalid_text(self, tmp_path) -> None:
        archive = FileSpecArchive(tmp_path)
        reply = _reply(_line("p", write_scope=["\ud800"]), _line("u"))
        first, _calls = _planner(reply, archive=archive)

        with pytest.raises(DispatchError, match="invalid_text:line-0"):
            await first.plan("goal")

        assert first.last_result is not None
        assert [line.name for line in first.last_result.lines] == ["u"]
        assert [item.task_id for item in first.last_result.assignment.assignments] == [
            "line-1"
        ]
        stored = tmp_path / first.last_result.spec_ref.removeprefix("spec:")
        plan_text = (stored / "plan.json").read_text(encoding="utf-8")
        assert "invalid_text:line-0" in plan_text
        assert "\\ud800" not in plan_text

        second, _calls = _planner(reply, archive=archive)
        with pytest.raises(DispatchError, match="invalid_text:line-0"):
            await second.plan("goal")

        assert second.last_result is not None
        assert second.last_result.spec_ref == first.last_result.spec_ref
        assert len(list(tmp_path.iterdir())) == 1

    async def test_a_normal_illegal_path_archives_the_same_way(self, tmp_path) -> None:
        archive = FileSpecArchive(tmp_path)
        reply = _reply(_line("p", write_scope=["/outside.py"]), _line("u"))
        first, _calls = _planner(reply, archive=archive)

        with pytest.raises(DispatchError, match="invalid_write_scope:line-0"):
            await first.plan("goal")

        assert first.last_result is not None
        assert [line.name for line in first.last_result.lines] == ["u"]
        stored = tmp_path / first.last_result.spec_ref.removeprefix("spec:")
        plan_text = (stored / "plan.json").read_text(encoding="utf-8")
        assert "invalid_write_scope:line-0" in plan_text
        assert "/outside.py" not in plan_text

        second, _calls = _planner(reply, archive=archive)
        with pytest.raises(DispatchError, match="invalid_write_scope:line-0"):
            await second.plan("goal")

        assert second.last_result is not None
        assert second.last_result.spec_ref == first.last_result.spec_ref


class TestOverlapIgnoresLinesThatCannotBeAssigned:
    """A child of a filtered line does not conflict with an unrelated line."""

    def _payload(self) -> dict[str, object]:
        return _payload(
            _line("p", write_scope=["/etc/passwd"]),
            _line("c", depends_on=["p"], write_scope=["src/shared.py"]),
            _line("u", write_scope=["src/shared.py"]),
        )

    async def test_the_validator_assigns_the_unrelated_line(self) -> None:
        outcome = validate_plan(self._payload())

        assert not isinstance(outcome, PlanRejection)
        assert [line.name for line in outcome.lines] == ["c", "u"]
        assert [(item.task_id, item.reason) for item in outcome.assignment.unassigned] == [
            ("line-1", "missing_dependency:line-0"),
            ("line-0", "invalid_write_scope:line-0"),
        ]
        assert [item.task_id for item in outcome.assignment.assignments] == ["line-2"]
        assert all(
            "write_scope_conflict" not in item.reason
            for item in outcome.assignment.unassigned
        )

    async def test_the_fake_planner_assigns_the_unrelated_line(self) -> None:
        planner, _calls = _planner(_reply(
            _line("p", write_scope=["/etc/passwd"]),
            _line("c", depends_on=["p"], write_scope=["src/shared.py"]),
            _line("u", write_scope=["src/shared.py"]),
        ))

        with pytest.raises(DispatchError, match="invalid_write_scope:line-0"):
            await planner.plan("goal")

        assert planner.last_result is not None
        assert [item.task_id for item in planner.last_result.assignment.assignments] == [
            "line-2"
        ]
        assert "write_scope_conflict" not in str(
            planner.last_result.assignment.unassigned
        )


class TestDuplicateTaskIdIsTheSameCode:
    """Validator, assign_dag and the fake planner share one duplicate walk."""

    @pytest.mark.parametrize(
        "lines, repeated",
        [
            ([_line("dup"), _line("dup")], "dup"),
            ([_line("dup"), _line("keep"), _line("dup")], "dup"),
            (
                [_line("dup", write_scope=["/etc/passwd"]), _line("dup")],
                "dup",
            ),
        ],
        ids=["two lines", "three lines", "illegal scope"],
    )
    async def test_the_three_layers_report_one_code(
        self, lines: list[dict[str, object]], repeated: str, tmp_path
    ) -> None:
        from src.tools.auto_dispatch.selection import assign_dag, dag_agents_from_policy

        code = f"duplicate_task_id:{repeated}"
        outcome = validate_plan(_payload(*lines))
        assert isinstance(outcome, PlanRejection)
        assert outcome.code == code

        direct = []
        for line in lines:
            scope = line["write_scope"]
            deps = line["depends_on"]
            assert isinstance(scope, list)
            assert isinstance(deps, list)
            direct.append(
                Line(
                    id=str(line["name"]),
                    name=str(line["name"]),
                    task=str(line["task"]),
                    write_scope=tuple(str(item) for item in scope),
                    depends_on=tuple(str(item) for item in deps),
                )
            )
        plan = assign_dag(direct, dag_agents_from_policy())
        assert plan.assignments == ()
        assert {item.reason for item in plan.unassigned} == {code}

        archive = FileSpecArchive(tmp_path)
        planner, _calls = _planner(_reply(*lines), archive=archive)
        with pytest.raises(DispatchError, match=code):
            await planner.plan("goal")

        assert planner.last_result is None
        assert list(tmp_path.iterdir()) == []
