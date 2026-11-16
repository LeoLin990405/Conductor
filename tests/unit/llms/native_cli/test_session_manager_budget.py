"""N29 S4: context estimate, worker-requested rotations and lineage ids.

Acceptance 9 of docs/plans/next/n29-worker-context-budget.md. Only tmp_path
files; no network, no real model, no git (the handover's git facts are
stubbed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.registry import context_window_tokens
from src.llms.native_cli.session_manager import (
    CONTEXT_EXHAUSTED,
    MAX_ROTATIONS,
    ROTATION_EXHAUSTED_REASON,
    WORKER_REQUESTED,
    SessionLineage,
    WorkerSessionManager,
)
from src.llms.native_cli.sessions import NativeCliSessionStore

KEY = "thread:t1:kimi"


@dataclass(frozen=True)
class _Estimate:
    """A ContextEstimate look-alike (this layer must not import src.tools)."""

    used_tokens: int
    window_tokens: int
    window_assumed: bool = False
    source: str = "log_bytes"
    drives_rotation: bool = True
    provider: str = "kimi"


def _manager(root: Path) -> WorkerSessionManager:
    root.mkdir(parents=True, exist_ok=True)
    manager = WorkerSessionManager(
        path=root / "worker-sessions.json",
        store=NativeCliSessionStore(root / "sessions.json"),
        clock=lambda: 1_700_000_000.0,
        worktree=root,
        handoff_dir=root / "handoffs",
    )
    manager._git = lambda *_: ""
    return manager


def _lines(root: Path) -> dict:
    return json.loads((root / "worker-sessions.json").read_text(encoding="utf-8"))[
        "lines"
    ]


def test_worker_requested_is_a_whitelisted_reason(tmp_path) -> None:
    assert WORKER_REQUESTED == "worker_requested"
    assert WorkerSessionManager._rotation_reason(WORKER_REQUESTED) == WORKER_REQUESTED
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-1")
    manager.rotate(KEY, reason=WORKER_REQUESTED, self_report="ok")
    archived = _lines(tmp_path)[KEY]["archived"]
    assert archived[-1]["reason"] == "worker_requested"
    assert archived[-1]["reason"] != "unknown"


def test_an_old_state_file_without_the_new_fields_reads_and_writes(tmp_path) -> None:
    state = tmp_path / "worker-sessions.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    KEY: {
                        "session_id": "sess-old",
                        "created_at": "2026-01-01T00:00:00Z",
                        "turns": 2,
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "provider": "kimi",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = _manager(tmp_path)
    assert manager.lineage(KEY) is None
    assert manager.context_estimate(KEY) is None
    assert manager.current(KEY).turns == 2
    assert manager.proactive_rotation_due(KEY) is False

    manager.record_turn(KEY, usage=NativeCliUsage(input_tokens=1, output_tokens=1))
    line = _lines(tmp_path)[KEY]
    assert line["turns"] == 3 and line["input_tokens"] == 11
    assert "context_estimate" not in line
    assert "lineage_current" not in line

    lineage = manager.ensure_lineage(KEY)
    assert lineage.generation == 1 and lineage.previous is None
    assert lineage.first == lineage.current and len(lineage.current) == 32
    assert manager.ensure_lineage(KEY) == lineage
    assert _manager(tmp_path).lineage(KEY) == lineage

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert result.rotated is True
    assert manager.lineage(KEY).previous == lineage.current


@pytest.mark.parametrize(
    ("used", "due"), [(79_999, False), (80_000, True), (95_000, True), (10, False)]
)
def test_a_driving_estimate_decides_proactive_rotation(tmp_path, used, due) -> None:
    manager = _manager(tmp_path)
    # Reported usage says nothing (0); the estimate decides.
    manager.record_turn(
        KEY, session_id="sess-1", estimate=_Estimate(used, 100_000)
    )
    assert manager.proactive_rotation_due(KEY) is due
    assert manager.should_rotate(KEY) is due


def test_an_estimate_given_as_a_mapping_is_accepted(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(
        KEY,
        session_id="sess-1",
        estimate={
            "provider": "kimi",
            "used_tokens": 90,
            "window_tokens": 100,
            "left_tokens": 10,
            "ratio": 0.9,
            "window_assumed": True,
            "source": "launches",
            "drives_rotation": True,
        },
    )
    assert manager.context_estimate(KEY) == {
        "used_tokens": 90,
        "window_tokens": 100,
        "window_assumed": True,
        "source": "launches",
        "drives_rotation": True,
    }
    assert manager.proactive_rotation_due(KEY) is True


def test_a_non_driving_estimate_falls_back_to_the_usage_ratio(tmp_path) -> None:
    window = context_window_tokens("kimi")
    assert window
    manager = _manager(tmp_path)
    manager.record_turn(
        KEY,
        session_id="sess-1",
        estimate=_Estimate(10**9, 100, drives_rotation=False, source="reported"),
    )
    assert manager.proactive_rotation_due(KEY) is False
    # The legacy usage_ratio path still triggers.
    manager.record_turn(
        KEY,
        usage=NativeCliUsage(input_tokens=int(window * 0.9), output_tokens=0),
        estimate=_Estimate(1, 100, drives_rotation=False, source="reported"),
    )
    assert manager.proactive_rotation_due(KEY) is True


def test_a_driving_estimate_with_no_window_falls_back(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-1", estimate=_Estimate(500, 0))
    assert manager.context_estimate(KEY)["window_tokens"] == 0
    assert manager.proactive_rotation_due(KEY) is False


@pytest.mark.parametrize(
    "bad",
    [
        _Estimate(-1, 100),
        _Estimate(10, 100, source="guess"),
        {"used_tokens": True, "window_tokens": 100, "window_assumed": False,
         "source": "log_bytes", "drives_rotation": True},
        {"used_tokens": "10", "window_tokens": 100, "window_assumed": False,
         "source": "log_bytes", "drives_rotation": True},
        {"used_tokens": 10, "window_tokens": 100, "window_assumed": "no",
         "source": "log_bytes", "drives_rotation": True},
        object(),
        42,
    ],
)
def test_a_malformed_estimate_is_not_stored_and_does_not_raise(tmp_path, bad) -> None:
    manager = _manager(tmp_path)
    record = manager.record_turn(KEY, session_id="sess-1", estimate=bad)
    assert record.turns == 1
    assert "context_estimate" not in _lines(tmp_path)[KEY]
    assert manager.context_estimate(KEY) is None


def test_reported_usage_still_accumulates_with_an_estimate(tmp_path) -> None:
    manager = _manager(tmp_path)
    for _ in range(3):
        manager.record_turn(
            KEY,
            session_id="sess-1",
            usage=NativeCliUsage(input_tokens=100, output_tokens=7),
            estimate=_Estimate(10, 1000),
        )
    record = manager.current(KEY)
    assert record.input_tokens == 300 and record.output_tokens == 21
    assert manager.context_estimate(KEY)["used_tokens"] == 10


def test_worker_requested_turns_do_not_reset_rotations_and_park(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-0")
    for n in range(1, MAX_ROTATIONS + 1):
        assert manager.rotate(KEY, reason=WORKER_REQUESTED).rotated is True
        manager.record_turn(KEY, session_id=f"sess-{n}", worker_requested=True)
        assert manager.current(KEY).rotations == n
    parked = manager.rotate(KEY, reason=WORKER_REQUESTED)
    assert parked.parked is True and parked.rotated is False
    assert parked.reason_code == ROTATION_EXHAUSTED_REASON
    assert _lines(tmp_path)[KEY]["parked_reason"] == ROTATION_EXHAUSTED_REASON


def test_a_normal_turn_still_resets_the_rotation_count(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-0")
    for n in range(1, MAX_ROTATIONS + 2):
        assert manager.rotate(KEY, reason=WORKER_REQUESTED).rotated is True
        manager.record_turn(KEY, session_id=f"sess-{n}")
        assert manager.current(KEY).rotations == 0


def test_lineage_advances_across_two_rotations(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-0", estimate=_Estimate(99, 100))
    assert manager.lineage(KEY) is None

    manager.rotate(KEY, reason=WORKER_REQUESTED)
    first = manager.lineage(KEY)
    assert isinstance(first, SessionLineage)
    assert first.generation == 2
    assert first.previous is not None and first.previous == first.first
    assert first.current not in (first.first, "sess-0")
    # The replaced session's estimate is gone with it.
    assert manager.context_estimate(KEY) is None
    assert "context_estimate" not in _lines(tmp_path)[KEY]
    archived = _lines(tmp_path)[KEY]["archived"][-1]
    assert archived["lineage_current"] == first.previous
    assert archived["session_generation"] == 1

    manager.record_turn(KEY, session_id="sess-1")
    manager.rotate(KEY, reason=WORKER_REQUESTED)
    second = manager.lineage(KEY)
    assert second.generation == 3
    assert second.first == first.first
    assert second.previous == first.current
    assert second.current not in (first.current, first.first)
    line = _lines(tmp_path)[KEY]
    assert line["lineage_first"] == second.first
    assert line["lineage_previous"] == second.previous
    assert line["lineage_current"] == second.current
    assert line["session_generation"] == 3


def test_ensure_lineage_then_rotate_keeps_first(tmp_path) -> None:
    manager = _manager(tmp_path)
    start = manager.ensure_lineage(KEY)
    manager.record_turn(KEY, session_id="sess-0")
    assert manager.lineage(KEY) == start
    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    after = manager.lineage(KEY)
    assert after == SessionLineage(
        first=start.first, previous=start.current, current=after.current, generation=2
    )


def test_ensure_lineage_respects_the_read_only_guard(tmp_path) -> None:
    _manager(tmp_path).record_turn(KEY, session_id="sess-0")
    reader = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        read_only=True,
    )
    with pytest.raises(PermissionError):
        reader.ensure_lineage(KEY)
    assert reader.lineage(KEY) is None
    assert "lineage_current" not in _lines(tmp_path)[KEY]


def _fail_save_on(monkeypatch, manager, call: int) -> None:
    original = manager._save
    counter = {"n": 0}

    def _save() -> None:
        counter["n"] += 1
        if counter["n"] >= call:
            raise OSError("the disk is full")
        original()

    monkeypatch.setattr(manager, "_save", _save)


@pytest.mark.parametrize("step", [2, 3, 4])
def test_an_interrupted_rotation_rolls_forward_to_the_intent_lineage(
    tmp_path, monkeypatch, step
) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-old", estimate=_Estimate(99, 100))
    before = manager.ensure_lineage(KEY)

    if step == 3:
        def refuse_legacy(_):
            raise OSError("the disk is full")

        monkeypatch.setattr(manager.store, "_save", refuse_legacy)
    else:
        _fail_save_on(monkeypatch, manager, step)

    with pytest.raises(OSError):
        manager.rotate(KEY, reason=WORKER_REQUESTED)

    pending = _lines(tmp_path)[KEY]["rotation_pending"]
    intended = pending["lineage"]
    assert intended["lineage_first"] == before.first
    assert intended["lineage_previous"] == before.current
    assert intended["session_generation"] == 2

    monkeypatch.undo()
    # A fresh manager finishes the rotation when it opens the file.
    reopened = _manager(tmp_path)
    line = _lines(tmp_path)[KEY]
    assert "rotation_pending" not in line
    assert line["session_id"] == pending["new_id"]
    assert reopened.lineage(KEY) == SessionLineage(
        first=before.first,
        previous=before.current,
        current=intended["lineage_current"],
        generation=2,
    )
    assert "context_estimate" not in line
    assert "sess-old" in reopened.archived_session_ids(KEY)


def test_a_hand_written_intent_rolls_forward_to_its_lineage(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.record_turn(KEY, session_id="sess-old")
    manager._commit(
        KEY,
        {
            **manager._disk_line(KEY),
            "rotation_pending": {
                "key": KEY,
                "old_id": "sess-old",
                "new_id": "sess-new",
                "archive_path": str(tmp_path / "handoffs" / "note.md"),
                "reason": WORKER_REQUESTED,
                "rotations": 1,
                "lineage": {
                    "lineage_first": "a" * 32,
                    "lineage_previous": "a" * 32,
                    "lineage_current": "b" * 32,
                    "session_generation": 2,
                },
            },
        },
    )
    reopened = _manager(tmp_path)
    assert reopened.lineage(KEY) == SessionLineage(
        first="a" * 32, previous="a" * 32, current="b" * 32, generation=2
    )
    archived = _lines(tmp_path)[KEY]["archived"][-1]
    assert archived["reason"] == "worker_requested"
    assert archived["lineage_current"] == "a" * 32


# ----------------------------------------------------------------------
# N29 fix A: an operator retry/recover clears a parked line.


def _park(manager: WorkerSessionManager) -> None:
    manager.record_turn(KEY, session_id="sess-0")
    for n in range(1, MAX_ROTATIONS + 1):
        assert manager.rotate(KEY, reason=WORKER_REQUESTED).rotated is True
        manager.record_turn(KEY, session_id=f"sess-{n}", worker_requested=True)
    assert manager.rotate(KEY, reason=WORKER_REQUESTED).parked is True


def test_clear_parked_lets_the_line_rotate_again_from_zero(tmp_path) -> None:
    manager = _manager(tmp_path)
    _park(manager)
    manager.record_turn(KEY, estimate=_Estimate(95_000, 100_000))
    assert manager.is_parked(KEY) is True
    assert manager.proactive_rotation_due(KEY) is False
    assert manager.should_rotate(KEY) is False

    assert manager.clear_parked(KEY) is True
    line = _lines(tmp_path)[KEY]
    assert "parked" not in line and "parked_reason" not in line
    assert line["rotations"] == 0
    assert manager.is_parked(KEY) is False
    assert _manager(tmp_path).is_parked(KEY) is False
    assert manager.proactive_rotation_due(KEY) is True
    assert manager.should_rotate(KEY) is True

    result = manager.rotate(KEY, reason=WORKER_REQUESTED)
    assert result.rotated is True and result.parked is False
    assert manager.current(KEY).rotations == 1
    assert manager.clear_parked(KEY) is False


def test_clear_parked_rolls_an_interrupted_rotation_forward_first(
    tmp_path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    _park(manager)
    manager.clear_parked(KEY)
    manager.record_turn(KEY, session_id="sess-x")
    _fail_save_on(monkeypatch, manager, 2)
    with pytest.raises(OSError):
        manager.rotate(KEY, reason=WORKER_REQUESTED)
    monkeypatch.undo()
    pending = _lines(tmp_path)[KEY]["rotation_pending"]
    # Park the line by hand under the pending intent, as a crash could leave it.
    raw = json.loads((tmp_path / "worker-sessions.json").read_text("utf-8"))
    raw["lines"][KEY]["parked"] = True
    raw["lines"][KEY]["parked_reason"] = ROTATION_EXHAUSTED_REASON
    (tmp_path / "worker-sessions.json").write_text(json.dumps(raw), "utf-8")

    fresh = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        clock=lambda: 1_700_000_000.0,
        worktree=tmp_path,
        handoff_dir=tmp_path / "handoffs",
    )
    fresh._git = lambda *_: ""
    assert fresh.clear_parked(KEY) is True
    line = _lines(tmp_path)[KEY]
    assert "rotation_pending" not in line
    assert line["session_id"] == pending["new_id"]
    assert "sess-x" in fresh.archived_session_ids(KEY)
    assert line["rotations"] == 0 and "parked" not in line


def test_clear_parked_respects_the_read_only_guard(tmp_path) -> None:
    _park(_manager(tmp_path))
    before = (tmp_path / "worker-sessions.json").read_bytes()
    reader = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        read_only=True,
    )
    assert reader.is_parked(KEY) is True
    with pytest.raises(PermissionError):
        reader.clear_parked(KEY)
    assert (tmp_path / "worker-sessions.json").read_bytes() == before
    assert reader.is_parked(KEY) is True


def test_clear_parked_on_an_unknown_or_unparked_key_writes_nothing(tmp_path) -> None:
    manager = _manager(tmp_path)
    assert manager.clear_parked(KEY) is False
    assert not (tmp_path / "worker-sessions.json").exists()
    assert manager.is_parked(KEY) is False

    manager.record_turn(KEY, session_id="sess-0")
    manager.rotate(KEY, reason=WORKER_REQUESTED)
    before = (tmp_path / "worker-sessions.json").read_bytes()
    assert manager.clear_parked(KEY) is False
    assert manager.clear_parked("thread:other:kimi") is False
    assert (tmp_path / "worker-sessions.json").read_bytes() == before
    assert _lines(tmp_path)[KEY]["rotations"] == 1


def test_clear_parked_on_an_old_state_file_without_the_fields(tmp_path) -> None:
    state = tmp_path / "worker-sessions.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    KEY: {
                        "session_id": "sess-old",
                        "created_at": "2026-01-01T00:00:00Z",
                        "turns": 2,
                        "provider": "kimi",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    before = state.read_bytes()
    manager = _manager(tmp_path)
    assert manager.is_parked(KEY) is False
    assert manager.clear_parked(KEY) is False
    assert state.read_bytes() == before

    # An old file that is parked but has no rotation count still clears.
    raw = json.loads(before)
    raw["lines"][KEY]["parked"] = True
    state.write_text(json.dumps(raw), encoding="utf-8")
    manager = _manager(tmp_path)
    assert manager.is_parked(KEY) is True
    assert manager.clear_parked(KEY) is True
    assert _lines(tmp_path)[KEY]["rotations"] == 0
    assert manager.rotate(KEY, reason=CONTEXT_EXHAUSTED).rotated is True
    assert manager.current(KEY).rotations == 1
