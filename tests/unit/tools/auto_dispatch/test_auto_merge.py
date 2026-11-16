"""N73 §3.3: high-risk paths block G6 auto-approval of the whole dispatch.

The built-in list is the PRD table. ``CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS``
may only append. ``is_high_risk`` unions the diff, the declared write
scope, and the planner flag, and does not read the environment.
"""

from __future__ import annotations

import inspect
import re

import pytest

import src.tools.auto_dispatch.auto_merge as auto_merge
from src.tools.auto_dispatch.auto_merge import (
    HIGH_RISK_ENV,
    HIGH_RISK_GLOBS,
    high_risk_globs,
    is_high_risk,
)
from src.tools.auto_dispatch.gates import DEFAULT_RISK_GLOBS

# (glob, a path it must match, a path that must match nothing in the set)
CASES = (
    (
        "src/tools/auto_dispatch/machine.py",
        "src/tools/auto_dispatch/machine.py",
        "src/tools/auto_dispatch/types.py",
    ),
    (
        "scripts/dispatch_launch.sh",
        "scripts/dispatch_launch.sh",
        "scripts/dispatch_launch.sh.bak",
    ),
    (
        "src/tools/auto_dispatch/execution.py",
        "src/tools/auto_dispatch/execution.py",
        "src/tools/auto_dispatch/execution_extra.py",
    ),
    (
        "src/tools/auto_dispatch/delivery.py",
        "src/tools/auto_dispatch/delivery.py",
        "src/tools/auto_dispatch/delivery_notes.py",
    ),
    (
        "src/server/services/autonomy/git_delivery.py",
        "src/server/services/autonomy/git_delivery.py",
        "src/server/services/autonomy/git_delivery_notes.py",
    ),
    (
        "scripts/conductor_local_daemon.py",
        "scripts/conductor_local_daemon.py",
        "scripts/conductor_local_daemon_notes.py",
    ),
    (
        "src/llms/native_cli/isolation.py",
        "src/llms/native_cli/isolation.py",
        "src/llms/native_cli/isolation_notes.py",
    ),
    (
        "src/conductor_cli/daemon_policy.py",
        "src/conductor_cli/daemon_policy.py",
        "src/conductor_cli/daemon_policy_notes.py",
    ),
    (
        "src/conductor_cli/daemon_capability.py",
        "src/conductor_cli/daemon_capability.py",
        "src/conductor_cli/daemon_capability_notes.py",
    ),
    (
        "**/sandbox*",
        "src/ptc_agent/sandbox.py",
        "src/ptc_agent/no_sandbox.py",
    ),
    (
        "**/sandbox*/**",
        "src/ptc_agent/core/sandbox/_defaults.py",
        "src/ptc_agent/core/no_sandbox.py",
    ),
    (
        "src/tools/auto_dispatch/gates.py",
        "src/tools/auto_dispatch/gates.py",
        "src/tools/auto_dispatch/gates_notes.py",
    ),
    (
        "src/server/services/autonomy/stage_gate.py",
        "src/server/services/autonomy/stage_gate.py",
        "src/server/services/autonomy/stage_gate_notes.py",
    ),
    (
        "src/tools/auto_dispatch/approval.py",
        "src/tools/auto_dispatch/approval.py",
        "src/tools/auto_dispatch/approval_notes.py",
    ),
    (
        "src/tools/auto_dispatch/auto_merge.py",
        "src/tools/auto_dispatch/auto_merge.py",
        "src/tools/auto_dispatch/auto_merge_notes.py",
    ),
    (
        "src/tools/auto_dispatch/superagent_assign/feasibility.py",
        "src/tools/auto_dispatch/superagent_assign/feasibility.py",
        "src/tools/auto_dispatch/superagent_assign/effort.py",
    ),
    (
        "migrations/**",
        "migrations/versions/141_dispatch_gates.py",
        "notes/migrations/141.py",
    ),
    (
        "src/server/database/**",
        "src/server/database/dispatch.py",
        "src/server/database.py",
    ),
    (
        "src/conductor_cli/db_guard.py",
        "src/conductor_cli/db_guard.py",
        "src/conductor_cli/db_guard_notes.py",
    ),
    ("**/auth*", "src/server/auth_tokens.py", "src/server/oauth.py"),
    ("**/auth*/**", "src/server/auth/jwt_bearer.py", "src/server/oauth/x.py"),
    ("**/credential*", "pkg/credential_store.py", "src/server/credits.py"),
    ("**/credential*/**", "pkg/credentials/store.py", "pkg/my_credentials.py"),
    ("**/secret*", "secrets.py", "src/secure.py"),
    ("**/secret*/**", "pkg/secret/token.py", "pkg/top_secret.py"),
    ("**/keychain*", "src/llms/keychain_store.py", "src/keyring.py"),
    ("**/keychain*/**", "src/keychain/mac.py", "src/keyring/x.py"),
    (
        "src/llms/native_cli/failure.py",
        "src/llms/native_cli/failure.py",
        "src/llms/native_cli/failure_notes.py",
    ),
    ("config.yaml", "config.yaml", "agent_config.yaml"),
    ("**/.env*", ".env", "src/env.py"),
    ("deploy/**", "deploy/Dockerfile.backend", "src/deploy/Dockerfile"),
    (
        ".github/workflows/**",
        ".github/workflows/ci.yml",
        "docs/.github/workflows/ci.yml",
    ),
)


@pytest.fixture(autouse=True)
def _clear_high_risk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HIGH_RISK_ENV, raising=False)


def test_builtin_globs_are_the_prd_table() -> None:
    assert HIGH_RISK_GLOBS == tuple(pattern for pattern, _path, _near in CASES)
    assert len(HIGH_RISK_GLOBS) == len(set(HIGH_RISK_GLOBS))


def test_the_glob_translator_is_local() -> None:
    source = inspect.getsource(auto_merge)
    assert "from .gates" not in source
    assert "_glob_regex" not in source


def test_high_risk_is_a_different_set_from_second_review() -> None:
    assert "src/server/app/**" in DEFAULT_RISK_GLOBS
    assert "src/server/app/**" not in HIGH_RISK_GLOBS
    assert "src/tools/auto_dispatch/machine.py" not in DEFAULT_RISK_GLOBS
    assert not is_high_risk(("src/server/app/health.py",), (), False)


@pytest.mark.parametrize(("pattern", "path", "near"), CASES)
def test_each_builtin_glob_matches_its_class(
    pattern: str, path: str, near: str
) -> None:
    """Each pattern is judged on its own, then against the whole set.

    ``near`` fails the single pattern. A later test shows the misses that
    an unanchored ``search`` would accept, so an anchor regression is not
    only a row in this table.
    """
    assert pattern in HIGH_RISK_GLOBS
    assert is_high_risk((path,), (), False, globs=(pattern,))
    assert is_high_risk((), (path,), False, globs=(pattern,))
    assert not is_high_risk((near,), (), False, globs=(pattern,))
    assert not is_high_risk((), (near,), False, globs=(pattern,))
    assert is_high_risk((path,), (), False)
    assert not is_high_risk((near,), (near,), False)


def test_diff_write_scope_and_flag_are_one_union() -> None:
    safe_diff = ("README.md",)
    safe_scope = ("docs/**",)
    assert not is_high_risk(safe_diff, safe_scope, False)
    assert is_high_risk(("config.yaml",), safe_scope, False)
    assert is_high_risk(safe_diff, ("migrations/**",), False)
    assert is_high_risk(safe_diff, safe_scope, True)
    assert is_high_risk((), (), False) is False


def test_a_directory_scope_covering_a_high_risk_path_blocks() -> None:
    """The scope alone blocks, even when the diff does not name the file."""
    assert is_high_risk(
        ("README.md",),
        ("src/tools/auto_dispatch/**",),
        False,
    )
    assert is_high_risk(("README.md",), ("src/**",), False)
    assert is_high_risk(("README.md",), ("src/tools/auto_dispatch",), False)
    assert is_high_risk(
        ("README.md",),
        ("./src/tools/auto_dispatch/**",),
        False,
    )
    assert is_high_risk(("README.md",), ("**/auth*",), False)
    assert not is_high_risk(("README.md",), ("docs/**",), False)
    assert not is_high_risk(("README.md",), ("src/server/app/**",), False)
    assert is_high_risk(
        ("README.md",),
        ("web/**",),
        False,
        globs=("web/src/App.tsx",),
    )


def test_one_high_risk_line_blocks_auto_approval_of_the_dispatch() -> None:
    assert is_high_risk(
        (("README.md",), ("src/tools/auto_dispatch/machine.py",)),
        (("docs/**",), ("docs/**",)),
        (False, False),
    )
    assert is_high_risk(
        (("README.md",), ("src/tools/auto_dispatch/types.py",)),
        (("docs/**",), ("docs/**",)),
        (False, True),
    )
    assert not is_high_risk(
        (("README.md",), ("src/tools/auto_dispatch/types.py",)),
        (("docs/**",), ("docs/**",)),
        (False, False),
    )


def test_a_glob_fullmatches_the_whole_path() -> None:
    """Unanchored ``search`` hits these paths. The checker does not.

    That split is the anchor: ``*`` does not cross ``/``, and a prefix or
    a middle component is not the path.
    """
    nested = "src/ptc_agent/core/sandbox/_defaults.py"
    assert re.search(r"(?:.*/)?sandbox[^/]*", nested)
    assert not is_high_risk((nested,), (), False, globs=("**/sandbox*",))

    prefixed = "src/tools/auto_dispatch/machine.py.bak"
    assert re.search(re.escape("src/tools/auto_dispatch/machine.py"), prefixed)
    assert not is_high_risk(
        (prefixed,),
        (),
        False,
        globs=("src/tools/auto_dispatch/machine.py",),
    )

    hidden = "notes/migrations/141.py"
    assert re.search(r"migrations(?:/.*)?", hidden)
    assert not is_high_risk((hidden,), (), False, globs=("migrations/**",))

    inside_deploy = "src/deploy/Dockerfile"
    assert re.search(r"deploy(?:/.*)?", inside_deploy)
    assert not is_high_risk((inside_deploy,), (), False, globs=("deploy/**",))

    agent_config = "agent_config.yaml"
    assert re.search(re.escape("config.yaml"), agent_config)
    assert not is_high_risk((agent_config,), (), False, globs=("config.yaml",))

    with_newline = "config.yaml\n"
    assert re.search(re.escape("config.yaml"), with_newline)
    assert not is_high_risk((with_newline,), (), False, globs=("config.yaml",))

    assert is_high_risk(("ab",), (), False, globs=("a*",))
    assert not is_high_risk(("a/b",), (), False, globs=("a*",))
    assert is_high_risk(("a/b",), (), False, globs=("a/**",))
    assert is_high_risk(("a/bc",), (), False, globs=("a/b?",))
    assert not is_high_risk(("a/b/c",), (), False, globs=("a/b?",))


def test_diff_paths_are_normalized() -> None:
    assert is_high_risk(("./src/tools/auto_dispatch/machine.py",), (), False)
    assert is_high_risk(("src\\tools\\auto_dispatch\\machine.py",), (), False)
    assert is_high_risk(("./.env",), (), False)
    assert is_high_risk((".env.local",), (), False)
    assert is_high_risk(("deploy/.env.production",), (), False)
    assert is_high_risk(("./.github/workflows/ci.yml",), (), False)
    assert not is_high_risk(("",), ("",), False)
    assert not is_high_risk(("Dockerfile.sandbox",), (), False)
    assert not is_high_risk((".secrets",), (), False)


def test_unset_or_blank_env_keeps_the_builtin_tuple() -> None:
    assert high_risk_globs() is HIGH_RISK_GLOBS
    assert high_risk_globs(environ=ON) is HIGH_RISK_GLOBS
    for raw in ("", "   ", ",", " , , "):
        assert high_risk_globs(environ={HIGH_RISK_ENV: raw}) is HIGH_RISK_GLOBS


def test_env_may_only_add_globs() -> None:
    raw = "web/**, src/tools/auto_dispatch/machine.py, web/**, docs/**"
    globs = high_risk_globs(environ={HIGH_RISK_ENV: raw})
    assert globs[: len(HIGH_RISK_GLOBS)] == HIGH_RISK_GLOBS
    assert globs[len(HIGH_RISK_GLOBS) :] == ("web/**", "docs/**")
    assert globs.count("src/tools/auto_dispatch/machine.py") == 1
    assert is_high_risk(("web/src/App.tsx",), (), False, globs=globs)
    assert is_high_risk(("src/tools/auto_dispatch/gates.py",), (), False, globs=globs)


def test_reader_defaults_to_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HIGH_RISK_ENV, " web/** ")
    assert high_risk_globs()[-1] == "web/**"
    assert high_risk_globs(environ=ON) is HIGH_RISK_GLOBS


def test_is_high_risk_does_not_read_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HIGH_RISK_ENV, "web/**")
    assert not is_high_risk(("web/src/App.tsx",), (), False)
    assert is_high_risk(("web/src/App.tsx",), (), False, globs=high_risk_globs())
    assert not is_high_risk(("config.yaml",), (), False, globs=())
    assert is_high_risk(("config.yaml",), (), True, globs=())


@pytest.mark.parametrize(
    ("diff_files", "write_scope", "risk_bearing"),
    [
        ("config.yaml", (), False),
        ((), "config.yaml", False),
        ((), (), "yes"),
    ],
)
def test_a_bare_string_is_rejected(diff_files, write_scope, risk_bearing) -> None:
    with pytest.raises(TypeError):
        is_high_risk(diff_files, write_scope, risk_bearing)


def test_files_inside_auth_and_sandbox_directories_are_high_risk() -> None:
    """PRD 3.3 isolation and auth categories cover the code in those trees."""
    for path in (
        "src/server/auth/jwt_bearer.py",
        "src/ptc_agent/core/sandbox/runtime.py",
        "libs/ptc-cli/ptc_cli/sandbox/policy.py",
        "web/src/pages/Plugins/components/secrets/List.tsx",
    ):
        assert is_high_risk((path,), (), False), path
    assert is_high_risk((), ("src/server/auth/**",), False)
    assert not is_high_risk(("src/server/app/dispatch.py",), (), False)


# --- S7 / PRD 5.3 auto-approve predicate -------------------------------------

from src.tools.auto_dispatch.auto_merge import (  # noqa: E402
    AUTO_APPROVE_ENV,
    AUTO_APPROVER,
    LineApprovalFacts,
    auto_approve_decision,
    auto_approve_enabled,
)

ON = {AUTO_APPROVE_ENV: "1"}


def _facts(**overrides: object) -> LineApprovalFacts:
    base: dict[str, object] = {
        "line_id": "line-0",
        "g2_passed": True,
        "reviews": ("merge",),
        "diff_files": ("docs/notes.md",),
        "write_scope": ("docs/**",),
    }
    base.update(overrides)
    return LineApprovalFacts(**base)  # type: ignore[arg-type]


def test_low_risk_all_merge_dispatch_is_auto_approved() -> None:
    decision = auto_approve_decision([_facts()], g5_passed=True, environ=ON)
    assert decision.approve is True
    assert decision.approver == AUTO_APPROVER == "auto:n73"
    second = _facts(second_review_required=True, reviews=("MERGE", "merge"))
    assert auto_approve_decision([second], g5_passed=True, environ=ON).approve
    # A BLOCK in an earlier fix round is not a reason; only the final verdicts.
    fixed = _facts(review_history=("block", "merge"))
    assert auto_approve_decision([fixed], g5_passed=True, environ=ON).approve


def test_switch_off_keeps_every_dispatch_human() -> None:
    assert auto_approve_enabled(environ=ON)
    for value in ("0", "false", "NO", " off "):
        env = {AUTO_APPROVE_ENV: value}
        assert not auto_approve_enabled(environ=env)
        decision = auto_approve_decision([_facts()], g5_passed=True, environ=env)
        assert (decision.approve, decision.reason) == (False, "auto_approve_disabled")


def test_each_failed_condition_returns_to_the_human_gate() -> None:
    cases = [
        ([_facts()], {"g5_passed": False}, "g5_not_passed"),
        ([], {"g5_passed": True}, "no_lines"),
        ([_facts()], {"g5_passed": True, "review_unavailable_unblocked": True},
         "review_unavailable_unblocked"),
        ([_facts(diff_files=("src/tools/auto_dispatch/machine.py",))], {"g5_passed": True},
         "high_risk"),
        ([_facts(write_scope=("src/tools/auto_dispatch/**",))], {"g5_passed": True}, "high_risk"),
        ([_facts(risk_bearing=True)], {"g5_passed": True}, "high_risk"),
        ([_facts(parked=True)], {"g5_passed": True}, "parked"),
        ([_facts(g2_passed=False)], {"g5_passed": True}, "g2_not_passed"),
        ([_facts(review_history=("inconclusive",))], {"g5_passed": True}, "review_inconclusive"),
        ([_facts(review_history=("error",))], {"g5_passed": True}, "review_error"),
        ([_facts(review_history=("review_unparseable",))], {"g5_passed": True},
         "review_unparseable"),
        ([_facts(reviews=("block",))], {"g5_passed": True}, "review_not_merge"),
        ([_facts(reviews=())], {"g5_passed": True}, "review_not_merge"),
        ([_facts(second_review_required=True)], {"g5_passed": True}, "second_review_missing"),
        ([_facts(second_review_required=True, reviews=("merge", "block"))],
         {"g5_passed": True}, "review_not_merge"),
    ]
    for lines, kwargs, reason in cases:
        decision = auto_approve_decision(lines, environ=ON, **kwargs)  # type: ignore[arg-type]
        assert (decision.approve, decision.reason) == (False, reason), reason
        assert decision.approver == ""


def test_one_high_risk_line_holds_the_whole_dispatch() -> None:
    safe = _facts()
    risky = _facts(line_id="line-1", diff_files=("migrations/versions/1.py",))
    assert auto_approve_decision([safe, risky], g5_passed=True, environ=ON).reason == "high_risk"


def test_appended_high_risk_globs_count() -> None:
    env = {"CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS": "docs/**", AUTO_APPROVE_ENV: "1"}
    assert auto_approve_decision([_facts()], g5_passed=True, environ=env).reason == "high_risk"
