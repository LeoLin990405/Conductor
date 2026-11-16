"""One alias canonicalizer, checked against fake usage snapshots.

The GLM CLI is reported by a snapshot as ``zcode``, ``zai``, ``zhipu``,
``zcode-cli`` or ``glm`` depending on who wrote the row. Routing used to keep
that mapping inline, so a spelling a call site forgot made an exhausted
provider look wide open. These tests pin the behaviour with fake snapshot
files: whatever spelling the row uses, the reader lands on one runtime key and
the router refuses (or keeps) the same Worker.

The CLI is also registered twice -- ``zcode`` is the Worker, ``zhipu`` the
cancel-only lane -- so the tests below keep the two apart where a runtime is
bound (a preference, a disabled entry) and together where quota is read.
"""

import ast
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.server.services.autonomy.roles import (
    canonical_provider,
    provider_alias_spellings,
    runtime_identity,
    runtime_provider_id,
)
from src.server.services.native_runtime.router import (
    RuntimeRequest,
    RuntimeRoutingPolicy,
    aresolve_requested_provider,
    resolve_requested_provider,
    route_runtime,
)
from src.server.services.native_runtime.usage_headroom import read_usage_headroom

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-11-02T12:00:00Z"
RESET_TEXT = "2026-11-02T13:00:00Z"

# Every spelling the GLM CLI shows up under, in a snapshot row and in a policy
# file, plus the roster name itself.
GLM_SPELLINGS = ("glm", "zcode", "zai", "zhipu", "zcode-cli")
# The same CLI spelled by the deepseek rows.
DEEPSEEK_SPELLINGS = ("deepseek", "deepseek-dsh", "dsh")


@dataclass
class FakeStatus:
    provider: str
    installed: bool = True
    live_verified: bool = True
    capabilities: dict | None = None


def _statuses(*providers: str) -> list[FakeStatus]:
    return [
        FakeStatus(name, capabilities={"streaming": True, "approvals": True})
        for name in providers
    ]


def _snapshot(tmp_path, rows, name="snapshot.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"entries": rows}), encoding="utf-8")
    return str(path)


def _bridge_snapshot(tmp_path, rows, name="bridge.json"):
    """The other snapshot shape, the only one that carries a balance row."""
    path = tmp_path / name
    path.write_text(json.dumps({"providers": rows}), encoding="utf-8")
    return str(path)


def _window_row(provider, used_percent, *, updated_at=NOW_TEXT):
    return {
        "provider": provider,
        "updatedAt": updated_at,
        "primary": {"usedPercent": used_percent, "resetsAt": RESET_TEXT},
    }


def test_the_canonicalizer_folds_every_glm_spelling_onto_one_identity():
    for spelling in GLM_SPELLINGS:
        assert canonical_provider(spelling) == "glm", spelling
        assert canonical_provider(spelling.upper()) == "glm", spelling
        assert runtime_provider_id(canonical_provider(spelling)) == "zcode"

    # Every spelling of the canonical provider, so a caller never re-derives it.
    assert set(provider_alias_spellings("glm")) == set(GLM_SPELLINGS)
    for spelling in GLM_SPELLINGS:
        assert set(provider_alias_spellings(spelling)) == set(GLM_SPELLINGS)
    # A name with no alias is only itself.
    assert provider_alias_spellings("minimax") == ("minimax", "mcode")
    assert provider_alias_spellings("") == ()


@pytest.mark.parametrize("spelling", GLM_SPELLINGS)
def test_a_fake_snapshot_row_under_any_spelling_lands_on_one_key(tmp_path, spelling):
    states = read_usage_headroom(
        _snapshot(tmp_path, [_window_row(spelling, 100)]), now=NOW
    )

    assert states == {"zcode": "exhausted"}


@pytest.mark.parametrize("spelling", GLM_SPELLINGS)
def test_an_exhausted_snapshot_row_under_any_spelling_skips_the_glm_worker(
    tmp_path, spelling
):
    """The regression: an exhausted GLM row must not look like headroom.

    The status is named by whichever spelling the registry uses, so the same
    snapshot has to exclude it under all of them.
    """
    headroom = read_usage_headroom(
        _snapshot(tmp_path, [_window_row(spelling, 100)]), now=NOW
    )
    for provider in GLM_SPELLINGS:
        decision = route_runtime(
            _statuses(provider, "codex"),
            RuntimeRequest(task="review architecture"),
            policy=RuntimeRoutingPolicy(
                preferred_order=(provider, "codex"), fallback_only=()
            ),
            headroom=headroom,
        )

        assert decision.runtime == "codex", (spelling, provider)
        assert provider not in decision.fallbacks, (spelling, provider)


@pytest.mark.parametrize("spelling", GLM_SPELLINGS)
def test_an_available_snapshot_row_under_any_spelling_keeps_the_glm_worker(
    tmp_path, spelling
):
    headroom = read_usage_headroom(
        _snapshot(tmp_path, [_window_row(spelling, 12)]), now=NOW
    )

    decision = route_runtime(
        _statuses("zcode", "codex"),
        RuntimeRequest(task="review architecture"),
        policy=RuntimeRoutingPolicy(preferred_order=("zcode", "codex"), fallback_only=()),
        headroom=headroom,
    )

    assert decision.runtime == "zcode"
    assert "fresh quota headroom available" in decision.reason


def test_conflicting_alias_rows_merge_into_one_key_and_stay_exhausted(tmp_path):
    """Two spellings of one CLI cannot vote each other into headroom."""
    states = read_usage_headroom(
        _snapshot(
            tmp_path,
            [
                _window_row("zcode", 100),
                _window_row("zai", 4),
                _window_row("zhipu", 30),
            ],
        ),
        now=NOW,
    )

    assert states == {"zcode": "exhausted"}

    decision = route_runtime(
        _statuses("zcode", "codex"),
        RuntimeRequest(task="review architecture"),
        policy=RuntimeRoutingPolicy(preferred_order=("zcode", "codex"), fallback_only=()),
        headroom=states,
    )

    assert decision.runtime == "codex"
    assert "zcode" not in decision.fallbacks


@pytest.mark.parametrize("spelling", DEEPSEEK_SPELLINGS)
def test_deepseek_alias_rows_reach_the_same_runtime_key(tmp_path, spelling):
    # deepseek is a balance provider: the reading is an amount, not a window.
    path = _bridge_snapshot(
        tmp_path,
        [
            {
                "provider": spelling,
                "kind": "balance",
                "updated_at": NOW_TEXT,
                "balance": {"amount": 0, "currency": "CNY"},
            }
        ],
    )

    assert read_usage_headroom(path, now=NOW) == {"deepseek-dsh": "exhausted"}

    decision = route_runtime(
        _statuses("deepseek-dsh", "codex"),
        RuntimeRequest(task="refactor code"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("deepseek-dsh", "codex"), fallback_only=()
        ),
        headroom={"deepseek-dsh": "exhausted"},
    )

    assert decision.runtime == "codex"
    assert "deepseek-dsh" not in decision.fallbacks


@pytest.mark.parametrize("spelling", ("claude", "claude-code", "claudedev"))
def test_claude_alias_rows_reach_the_same_runtime_key(tmp_path, spelling):
    states = read_usage_headroom(
        _snapshot(tmp_path, [_window_row(spelling, 100)]), now=NOW
    )

    assert states == {"claude": "exhausted"}

    decision = route_runtime(
        _statuses("claude-code", "codex"),
        RuntimeRequest(task="review architecture"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("claude-code", "codex"), fallback_only=()
        ),
        headroom=states,
    )

    assert decision.runtime == "codex"
    assert "claude-code" not in decision.fallbacks


def test_a_distinct_provider_is_never_conflated_with_the_glm_alias(tmp_path):
    """Exhausting one CLI must not exhaust a different runtime id."""
    headroom = read_usage_headroom(
        _snapshot(tmp_path, [_window_row("zai", 100), _window_row("minimax", 5)]),
        now=NOW,
    )

    decision = route_runtime(
        _statuses("zcode", "minimax"),
        RuntimeRequest(task="review architecture", required_capabilities=("approvals",)),
        policy=RuntimeRoutingPolicy(
            preferred_order=("zcode", "minimax"), fallback_only=()
        ),
        headroom=headroom,
    )

    assert headroom == {"zcode": "exhausted", "minimax": "available"}
    assert decision.runtime == "minimax"
    assert "zcode" not in decision.fallbacks


def test_the_balance_estimate_comparison_uses_the_canonical_key(tmp_path):
    """A balance row under an alias spelling still compares to its threshold."""
    path = _bridge_snapshot(
        tmp_path,
        [
            {
                "provider": "dsh",
                "kind": "balance",
                "updated_at": NOW_TEXT,
                "balance": {"amount": 49, "currency": "CNY"},
            }
        ],
    )

    assert read_usage_headroom(path, now=NOW) == {"deepseek-dsh": "low"}


@pytest.mark.parametrize(
    "requested,expected",
    [
        ("glm", "zcode"),
        ("zai", "zcode"),
        # ``zhipu`` is its own runtime id (cancel-only), not the zcode Worker.
        ("zhipu", "zhipu"),
        ("zcode-cli", "zcode"),
        ("zcode", "zcode"),
        ("deepseek", "deepseek-dsh"),
        ("dsh", "deepseek-dsh"),
        ("deepseek-dsh", "deepseek-dsh"),
        ("kimi", "kimi"),
        ("moonshot", "kimi"),
        ("claudedev", "claude-code"),
        ("arkcli", "doubao"),
        ("", None),
    ],
)
def test_an_explicit_alias_request_binds_to_the_runtime_id(requested, expected):
    provider, decision = resolve_requested_provider(requested)

    assert provider == expected
    assert decision is None


def test_the_async_resolver_reads_a_fake_snapshot_and_skips_the_alias(tmp_path, monkeypatch):
    """End to end: a snapshot row spelled ``zai`` keeps the GLM Worker out."""
    from unittest.mock import AsyncMock

    snapshot = _snapshot(tmp_path, [_window_row("zai", 100)])
    monkeypatch.setattr(
        "src.server.services.native_runtime.registry.list_statuses",
        lambda: _statuses("zcode", "codex"),
    )
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
        AsyncMock(return_value=read_usage_headroom(snapshot, now=NOW)),
    )

    provider, decision = asyncio.run(
        aresolve_requested_provider("auto", task="review architecture")
    )

    assert provider == "codex"
    assert decision is not None
    assert decision.runtime == "codex"
    assert "zcode" not in decision.fallbacks


# The GLM CLI is registered twice: ``zcode`` is the Worker with the full
# capability set and ``zhipu`` is the cancel-only lane. Sharing one quota
# identity must not share a routing identity, or an operator preference for the
# Worker silently binds the cancel-only lane (and a policy that disables one
# lane disables both).
GLM_RUNTIME_IDS = ("zcode", "zhipu")


def test_a_preference_for_the_zcode_worker_does_not_bind_the_cancel_only_lane():
    """The regression: two runtime ids of one CLI stay two routing identities."""
    decision = route_runtime(
        _statuses("zhipu", "zcode"),
        RuntimeRequest(
            task="review architecture",
            preferred_provider="zcode",
            required_capabilities=("approvals",),
        ),
        policy=RuntimeRoutingPolicy(preferred_order=("zcode",), fallback_only=()),
    )

    assert decision.runtime == "zcode"


@pytest.mark.parametrize("preferred", GLM_RUNTIME_IDS)
def test_a_preference_for_either_glm_lane_binds_that_lane(preferred):
    decision = route_runtime(
        _statuses("zhipu", "zcode"),
        RuntimeRequest(
            task="review architecture",
            preferred_provider=preferred,
            required_capabilities=("approvals",),
        ),
        policy=RuntimeRoutingPolicy(preferred_order=("zcode",), fallback_only=()),
    )

    assert decision.runtime == preferred


def test_disabling_the_cancel_only_lane_leaves_the_zcode_worker_routable():
    decision = route_runtime(
        _statuses("zhipu", "zcode"),
        RuntimeRequest(task="review architecture", required_capabilities=("approvals",)),
        policy=RuntimeRoutingPolicy(
            preferred_order=("zcode", "codex"),
            fallback_only=(),
            disabled_providers=("zhipu",),
        ),
    )

    assert decision.runtime == "zcode"
    assert "zhipu" not in decision.fallbacks


def test_disabling_the_zcode_worker_leaves_the_cancel_only_lane_routable():
    decision = route_runtime(
        _statuses("zhipu"),
        RuntimeRequest(task="review architecture", required_capabilities=("approvals",)),
        policy=RuntimeRoutingPolicy(
            preferred_order=("zcode",),
            fallback_only=(),
            disabled_providers=("zcode",),
        ),
    )

    assert decision.runtime == "zhipu"


@pytest.mark.parametrize("spelling", ("glm", "zai", "zcode-cli"))
def test_disabling_any_dispatch_spelling_disables_the_glm_worker(spelling):
    """The alias folding an operator policy relies on still holds."""
    decision = route_runtime(
        _statuses("zcode", "codex"),
        RuntimeRequest(task="review architecture", required_capabilities=("approvals",)),
        policy=RuntimeRoutingPolicy(
            preferred_order=("zcode", "codex"),
            fallback_only=(),
            disabled_providers=(spelling,),
        ),
    )

    assert decision.runtime == "codex"
    assert "zcode" not in decision.fallbacks


def test_both_glm_lanes_still_read_the_one_shared_quota_row(tmp_path):
    """Distinct routing identities, one quota reading: the ``zai`` row is shared."""
    headroom = read_usage_headroom(
        _snapshot(tmp_path, [_window_row("zai", 100)]), now=NOW
    )

    assert headroom == {"zcode": "exhausted"}
    for provider in GLM_RUNTIME_IDS:
        decision = route_runtime(
            _statuses(provider, "codex"),
            RuntimeRequest(
                task="review architecture", required_capabilities=("approvals",)
            ),
            policy=RuntimeRoutingPolicy(
                preferred_order=(provider, "codex"), fallback_only=()
            ),
            headroom=headroom,
        )

        assert decision.runtime == "codex", provider
        assert provider not in decision.fallbacks, provider


def test_the_routing_identity_keeps_distinct_runtime_ids_apart():
    assert runtime_identity("zhipu") == "zhipu"
    assert runtime_identity("zcode") == "zcode"
    for spelling in ("glm", "zai", "zcode-cli"):
        assert runtime_identity(spelling) == "zcode", spelling
    # Spellings that name no runtime adapter fold through the same table.
    assert runtime_identity("deepseek") == "deepseek-dsh"
    assert runtime_identity("dsh") == "deepseek-dsh"
    assert runtime_identity("claudedev") == "claude-code"
    assert runtime_identity("") == ""


# (row spelling, the snapshot key it must land on, the runtime it refuses).
# Vendor spellings the dispatch roster and CodexBar use for the same CLI; the
# real reader dropped these rows, so the provider looked wide open.
_VENDOR_ROWS = [
    ("claudedev", "claude", "claude-code"),
    ("arkcli", "doubao", "doubao"),
    ("moonshot", "kimi", "kimi"),
    ("mcode", "minimax", "minimax"),
]


@pytest.mark.parametrize("spelling,key,runtime", _VENDOR_ROWS)
def test_a_vendor_spelled_row_reaches_routing_exhausted(tmp_path, spelling, key, runtime):
    states = read_usage_headroom(
        _snapshot(tmp_path, [_window_row(spelling, 100)]), now=NOW
    )

    assert states == {key: "exhausted"}

    decision = route_runtime(
        _statuses(runtime, "codex"),
        RuntimeRequest(task="review architecture"),
        policy=RuntimeRoutingPolicy(preferred_order=(runtime, "codex"), fallback_only=()),
        headroom=states,
    )

    assert decision.runtime == "codex", spelling
    assert runtime not in decision.fallbacks, spelling


def test_a_dsh_balance_row_is_classified_as_deepseek_balance(tmp_path):
    """``dsh`` is DeepSeek: its row is a balance, not a window-less quota row.

    Classifying by the raw spelling read ``dsh`` as a quota provider with no
    windows and dropped the spent balance, so DeepSeek stayed routable.
    """
    path = _bridge_snapshot(
        tmp_path,
        [
            {
                "provider": "dsh",
                "updated_at": NOW_TEXT,
                "balance": {"amount": 0, "currency": "CNY"},
            }
        ],
    )

    states = read_usage_headroom(path, now=NOW)

    assert states == {"deepseek-dsh": "exhausted"}
    decision = route_runtime(
        _statuses("deepseek-dsh", "codex"),
        RuntimeRequest(task="refactor code"),
        policy=RuntimeRoutingPolicy(
            preferred_order=("deepseek-dsh", "codex"), fallback_only=()
        ),
        headroom=states,
    )
    assert decision.runtime == "codex"


@pytest.mark.parametrize("spelling", GLM_SPELLINGS)
def test_the_codexbar_port_reads_the_zai_row_for_every_glm_spelling(tmp_path, spelling):
    """The CodexBar port folds through the same table, ``zcode-cli`` included."""
    from src.tools.auto_dispatch.headroom_codexbar import CodexBarHeadroom

    path = tmp_path / "codexbar-snapshot.json"
    path.write_text(
        json.dumps(
            {
                "updatedAt": NOW_TEXT,
                "providers": [
                    {
                        "provider": "zai",
                        "kind": "quota",
                        "status": "ok",
                        "updated_at": NOW_TEXT,
                        "windows": [{"used_percent": 90.0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    remaining = asyncio.run(CodexBarHeadroom(path, now=NOW).remaining(spelling))

    assert remaining == pytest.approx(0.1)


# Alias spellings only ``autonomy.roles`` may spell out. ``zai`` is also the
# CodexBar provider id of GLM's row, which headroom_codexbar names as a value.
_ALIAS_LITERALS = frozenset(
    {"zhipu", "zai", "dsh", "claudedev", "arkcli", "moonshot", "mcode",
     "cursor-agent", "zcode-cli", "openai-codex"}
)
_READERS = {
    "src/server/services/native_runtime/router.py": _ALIAS_LITERALS,
    "src/server/services/native_runtime/usage_headroom.py": _ALIAS_LITERALS,
    "src/tools/auto_dispatch/selection.py": _ALIAS_LITERALS,
    "src/tools/auto_dispatch/headroom_codexbar.py": _ALIAS_LITERALS - {"zai"},
}


def _code_strings(path: Path) -> set[str]:
    """String constants in code, leaving docstrings out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


@pytest.mark.parametrize("relative", sorted(_READERS))
def test_no_reader_keeps_an_alias_table_of_its_own(relative):
    """One canonicalizer: a reader that spells an alias has its own table."""
    root = Path(__file__).resolve().parents[5]
    found = _code_strings(root / relative) & _READERS[relative]

    assert found == set(), relative
