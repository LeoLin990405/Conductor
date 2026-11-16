"""Review fix 1: after an auto:n73 mark, neither delivery nor push happens."""

from __future__ import annotations

import pytest

from src.server.services.dispatch import DispatchService
from src.tools.auto_dispatch.protocols import ApprovalDecision
from src.tools.auto_dispatch.types import Dispatch, DispatchState

pytestmark = pytest.mark.asyncio


class _Gate:
    def __init__(self, decision):
        self._decision = decision

    async def decision(self, token):
        return self._decision


class _Coordinator:
    def delivery_lock(self, dispatch_id):
        return _AsyncNull()


class _AsyncNull:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _Port:
    def __init__(self):
        self.delivered = 0

    async def deliver(self, row):
        self.delivered += 1
        return {"state": "opened"}


def _service(row, decision):
    service = object.__new__(DispatchService)
    service._coordinator = _Coordinator()
    service._gate = _Gate(decision)

    async def require(dispatch_id):
        return row

    service._require = require
    return service


def _row():
    return Dispatch(
        id="d1", goal="g", user_id="u", created_at="now", state=DispatchState.COMPLETE,
        approval_token="tok",
        gates={"auto_approval": {"state": "auto_ready", "approver": "auto:n73"}},
    )


async def test_auto_ready_without_a_human_approval_is_never_delivered() -> None:
    port = _Port()
    service = _service(_row(), ApprovalDecision.PENDING)
    for automatic in (True, False):
        with pytest.raises(ValueError, match="human_approval_required"):
            await service._deliver_with(port, "d1", automatic=automatic)
    assert port.delivered == 0
