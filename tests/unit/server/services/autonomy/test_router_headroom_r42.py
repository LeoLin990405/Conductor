"""Router headroom canonicalizes vendor-CLI aliases before the state is read.

``route_runtime`` and the production resolvers that feed it
(``aresolve_requested_provider``, the ``/native-agents`` recommendation
endpoint, the native-delegation tool) look up one vendor's quota through
whatever spelling the snapshot used. glm / zcode / zhipu / zai are one CLI, so:

* a snapshot keyed by any of those spellings must refuse every spelling of the
  CLI as a candidate, and
* a candidate spelled differently from the snapshot key must still be read
  (a ``glm`` candidate reads a ``zcode`` snapshot, and back).

The snapshots here are plain dicts shaped like ``load_usage_headroom`` returns;
no file or network is touched, and the production-resolver case stubs the
loader the same way the live path loads it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.server.services.native_runtime.router import (
    RuntimeRequest,
    RuntimeRoutingPolicy,
    aresolve_requested_provider,
    route_runtime,
)
from src.server.services.native_runtime.usage_headroom import read_usage_headroom


@dataclass
class Status:
    provider: str
    installed: bool = True
    live_verified: bool = True
    capabilities: dict | None = None


def _glm_and_codex(candidate: str) -> list[Status]:
    return [
        Status(candidate, capabilities={"streaming": True}),
        Status("codex", capabilities={"streaming": True}),
    ]


def _policy(*providers: str) -> RuntimeRoutingPolicy:
    return RuntimeRoutingPolicy(preferred_order=providers, fallback_only=())


# (candidate spelling, snapshot key that carries the exhausted state).
# Every cross product of a GLM spelling on either side must read exhausted.
_GLM_EXHAUSTED_VECTORS = [
    ("zcode", "zcode"),
    ("zcode", "glm"),
    ("zcode", "zhipu"),
    ("zcode", "zai"),
    ("glm", "zcode"),
    ("glm", "zai"),
    ("zhipu", "zcode"),
    ("zhipu", "zai"),
    ("zai", "zcode"),
    ("zai", "zhipu"),
]


@pytest.mark.parametrize("candidate,snapshot_key", _GLM_EXHAUSTED_VECTORS)
def test_any_glm_spelling_reads_the_exhausted_state(candidate, snapshot_key):
    """An exhausted GLM snapshot refuses the CLI however either side spells it."""
    decision = route_runtime(
        _glm_and_codex(candidate),
        RuntimeRequest(task="review architecture"),
        policy=_policy(candidate, "codex"),
        headroom={snapshot_key: "exhausted", "codex": "available"},
    )

    assert decision.runtime == "codex"
    assert candidate not in decision.fallbacks
    assert "codex" in decision.scores


def test_a_healthy_glm_snapshot_is_not_refused():
    """The canonicalization only folds spellings; a fresh reading still routes."""
    decision = route_runtime(
        _glm_and_codex("zcode"),
        RuntimeRequest(task="review architecture"),
        policy=_policy("zcode", "codex"),
        headroom={"zai": "available", "codex": "low"},
    )

    assert decision.runtime == "zcode"


def test_the_most_exhausted_spelling_wins_when_they_conflict():
    """Conflicting spellings of one CLI never manufacture availability."""
    decision = route_runtime(
        _glm_and_codex("zcode"),
        RuntimeRequest(task="review architecture"),
        policy=_policy("zcode", "codex"),
        headroom={"zai": "available", "zcode": "exhausted", "codex": "available"},
    )

    assert decision.runtime == "codex"
    assert "zcode" not in decision.fallbacks


def test_deepseek_alias_group_reads_the_same_state():
    """The same folding covers deepseek / deepseek-dsh / dsh."""
    statuses = [
        Status("deepseek-dsh", capabilities={"streaming": True}),
        Status("codex", capabilities={"streaming": True}),
    ]
    decision = route_runtime(
        statuses,
        RuntimeRequest(task="review architecture"),
        policy=_policy("deepseek-dsh", "codex"),
        headroom={"deepseek": "exhausted", "codex": "available"},
    )

    assert decision.runtime == "codex"
    assert "deepseek-dsh" not in decision.fallbacks


def test_production_auto_resolver_canonicalizes_alias_snapshot(monkeypatch):
    """The live ``auto`` resolver binds the healthy peer, not the exhausted CLI.

    This exercises the production caller (``aresolve_requested_provider``), not
    a wrapper: the loader it uses is stubbed to a snapshot keyed by ``glm``
    while the candidate CLI is ``zcode``, and the exhausted reading must still
    be honoured.
    """
    monkeypatch.setattr(
        "src.server.services.native_runtime.registry.list_statuses",
        lambda: _glm_and_codex("zcode"),
    )
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
        AsyncMock(return_value={"glm": "exhausted", "codex": "available"}),
    )

    provider, decision = asyncio.run(
        aresolve_requested_provider("auto", task="review architecture")
    )

    assert provider == "codex"
    assert decision is not None


def test_production_resolver_keeps_a_healthy_alias_cli(monkeypatch):
    """The mirror: a fresh alias reading still binds the preferred CLI."""
    monkeypatch.setattr(
        "src.server.services.native_runtime.registry.list_statuses",
        lambda: _glm_and_codex("zcode"),
    )
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
        AsyncMock(return_value={"zhipu": "available", "codex": "low"}),
    )

    provider, decision = asyncio.run(
        aresolve_requested_provider("auto", task="review architecture")
    )

    assert provider == "zcode"
    assert decision is not None


def test_a_binary_spelled_snapshot_refuses_its_own_runtime(tmp_path):
    """The real reader folds an ``mcode`` row to ``minimax`` before routing.

    ``route_runtime`` already folds the minimax/mcode group, but the state only
    reaches it if the production reader (``read_usage_headroom``) keeps the
    row. A fresh MiniMax snapshot spelled ``mcode`` at 100% usage must be
    normalised to the ``minimax`` key, and only then does ``route_runtime``
    refuse the MiniMax runtime; without the fold the row is dropped, the state
    is empty, and MiniMax is routed as if it were wide open.
    """
    payload = {
        "providers": [
            {
                "provider": "mcode",
                "kind": "quota",
                "status": "ok",
                "updated_at": "2026-11-06T12:00:00Z",
                "windows": [
                    {"used_percent": 100.0, "resets_at_utc": "2026-11-06T17:00:00Z"}
                ],
            }
        ]
    }
    path = tmp_path / "usage-headroom.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    states = read_usage_headroom(
        str(path), now=datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert states == {"minimax": "exhausted"}

    decision = route_runtime(
        [
            Status("minimax", capabilities={"streaming": True}),
            Status("codex", capabilities={"streaming": True}),
        ],
        RuntimeRequest(task="review architecture"),
        policy=_policy("minimax", "codex"),
        headroom=states,
    )

    assert decision.runtime == "codex"
    assert "minimax" not in decision.fallbacks


# (snapshot spelling, the canonical runtime it must read as). Each vendor CLI
# carries a second spelling the router/selection readers already fold into one
# group; the real reader (``read_usage_headroom``) must fold it too, or the
# exhausted row is dropped and the runtime is routed as if it were wide open.
_VENDOR_ALIAS_VECTORS = [
    ("dsh", "deepseek-dsh"),
    ("claudedev", "claude"),
    ("arkcli", "doubao"),
]
_SPENT_QUOTA = {
    "kind": "quota",
    "windows": [{"used_percent": 100.0, "resets_at_utc": "2026-11-06T17:00:00Z"}],
}
# DeepSeek is metered by balance, whatever spelling the row uses: a ``dsh`` row
# is classified by its folded key, so its exhaustion is a spent balance.
_SPENT_BALANCE = {"kind": "balance", "balance": {"amount": 0, "currency": "CNY"}}


@pytest.mark.parametrize("alias,canonical", _VENDOR_ALIAS_VECTORS)
def test_a_vendor_spelled_snapshot_refuses_its_own_runtime(tmp_path, alias, canonical):
    """A fresh exhausted row spelled by a vendor alias refuses its runtime.

    ``read_usage_headroom`` folds dsh→deepseek-dsh, claudedev→claude and
    arkcli→doubao before storage; only then does ``route_runtime`` refuse the
    runtime. Without the fold the row is dropped, the state is empty, and the
    CLI is routed as if it still had all its quota -- the exhausted provider
    stays eligible.
    """
    spent = _SPENT_BALANCE if canonical == "deepseek-dsh" else _SPENT_QUOTA
    payload = {
        "providers": [
            {
                "provider": alias,
                "status": "ok",
                "updated_at": "2026-11-06T12:00:00Z",
                **spent,
            }
        ]
    }
    path = tmp_path / "usage-headroom.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    states = read_usage_headroom(
        str(path), now=datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert states == {canonical: "exhausted"}

    decision = route_runtime(
        [
            Status(canonical, capabilities={"streaming": True}),
            Status("codex", capabilities={"streaming": True}),
        ],
        RuntimeRequest(task="review architecture"),
        policy=_policy(canonical, "codex"),
        headroom=states,
    )

    assert decision.runtime == "codex"
    assert canonical not in decision.fallbacks
    assert "codex" in decision.scores


# --- the live dispatch service reads the real CodexBar-backed port ----------
# The service builds its own planner and selector; these tests drive that
# production wiring with a fake CodexBar snapshot written to tmp_path (the
# default path is pointed at it) and a stubbed native usage loader, so no real
# file, CLI or network is read.


def _codexbar_snapshot(path, used: dict[str, float]):
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    path.write_text(
        json.dumps(
            {
                "updatedAt": stamp,
                "providers": [
                    {
                        "provider": provider,
                        "kind": "quota",
                        "status": "ok",
                        "updated_at": stamp,
                        "windows": [{"used_percent": percent}],
                    }
                    for provider, percent in used.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _live_service(tmp_path, monkeypatch, *, pool, used, plan_lines, headroom=None):
    import src.tools.auto_dispatch.headroom_codexbar as codexbar
    from src.server.services.autonomy.roles import ROLE_POLICY_ENV
    from src.server.services.dispatch import DispatchService, DispatchSettings
    from src.tools.auto_dispatch import FileApprovalGate, InMemoryDispatchStore

    # The default work tail (doubao) is spent, so only the named pool competes.
    monkeypatch.setattr(
        codexbar,
        "DEFAULT_SNAPSHOT_PATH",
        _codexbar_snapshot(
            tmp_path / "codexbar-snapshot.json", {"doubao": 100.0, **used}
        ),
    )
    # The native usage snapshot is the second reader of the default port; it
    # has nothing to say here.
    monkeypatch.setattr(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
        AsyncMock(return_value={}),
    )
    policy_file = tmp_path / "role-policy.json"
    policy_file.write_text(json.dumps({"developer_pool": list(pool)}), encoding="utf-8")
    monkeypatch.setenv(ROLE_POLICY_ENV, str(policy_file))
    payload = {
        "schema_version": 1,
        "spec": {"title": "T", "body": "B"},
        "lines": [
            {
                "name": name,
                "task": name,
                "depends_on": [],
                "write_scope": [f"src/{name}.py"],
                "declared_paths": [f"src/{name}.py"],
                "acceptance": [f"cmd: pytest -q tests/{name}.py"],
                "risk_bearing": False,
                **extra,
            }
            for name, extra in plan_lines
        ],
    }

    async def complete(prompt: str, system: str) -> str:
        return json.dumps(payload)

    monkeypatch.setattr(DispatchService, "_planner_complete", lambda self, user_id: complete)
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    kwargs = {"headroom": headroom} if headroom is not None else {}
    return DispatchService(
        DispatchSettings(adapters="live", runs_dir=tmp_path / "runs", repo_root=repo),
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        **kwargs,
    )


def _collaborators(service):
    from src.tools.auto_dispatch import Dispatch

    return service._machine(
        Dispatch(id="d1", goal="ship it", user_id="u1", created_at="now")
    )._c


def test_the_live_service_defaults_to_the_codexbar_port(tmp_path, monkeypatch):
    """Planner and selector share one port, and it reads the CodexBar file."""
    from src.tools.auto_dispatch.headroom_codexbar import CodexBarHeadroom
    from src.tools.auto_dispatch.selection import MostConstrainedHeadroom

    service = _live_service(
        tmp_path, monkeypatch, pool=("glm",), used={"zai": 100.0}, plan_lines=()
    )
    collaborators = _collaborators(service)
    port = collaborators.planner._headroom
    # N63-1: the drive's port is wrapped in a per-drive plan_status cache.
    assert collaborators.selector._headroom is port
    port = port.inner

    assert isinstance(port, MostConstrainedHeadroom)
    assert any(isinstance(inner, CodexBarHeadroom) for inner in port.ports)
    # Every spelling of the GLM Worker reads the one ``zai`` row as spent.
    for spelling in ("glm", "zcode", "zhipu", "zai", "zcode-cli"):
        assert asyncio.run(port.remaining(spelling)) == 0.0


def test_an_injected_port_still_wins_over_the_default(tmp_path, monkeypatch):
    from src.tools.auto_dispatch.selection import UnlimitedHeadroom

    injected = UnlimitedHeadroom()
    service = _live_service(
        tmp_path,
        monkeypatch,
        pool=("glm",),
        used={"zai": 100.0},
        plan_lines=(),
        headroom=injected,
    )
    collaborators = _collaborators(service)

    assert collaborators.planner._headroom.inner is injected
    assert collaborators.selector._headroom is collaborators.planner._headroom


def test_the_live_planner_refuses_a_spent_worker_with_a_reason(tmp_path, monkeypatch):
    """Production assignment: a CodexBar-exhausted GLM is never handed the line.

    The line is unassigned with ``quota_exhausted`` rather than seated on a
    Worker whose window is spent; the old default port (native snapshot or
    unlimited) never read this file and placed the line on glm.
    """
    service = _live_service(
        tmp_path,
        monkeypatch,
        pool=("glm",),
        used={"zai": 100.0},
        plan_lines=(("one", {}),),
    )

    planned = asyncio.run(_collaborators(service).planner.plan_dag("ship it"))

    assert planned.unassigned_reasons() == ("line-0:quota_exhausted",)


def test_the_live_planner_skips_a_spent_worker_for_a_peer(tmp_path, monkeypatch):
    service = _live_service(
        tmp_path,
        monkeypatch,
        pool=("glm", "kimi"),
        used={"zai": 100.0, "kimi": 10.0},
        plan_lines=(("one", {}),),
    )

    planned = asyncio.run(_collaborators(service).planner.plan_dag("ship it"))

    assert planned.assignment.provider_of("line-0") == "kimi"
    assert planned.unassigned_reasons() == ()


def test_the_live_planner_routes_a_large_line_to_the_widest_quota(tmp_path, monkeypatch):
    """The planner's ``estimated_size`` reaches the production assignment.

    kimi (first in the roster, 5% left) and cursor (95% left) are both open; a
    large line goes to cursor, a medium one keeps the roster order.
    """
    service = _live_service(
        tmp_path,
        monkeypatch,
        pool=("kimi", "cursor"),
        used={"kimi": 95.0, "cursor": 5.0},
        plan_lines=(("heavy", {"estimated_size": "large"}),),
    )

    planned = asyncio.run(_collaborators(service).planner.plan_dag("ship it"))

    assert planned.assignment.provider_of("line-0") == "cursor"


def test_the_live_selector_reads_the_same_spent_window(tmp_path, monkeypatch):
    """The reassignment selector reads the CodexBar port, not a second quota."""
    from src.tools.auto_dispatch.types import Line

    service = _live_service(
        tmp_path,
        monkeypatch,
        pool=("kimi", "cursor"),
        used={"kimi": 95.0, "cursor": 5.0},
        plan_lines=(),
    )
    selector = _collaborators(service).selector
    line = Line(
        id="line-0",
        name="heavy",
        task="t",
        estimated_size="large",
        write_scope=("src/heavy.py",),
    )

    assert asyncio.run(selector.select(line, [], dispatch_id="test")) == "cursor"
