"""The approval gate: request, decide, resolve, and what it blocks."""

from __future__ import annotations

import json

import pytest

from src.tools.auto_dispatch.approval import FileApprovalGate
from src.tools.auto_dispatch.protocols import ApprovalDecision
from src.tools.auto_dispatch.types import Dispatch, Line


def _dispatch() -> Dispatch:
    dispatch = Dispatch(id="d1", goal="ship the thing", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    dispatch.lines = [
        Line(id="line-0", name="implement", task="write it", assigned_model="codex")
    ]
    return dispatch


async def test_request_approval_writes_a_readable_summary_and_a_token(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    token = await gate.request_approval(_dispatch())
    summary = (tmp_path / "d1" / "summary.md").read_text(encoding="utf-8")
    assert "ship the thing" in summary
    assert "line-0" in summary
    assert "integrate/auto-dispatch-d1" in summary
    assert token
    assert await gate.decision(token) is ApprovalDecision.PENDING


async def test_resolve_records_an_approval(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    token = await gate.request_approval(_dispatch())
    assert await gate.resolve(token, True, "looks right") is ApprovalDecision.APPROVED
    assert await gate.decision(token) is ApprovalDecision.APPROVED


async def test_resolve_records_a_rejection(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    token = await gate.request_approval(_dispatch())
    assert await gate.resolve(token, False, "too risky") is ApprovalDecision.REJECTED
    assert await gate.decision(token) is ApprovalDecision.REJECTED


async def test_resolve_dispatch_works_from_the_id_alone(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    await gate.request_approval(_dispatch())
    assert await gate.resolve_dispatch("d1", True) is ApprovalDecision.APPROVED


async def test_resolve_dispatch_is_idempotent(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    await gate.request_approval(_dispatch())
    await gate.resolve_dispatch("d1", True)
    assert await gate.resolve_dispatch("d1", False) is ApprovalDecision.APPROVED


async def test_an_unknown_token_raises(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    with pytest.raises(KeyError):
        await gate.decision("nope")


async def test_resolving_a_dispatch_that_never_asked_keeps_the_root_clean(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    assert await gate.resolve_dispatch("d1", True, "ship it") is ApprovalDecision.APPROVED
    # The gate file lands in the dispatch's own directory, never in the root
    # beside the token index.
    assert (tmp_path / "d1" / "gate.json").is_file()
    assert not (tmp_path / "gate.json").exists()
    assert await gate.pending_dispatch_ids() == []


async def test_a_minted_token_resolves_the_same_dispatch(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    await gate.resolve_dispatch("d1", False, "not yet")
    payload = json.loads((tmp_path / "d1" / "gate.json").read_text(encoding="utf-8"))
    assert payload["state"] == "REJECTED"
    assert payload["note"] == "not yet"
    assert await gate.decision(payload["token"]) is ApprovalDecision.REJECTED


async def test_pending_dispatch_ids_lists_only_what_is_unanswered(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path)
    await gate.request_approval(_dispatch())
    other = _dispatch()
    other.id = "d2"
    await gate.request_approval(other)
    await gate.resolve_dispatch("d2", True)
    assert await gate.pending_dispatch_ids() == ["d1"]


async def test_an_empty_gate_root_has_no_pending(tmp_path) -> None:
    assert await FileApprovalGate(tmp_path / "missing").pending_dispatch_ids() == []

pytestmark = pytest.mark.asyncio

