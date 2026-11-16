from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.server.services.autonomy.roles import (
    DISPATCH_ONLY_PROVIDERS,
    DISPATCH_ONLY_REASONS,
    PROVIDER_ALIASES,
    reserved_work_audit,
    runtime_provider_id,
    DEFAULT_DEVELOPER_POOL,
    DEFAULT_PLANNER,
    DEFAULT_PRIMARY_REVIEWER,
    DEFAULT_SECONDARY_REVIEWER_POOL,
    ROLE_POLICY_ENV,
    AgentState,
    RolePolicy,
    RolePolicyError,
    can_take_line,
    load_role_policy,
    secondary_reviewers,
)


def _agent(provider: str, **kwargs) -> AgentState:
    return AgentState(provider=provider, **kwargs)


def test_default_policy_reserves_planning_and_review_from_code():
    policy = RolePolicy.default()

    assert policy.planner == (DEFAULT_PLANNER,)
    assert policy.primary_reviewer == (DEFAULT_PRIMARY_REVIEWER,)
    assert policy.developer_pool == DEFAULT_DEVELOPER_POOL
    assert policy.secondary_reviewer_pool == DEFAULT_SECONDARY_REVIEWER_POOL
    assert policy.secondary_reviewer_pool == secondary_reviewers(policy.developer_pool)
    assert policy.may_write_code(DEFAULT_PLANNER) is False
    assert policy.may_write_code(DEFAULT_PRIMARY_REVIEWER) is False
    assert policy.may_write_code("kimi") is True


def test_reserved_roles_are_excluded_from_developer_selection():
    policy = RolePolicy.default()
    agents = [
        _agent(DEFAULT_PLANNER, success_rate=1.0),
        _agent(DEFAULT_PRIMARY_REVIEWER, success_rate=1.0),
        _agent("kimi", success_rate=0.5),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    assert chosen.provider == "kimi"


def test_reserved_code_requires_explicit_override():
    policy = RolePolicy(
        allow_reserved_code=True, developer_pool=(DEFAULT_PLANNER,)
    )
    agents = [_agent(DEFAULT_PLANNER, success_rate=1.0)]

    assert policy.choose_developer(agents).provider == DEFAULT_PLANNER


def test_developer_selection_filters_capabilities_and_capacity():
    policy = RolePolicy.default()
    agents = [
        _agent("kimi", capabilities=frozenset({"frontend"}), active=1, capacity=1),
        _agent("cursor", capabilities=frozenset({"frontend"}), success_rate=0.9),
        _agent("grok", capabilities=frozenset({"backend"}), success_rate=1.0),
    ]

    chosen = policy.choose_developer(agents, capabilities={"frontend"})

    assert chosen is not None
    assert chosen.provider == "cursor"


def test_developer_selection_prefers_success_then_free_capacity():
    policy = RolePolicy.default()
    agents = [
        _agent("kimi", success_rate=0.8, active=1, capacity=2),
        _agent("cursor", success_rate=0.9, active=1, capacity=1),
        _agent("grok", success_rate=0.9, active=2, capacity=4),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    # cursor has the same success rate but no free place, so grok wins.
    assert chosen.provider == "grok"


def test_primary_reviewer_never_the_author():
    policy = RolePolicy.default()
    agents = [_agent(DEFAULT_PRIMARY_REVIEWER)]

    assert policy.choose_primary_reviewer(agents, author=DEFAULT_PRIMARY_REVIEWER) is None
    assert policy.choose_primary_reviewer(agents, author="kimi").provider == DEFAULT_PRIMARY_REVIEWER


def test_secondary_reviewer_comes_from_dynamic_pool_not_author():
    policy = RolePolicy.default()
    agents = [
        _agent("kimi", success_rate=0.9),
        _agent("cursor", success_rate=0.8),
        _agent("stepfun", success_rate=0.7),
        _agent(DEFAULT_PRIMARY_REVIEWER, success_rate=1.0),
    ]

    chosen = policy.choose_secondary_reviewer(
        agents, author="kimi", primary_reviewer=DEFAULT_PRIMARY_REVIEWER
    )

    # cursor is in the work roster but has no read-only CLI mode yet (v2), so
    # the only default secondary reviewer left is stepfun.
    assert chosen is not None
    assert chosen.provider == "stepfun"


def test_secondary_reviewer_returns_none_when_pool_is_dry():
    policy = RolePolicy.default()
    agents = [_agent("kimi"), _agent(DEFAULT_PRIMARY_REVIEWER)]

    assert policy.choose_secondary_reviewer(agents, author="kimi") is None


def test_policy_mapping_supports_custom_pools_and_fallbacks():
    policy = RolePolicy.from_mapping(
        {
            "planner": "claude",
            "primary_reviewer": ["codex", "kimi"],
            "developer_pool": "kimi,cursor,grok,deepseek",
            "secondary_reviewer_pool": ["grok", "deepseek"],
            "allow_reserved_code": True,
        }
    )

    assert policy.planner == ("claude",)
    assert policy.primary_reviewer == ("codex", "kimi")
    assert policy.developer_pool == ("kimi", "cursor", "grok", "deepseek")
    assert policy.secondary_reviewer_pool == ("grok", "deepseek")
    assert policy.allow_reserved_code is True


def test_planner_selection_uses_reserved_pool():
    policy = RolePolicy.default()
    agents = [_agent("kimi"), _agent(DEFAULT_PLANNER)]

    chosen = policy.choose_planner(agents)

    assert chosen is not None
    assert chosen.provider == DEFAULT_PLANNER


def test_developer_pool_is_enforced_even_for_a_better_candidate():
    policy = RolePolicy(developer_pool=("kimi",))
    agents = [
        _agent("kimi", success_rate=0.2),
        _agent("cursor", success_rate=1.0),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    assert chosen.provider == "kimi"


def test_string_false_does_not_enable_reserved_code():
    policy = RolePolicy.from_mapping({"allow_reserved_code": "false"})

    assert policy.allow_reserved_code is False
    policy = RolePolicy.from_mapping({"allow_reserved_code": "true"})
    assert policy.allow_reserved_code is True


def test_unlimited_quota_ranks_before_any_finite_quota():
    policy = RolePolicy.default()
    agents = [
        _agent("glm", quota_remaining=10_000, success_rate=0.5),
        _agent("kimi", quota_remaining=None, success_rate=0.5),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    assert chosen.provider == "kimi"


def test_ties_follow_roster_order_not_provider_name():
    policy = RolePolicy.default()
    agents = [
        _agent("stepfun", success_rate=0.4, quota_remaining=None),
        _agent("kimi", success_rate=0.4, quota_remaining=None),
    ]

    chosen = policy.choose_developer(agents)

    assert chosen is not None
    # stepfun sorts after kimi by name; the roster lists kimi first.
    assert chosen.provider == "kimi"


def test_review_ineligible_developer_is_not_a_secondary_reviewer():
    policy = RolePolicy.default()
    agents = [_agent("kimi", success_rate=1.0), _agent("glm", success_rate=1.0)]

    assert policy.choose_secondary_reviewer(agents, author="cursor") is None


def test_provider_aliases_map_onto_native_registry_ids():
    from src.server.services.native_runtime.registry import provider_ids

    registry = set(provider_ids())
    # The source assembles the planner runtime id. This test, outside the
    # grepped trees, locks that value to the registry key.
    assert dict(PROVIDER_ALIASES) == {
        "claude": "claude-code",
        "glm": "zcode",
        "deepseek": "deepseek-dsh",
        "minimax": "minimax",
    }
    assert set(PROVIDER_ALIASES.values()) <= registry
    assert set(PROVIDER_ALIASES).isdisjoint(DISPATCH_ONLY_REASONS)
    assert runtime_provider_id("kimi") == "kimi"
    assert runtime_provider_id("cursor") == "cursor"
    assert runtime_provider_id("grok") == "grok"


def test_roster_names_are_registry_keys_or_an_explicit_dispatch_whitelist():
    from src.server.services.native_runtime.registry import provider_ids

    policy = RolePolicy.default()
    names = (
        set(policy.planner)
        | set(policy.primary_reviewer)
        | set(policy.developer_pool)
        | set(policy.work_tail)
        | set(policy.secondary_reviewer_pool)
    )
    registry = set(provider_ids())
    missing = {name for name in names if runtime_provider_id(name) not in registry}

    assert missing == set(DISPATCH_ONLY_PROVIDERS) == set(DISPATCH_ONLY_REASONS)
    assert set(DISPATCH_ONLY_PROVIDERS).isdisjoint(registry)
    for name in missing:
        assert DISPATCH_ONLY_REASONS[name].strip()
        assert name not in PROVIDER_ALIASES
    for name in names - missing:
        assert runtime_provider_id(name) in registry


def test_role_policy_file_accepts_yaml_and_json(tmp_path, monkeypatch):
    yaml_file = tmp_path / "policy.yaml"
    yaml_file.write_text(
        "planner: claude\nprimary_reviewer:\n  - codex\ndeveloper_pool: [kimi, grok]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ROLE_POLICY_ENV, str(yaml_file))
    policy = load_role_policy()
    assert policy.developer_pool == ("kimi", "grok")
    # neither kimi nor grok has a read-only review mode, so the derived
    # secondary pool is empty rather than a name that cannot review
    assert policy.secondary_reviewer_pool == ()
    assert policy.planner == ("claude",)

    json_file = tmp_path / "policy.json"
    json_file.write_text(
        '{"developer_pool": ["cursor", "stepfun"], "allow_reserved_code": true}',
        encoding="utf-8",
    )
    monkeypatch.setenv(ROLE_POLICY_ENV, str(json_file))
    loaded = load_role_policy()
    assert loaded.developer_pool == ("cursor", "stepfun")
    assert loaded.allow_reserved_code is True
    # cursor has no read-only CLI mode yet (v2); stepfun does
    assert loaded.secondary_reviewer_pool == ("stepfun",)


def test_strict_roster_rejects_a_non_string_at_its_index(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"developer_pool": ["kimi", 123], "secondary_reviewer_pool": ["grok", null]}',
        encoding="utf-8",
    )
    monkeypatch.setenv(ROLE_POLICY_ENV, str(policy_file))
    try:
        load_role_policy()
    except RolePolicyError as exc:
        message = str(exc)
    else:
        raise AssertionError("a non-string roster entry must not be dropped")
    assert "developer_pool[1]" in message


def test_aliases_share_a_canonical_provider():
    from src.server.services.autonomy.roles import canonical_provider

    assert canonical_provider("claudedev") == "claude"
    assert canonical_provider("codexrev") == "codex"
    assert canonical_provider("step") == "stepfun"
    assert canonical_provider("stepfun") == "stepfun"
    assert canonical_provider("  CODEX ") == "codex"


def test_role_policy_file_rejects_invalid_or_missing_input(tmp_path, monkeypatch):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv(ROLE_POLICY_ENV, str(missing))
    try:
        load_role_policy()
    except RolePolicyError:
        pass
    else:
        raise AssertionError("a missing policy file must not fall back")

    invalid = tmp_path / "bad.yaml"
    invalid.write_text("planner: [\n", encoding="utf-8")
    monkeypatch.setenv(ROLE_POLICY_ENV, str(invalid))
    try:
        load_role_policy()
    except RolePolicyError:
        pass
    else:
        raise AssertionError("invalid YAML must not fall back")

    wrong = tmp_path / "list.json"
    wrong.write_text('["kimi"]', encoding="utf-8")
    monkeypatch.setenv(ROLE_POLICY_ENV, str(wrong))
    try:
        load_role_policy()
    except RolePolicyError:
        pass
    else:
        raise AssertionError("a non-mapping policy file must not fall back")


def test_unset_role_policy_file_uses_the_builtin_roster(monkeypatch):
    monkeypatch.delenv(ROLE_POLICY_ENV, raising=False)
    policy = load_role_policy()
    assert policy.planner == (DEFAULT_PLANNER,)
    assert policy.developer_pool == DEFAULT_DEVELOPER_POOL


def test_the_default_developer_pool_ends_with_minimax():
    policy = RolePolicy.default()

    assert policy.developer_pool[-1] == "minimax"
    assert "minimax" in policy.developer_pool
    # roster order is the tie-break, so last in the pool is the first dropped
    assert policy.developer_pool.index("minimax") == len(policy.developer_pool) - 1


def test_minimax_is_never_a_reviewer():
    policy = RolePolicy.default()
    agents = [_agent(name, success_rate=1.0) for name in policy.developer_pool]

    assert "minimax" not in policy.secondary_reviewer_pool
    assert policy.choose_secondary_reviewer(agents, author="kimi") is not None
    chosen = policy.choose_secondary_reviewer(agents, author="kimi")
    assert chosen.provider != "minimax"


def test_minimax_resolves_to_its_native_worker_key():
    from src.server.services.native_runtime.registry import provider_ids

    # minimax spells its native registry key as itself; the binary is mcode.
    # the mapping is explicit even though both sides spell minimax the same
    assert runtime_provider_id("minimax") == "minimax"
    assert "minimax" in set(provider_ids())
    assert PROVIDER_ALIASES["minimax"] == "minimax"


def test_can_take_line_folds_aliases_before_the_capability_check():
    """arkcli and doubao are one Worker; both are text-only on a write line."""
    write_line = SimpleNamespace(write_scope=("src/a.py",))
    assert can_take_line("arkcli", write_line) == (False, "text_only_worker")
    assert can_take_line("doubao", write_line) == (False, "text_only_worker")
    assert can_take_line("doubao", SimpleNamespace(write_scope=())) == (True, None)
    assert can_take_line("arkcli", SimpleNamespace(write_scope=())) == (True, None)


def test_can_take_line_accepts_a_writing_worker():
    write_line = SimpleNamespace(write_scope=("src/a.py",))
    assert can_take_line("kimi", write_line) == (True, None)
    # The registry's own spelling of the GLM Worker reaches the same adapter.
    assert can_take_line("zcode", write_line) == (True, None)
    assert can_take_line("deepseek-dsh", write_line) == (True, None)
    assert can_take_line("opencode", SimpleNamespace(write_scope=("src/",))) == (
        True,
        None,
    )


def test_can_take_line_refuses_an_unknown_identity():
    """An identity the registry cannot resolve is unknown, not capable."""
    for name in ("foo", "no-such-worker", ""):
        assert can_take_line(name, SimpleNamespace(write_scope=("src/a.py",))) == (
            False,
            "unknown_worker",
        )


def test_can_take_line_answers_for_reserved_roles_but_policy_still_gates():
    """claude/codex are known roles; only the role policy may hand them work."""
    write_line = SimpleNamespace(write_scope=("src/a.py",))
    assert can_take_line("claude", write_line) == (True, None)
    assert can_take_line("codex", write_line) == (True, None)


def test_choose_developer_records_unknown_worker_refusal():
    policy = RolePolicy(developer_pool=("no-such-worker",))
    chosen = policy.choose_developer(
        [_agent("no-such-worker")],
        line=SimpleNamespace(write_scope=("src/a.py",)),
    )
    assert chosen is None
    assert policy.assignment_refusal == "unknown_worker"
    assert policy.text_only_filtered is None


def test_glm_maps_to_the_zcode_worker_key():
    from src.server.services.native_runtime.registry import provider_ids

    assert runtime_provider_id("glm") == "zcode"
    assert "zcode" in set(provider_ids())
    assert PROVIDER_ALIASES["glm"] == "zcode"


def test_doubao_is_a_native_worker_not_dispatch_only():
    from src.server.services.native_runtime.registry import (
        DEVELOPMENT_WORKERS,
        provider_ids,
    )

    assert DEVELOPMENT_WORKERS["doubao"] == "doubao"
    assert runtime_provider_id("doubao") == "doubao"
    assert "doubao" in set(provider_ids())
    assert "doubao" not in DISPATCH_ONLY_PROVIDERS
    assert "doubao" not in DISPATCH_ONLY_REASONS


def test_choose_developer_keeps_a_text_only_worker_off_a_write():
    policy = RolePolicy(developer_pool=("doubao", "kimi"))
    chosen = policy.choose_developer(
        [_agent("doubao"), _agent("kimi")],
        line=SimpleNamespace(write_scope=("src/a.py",)),
    )
    assert chosen is not None
    assert chosen.provider == "kimi"
    assert policy.text_only_filtered == "text_only_worker"


def test_a_write_task_with_only_doubao_is_unassigned():
    from src.server.services.autonomy.role_plan import TaskSpec, plan_developer_assignments

    policy = RolePolicy(developer_pool=("doubao",))
    plan = plan_developer_assignments(
        [TaskSpec(task_id="line-0", write_scope=("src/a.py",))],
        [_agent("doubao")],
        policy=policy,
    )
    assert plan.assignments == ()
    assert plan.unassigned[0].reason == "no_eligible_worker"
    assert policy.text_only_filtered == "text_only_worker"


def test_an_empty_scope_can_still_use_doubao():
    policy = RolePolicy(developer_pool=("doubao",))
    chosen = policy.choose_developer(
        [_agent("doubao")], line=SimpleNamespace(write_scope=())
    )
    assert chosen is not None
    assert chosen.provider == "doubao"
    assert policy.text_only_filtered is None


def test_the_author_step_bars_stepfun_from_both_reviews():
    policy = RolePolicy.default()
    agents = [_agent(name, success_rate=1.0) for name in ("codex", "stepfun", "grok")]

    assert policy.choose_primary_reviewer(agents, author="step").provider == "codex"
    assert policy.choose_secondary_reviewer(agents, author="step") is None


def test_the_author_step_bars_stepfun_as_a_primary_reviewer_too():
    policy = RolePolicy.default()
    agents = [_agent("codex"), _agent("stepfun")]

    chosen = policy.choose_secondary_reviewer(
        agents, author="kimi", primary_reviewer="step"
    )

    assert chosen is None


def test_a_reviewer_without_a_read_only_mode_is_refused():
    policy = RolePolicy.default()
    agents = [_agent("grok", success_rate=1.0), _agent("kimi", success_rate=1.0)]

    # grok has no read-only CLI mode, so it cannot take a review even though a
    # policy file may still name it.
    assert policy.choose_secondary_reviewer(agents, author="kimi") is None


def test_a_policy_file_cannot_force_a_reviewer_without_a_read_only_mode():
    strict = replace(
        RolePolicy.default(), secondary_reviewer_pool=("grok", "cursor", "stepfun")
    )
    agents = [_agent(name, success_rate=1.0) for name in ("grok", "cursor", "stepfun")]

    chosen = strict.choose_secondary_reviewer(agents, author="kimi")

    assert chosen is not None
    assert chosen.provider == "stepfun"


def test_a_primary_reviewer_without_a_read_only_mode_is_refused():
    # A review runs the CLI read-only, so a policy that names a primary reviewer
    # with no read-only mode has no usable reviewer at all.
    strict = replace(RolePolicy.default(), primary_reviewer=("grok",))
    agents = [_agent("grok", success_rate=1.0)]

    assert strict.choose_primary_reviewer(agents, author="kimi") is None


def test_a_read_only_capable_primary_reviewer_is_still_chosen():
    policy = RolePolicy.default()
    agents = [_agent("codex", success_rate=1.0)]

    chosen = policy.choose_primary_reviewer(agents, author="kimi")

    assert chosen is not None
    assert chosen.provider == "codex"


def test_the_primary_reviewer_is_still_never_the_author():
    policy = RolePolicy.default()
    agents = [_agent("codex", success_rate=1.0)]

    assert policy.choose_primary_reviewer(agents, author="codex") is None
    assert policy.choose_primary_reviewer(agents, author="step").provider == "codex"


def test_the_default_policy_never_allows_reserved_code():
    # The override is an emergency switch, so the default configuration keeps
    # the planner and the primary reviewer out of the development pool.
    policy = RolePolicy.default()

    assert policy.allow_reserved_code is False
    assert policy.may_write_code(DEFAULT_PLANNER) is False
    assert policy.may_write_code(DEFAULT_PRIMARY_REVIEWER) is False


def test_the_reserved_code_override_records_why_it_was_used():
    policy = replace(RolePolicy.default(), allow_reserved_code=True)

    assert policy.may_write_code(DEFAULT_PLANNER) is True
    audit = reserved_work_audit(DEFAULT_PLANNER)
    assert DEFAULT_PLANNER in audit
    assert "allow_reserved_code" in audit
    assert "reserved" in audit
