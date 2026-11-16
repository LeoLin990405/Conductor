"""The machine's Super-agent assignment collaborator (N73 L4/L5).

The service builds one per dispatch (``DispatchService._machine``): the
backend (``LLMService.complete`` with the background slot), a feasibility
snapshot factory, the outcome port for success rates, trusted hints and the
effective mode and sample (Q8 rollout, :mod:`.rollout`). The machine only
calls :meth:`decide` and :meth:`sample`; with no assigner wired the machine
behaves exactly as before.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from . import runner
from .context import assemble_inputs
from .feasibility import Snapshot
from .modes import assign_mode, assign_sample


class SuperagentAssigner:
    def __init__(
        self,
        backend: Any,
        *,
        snapshot_for: Callable[[Any, Any], Snapshot],
        outcomes_port: Any = None,
        hints_for: Callable[[Any], Sequence[tuple[str, str]]] | None = None,
        mode: Callable[[], str] = assign_mode,
        sample: Callable[[], float] = assign_sample,
    ) -> None:
        self._backend = backend
        self._snapshot_for = snapshot_for
        self._port = outcomes_port
        self._hints_for = hints_for or (lambda dispatch: ())
        self._mode = mode
        self._sample = sample

    def mode(self) -> str:
        return self._mode()

    def sample(self) -> float:
        return self._sample()

    async def decide(self, dispatch: Any, lines: Sequence[Any]) -> dict[str, runner.LineDecision]:
        async def inputs_for(subset: Sequence[Any]) -> Any:
            return await assemble_inputs(list(subset), self._port)

        return await runner.decide(
            lines,
            mode=self.mode(),
            backend=self._backend,
            snapshot_for=lambda line: self._snapshot_for(dispatch, line),
            inputs_for=inputs_for,
            hints=tuple(self._hints_for(dispatch)),
        )
