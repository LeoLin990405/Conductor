"""N73 L5: the live Super-agent assignment collaborator for a dispatch.

``DispatchService._machine`` wires one per live dispatch:

* the backend is one structured ``LLMService.complete`` call (background
  slot) on the Router's model (``router_handoff.active_router_model``, R6 L3),
  wrapped in ``AssignBackend`` so an ERROR is never raised and never a choice;
* the feasibility snapshot is gathered by code before the call: the advisory
  work roster (the policy pool plus the built-in roster, never claude/codex),
  ``can_take_line``, the CLI binaries on PATH, the operator's
  ``disabled_providers``, the dispatch's provider skips, CodexBar headroom and
  plan status, the paid-credit switch and the line's reviewers;
* trusted hints: the role registry's preferred order and ``avoid`` reasons,
  the providers' headroom, the runtimes the launcher supports;
* the mode and sample come from the Q8 rollout over recent dispatches
  (``superagent_assign.rollout``); an explicit ``ASSIGN_MODE`` overrides.

Nothing here reads code or task files and nothing leaves the LLMService path.
"""

from __future__ import annotations

import logging
import shutil
import time
from typing import Any, Mapping, Sequence

from src.tools.auto_dispatch.superagent_assign.assigner import SuperagentAssigner
from src.tools.auto_dispatch.superagent_assign.backend import AssignBackend
from src.tools.auto_dispatch.superagent_assign.feasibility import Snapshot, launcher_runtimes
from src.tools.auto_dispatch.superagent_assign.rollout import (
    PostgresRolloutStore,
    RolloutState,
    advance,
)

logger = logging.getLogger(__name__)

#: The executable ``dispatch_launch.sh`` runs for each roster name.
LAUNCH_BINARIES: Mapping[str, tuple[str, ...]] = {
    "kimi": ("kimi",),
    "glm": ("zcode", "zcode-cli"),
    "grok": ("grok",),
    "cursor": ("cursor-agent",),
    "stepfun": ("step",),
    "deepseek": ("dsh",),
    "minimax": ("mcode",),
    "doubao": ("arkcli",),
}
ROLLOUT_WINDOW = 200


def _disabled() -> tuple[str, ...]:
    try:
        from src.config.settings import get_native_runtime_routing_config

        return tuple(
            str(p).strip().lower()
            for p in get_native_runtime_routing_config().disabled_providers
            if str(p).strip()
        )
    except Exception:
        return ()


class LiveAssigner(SuperagentAssigner):
    """Prefetches the async facts (rollout, headroom), then runs the round."""

    def __init__(
        self,
        llm: Any,
        *,
        policy: Any,
        headroom: Any,
        store: Any,
        outcomes_port: Any,
        which=shutil.which,
        clock=time.time,
        rollout_store: Any = None,
    ) -> None:
        self._rollout_store = rollout_store or PostgresRolloutStore()
        self._policy = policy
        self._headroom_port = headroom
        self._store = store
        self._which = which
        self._clock = clock
        self._readings: dict[str, float] = {}
        self._plan: dict[str, dict] = {}
        self.rollout: RolloutState = RolloutState("shadow", "shadow", 1.0)
        super().__init__(
            AssignBackend(self._complete_factory(llm)),
            snapshot_for=self._snapshot,
            outcomes_port=outcomes_port,
            hints_for=self._hints,
            mode=lambda: self.rollout.mode,
            sample=lambda: self.rollout.sample,
        )

    @staticmethod
    def _complete_factory(llm: Any):
        async def complete(**kwargs: Any) -> Any:
            if llm is None:
                raise RuntimeError("no LLM service for Super-agent assignment")
            from src.server.services.autonomy.router_handoff import active_router_model

            model = active_router_model()
            if model:
                kwargs.setdefault("request_model", model)
            return await llm.complete(**kwargs)

        return complete

    def roster(self) -> tuple[str, ...]:
        return tuple(self._policy.advisory_work_roster())

    async def _prefetch(self) -> None:
        try:
            recent = await self._store.list(limit=ROLLOUT_WINDOW)
        except Exception:
            recent = []
        try:
            self.rollout = await advance(recent, self._rollout_store)
        except Exception:
            # The stored stage cannot be read: shadow, never an enforce guess.
            logger.warning("N73 rollout store unavailable; using shadow", exc_info=True)
            self.rollout = RolloutState("shadow", "shadow", 1.0)
        if self.rollout.rolled_back:
            logger.warning(
                "N73 rollout rolled back to shadow: %s", ", ".join(self.rollout.reasons)
            )
        self._readings, self._plan = {}, {}
        for name in self.roster():
            try:
                self._readings[name] = float(await self._headroom_port.remaining(name))
            except Exception:
                continue
            reader = getattr(self._headroom_port, "plan_status", None)
            if reader is not None:
                try:
                    status = await reader(name)
                except Exception:
                    status = None
                if isinstance(status, dict):
                    self._plan[name] = status

    async def decide(self, dispatch: Any, lines: Sequence[Any]):
        await self._prefetch()
        if self.rollout.mode == "off":
            return {}
        if self.rollout.mode == "shadow" and not shadow_call_enabled():
            # Review fix 4: a shadow call costs a Router-model call per
            # assignment; the operator can switch it off.
            return {}
        return await super().decide(dispatch, lines)

    def _snapshot(self, dispatch: Any, line: Any) -> Snapshot:
        from src.server.services.autonomy.roles import can_take_line
        from src.tools.auto_dispatch.selection import paid_credits_allowed

        roster = self.roster()
        gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
        snap = (gates.get("lines") or {}).get(line.id) if isinstance(gates.get("lines"), dict) else None
        machine = snap.get("machine") if isinstance(snap, dict) and isinstance(snap.get("machine"), dict) else {}
        reviewers = [
            str(machine.get(key) or "")
            for key in ("primary_reviewer", "secondary_reviewer")
            if machine.get(key)
        ]
        return Snapshot(
            eligible_models=roster,
            can_take_line=tuple(n for n in roster if can_take_line(n, line)[0]),
            cli_found=tuple(
                n for n in roster if any(self._which(b) for b in LAUNCH_BINARIES.get(n, (n,)))
            ),
            disabled_providers=_disabled(),
            provider_skips=dict(gates.get("provider_skips") or {}),
            now=self._clock(),
            headroom=dict(self._readings),
            plan_full={n: bool(s.get("plan_full")) for n, s in self._plan.items()},
            paid_credits={n: s.get("credits") for n, s in self._plan.items()},
            paid_credits_allowed=paid_credits_allowed(),
            line_reviewers=tuple(reviewers),
        )

    def _hints(self, dispatch: Any) -> list[tuple[str, str]]:
        hints = self._policy.assignment_hints()
        out = [("preferred", ", ".join(hints.get("preferred") or []) or "(none)")]
        for name, reason in sorted((hints.get("avoid") or {}).items()):
            out.append((f"avoid {name}", reason or "operator listed"))
        for name in self.roster():
            if name in self._readings:
                out.append((f"headroom {name}", f"{self._readings[name]:.2f}"))
            runtimes = sorted(launcher_runtimes(name))
            out.append((f"runtimes {name}", ", ".join(runtimes)))
        return out


SHADOW_CALL_ENV = "CONDUCTOR_DISPATCH_ASSIGN_SHADOW_CALL"


def shadow_call_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether shadow mode still makes the Super-agent call (default on)."""
    import os

    source = os.environ if environ is None else environ
    return str(source.get(SHADOW_CALL_ENV, "") or "").strip().lower() not in {
        "0", "false", "no", "off",
    }


def build_assigner(
    *, llm: Any, policy: Any, headroom: Any, store: Any, outcomes_port: Any
) -> LiveAssigner | None:
    """The live assigner, or None when the operator set ``ASSIGN_MODE=off``."""
    import os

    from src.tools.auto_dispatch.superagent_assign.modes import ASSIGN_MODE_ENV

    if str(os.environ.get(ASSIGN_MODE_ENV, "") or "").strip().lower() == "off":
        return None
    return LiveAssigner(
        llm, policy=policy, headroom=headroom, store=store, outcomes_port=outcomes_port
    )
