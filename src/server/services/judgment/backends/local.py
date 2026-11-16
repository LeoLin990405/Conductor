"""Local small-model backend: interface only in v1 (N61 4.1, section 9).

No dependency is added and no model is downloaded. A port built on this
backend returns ERROR for every judgment, which each use's fallback handles.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..types import JudgmentRequest

_REASON = (
    "local judgment backend is not implemented in v1: no local model is "
    "installed (candidates: jev-lite, system-one-open; see N61 section 9)"
)


def unavailable_reason() -> str:
    """Why the local backend cannot answer."""
    return _REASON


class LocalJudgmentBackend:
    name = "local"
    calibrated = False

    def unavailable_reason(self) -> str:
        return unavailable_reason()

    async def answer(
        self, request: JudgmentRequest
    ) -> Mapping[str, Mapping[str, float]]:
        raise NotImplementedError(_REASON)
