"""A scripted judgment backend."""

from __future__ import annotations

import asyncio
from typing import Any

from src.server.services.judgment.uses import USES

# Every use samples every key: the v1 default rate (0.1) would skip most tests.
FULL_SAMPLE = {f"CONDUCTOR_JUDGMENT_{name.upper()}_SAMPLE": "1" for name in USES}


class FakeBackend:
    def __init__(self, *outputs: Any, name: str = "fake", delay: float = 0.0) -> None:
        self.outputs = list(outputs)
        self.name = name
        self.delay = delay
        self.calls = 0

    async def answer(self, request):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.outputs[min(self.calls, len(self.outputs)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out
