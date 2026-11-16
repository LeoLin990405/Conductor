"""Capability-aware runtime routing for a *new* thread.

The native runtime is bound to a thread for its lifetime: a vendor CLI owns the
session, tool loop and prompt cache, so routing happens before thread creation,
not in the middle of a turn. This module is deliberately side-effect free so it
can be used by the chat create path, an API recommendation, or tests.

What this is not:

- it does not invoke a model or a CLI;
- it does not hide a model/agent choice inside a prompt;
- it cannot switch a live native thread to another provider.

Every decision carries a reason and the fallbacks the caller may offer, so the
product can show *why* a runtime was selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from src.utils.provider_roles import provider_role

from src.server.services.autonomy.roles import (
    provider_alias_spellings,
    runtime_identity,
)

CAPABILITY_NAMES = (
    "streaming",
    "tool_events",
    "session_resume",
    "cache_usage",
    "approvals",
    "mcp",
    "memory",
    "skills",
    "workspace_files",
    "cancel",
)

# Baselines for providers not named by the configured preferred order.
_PROVIDER_PRIORITY = {
    "cursor": 0.70,
    "grok": 0.66,
    "zcode": 0.35,
}

_TASK_AFFINITY = (
    (("代码", "coding", "code", "refactor", "重构", "bug", "debug", "test", "测试", "commit", "pr"), "codex", 0.18),
    (("资讯", "新闻", "研究", "研报", "市场", "宏观"), "deepseek-dsh", 0.12),
    (
        ("review", "审查", "架构", "architecture", "设计", "design"),
        "codex",
        0.08,
    ),
    (("cursor", "ide", "编辑器", "工作区", "workspace"), "cursor", 0.10),
    (("mcp", "memory", "记忆", "skill", "技能", "多工具"), None, 0.05),
)


@dataclass(frozen=True)
class RuntimeRequest:
    task: str = ""
    required_capabilities: tuple[str, ...] = ()
    preferred_provider: str | None = None
    allow_ptc: bool = True


@dataclass(frozen=True)
class RuntimeDecision:
    runtime: str
    reason: str
    fallbacks: tuple[str, ...]
    confidence: float
    scores: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "reason": self.reason,
            "fallbacks": list(self.fallbacks),
            "confidence": round(self.confidence, 3),
            "scores": {name: round(value, 3) for name, value in self.scores.items()},
        }


@dataclass(frozen=True)
class RuntimeRoutingPolicy:
    preferred_order: tuple[str, ...]
    fallback_only: tuple[str, ...]
    disabled_providers: tuple[str, ...] = ()


def _routing_policy(value: Any | None) -> RuntimeRoutingPolicy:
    if value is None:
        from src.config.settings import get_native_runtime_routing_config

        value = get_native_runtime_routing_config()
    return RuntimeRoutingPolicy(
        preferred_order=tuple(getattr(value, "preferred_order", ()) or ()),
        fallback_only=tuple(getattr(value, "fallback_only", ()) or ()),
        disabled_providers=tuple(getattr(value, "disabled_providers", ()) or ()),
    )


def _routing_key(provider: str) -> str:
    """The runtime id a routing decision binds, for any spelling of it.

    The alias table lives in ``autonomy.roles`` and is the only place that
    knows ``glm``/``zcode``/``zhipu``/``zai`` are one CLI, so routing, the
    headroom lookup and the snapshot reader cannot drift apart. Only the
    *quota* identity folds: a preference, a fallback rule or a disabled entry
    names one adapter, so ``zhipu`` (cancel-only) stays apart from ``zcode``
    (the Worker). Quota stays shared, because :func:`_headroom_state` asks for
    every alias spelling instead of this key.
    """
    return runtime_identity(provider)


def _ordered_candidates(
    scores: dict[str, float],
    policy: RuntimeRoutingPolicy,
    headroom: Mapping[str, str] | None = None,
) -> list[str]:
    configured_order = {
        _routing_key(provider): index
        for index, provider in enumerate(policy.preferred_order)
    }
    fallback_only = {_routing_key(provider) for provider in policy.fallback_only}

    fallback_positions = {
        _routing_key(provider): index
        for index, provider in enumerate(policy.fallback_only)
    }
    quota_rank = {"available": 0, "unknown": 1, "low": 2, "exhausted": 3}

    def state(provider: str) -> str:
        return _headroom_state(provider, headroom or {})

    def key(provider: str) -> tuple[int, int, int, float]:
        route_key = _routing_key(provider)
        is_fallback = route_key in fallback_only
        baseline = (
            fallback_positions[route_key]
            if is_fallback
            else configured_order.get(route_key, len(configured_order))
        )
        return (
            int(is_fallback),
            quota_rank[state(provider)],
            baseline,
            -scores[provider],
        )

    return sorted(scores, key=key)


def _scores_in_rank_order(ordered: list[str]) -> dict[str, float]:
    count = len(ordered)
    return {
        provider: (count - rank) / count
        for rank, provider in enumerate(ordered)
    }


def _caps(status: Any) -> dict[str, bool]:
    capabilities = getattr(status, "capabilities", None)
    if capabilities is None:
        return {}
    as_dict = getattr(capabilities, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    if isinstance(capabilities, dict):
        return dict(capabilities)
    return {
        name: bool(getattr(capabilities, name, False)) for name in CAPABILITY_NAMES
    }


def _candidate_name(status: Any) -> str:
    return str(getattr(status, "provider", ""))


def _affinity(provider: str, task: str) -> float:
    text = task.lower()
    score = 0.0
    for keywords, target, weight in _TASK_AFFINITY:
        if target is None:
            if any(word in text for word in keywords):
                score = max(score, weight)
            continue
        if target == provider and any(word in text for word in keywords):
            score += weight
    return score


def route_runtime(
    statuses: Iterable[Any],
    request: RuntimeRequest,
    *,
    headroom: Mapping[str, str] | None = None,
    policy: Any | None = None,
) -> RuntimeDecision:
    """Pick the best installed/live native runtime, else PTC.

    ``statuses`` are the same objects exposed by ``/api/v1/native-agents``.
    Required capabilities are hard gates; task affinity opens native routing
    without overriding the configured provider order. An explicit preferred
    provider wins when eligible.
    """
    required = tuple(
        name for name in request.required_capabilities if name in CAPABILITY_NAMES
    )
    routing_policy = _routing_policy(policy)
    preferred_order = {
        _routing_key(provider): index
        for index, provider in enumerate(routing_policy.preferred_order)
    }
    fallback_only = {
        _routing_key(provider) for provider in routing_policy.fallback_only
    }
    disabled_providers = {
        _routing_key(provider) for provider in routing_policy.disabled_providers
    }
    candidates: list[tuple[str, dict[str, bool]]] = []
    rejected: dict[str, str] = {}
    for status in statuses:
        provider = _candidate_name(status)
        if not provider:
            continue
        if _routing_key(provider) in disabled_providers:
            rejected[provider] = "disabled by operator policy"
            continue
        if provider_role(provider) != "native":
            rejected[provider] = f"provider role is {provider_role(provider)}-only"
            continue
        if not getattr(status, "installed", False) or not getattr(
            status, "live_verified", False
        ):
            continue
        caps = _caps(status)
        missing = [name for name in required if not caps.get(name, False)]
        if missing:
            rejected[provider] = "missing " + ", ".join(missing)
            continue
        candidates.append((provider, caps))

    scores: dict[str, float] = {}
    for provider, _caps_map in candidates:
        route_key = _routing_key(provider)
        if route_key in fallback_only:
            # Affinity must not promote a fallback-only provider above regular
            # candidates. An explicit preference remains an intentional override.
            scores[provider] = _PROVIDER_PRIORITY.get(provider, 0.0)
        elif route_key in preferred_order:
            scores[provider] = 1.0 - 0.01 * preferred_order[route_key]
        else:
            scores[provider] = _PROVIDER_PRIORITY.get(provider, 0.5)

    quota = headroom if isinstance(headroom, Mapping) else {}
    ordered = _ordered_candidates(scores, routing_policy, quota)
    ordered = [
        provider
        for provider in ordered
        if _headroom_state(provider, quota) != "exhausted"
    ]

    known_states = [
        _headroom_state(provider, quota) for provider, _ in candidates
    ]
    all_exhausted = bool(candidates) and all(
        state == "exhausted" for state in known_states
    )

    # A general chat turn with no native-shaped signal should stay on PTC: the
    # router is a capability/affinity gate, not a policy that forces every new
    # thread onto a vendor CLI. Explicit preference or required capabilities
    # still move the turn to native.
    if (
        request.allow_ptc
        and not required
        and not (request.preferred_provider or "").strip()
        and not any(_affinity(provider, request.task) for provider, _ in candidates)
    ):
        return RuntimeDecision(
            runtime="ptc",
            reason="no native capability or task affinity signal; PTC is the default",
            fallbacks=tuple(ordered),
            confidence=0.5,
            scores=_scores_in_rank_order(ordered),
        )

    preferred = (request.preferred_provider or "").strip()
    preferred_key = _routing_key(preferred) if preferred else ""
    preferred_match = next(
        (
            name
            for name in scores
            if _routing_key(name) == preferred_key
        ),
        None,
    )
    if (
        preferred
        and provider_role(preferred) != "native"
        and preferred not in rejected
    ):
        rejected[preferred] = f"provider role is {provider_role(preferred)}-only"
    rejected_match = next(
        (
            name
            for name in rejected
            if _routing_key(name) == preferred_key
        ),
        None,
    )
    if not preferred and all_exhausted:
        return RuntimeDecision(
            runtime="ptc" if request.allow_ptc else "none",
            reason=(
                "all eligible native providers are known exhausted; using PTC"
                if request.allow_ptc
                else "all eligible native providers are known exhausted; no runtime available"
            ),
            fallbacks=(),
            confidence=0.55,
            scores=_scores_in_rank_order(
                _ordered_candidates(scores, routing_policy, quota)
            ),
        )
    if preferred:
        if preferred_match:
            ordered = [preferred_match]
        elif rejected_match:
            # An explicit provider that cannot meet the hard capabilities must
            # not silently become a different provider without saying so.
            ordered = []
        else:
            ordered = []
    fallbacks: tuple[str, ...] = ()
    if ordered:
        chosen = ordered[0]
        displayed_scores = _scores_in_rank_order(ordered)
        fallbacks = tuple(ordered[1:])
        if request.allow_ptc:
            fallbacks = (*fallbacks, "ptc")
        affinity = _affinity(chosen, request.task)
        capability_text = (
            f", meets: {', '.join(required)}" if required else ""
        )
        route_key = _routing_key(chosen)
        if preferred:
            reason = f"explicit preference {chosen} is eligible{capability_text}"
        elif route_key in fallback_only:
            reason = f"{chosen} is the configured fallback runtime{capability_text}"
        elif route_key in preferred_order:
            reason = (
                f"{chosen} ranks highest among eligible providers in configured order"
                f"{capability_text}"
            )
        else:
            reason = f"{chosen} is the best eligible native runtime{capability_text}"
        state = _headroom_state(chosen, quota)
        if not preferred and state == "available":
            reason += "; fresh quota headroom available"
        elif not preferred and state == "low":
            reason += "; fresh balance is low"
        elif not preferred and any(item == "available" for item in known_states):
            reason += "; no fresh quota reading for this provider"
        if (
            affinity
            and not preferred
            and route_key not in fallback_only
            and route_key not in preferred_order
        ):
            reason += f" (task affinity +{affinity:.2f})"
        confidence = (
            0.99
            if preferred
            else 0.8
            if route_key in preferred_order or route_key in fallback_only
            else 0.7
        )
        return RuntimeDecision(
            runtime=chosen,
            reason=reason,
            fallbacks=fallbacks,
            confidence=confidence,
            scores=displayed_scores,
        )

    if preferred and rejected_match:
        reason = (
            f"preferred {preferred} is not eligible "
            f"({rejected[rejected_match]}); using PTC"
        )
    elif required:
        reason = (
            "no installed/live native runtime meets "
            + ", ".join(required)
            + "; using PTC"
        )
    else:
        reason = "no installed/live native runtime; using PTC"
    if not request.allow_ptc:
        reason += "; PTC is disallowed"
    return RuntimeDecision(
        runtime="ptc" if request.allow_ptc else "none",
        reason=reason,
        fallbacks=(),
        confidence=0.45,
            scores=_scores_in_rank_order(
                _ordered_candidates(scores, routing_policy, quota)
            ),
    )


def _headroom_state(provider: str, headroom: Mapping[str, str]) -> str:
    """Read every alias spelling of one CLI; the most exhausted reading wins."""
    aliases = provider_alias_spellings(provider)
    valid_states = {"available", "unknown", "low", "exhausted"}
    states = [
        headroom[name]
        for name in aliases
        if headroom.get(name) in valid_states
    ]
    if not states:
        return "unknown"
    rank = {"available": 0, "unknown": 1, "low": 2, "exhausted": 3}
    return max(states, key=rank.__getitem__)


def resolve_requested_provider(
    requested: str | None,
    *,
    task: str = "",
    required_capabilities: Iterable[str] = (),
    allow_ptc: bool = True,
) -> tuple[str | None, RuntimeDecision | None]:
    """Resolve a create-thread request into a concrete binding.

    ``requested`` may be a provider id or the ``auto`` sentinel. Returns the
    provider to persist (None means PTC) and, for auto, the decision object the
    caller should record in thread metadata. Existing threads never call this:
    their binding is immutable.
    """
    value = (requested or "").strip()
    if value != "auto":
        return (runtime_identity(value) or None), None
    from .registry import list_statuses

    decision = route_runtime(
        list_statuses(),
        RuntimeRequest(
            task=task,
            required_capabilities=tuple(required_capabilities),
            allow_ptc=allow_ptc,
        ),
    )
    provider = None if decision.runtime in {"ptc", "none"} else decision.runtime
    return provider, decision


async def aresolve_requested_provider(
    requested: str | None,
    *,
    task: str = "",
    required_capabilities: Iterable[str] = (),
    allow_ptc: bool = True,
) -> tuple[str | None, RuntimeDecision | None]:
    """Resolve automatic routing with the configured bounded usage snapshot."""
    value = (requested or "").strip()
    if value != "auto":
        return (runtime_identity(value) or None), None
    from .registry import list_statuses
    from .usage_headroom import load_usage_headroom

    headroom = await load_usage_headroom()
    decision = route_runtime(
        list_statuses(),
        RuntimeRequest(
            task=task,
            required_capabilities=tuple(required_capabilities),
            allow_ptc=allow_ptc,
        ),
        headroom=headroom,
    )
    provider = None if decision.runtime in {"ptc", "none"} else decision.runtime
    return provider, decision
