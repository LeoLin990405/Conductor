"""Auto-dispatch headroom canonicalizes vendor-CLI aliases before the state.

A Worker, a policy file and the snapshot can each spell one vendor CLI
differently: glm / zcode / zhipu / zai are one CLI. Before either headroom
reader looks up a state it folds the name to the whole alias group, so:

* ``UsageSnapshotHeadroom`` (over the native-runtime snapshot) answers the same
  fraction for every spelling, whichever spelling the snapshot used;
* ``CodexBarHeadroom`` (over the CodexBar snapshot, whose GLM row is ``zai``)
  reads that row for every worker spelling; and
* a distinct provider is never folded into the group.

The snapshots are fakes: plain state dicts, or a temp file written for the
CodexBar reader. No CodexBar CLI runs and nothing reaches the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.server.services.autonomy.role_plan import read_headroom
from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.server.services.native_runtime.usage_headroom import read_usage_headroom
from src.tools.auto_dispatch.headroom_codexbar import CodexBarHeadroom
from src.tools.auto_dispatch.selection import QuotaAwareModelSelector, UsageSnapshotHeadroom
from src.tools.auto_dispatch.types import Line

# Every spelling of the one GLM CLI.
GLM_SPELLINGS = ("glm", "zcode", "zhipu", "zai")


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", GLM_SPELLINGS)
async def test_every_glm_spelling_reads_an_exhausted_snapshot(spelling):
    """A snapshot keyed by the native-runtime id refuses every spelling."""
    port = UsageSnapshotHeadroom({"zcode": "exhausted", "kimi": "available"})
    assert await port.remaining(spelling) == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_key", GLM_SPELLINGS)
async def test_a_glm_worker_reads_any_snapshot_spelling(snapshot_key):
    """The mirror: any snapshot spelling is read for every worker spelling."""
    port = UsageSnapshotHeadroom({snapshot_key: "exhausted", "kimi": "available"})
    for spelling in GLM_SPELLINGS:
        assert await port.remaining(spelling) == 0.0


@pytest.mark.asyncio
async def test_a_healthy_alias_snapshot_stays_eligible():
    """Folding spellings must not turn a fresh reading into exhaustion."""
    port = UsageSnapshotHeadroom({"zai": "low", "kimi": "available"})
    for spelling in GLM_SPELLINGS:
        assert await port.remaining(spelling) == 0.25


@pytest.mark.asyncio
async def test_a_distinct_provider_is_never_folded_into_the_group():
    """kimi/grok are not the GLM CLI and keep their own (missing) reading."""
    port = UsageSnapshotHeadroom({"zcode": "exhausted"})
    assert await port.remaining("kimi") == 1.0
    assert await port.remaining("grok") == 1.0


@pytest.mark.asyncio
async def test_deepseek_spellings_share_one_reading():
    """The same folding covers deepseek / deepseek-dsh / dsh."""
    port = UsageSnapshotHeadroom({"deepseek-dsh": "exhausted", "kimi": "available"})
    assert await port.remaining("deepseek") == 0.0
    assert await port.remaining("dsh") == 0.0
    assert await port.remaining("kimi") == 1.0


# --- CodexBar reader -------------------------------------------------------
# The CodexBar snapshot spells the GLM CLI ``zai``; the worker is ``glm``.

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _provider_row(provider: str, observed_at: str, used_percent: float) -> dict:
    return {
        "provider": provider,
        "kind": "quota",
        "status": "ok",
        "updated_at": observed_at,
        "windows": [
            {"used_percent": used_percent, "resets_at_utc": "2026-11-06T17:00:00Z"}
        ],
    }


def _snapshot(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "codexbar-snapshot.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", GLM_SPELLINGS)
async def test_codexbar_reads_the_zai_row_for_every_worker_spelling(
    tmp_path, spelling
):
    """The CodexBar GLM row is ``zai``; every worker spelling reads it spent."""
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [_provider_row("zai", _stamp(NOW), 100.0)],
    }
    headroom = CodexBarHeadroom(_snapshot(tmp_path, payload), now=NOW)
    assert await headroom.remaining(spelling) == 0.0


@pytest.mark.asyncio
async def test_codexbar_does_not_fold_a_distinct_worker(tmp_path):
    payload = {
        "updatedAt": _stamp(NOW),
        "providers": [_provider_row("zai", _stamp(NOW), 100.0)],
    }
    headroom = CodexBarHeadroom(_snapshot(tmp_path, payload), now=NOW)
    # kimi has no row and is not the GLM CLI, so it reads full.
    assert await headroom.remaining("kimi") == 1.0


# --- end to end through read_headroom --------------------------------------

@pytest.mark.asyncio
async def test_a_zcode_developer_reads_a_glm_keyed_exhaustion():
    """Cross-spelling: developer ``zcode``, snapshot keyed ``glm``.

    ``read_headroom`` canonicalizes the developer, the port folds the group,
    and the exhausted reading is recorded as ``quota_exhausted``.
    """
    policy = RolePolicy(developer_pool=("zcode", "kimi"))
    agents = [
        AgentState(provider="zcode", success_rate=1.0),
        AgentState(provider="kimi", success_rate=0.5),
    ]
    port = UsageSnapshotHeadroom({"glm": "exhausted", "kimi": "available"})

    readings = await read_headroom(("zcode", "kimi"), port)
    assert readings["zcode"] == 0.0

    chosen = policy.choose_developer(agents, headroom=readings)
    assert chosen is not None
    assert chosen.provider == "kimi"
    assert policy.assignment_refusal == "quota_exhausted"


@pytest.mark.asyncio
async def test_a_zhipu_keyed_exhaustion_still_refuses_the_glm_developer():
    """A snapshot spelled ``zhipu`` refuses a ``glm`` developer, not just open."""
    policy = RolePolicy(developer_pool=("glm", "kimi"))
    agents = [
        AgentState(provider="glm", success_rate=1.0),
        AgentState(provider="kimi", success_rate=0.5),
    ]
    port = UsageSnapshotHeadroom({"zhipu": "exhausted", "kimi": "available"})

    readings = await read_headroom(("glm", "kimi"), port)
    assert readings["glm"] == 0.0

    chosen = policy.choose_developer(agents, headroom=readings)
    assert chosen is not None
    assert chosen.provider == "kimi"
    assert policy.assignment_refusal == "quota_exhausted"


# --- the real native reader folds a binary/vendor spelling ------------------
# CodexBarHeadroom and the selection/router alias groups already fold
# mcode/step/cursor-agent; the native-runtime reader (read_usage_headroom)
# must fold them too, or a snapshot spelled by a CLI's binary is dropped and
# the provider it names is left looking wide open at assignment.


def _native_snapshot(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "usage-headroom.json"
    path.write_text(json.dumps({"providers": rows}), encoding="utf-8")
    return path


def _native_row(provider: str, used_percent: float) -> dict:
    from src.server.services.autonomy.roles import canonical_provider

    if canonical_provider(provider) == "deepseek":
        # DeepSeek is metered by balance under every spelling (dsh included):
        # the reader classifies the row by its folded key.
        return {
            "provider": provider,
            "kind": "balance",
            "status": "ok",
            "updated_at": _stamp(NOW),
            "balance": {"amount": 0 if used_percent >= 100 else 500, "currency": "CNY"},
        }
    return {
        "provider": provider,
        "kind": "quota",
        "status": "ok",
        "updated_at": _stamp(NOW),
        "windows": [{"used_percent": used_percent, "resets_at_utc": "2026-11-06T17:00:00Z"}],
    }


# (snapshot spelling, the canonical provider it must read as, a healthy peer).
_ALIAS_EXHAUSTED_VECTORS = [
    ("mcode", "minimax", "cursor"),
    ("step", "stepfun", "cursor"),
    ("cursor-agent", "cursor", "minimax"),
    # Vendor spellings the router/selection readers already fold into one group;
    # the real reader must fold them too or a spent row is dropped and the CLI
    # it names is left looking wide open at assignment.
    ("dsh", "deepseek-dsh", "cursor"),
    ("arkcli", "doubao", "cursor"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias,canonical,peer", _ALIAS_EXHAUSTED_VECTORS)
async def test_a_binary_spelled_snapshot_reads_its_provider_exhausted(
    tmp_path, alias, canonical, peer
):
    """The real reader normalises the alias, so assignment refuses the CLI.

    A fresh snapshot spelled by the CLI's binary (``mcode`` for MiniMax,
    ``step`` for StepFun, ``cursor-agent`` for Cursor) at 100% usage must not
    leave that provider wide open: the reader folds it to the canonical key,
    and ``choose_developer`` records ``quota_exhausted`` and picks the peer.
    """
    states = read_usage_headroom(
        str(_native_snapshot(tmp_path, [_native_row(alias, 100.0)])), now=NOW
    )
    assert states == {canonical: "exhausted"}

    policy = RolePolicy(developer_pool=(canonical, peer))
    agents = [
        AgentState(provider=canonical, success_rate=1.0),
        AgentState(provider=peer, success_rate=0.5),
    ]
    port = UsageSnapshotHeadroom(states)

    readings = await read_headroom((canonical, peer), port)
    assert readings[canonical] == 0.0

    chosen = policy.choose_developer(agents, headroom=readings)
    assert chosen is not None
    assert chosen.provider == peer
    assert policy.assignment_refusal == "quota_exhausted"


@pytest.mark.asyncio
async def test_a_claudedev_snapshot_refuses_the_reserved_claude_developer(tmp_path):
    """The reader folds claudedev -> claude; the selection reader honours it.

    Claude is reserved, so a plain policy never offers it as a developer. With
    ``allow_reserved_code`` the exhaustion is still the reason it is refused: a
    claudedev-keyed spent snapshot must not leave the claude CLI looking wide
    open wherever it is eligible (review, or reserved work).
    """
    states = read_usage_headroom(
        str(_native_snapshot(tmp_path, [_native_row("claudedev", 100.0)])), now=NOW
    )
    assert states == {"claude": "exhausted"}

    policy = RolePolicy(developer_pool=("claude", "cursor"), allow_reserved_code=True)
    agents = [
        AgentState(provider="claude", success_rate=1.0),
        AgentState(provider="cursor", success_rate=0.5),
    ]
    port = UsageSnapshotHeadroom(states)

    readings = await read_headroom(("claude", "cursor"), port)
    assert readings["claude"] == 0.0

    chosen = policy.choose_developer(agents, headroom=readings)
    assert chosen is not None
    assert chosen.provider == "cursor"
    assert policy.assignment_refusal == "quota_exhausted"


# --- the live selector honours a task's estimated size ----------------------
# Live reassignment reaches QuotaAwareModelSelector.select through the machine's
# _select_with_retry. Before the size-aware fix the selector only checked that
# headroom exceeded zero, so a large task with kimi=0.05 and cursor=0.95 still
# took kimi (first in the roster). A large task must rank by remaining quota
# first. Only the two named models stay eligible in each case, so the result is
# independent of the deployment's roster order.


class _FixedHeadroom:
    """A fake HeadroomPort: named remaining fractions, everyone else exhausted."""

    def __init__(self, remaining: dict[str, float]) -> None:
        self._remaining = remaining

    async def remaining(self, model: str) -> float:
        return self._remaining.get(model, 0.0)


@pytest.mark.asyncio
async def test_a_large_task_takes_the_widest_quota_over_roster_order() -> None:
    """The blocker's own numbers: a large task goes to the widest quota."""
    selector = QuotaAwareModelSelector(_FixedHeadroom({"kimi": 0.05, "cursor": 0.95}))
    line = Line(id="line-0", name="implement", task="do it", estimated_size="large")
    assert await selector.select(line, []) == "cursor"


@pytest.mark.asyncio
async def test_a_risk_bearing_line_is_treated_as_large() -> None:
    """``risk_bearing`` implies a large estimate, so it also prefers the quota."""
    selector = QuotaAwareModelSelector(_FixedHeadroom({"kimi": 0.05, "cursor": 0.95}))
    line = Line(id="line-0", name="implement", task="do it", risk_bearing=True)
    assert await selector.select(line, []) == "cursor"


@pytest.mark.asyncio
async def test_a_large_task_breaks_the_spread_by_quota_not_stats() -> None:
    """Size-aware: a large task ranks by quota where a smaller one ranks by proof.

    Both models are unassigned. kimi holds more quota (0.9) but has no recorded
    run; cursor is nearly drained (0.1) but has a proven rate. A large task
    takes kimi on quota; a medium task takes cursor on the recorded rate. Only
    the two named models stay eligible, so the result is roster-independent --
    this is what keeps the fix from collapsing into "always prefer the quota".
    """
    class _Headroom:
        async def remaining(self, model: str) -> float:
            return {"kimi": 0.9, "cursor": 0.1}.get(model, 0.0)

    class _Stats:
        async def success_rate(self, model: str) -> float | None:
            return {"cursor": 0.5}.get(model)

    large = Line(id="l", name="i", task="t", estimated_size="large")
    assert await QuotaAwareModelSelector(_Headroom(), stats=_Stats()).select(large, []) == "kimi"

    medium = Line(id="l", name="i", task="t")
    assert await QuotaAwareModelSelector(_Headroom(), stats=_Stats()).select(medium, []) == "cursor"


class _MutableHeadroom:
    def __init__(self, quota: dict[str, float]) -> None:
        self.quota = quota

    async def remaining(self, model: str) -> float:
        return self.quota.get(model, 0.0)


@pytest.mark.asyncio
async def test_a_large_task_is_not_pushed_onto_a_low_tier_by_another_seat() -> None:
    """The blocker's numbers with unequal load: quota tier leads, not the spread.

    cursor (0.95) already holds one line and kimi (0.05) holds none. Ranking
    least-assigned first sent the large line to kimi, nearly spent; the tier
    must lead, so the large line still takes cursor. Fails on the
    assignments-first ranking.
    """
    port = _MutableHeadroom({"cursor": 0.95, "kimi": 0.0})
    selector = QuotaAwareModelSelector(port)
    assert await selector.select(Line(id="line-0", name="i", task="t"), []) == "cursor"

    port.quota["kimi"] = 0.05
    large = Line(id="line-1", name="i", task="t", estimated_size="large")
    assert await selector.select(large, []) == "cursor"

    # A medium line keeps the round-robin spread: headroom is only its gate.
    medium = Line(id="line-2", name="i", task="t")
    assert await selector.select(medium, []) == "kimi"


@pytest.mark.asyncio
async def test_a_large_task_spreads_inside_one_tier() -> None:
    """Inside a tier the least-assigned spread still decides."""
    port = _MutableHeadroom({"kimi": 0.9, "cursor": 0.0})
    selector = QuotaAwareModelSelector(port)
    assert await selector.select(Line(id="line-0", name="i", task="t"), []) == "kimi"

    port.quota["cursor"] = 0.6  # the same "high" tier as kimi
    large = Line(id="line-1", name="i", task="t", estimated_size="large")
    assert await selector.select(large, []) == "cursor"


@pytest.mark.asyncio
async def test_a_large_reassignment_moves_off_the_seated_model() -> None:
    """The line's own seat ranks last, so a reassignment still moves on.

    The line is seated on kimi, which holds far more quota; once quota leads
    for a large task the seat is demoted explicitly, so the reassignment takes
    the low-quota cursor -- and kimi is still the last resort when it is the
    only model left.
    """
    port = _MutableHeadroom({"kimi": 0.9, "cursor": 0.1})
    seated = Line(
        id="line-0", name="i", task="t", estimated_size="large", assigned_model="kimi"
    )
    assert await QuotaAwareModelSelector(port).select(seated, []) == "cursor"

    port.quota["cursor"] = 0.0
    assert await QuotaAwareModelSelector(port).select(seated, []) == "kimi"
