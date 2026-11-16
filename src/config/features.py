"""Feature-flag registry: code-declared catalog, deployment overrides, user opt-ins.

Each feature carries two orthogonal controls:

- ``enabled`` — the kill switch. ``false`` turns the feature off for everyone
  and hides every surface; the gate is irrelevant.
- ``gate`` — the access model while enabled:
    * ``none``    — available to everyone
    * ``opt_in``  — off until the user opts in (Settings toggle)
    * ``opt_out`` — on unless the user opts out (Settings toggle)
    * ``plan``    — requires a platform plan whose access tier meets the
      feature's ``min_tier`` (OSS/self-hosted deployments bypass plan gates,
      matching the search-provider tier-gating convention)

Three definition layers, resolved in order:

1. **Catalog** (this module): every feature the codebase knows about, with its
   hard defaults and the frontend-facing label/description. A feature must be
   declared here to exist anywhere else.
2. **Deployment posture** (``config.yaml`` ``features:`` section): per-deploy
   override of ``enabled`` / ``gate`` / ``min_tier``; unset fields inherit
   the catalog.
3. **User override** (``user_preferences.other_preference.feature_overrides``,
   server-side): honored only for ``opt_in``/``opt_out`` gates.

Server code resolves per-user via ``src.server.services.features``; this
module stays user-agnostic so the agent library can consult defaults.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, NamedTuple

from src.config import settings
from src.config.settings import get_infrastructure_config


class FeatureGate(StrEnum):
    NONE = "none"
    OPT_IN = "opt_in"
    OPT_OUT = "opt_out"
    PLAN = "plan"


# Gates whose features accept a per-user override (the Settings toggle).
USER_CONTROLLABLE_GATES = frozenset({FeatureGate.OPT_IN, FeatureGate.OPT_OUT})


@dataclass(frozen=True)
class FeatureSpec:
    """A code-declared feature and its hard defaults."""

    key: str
    label: str
    description: str
    enabled: bool = False
    gate: FeatureGate = FeatureGate.NONE
    min_tier: int | None = None  # plan gate only
    tradeoffs: str | None = None  # honest cost of opting in, shown in Settings


FEATURES: dict[str, FeatureSpec] = {
    "agent_teams": FeatureSpec(
        key="agent_teams",
        label="Agent collaboration",
        description=(
            "One capability for multi-agent work: the main model chooses "
            "whether to answer directly, delegate to a sub-agent, or run "
            "independent workstreams in parallel. There are no fixed role "
            "presets and no per-thread Swarm switch."
        ),
        enabled=True,
        gate=FeatureGate.OPT_OUT,
        tradeoffs=(
            "Delegated and parallel turns can spend more tokens than a single "
            "agent. The main model decides when the extra work is worth it; "
            "turning this off disables all sub-agent dispatch for new turns."
        ),
    ),
    "native_runtime": FeatureSpec(
        key="native_runtime",
        label="Native agent runtime (experimental)",
        description=(
            "Runs a thread inside a vendor agent CLI instead of the PTC agent. "
            "The CLI owns its prompt, tool loop, session and cache; Conductor "
            "carries the thread, history, cancellation, workspace transport and "
            "the HITL card. Codex is live end to end with token-level streaming "
            "and per-command approvals through its app-server protocol. DeepSeek "
            "DSH, MiniMax, MiMo and ZCode are live adapters with their own "
            "sessions/cache; their vendor surfaces do not expose approvals, so "
            "those capabilities stay explicitly false."
        ),
        enabled=True,
        gate=FeatureGate.OPT_IN,
        tradeoffs=(
            "A native thread cannot use Conductor's PTC tool loop or subagents. "
            "Codex threads can pause for a command/file approval; other CLIs run "
            "non-interactively and report approvals=false. Only Codex exposes "
            "true token deltas; the others stream event-level chunks. Answer "
            "quality and cost follow the selected CLI's account and policy, not "
            "Conductor's model settings. Keep it off unless you are testing this "
            "path."
        ),
    ),
    "memory_federation": FeatureSpec(
        key="memory_federation",
        label="Federated memory (experimental)",
        description=(
            "Search this account's explicitly bound local memory repositories "
            "alongside Conductor memory when building turn context."
        ),
        enabled=True,
        gate=FeatureGate.OPT_IN,
        tradeoffs=(
            "Only sources bound to this account are searched. Local sources can "
            "become unavailable independently, and their results retain source labels."
        ),
    ),
    "memory_consolidation": FeatureSpec(
        key="memory_consolidation",
        label="Memory consolidation proposals (experimental)",
        description=(
            "Periodically identifies exact duplicate memories and conflicting "
            "memories with an explicit shared source lineage. It creates review "
            "proposals only; a person must choose whether to apply one."
        ),
        enabled=True,
        gate=FeatureGate.OPT_IN,
        tradeoffs=(
            "The service reads your active user memories to find exact duplicates "
            "or explicit lineage conflicts. It does not rewrite memory or approve "
            "a proposal automatically."
        ),
    ),
    "memory": FeatureSpec(
        key="memory",
        label="Automatic memory",
        description=(
            "Uses saved memories to personalize new turns and creates memory "
            "review proposals from conversation activity. Turning this off "
            "stops automatic recall and capture while keeping manual review, "
            "forget, and export available."
        ),
        enabled=True,
        gate=FeatureGate.OPT_OUT,
        tradeoffs=(
            "Turning this off removes saved memory from automatic turn context "
            "and stops new automatic memory proposals. Existing memories and "
            "approved tool restrictions remain available for explicit management."
        ),
    ),
    "super_agent": FeatureSpec(
        key="super_agent",
        label="Super-agent (experimental)",
        description=(
            "Turns incoming GitHub comments into Tickets that a restricted "
            "Router agent evaluates and dispatches to background Workers. "
            "The Inbox page shows what needs a person; each Worker runs in "
            "its own thread and workspace directory."
        ),
        enabled=True,
        gate=FeatureGate.NONE,
        tradeoffs=(
            "A Router turn and a Worker turn cost model tokens; a busy "
            "repository can spend more than a normal chat. Workers can act "
            "on repository comments, so start with a narrow allowed list. "
            "Turning the flag off stops new intake and dispatch but lets "
            "running work finish."
        ),
    ),

    "site_crawl": FeatureSpec(
        key="site_crawl",
        label="Site crawling (experimental)",
        description=(
            "Gives the agent WebMap and WebCrawl tools: list a site's URLs, "
            "then crawl whole sections into workspace files for multi-page "
            "research (docs sites, document archives)."
        ),
        enabled=True,
        gate=FeatureGate.OPT_IN,
        tradeoffs=(
            "Crawling bills per delivered page up to the call's page limit, "
            "so a broad crawl costs more than targeted fetches. Experimental — "
            "behavior and pricing may change."
        ),
    ),
    "scratchpad": FeatureSpec(
        key="scratchpad",
        label="Scratchpad and working notes",
        description=(
            "Gives the agent a scratchpad folder in each thread for its "
            "intermediate files, and has it keep running notes there: key "
            "findings, decisions and why, and corrections you give mid-task. "
            "The notes carry a long task through after older messages in the "
            "thread are summarized."
        ),
        enabled=True,
        gate=FeatureGate.OPT_IN,
        tradeoffs=(
            "The agent spends a few extra tool calls per task keeping its "
            "notes. The scratchpad and its notes are deleted when you archive "
            "the thread, so keep anything you want to last in the workspace's "
            "own files."
        ),
    ),

}


class SystemFlag(NamedTuple):
    enabled: bool
    gate: FeatureGate
    min_tier: int | None


def get_feature_specs() -> dict[str, FeatureSpec]:
    return dict(FEATURES)


def system_flag(key: str) -> SystemFlag:
    """Deployment-level state of a feature: catalog defaults overlaid with the
    config.yaml ``features:`` entry (unset fields inherit the catalog)."""
    spec = FEATURES[key]
    override = get_infrastructure_config().features.get(key)
    enabled = spec.enabled
    gate = spec.gate
    min_tier = spec.min_tier
    if override is not None:
        if override.enabled is not None:
            enabled = override.enabled
        if override.gate is not None:
            gate = FeatureGate(override.gate)
        if override.min_tier is not None:
            min_tier = override.min_tier
    return SystemFlag(enabled=enabled, gate=gate, min_tier=min_tier)


def is_feature_enabled_system(key: str) -> bool:
    """Kill-switch check: is the feature on at all in this deployment?

    Gate-agnostic — an ``opt_in`` feature nobody enabled yet is still "on"
    here. Used where no user context exists but the feature merely needs to
    exist (skill registry listings, sandbox skill sync).
    """
    return system_flag(key).enabled


def has_plan_gated_features() -> bool:
    """Whether any cataloged feature currently resolves to a plan gate —
    lets callers skip the platform tier fetch entirely when none does."""
    return any(system_flag(key).gate is FeatureGate.PLAN for key in FEATURES)


def effective_flags(
    user_overrides: Mapping[str, object] | None,
    *,
    access_tier: int | None = None,
    platform_mode: bool | None = None,
) -> dict[str, bool]:
    """Resolve every cataloged feature for one user.

    ``user_overrides`` is the raw ``feature_overrides`` mapping from the
    user's preferences; non-bool values and overrides on non-opt gates are
    ignored rather than errored so a stale or hand-edited row can't break
    turns. Plan gates fail closed in platform mode when ``access_tier`` is
    unknown or below ``min_tier``; non-platform deployments bypass them.
    """
    if platform_mode is None:
        platform_mode = settings.HOST_MODE == "platform"

    resolved: dict[str, bool] = {}
    for key in FEATURES:
        flag = system_flag(key)
        if not flag.enabled:
            resolved[key] = False
            continue
        override = (user_overrides or {}).get(key)
        override = override if isinstance(override, bool) else None
        if flag.gate is FeatureGate.NONE:
            resolved[key] = True
        elif flag.gate is FeatureGate.OPT_IN:
            resolved[key] = override is True
        elif flag.gate is FeatureGate.OPT_OUT:
            resolved[key] = override is not False
        else:  # FeatureGate.PLAN
            resolved[key] = not platform_mode or (
                access_tier is not None
                and flag.min_tier is not None
                and access_tier >= flag.min_tier
            )
    return resolved


def default_feature_enabled(key: str) -> bool:
    """No-user-context effective default (opt_in and platform plan gates
    resolve False). Fallback for entry points that skip per-user resolution."""
    return effective_flags(None).get(key, False)
