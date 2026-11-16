"""N87 review finding 5: the fix round after a REPLANNING-route rereview.

``recover --rereview`` from ``replanning`` takes the ``replace_developer``
option with ``operator`` as the Stage-Gate developer, while the line's
``assigned_model`` stays the real provider. After the rereview the review
BLOCKs and the drive starts a fix round. That round must launch the line's
real provider, its ``submit_work`` from ``fixing`` must be accepted (not
refused, not parked as a developer mismatch), and the fixed work must go back
through G2 and the review to pass.
"""

from __future__ import annotations

import pytest

from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import LineStatus, operator
from src.tools.auto_dispatch.machine import _load_state
from src.tools.auto_dispatch.types import Verdict
from tests.unit.tools.auto_dispatch.test_n87_rereview import _exhausted

pytestmark = pytest.mark.asyncio


class BlockOnceThenPass:
    """One BLOCK after the rereview, then PASS."""

    def __init__(self) -> None:
        self.blocks_left = 1
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        if self.blocks_left:
            self.blocks_left -= 1
            return Verdict.blocked(["one more thing to fix"])
        return Verdict.ok("fixed")


def _handoff(*_args: object, **_kwargs: object) -> dict[str, object]:
    # The test repo has no Git history; a fix round needs complete facts.
    return {
        "branch": "feature/l",
        "commit": "abc123",
        "files": ("a.py",),
        "test_command": "pytest",
        "results": "pass",
        "known_limitations": "none",
    }


def _events(snap: dict) -> list[dict]:
    events = snap.get("events")
    return [item for item in events if isinstance(item, dict)] if isinstance(events, list) else []


async def test_the_fix_round_after_a_replanning_rereview_is_accepted(tmp_path) -> None:
    collab, machine, _commands, _reviewer, _result = await _exhausted(tmp_path)
    stored = await collab.store.get("n87")
    line = stored.line("l")
    real = line.assigned_model
    assert real
    assert _load_state(stored.gates["lines"]["l"]).stage is Stage.REPLANNING

    assert operator.apply_recover(stored, line, "rereview", note="fixed by hand") is None
    assert line.assigned_model == real

    collab.gates.handoff_facts = _handoff
    reviewer = BlockOnceThenPass()
    collab.reviewer = reviewer
    launches_before = len(collab.launcher.launches)
    events_before = len(_events(stored.gates["lines"]["l"]))
    await collab.store.save(stored)

    done = await machine.run(stored)

    done_line = done.line("l")
    snap = done.gates["lines"]["l"]
    fresh = _events(snap)[events_before:]
    reasons = [item.get("reason") for item in fresh]
    # The review BLOCKed and sent the line to fixing.
    assert any(
        item.get("from_stage") == "primary_review" and item.get("to_stage") == "fixing"
        for item in fresh
    ), reasons
    # The fix round ran with the line's real provider, not the operator.
    fix_launches = collab.launcher.launches[launches_before:]
    assert [model for _line_id, model, _skills in fix_launches] == [real]
    # Its submission from fixing was accepted and went back to review.
    assert any(
        item.get("from_stage") == "fixing"
        and item.get("to_stage") == "primary_review"
        and item.get("reason") == "fix_resubmitted"
        for item in fresh
    ), reasons
    # Not refused, not parked as a developer mismatch: the line passes.
    assert done_line.status is LineStatus.PASSED, (
        done_line.status,
        done_line.failure_reason,
        done_line.blockers,
        reasons,
    )
    assert done_line.failure_reason is None
    assert len(reviewer.calls) == 2
