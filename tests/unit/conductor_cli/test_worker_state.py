"""The CLI reads the session state itself; it answers what the manager answers.

``conductor_cli.worker_state`` parses the state file and the legacy session
map, because the wheel that ships the CLI does not ship the native-CLI
session manager. These tests read the same files with both -- the CLI's own
reader and the real ``WorkerSessionManager`` in its read-only mode -- and
compare the answers, so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conductor_cli.worker_state import (
    STATE_MISSING,
    STATE_UNREADABLE,
    WorkerStateReader,
)
from conductor_cli.workers import _cells, workers_result
from src.llms.native_cli.session_manager import WorkerSessionManager
from src.llms.native_cli.sessions import NativeCliSessionStore

#: A snapshot with one worker in it, so that the command has a row to
#: report on even when the session state says nothing about it.
_SNAPSHOT = {
    "updatedAt": "2026-11-06T11:59:00Z",
    "providers": [
        {
            "provider": "kimi",
            "kind": "quota",
            "status": "ok",
            "updated_at": "2026-11-06T11:59:00Z",
            "windows": [{"used_percent": 42.0, "resets_at_utc": "2026-11-07T00:00:00Z"}],
        }
    ],
}

#: One line of the state file: a worker that has used half its window,
#: failed once and rotated twice.
_LINE = {
    "provider": "kimi",
    "session_id": "session-kimi-1",
    "created_at": "2026-11-06T09:00:00Z",
    "turns": 4,
    "input_tokens": 131072,
    "output_tokens": 0,
    "rotations": 2,
    "last_failure_class": "rate_limited",
}

#: The fields both answers carry. The names are the same on purpose: the
#: CLI's record is the manager's record, read from the same file.
FIELDS = (
    "session_id",
    "created_at",
    "turns",
    "input_tokens",
    "output_tokens",
    "last_failure_class",
    "rotations",
    "last_failure_at",
    "last_failure_seq",
    "turns_at_failure",
    "success_seq",
    "quota_failed_at_seq",
    "pending_rotation",
)


def _write_state(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_legacy(path: Path, payload: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _reader(tmp_path: Path, state: dict[str, Any], legacy: dict[str, str]):
    """The CLI's reader on a state file and a legacy map."""
    state_path = _write_state(tmp_path / "worker-sessions.json", state)
    legacy_path = _write_legacy(tmp_path / "sessions.json", legacy)
    return WorkerStateReader(path=state_path, legacy_path=legacy_path)


def _manager(tmp_path: Path, state: dict[str, Any], legacy: dict[str, str]):
    """The real session manager, read-only, on the same two files."""
    state_path = _write_state(tmp_path / "worker-sessions.json", state)
    legacy_path = _write_legacy(tmp_path / "sessions.json", legacy)
    return WorkerSessionManager(
        path=state_path,
        store=NativeCliSessionStore(legacy_path),
        read_only=True,
    )


def _answer(record: Any) -> dict[str, Any]:
    return {name: getattr(record, name) for name in FIELDS}


#: A state file of the shape the current code writes.
CURRENT_STATE: dict[str, Any] = {
    "lines": {
        "thread:t1:kimi": {
            "provider": "kimi",
            "session_id": "session-kimi-1",
            "created_at": "2026-11-06T09:00:00Z",
            "turns": 4,
            "input_tokens": 1000,
            "output_tokens": 500,
            "rotations": 1,
            "last_failure_class": "rate_limited",
            "last_failure_at": "2026-11-06T10:00:00Z",
            "last_failure_seq": 2,
            "turns_at_failure": 3,
        },
        "thread:t2:step": {
            "provider": "step",
            "session_id": "session-step-1",
            "created_at": "2026-11-06T08:00:00Z",
            "turns": 2,
            "input_tokens": 200,
            "output_tokens": 100,
            "rotations": 0,
        },
    },
    "workers": {
        "kimi": {"success_seq": 5},
        "stepfun": {"success_seq": 2, "quota_failed_at_seq": 3},
    },
}

#: A state file from before the worker records existed: the counters are on
#: the lines, spelled literally.
LEGACY_STATE: dict[str, Any] = {
    "lines": {
        "thread:t1:step": {
            "provider": "step",
            "session_id": "session-step-legacy",
            "created_at": "2026-10-01T08:00:00Z",
            "turns": 9,
            "input_tokens": 4000,
            "output_tokens": 2000,
            "rotations": 2,
            "success_seq": 4,
        },
        "thread:t2:stepfun": {
            "provider": "stepfun",
            "session_id": "session-stepfun-legacy",
            "created_at": "2026-10-02T08:00:00Z",
            "turns": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "success_seq": 6,
        },
    },
}

LEGACY_MAP: dict[str, str] = {"thread:t0:claude": "session-claude-old"}


@pytest.mark.parametrize(
    ("state", "legacy"),
    [(CURRENT_STATE, LEGACY_MAP), (LEGACY_STATE, LEGACY_MAP), ({}, LEGACY_MAP)],
    ids=["current", "legacy-counters", "empty"],
)
def test_the_cli_answers_what_the_session_manager_answers(
    tmp_path: Path, state: dict[str, Any], legacy: dict[str, str]
) -> None:
    reader = _reader(tmp_path, state, legacy)
    manager = _manager(tmp_path, state, legacy)

    keys = sorted(set(reader._lines()) | set(manager._load()) | set(legacy))

    assert reader.legacy_keys() == manager.legacy_keys()
    for key in keys:
        assert _answer(reader.current(key)) == _answer(manager.current(key))
        assert reader.usage_ratio(key) == manager.usage_ratio(key)


def test_two_spellings_of_one_worker_share_the_counters(tmp_path: Path) -> None:
    # "step" and "stepfun" are one worker: the successes of the two lines
    # add up (4 + 6 = 10) and the older quota marker is dropped, so the
    # worker reads as not out of quota -- the fail-open rule of PRD N2.
    reader = _reader(tmp_path, LEGACY_STATE, {})

    step = reader.current("thread:t1:step")
    stepfun = reader.current("thread:t2:stepfun")

    assert step is not None and stepfun is not None
    assert step.success_seq == 10
    assert stepfun.success_seq == 10
    assert step.quota_failed_at_seq is None
    assert stepfun.quota_failed_at_seq is None


def test_the_quota_marker_of_the_newer_generation_wins(tmp_path: Path) -> None:
    state = {
        "lines": {
            "thread:t1:step": {"provider": "step", "session_id": "a"},
            "thread:t2:stepfun": {"provider": "stepfun", "session_id": "b"},
        },
        "workers": {
            "step": {"success_seq": 1, "quota_failed_at_seq": 4},
            "stepfun": {"success_seq": 2, "quota_failed_at_seq": 9},
        },
    }
    reader = _reader(tmp_path, state, {})

    record = reader.current("thread:t1:step")

    assert record is not None
    assert record.success_seq == 3
    assert record.quota_failed_at_seq == 9


def test_a_session_that_lives_only_in_the_legacy_map_is_still_seen(
    tmp_path: Path,
) -> None:
    reader = _reader(tmp_path, {}, LEGACY_MAP)

    record = reader.current("thread:t0:claude")

    assert record is not None
    assert record.session_id == "session-claude-old"
    assert record.created_at == ""
    assert "thread:t0:claude" in reader.legacy_keys()


def test_a_line_without_a_session_is_not_a_session(tmp_path: Path) -> None:
    reader = _reader(tmp_path, {"lines": {"thread:t1:kimi": {"provider": "kimi"}}}, {})

    assert reader.current("thread:t1:kimi") is None


def test_the_reader_writes_nothing(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path / "worker-sessions.json", CURRENT_STATE)
    legacy_path = _write_legacy(tmp_path / "sessions.json", LEGACY_MAP)
    before = (state_path.read_bytes(), legacy_path.read_bytes())
    reader = WorkerStateReader(path=state_path, legacy_path=legacy_path)

    reader.current("thread:t1:kimi")
    reader.usage_ratio("thread:t1:kimi")
    reader.legacy_keys()

    assert (state_path.read_bytes(), legacy_path.read_bytes()) == before


def test_the_legacy_map_is_looked_for_under_the_home_not_next_to_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two files keep the places the session manager keeps them.

    The session manager reads its legacy map from the user's home and not
    from the directory of the state file, even when the state file is
    somewhere else. A reader that looked for the map next to a custom state
    file would report a different set of sessions than the manager does.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_path = _write_state(tmp_path / "elsewhere" / "worker-sessions.json", {})

    reader = WorkerStateReader(path=state_path)

    assert reader.legacy_path == (
        home / ".cache" / "conductor" / "native-cli" / "sessions.json"
    )


def test_the_default_paths_are_under_the_home(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    reader = WorkerStateReader()

    assert reader.path == (
        home / ".cache" / "conductor" / "native-cli" / "worker-sessions.json"
    )
    assert reader.legacy_path == (
        home / ".cache" / "conductor" / "native-cli" / "sessions.json"
    )


def test_a_state_file_that_cannot_be_read_is_an_unknown_state(tmp_path: Path) -> None:
    state_path = tmp_path / "worker-sessions.json"
    state_path.write_text("{not json", encoding="utf-8")
    reader = WorkerStateReader(path=state_path, legacy_path=tmp_path / "sessions.json")

    # The state file is there and cannot be read: that is **not** a state
    # file that holds nothing. The reader says so, and the command turns
    # that into "not known" -- it must never report "this worker used no
    # context and never failed", which is what an empty state says.
    assert reader.state_outcome == STATE_UNREADABLE
    assert reader.current("thread:t1:kimi") is None
    assert reader.usage_ratio("thread:t1:kimi") is None


def test_a_broken_state_file_leaves_the_command_without_an_answer(
    tmp_path: Path,
) -> None:
    """A broken state file must not turn into a healthy-looking worker."""
    state_path = tmp_path / "worker-sessions.json"
    state_path.write_text("{not json", encoding="utf-8")
    legacy_path = _write_legacy(tmp_path / "sessions.json", {"thread:t1:kimi": "s-1"})
    reader = WorkerStateReader(path=state_path, legacy_path=legacy_path)
    manager = WorkerSessionManager(
        path=state_path,
        store=NativeCliSessionStore(legacy_path),
        read_only=True,
    )

    row = workers_result(manager=reader, snapshot=_SNAPSHOT)["workers"][0]
    manager_row = workers_result(manager=manager, snapshot=_SNAPSHOT)["workers"][0]

    # The session is still listed -- it is in the legacy map -- but nothing
    # about it is known: "unknown" is not "zero".
    assert row["sessions"] == 1
    assert row["context_used_percent"] is None
    assert row["last_failure_class"] is None
    assert row["rotations"] is None
    # The session manager invents a zero out of the file it could not read;
    # the reader says "not known" instead.
    assert manager_row["rotations"] == 0


def test_an_oversized_state_file_is_unknown_and_not_zero(tmp_path: Path) -> None:
    """A state file bigger than the cap is not read, and not invented.

    The file is too big to be parsed by a status command, so the command
    says "not known" about the workers in it. It must not say "used no
    context": that is a claim about a file nobody read.
    """
    payload = {
        "lines": {"thread:t1:kimi": dict(_LINE)},
        "workers": {"kimi": {"success_seq": 5}},
    }
    blob = json.dumps(payload).encode("utf-8")
    padding = b" " * (1_349_181 - len(blob))
    state_path = tmp_path / "worker-sessions.json"
    state_path.write_bytes(blob[:-1] + padding + b"}")
    legacy_path = _write_legacy(tmp_path / "sessions.json", {"thread:t1:kimi": "s-1"})
    reader = WorkerStateReader(path=state_path, legacy_path=legacy_path)
    manager = WorkerSessionManager(
        path=state_path,
        store=NativeCliSessionStore(legacy_path),
        read_only=True,
    )

    result = workers_result(manager=reader, snapshot=_SNAPSHOT)
    manager_row = workers_result(manager=manager, snapshot=_SNAPSHOT)["workers"][0]

    assert reader.state_outcome == STATE_UNREADABLE
    row = result["workers"][0]
    assert row["sessions"] == 1
    assert row["context_used_percent"] is None
    assert row["last_failure_class"] is None
    assert row["rotations"] is None
    # The session manager reads the whole file and reports what is in it;
    # the CLI keeps its size cap and says "not known" instead.
    assert manager_row["context_used_percent"] == 50.0


def test_an_unknown_count_is_a_dash_and_never_a_zero(tmp_path: Path) -> None:
    """The table shows a dash for a count nobody knows, not a zero."""
    state_path = tmp_path / "worker-sessions.json"
    state_path.write_text("{not json", encoding="utf-8")
    legacy_path = _write_legacy(tmp_path / "sessions.json", {"thread:t1:kimi": "s-1"})
    reader = WorkerStateReader(path=state_path, legacy_path=legacy_path)

    result = workers_result(manager=reader, snapshot=_SNAPSHOT)
    cells = _cells(result["workers"][0])

    # provider, sessions, context, failure, rotations
    assert cells[4] == "-"
    assert cells[2] == "unknown"


def test_a_legacy_only_session_still_reports_no_context_used(tmp_path: Path) -> None:
    """A missing state file keeps its old answer: no context used.

    "There is no state file" and "the state file could not be read" are
    different, and the command says different things for them: the first
    is a worker that has used no context, the second is a worker nobody
    knows about.
    """
    legacy_path = _write_legacy(tmp_path / "sessions.json", {"thread:t1:kimi": "s-1"})
    reader = WorkerStateReader(
        path=tmp_path / "does-not-exist.json", legacy_path=legacy_path
    )

    assert reader.state_outcome == STATE_MISSING
    assert reader.usage_ratio("thread:t1:kimi") == 0.0

    result = workers_result(manager=reader, snapshot=_SNAPSHOT)
    assert result["workers"][0]["context_used_percent"] == 0.0


def test_a_missing_state_file_is_an_empty_state(tmp_path: Path) -> None:
    reader = WorkerStateReader(
        path=tmp_path / "does-not-exist.json",
        legacy_path=tmp_path / "nor-this-one.json",
    )

    assert reader.current("thread:t1:kimi") is None
    assert reader.legacy_keys() == ()


def test_a_key_without_counters_has_used_no_context(tmp_path: Path) -> None:
    # The same answer the session manager gives: a line the state file does
    # not hold has used nothing, and the manager answers that with zero --
    # not with "unknown". The CLI must not report a different number in an
    # installation than in a checkout.
    reader = _reader(tmp_path, {}, {})
    manager = _manager(tmp_path, {}, {})

    assert reader.usage_ratio("thread:t1:kimi") == manager.usage_ratio(
        "thread:t1:kimi"
    )
    assert reader.usage_ratio("thread:t1:kimi") == 0.0
