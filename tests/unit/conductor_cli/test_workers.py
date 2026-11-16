"""``conductor workers``: the read-only worker health listing.

The session manager and the CodexBar snapshot are injected in every test,
so nothing here reads the real home directory, a real session store or a
real ``codexbar`` binary.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conductor_cli import cli
from conductor_cli.worker_state import WorkerStateReader
from conductor_cli.workers import render_workers, workers_result

SENTINEL = "sentinel-secret-token-abc123"
NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED_AT = "2026-11-06T11:59:00Z"
RESET_AT = "2026-11-07T00:00:00Z"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Point ``HOME`` at a directory of this test's own.

    ``conductor workers`` reads the session state under ``~/.cache/conductor``:
    the session manager's own state file and the legacy flat map of session
    ids. That is where a real installation keeps them, so a test that runs
    the command without saying where the state is reads the **operator's**
    state -- and a test that lists the workers it expects can fail because
    the operator's machine has a session this test knows nothing about.

    It used to be isolated by patching the native-CLI session manager's
    default path constant. The CLI reads the state itself now, so that patch
    no longer says anything -- and the isolation was gone with it. So the
    isolation is here, where the read happens: ``HOME`` is a directory of
    this test's own, and the command reads that.
    """
    home = tmp_path / "isolated-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # The native-CLI session store works out its default path when its
    # module is imported, so the ``HOME`` of that moment is baked into the
    # constant. A store built without an explicit path would read that home
    # -- the operator's -- and see the sessions of a machine this test
    # knows nothing about. So the default is pointed at this test's own
    # home as well, and the ``HOME`` above covers the reads that happen
    # later.
    try:
        import src.llms.native_cli.sessions as sessions
    except ImportError:  # pragma: no cover - a checkout without ``src``
        sessions = None
    if sessions is not None:
        monkeypatch.setattr(
            sessions,
            "DEFAULT_SESSION_PATH",
            home / ".cache" / "conductor" / "native-cli" / "sessions.json",
        )


@dataclass(frozen=True)
class _Record:
    """The session record the session manager hands out.

    ``success_seq`` is the generation: how many turns have succeeded over
    the whole line, across every session it has had. ``quota_failed_at_seq``
    is the generation a quota failure happened in; the failure still blocks
    the worker while no turn has succeeded since it.
    """

    session_id: str = "session-1"
    created_at: str = "2026-11-06T09:00:00Z"
    turns: int = 4
    input_tokens: int = 1000
    output_tokens: int = 500
    last_failure_class: str | None = None
    rotations: int = 0
    last_failure_at: str | None = None
    last_failure_seq: int = 0
    turns_at_failure: int = 4
    success_seq: int = 0
    quota_failed_at_seq: int | None = None


class _Manager:
    """A stand-in for ``WorkerSessionManager``, on the same contract."""

    def __init__(self, records=None, ratios=None, keys=(), path=None, legacy_keys=()):
        self._records = dict(records or {})
        self._ratios = dict(ratios or {})
        self._keys = list(keys)
        self._legacy_keys = list(legacy_keys)
        self.path = path

    def providers(self):
        return list(self._keys)

    def legacy_keys(self):
        return list(self._legacy_keys)

    def current(self, key):
        return self._records.get(key)

    def usage_ratio(self, key):
        return self._ratios.get(key)


def _window(used=42.0, reset=RESET_AT):
    window = {"used_percent": used}
    if reset is not None:
        window["resets_at_utc"] = reset
    return window


def _row(provider, *, used=42.0, reset=RESET_AT, status="ok", observed=OBSERVED_AT):
    return {
        "provider": provider,
        "kind": "quota",
        "status": status,
        "updated_at": observed,
        "windows": [_window(used, reset)],
    }


def _snapshot(*rows):
    return {"updatedAt": OBSERVED_AT, "providers": list(rows)}


def _windows(provider):
    return lambda name: 200_000 if name == provider else None


def _row_of(result, provider):
    for row in result["workers"]:
        if row["provider"] == provider:
            return row
    raise AssertionError(f"{provider} is not listed: {result}")


def test_a_worker_reports_sessions_usage_failure_rotations_and_quota():
    manager = _Manager(
        records={"kimi": _Record(last_failure_class="rate_limited", rotations=1)},
        ratios={"kimi": 0.425},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=100.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert result == {
        "workers": [
            {
                "provider": "kimi",
                "sessions": 1,
                "context_used_percent": 42.5,
                "last_failure_class": "rate_limited",
                "rotations": 1,
                "exhausted": True,
                "reset_at": RESET_AT,
                "pending_rotation": False,
            }
        ]
    }


def test_a_session_key_names_the_provider_of_its_worker():
    manager = _Manager(
        records={"thread:abc123:kimi": _Record(rotations=2)},
        ratios={"thread:abc123:kimi": 0.5},
        keys=["thread:abc123:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["sessions"] == 1
    assert row["rotations"] == 2
    assert row["context_used_percent"] == 50.0


def test_the_sessions_of_one_provider_are_counted_together():
    manager = _Manager(
        records={
            "thread:one:kimi": _Record(rotations=1),
            "thread:two:kimi": _Record(rotations=0, last_failure_class="network"),
        },
        ratios={"thread:one:kimi": 0.8, "thread:two:kimi": 0.2},
        keys=["thread:one:kimi", "thread:two:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["sessions"] == 2
    assert row["rotations"] == 1
    # The fullest session is the one the operator needs to see.
    assert row["context_used_percent"] == 80.0
    assert row["last_failure_class"] == "network"


def test_the_manager_state_file_is_where_the_session_keys_come_from(tmp_path):
    state = tmp_path / "worker-sessions.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    "thread:abc123:kimi": {
                        "provider": "kimi",
                        "session_id": "s-1",
                        "rotations": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = _Manager(
        records={"thread:abc123:kimi": _Record(rotations=1)},
        ratios={"thread:abc123:kimi": 0.25},
        path=state,
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["sessions"] == 1
    assert row["rotations"] == 1
    assert row["context_used_percent"] == 25.0


def test_a_legacy_session_key_is_listed_too(tmp_path):
    """A session that lives only in the legacy map is still a session."""
    session_manager = pytest.importorskip("src.llms.native_cli.session_manager")
    sessions = pytest.importorskip("src.llms.native_cli.sessions")
    legacy = tmp_path / "sessions.json"
    legacy.write_text(
        json.dumps({"thread:abc123:kimi": "s-legacy"}), encoding="utf-8"
    )
    state = tmp_path / "worker-sessions.json"
    state.write_text(json.dumps({"version": 1, "lines": {}}), encoding="utf-8")
    manager = session_manager.WorkerSessionManager(
        path=state, store=sessions.NativeCliSessionStore(path=legacy)
    )
    # The manager answers for the key even though the rich state has no line.
    assert manager.current("thread:abc123:kimi") is not None
    result = workers_result(manager=manager, snapshot=_snapshot(), now=NOW)
    row = _row_of(result, "kimi")
    assert row["sessions"] == 1
    assert row["last_failure_class"] is None


def test_the_rich_and_the_legacy_keys_are_one_worker_with_both_sessions():
    manager = _Manager(
        records={
            "thread:rich:kimi": _Record(),
            "thread:legacy:kimi": _Record(),
        },
        ratios={"thread:rich:kimi": 0.25, "thread:legacy:kimi": 0.1},
        keys=["thread:rich:kimi"],
        legacy_keys=["thread:legacy:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["sessions"] == 2
    assert row["context_used_percent"] == 25.0


def test_a_key_in_both_stores_is_one_session():
    manager = _Manager(
        records={"kimi": _Record()},
        ratios={"kimi": 0.25},
        keys=["kimi"],
        legacy_keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["sessions"] == 1


def test_the_reset_time_comes_from_the_snapshot_window():
    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=100.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["exhausted"] is True
    assert row["reset_at"] == RESET_AT


def test_a_worker_below_the_limit_is_not_exhausted():
    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=42.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["exhausted"] is False
    assert row["reset_at"] == RESET_AT


def test_a_stale_snapshot_row_says_nothing_about_the_quota():
    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=100.0, observed="2026-11-06T10:00:00Z")),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["exhausted"] is None
    assert row["reset_at"] is None


def test_a_window_that_already_reset_does_not_exhaust_the_worker():
    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=100.0, reset="2026-11-06T11:00:00Z")),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["exhausted"] is None
    assert row["reset_at"] is None


def test_a_quota_failure_marks_the_worker_exhausted():
    manager = _Manager(
        records={
            "kimi": _Record(
                last_failure_class="quota_exhausted",
                success_seq=0,
                quota_failed_at_seq=0,
            )
        },
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is True


def test_the_most_recent_failure_is_the_one_that_happened_last():
    """A failure on the older session can come after one on the newer one."""
    manager = _Manager(
        records={
            "thread:old:kimi": _Record(
                session_id="s-a",
                created_at="2026-11-06T09:00:00Z",
                last_failure_class="auth_failed",
                last_failure_at="2026-11-06T10:00:00.000000+00:00",
                last_failure_seq=2,
            ),
            "thread:new:kimi": _Record(
                session_id="s-b",
                created_at="2026-11-06T09:30:00Z",
                last_failure_class="network",
                last_failure_at="2026-11-06T09:45:00.000000+00:00",
                last_failure_seq=1,
            ),
        },
        ratios={"thread:old:kimi": 0.1, "thread:new:kimi": 0.1},
        keys=["thread:old:kimi", "thread:new:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["last_failure_class"] == "auth_failed"


def test_the_newer_session_wins_when_it_failed_last():
    manager = _Manager(
        records={
            "thread:old:kimi": _Record(
                session_id="s-a",
                created_at="2026-11-06T09:00:00Z",
                last_failure_class="network",
                last_failure_at="2026-11-06T09:10:00.000000+00:00",
                last_failure_seq=1,
            ),
            "thread:new:kimi": _Record(
                session_id="s-b",
                created_at="2026-11-06T09:30:00Z",
                last_failure_class="auth_failed",
                last_failure_at="2026-11-06T09:50:00.000000+00:00",
                last_failure_seq=2,
            ),
        },
        ratios={"thread:old:kimi": 0.1, "thread:new:kimi": 0.1},
        keys=["thread:old:kimi", "thread:new:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["last_failure_class"] == "auth_failed"


def test_two_failures_in_the_same_microsecond_are_told_apart_by_the_sequence():
    manager = _Manager(
        records={
            "thread:one:kimi": _Record(
                session_id="s-a",
                last_failure_class="network",
                last_failure_at="2026-11-06T09:45:00.000000+00:00",
                last_failure_seq=1,
            ),
            "thread:two:kimi": _Record(
                session_id="s-b",
                last_failure_class="auth_failed",
                last_failure_at="2026-11-06T09:45:00.000000+00:00",
                last_failure_seq=2,
            ),
        },
        ratios={"thread:one:kimi": 0.1, "thread:two:kimi": 0.1},
        keys=["thread:one:kimi", "thread:two:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["last_failure_class"] == "auth_failed"


def test_a_record_without_a_failure_time_falls_back_to_its_creation():
    """An older record keeps its place: its failure came after its birth."""
    manager = _Manager(
        records={
            "thread:new:kimi": _Record(
                session_id="s-a",
                created_at="2026-11-06T09:00:00Z",
                last_failure_class="network",
            ),
            "thread:old:kimi": _Record(
                session_id="s-b",
                created_at="2026-11-06T08:00:00Z",
                last_failure_class="auth_failed",
                last_failure_at="2026-11-06T08:30:00.000000+00:00",
                last_failure_seq=1,
            ),
        },
        ratios={"thread:new:kimi": 0.1, "thread:old:kimi": 0.1},
        keys=["thread:new:kimi", "thread:old:kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["last_failure_class"] == "network"


def test_a_quota_failure_followed_by_a_successful_turn_is_not_exhausted():
    """The failure stays on record; the fresh snapshot is the state now."""
    manager = _Manager(
        records={
            "kimi": _Record(
                last_failure_class="quota_exhausted",
                last_failure_at="2026-11-06T09:30:00.000000+00:00",
                last_failure_seq=1,
                turns=5,
                turns_at_failure=4,
                success_seq=5,
                quota_failed_at_seq=4,
            )
        },
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["last_failure_class"] == "quota_exhausted"
    assert row["exhausted"] is False


def test_a_quota_failure_without_a_successful_turn_still_exhausts():
    manager = _Manager(
        records={
            "kimi": _Record(
                last_failure_class="quota_exhausted",
                last_failure_at="2026-11-06T09:30:00.000000+00:00",
                last_failure_seq=1,
                turns=4,
                turns_at_failure=4,
                success_seq=4,
                quota_failed_at_seq=4,
            )
        },
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["last_failure_class"] == "quota_exhausted"
    assert row["exhausted"] is True


def test_a_recovered_quota_failure_without_a_snapshot_says_nothing():
    manager = _Manager(
        records={
            "kimi": _Record(
                last_failure_class="quota_exhausted",
                last_failure_at="2026-11-06T09:30:00.000000+00:00",
                last_failure_seq=1,
                turns=5,
                turns_at_failure=4,
                success_seq=5,
                quota_failed_at_seq=4,
            )
        },
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["last_failure_class"] == "quota_exhausted"
    assert row["exhausted"] is None
    assert row["reset_at"] is None


def test_an_unrecovered_quota_failure_without_a_snapshot_exhausts():
    manager = _Manager(
        records={
            "kimi": _Record(
                last_failure_class="quota_exhausted",
                last_failure_at="2026-11-06T09:30:00.000000+00:00",
                last_failure_seq=1,
                turns=4,
                turns_at_failure=4,
                success_seq=4,
                quota_failed_at_seq=4,
            )
        },
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["last_failure_class"] == "quota_exhausted"
    assert row["exhausted"] is True


def test_a_quota_failure_is_judged_by_the_generation_and_not_by_the_turns(tmp_path):
    """quota failure -> one successful turn -> a rotation -> the new session's turn.

    The generation is the count of successful turns over the whole line. It
    is not reset by a rotation, so the quota failure stays covered after
    the session that hit it has been replaced.
    """
    session_manager = pytest.importorskip("src.llms.native_cli.session_manager")
    base = pytest.importorskip("src.llms.native_cli.base")
    sessions = pytest.importorskip("src.llms.native_cli.sessions")
    key = "thread:abc123:kimi"
    manager = session_manager.WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=sessions.NativeCliSessionStore(tmp_path / "sessions.json"),
        worktree=tmp_path,
        handoff_dir=tmp_path / "handoffs",
    )
    usage = base.NativeCliUsage(input_tokens=100, output_tokens=50)

    def _row_now():
        result = workers_result(
            manager=manager,
            snapshot=_snapshot(_row("kimi", used=10.0)),
            context_windows=_windows("kimi"),
            now=NOW,
        )
        return _row_of(result, "kimi")

    manager.record_turn(key, session_id="session-old", usage=usage)
    manager.record_turn(key, usage=usage)
    manager.record_failure(key, "quota_exhausted")
    # The failure happened and no turn has succeeded since: still exhausted.
    assert _row_now()["exhausted"] is True

    manager.record_turn(key, usage=usage)
    # One successful turn after the failure says the worker is working again.
    assert _row_now()["exhausted"] is False

    manager.rotate(key, reason=session_manager.ACTIVE_ROTATION)
    # The rotation starts a new session, not a new generation: the failure
    # stays covered and the worker stays recovered.
    assert _row_now()["exhausted"] is False

    manager.record_turn(key, session_id="session-new", usage=usage)
    row = _row_now()
    assert row["exhausted"] is False
    # The failure happened; it stays on record even though it is over.
    assert row["last_failure_class"] == "quota_exhausted"


@pytest.mark.parametrize(
    ("success_seq", "quota_failed_at_seq", "blocked"),
    [
        (3, 2, False),
        (2, 2, True),
        (1, 2, True),
    ],
)
def test_the_generation_says_whether_a_quota_failure_still_blocks(
    success_seq, quota_failed_at_seq, blocked
):
    manager = _Manager(
        records={
            "kimi": _Record(
                last_failure_class="quota_exhausted",
                success_seq=success_seq,
                quota_failed_at_seq=quota_failed_at_seq,
            )
        },
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is blocked


def test_a_session_that_never_failed_for_quota_does_not_block_the_worker():
    manager = _Manager(
        records={"kimi": _Record(success_seq=4)},
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is False


def test_a_record_written_before_the_generation_was_counted_does_not_block():
    """An old state file knows no generation: it cannot say the worker is stuck."""

    class _OldRecord:
        session_id = "s-1"
        created_at = "2026-11-06T09:00:00Z"
        turns = 4
        input_tokens = 1000
        output_tokens = 500
        last_failure_class = "quota_exhausted"
        rotations = 0
        last_failure_at = "2026-11-06T09:30:00.000000+00:00"
        last_failure_seq = 1
        turns_at_failure = 4

    manager = _Manager(
        records={"kimi": _OldRecord()},
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["last_failure_class"] == "quota_exhausted"
    assert row["exhausted"] is False


def test_a_healthy_snapshot_decides_when_no_quota_failure_blocks_the_worker():
    manager = _Manager(
        records={"kimi": _Record()},
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=42.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is False


def test_an_exhausted_snapshot_decides_when_no_quota_failure_blocks_the_worker():
    manager = _Manager(
        records={"kimi": _Record()},
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=100.0)),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is True


def test_an_unknown_snapshot_stays_unknown_when_no_quota_failure_blocks():
    manager = _Manager(
        records={"kimi": _Record()},
        ratios={"kimi": 0.1},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["exhausted"] is None
    assert row["reset_at"] is None


def test_a_worker_without_a_context_window_reports_unknown_not_zero(capsys):
    manager = _Manager(records={"kimi": _Record()}, ratios={"kimi": 0.0}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi")),
        context_windows=lambda name: None,
        now=NOW,
    )
    assert _row_of(result, "kimi")["context_used_percent"] is None
    render_workers(result, json_mode=False)
    printed = capsys.readouterr().out
    assert "unknown" in printed
    assert "0%" not in printed


def test_the_worker_set_is_the_union_of_the_manager_and_the_snapshot():
    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("codex")),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert [row["provider"] for row in result["workers"]] == ["codex", "kimi"]


def test_the_snapshot_and_the_session_store_spell_the_same_worker_one_way():
    manager = _Manager(ratios={"glm": 0.1}, keys=["zhipu"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("zai")),
        context_windows=_windows("glm"),
        now=NOW,
    )
    assert [row["provider"] for row in result["workers"]] == ["glm"]


def test_json_output_is_valid_json_with_the_documented_keys(capsys):
    manager = _Manager(
        records={"kimi": _Record(last_failure_class="rate_limited", rotations=1)},
        ratios={"kimi": 0.425},
        keys=["kimi"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi")),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    render_workers(result, json_mode=True)
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == ["workers"]
    assert set(payload["workers"][0]) == {
        "provider",
        "sessions",
        "context_used_percent",
        "last_failure_class",
        "rotations",
        "exhausted",
        "reset_at",
        "pending_rotation",
    }
    assert payload["workers"][0]["context_used_percent"] == 42.5


def test_the_sentinel_token_never_reaches_the_output(capsys):
    """No sentinel can appear in stdout, stderr, the JSON or the payload."""
    manager = _Manager(
        records={
            SENTINEL: _Record(last_failure_class=SENTINEL),
            "kimi": _Record(last_failure_class=SENTINEL),
        },
        ratios={"kimi": 0.1},
        keys=[SENTINEL, "kimi"],
    )
    poisoned = dict(_row("kimi"))
    poisoned["note"] = SENTINEL
    snapshot = _snapshot(
        poisoned,
        {
            "provider": SENTINEL,
            "kind": "quota",
            "status": "ok",
            "updated_at": OBSERVED_AT,
            "windows": [{"used_percent": 100.0, "resets_at_utc": SENTINEL}],
        },
    )
    result = workers_result(
        manager=manager,
        snapshot=snapshot,
        context_windows=_windows("kimi"),
        now=NOW,
    )
    render_workers(result, json_mode=False)
    render_workers(result, json_mode=True)
    printed = capsys.readouterr()
    assert SENTINEL not in printed.out
    assert SENTINEL not in printed.err
    assert SENTINEL not in json.dumps(result)
    assert [row["provider"] for row in result["workers"]] == ["kimi"]
    assert _row_of(result, "kimi")["last_failure_class"] is None


def test_terminal_controls_are_stripped_from_the_output(capsys):
    manager = _Manager(
        records={"kimi\x1b[0m": _Record(last_failure_class="rate_limited\x1b[31m")},
        ratios={"kimi\x1b[0m": 0.1},
        keys=["kimi\x1b[0m"],
    )
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi")),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    render_workers(result, json_mode=False)
    render_workers(result, json_mode=True)
    printed = capsys.readouterr()
    assert "\x1b" not in printed.out
    assert "\x1b" not in printed.err
    assert "kimi" in printed.out
    assert "rate_limited" in printed.out


def test_the_command_reads_the_snapshot_without_writing_or_spawning(
    monkeypatch, tmp_path, capsys
):
    def _forbidden(*args, **kwargs):
        raise AssertionError("conductor workers must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    # The legacy session map of the real installation is not this test's:
    # the package's conftest points HOME at a directory of this test's own.
    snapshot_path = tmp_path / "codexbar-snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(_row("kimi"))), encoding="utf-8")
    sessions_path = tmp_path / "sessions.json"
    before = sorted(path.name for path in tmp_path.iterdir())

    exit_code = cli.main(
        [
            "workers",
            "--snapshot",
            str(snapshot_path),
            "--session-state",
            str(sessions_path),
        ]
    )

    assert exit_code == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert "kimi" in capsys.readouterr().out


def test_the_snapshot_source_may_be_a_callable():
    calls = []

    def _source():
        calls.append("read")
        return _snapshot(_row("kimi"))

    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_source,
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert calls == ["read"]
    assert _row_of(result, "kimi")["exhausted"] is False


def test_a_failing_snapshot_source_leaves_the_quota_unknown():
    def _source():
        raise OSError("the snapshot cannot be read")

    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_source,
        context_windows=_windows("kimi"),
        now=NOW,
    )
    row = _row_of(result, "kimi")
    assert row["exhausted"] is None
    assert row["reset_at"] is None


def test_a_manager_may_list_its_keys_under_another_name():
    class _KeysManager(_Manager):
        def keys(self):
            return list(self._keys)

    manager = _KeysManager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    assert [row["provider"] for row in result["workers"]] == ["kimi"]


def test_the_real_session_manager_and_registry_are_understood(tmp_path):
    """The real manager's state file and the real registry must work."""
    session_manager = pytest.importorskip("src.llms.native_cli.session_manager")
    state = tmp_path / "worker-sessions.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    "thread:abc123:kimi": {
                        "provider": "kimi",
                        "session_id": "s-1",
                        "created_at": "2026-11-06T09:00:00Z",
                        "input_tokens": 100_000,
                        "output_tokens": 0,
                        "rotations": 1,
                        "last_failure_class": "context_exhausted",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = session_manager.WorkerSessionManager(
        path=state,
        # The legacy session map is said where it is: a manager built
        # without a store falls back to the path the native-CLI module
        # worked out when it was imported, which is the home of whoever
        # ran the tests -- not a map this test knows about.
        store=session_manager.NativeCliSessionStore(tmp_path / "sessions.json"),
    )
    result = workers_result(manager=manager, snapshot=_snapshot(_row("kimi")), now=NOW)
    row = _row_of(result, "kimi")
    assert row["sessions"] == 1
    assert row["rotations"] == 1
    assert row["last_failure_class"] == "context_exhausted"
    # The window comes from the registry: Kimi's own 262,144 tokens.
    assert row["context_used_percent"] == 38.1


def test_the_json_output_is_printed_through_the_sanitiser(monkeypatch, capsys):
    """The JSON must pass through terminal_text, not merely look clean."""
    import conductor_cli.workers as workers_module

    seen = []
    original = workers_module.plain

    def _spy(value):
        seen.append(value)
        return original(value)

    monkeypatch.setattr(workers_module, "plain", _spy)
    manager = _Manager(ratios={"kimi": 0.1}, keys=["kimi"])
    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi")),
        context_windows=_windows("kimi"),
        now=NOW,
    )
    render_workers(result, json_mode=True)
    printed = capsys.readouterr().out
    assert json.loads(printed)["workers"][0]["provider"] == "kimi"
    assert any(
        isinstance(value, str) and value.lstrip().startswith("{") for value in seen
    ), "the JSON text did not pass through the sanitiser"


def test_the_cli_registers_the_workers_command(capsys):
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--help"])
    assert exit_info.value.code == 0
    assert "workers" in capsys.readouterr().out


def test_the_home_the_command_reads_is_this_test_s_own(monkeypatch, tmp_path):
    """The command reads the home of this test, not the operator's.

    This is the guard for the isolation above. The home directory of the
    operator holds a legacy session map of their own, and a command that
    read it would show a worker this test knows nothing about. The
    operator's home is written out here -- with a session in it -- and the
    assertion is that the command does not read it.
    """
    fixture_home = Path(os.environ["HOME"])
    assert fixture_home == tmp_path / "isolated-home"

    operator = tmp_path / "operator-home"
    native = operator / ".cache" / "conductor" / "native-cli"
    native.mkdir(parents=True)
    (native / "sessions.json").write_text(
        json.dumps({"thread:other:claude": "s"}), encoding="utf-8"
    )

    reader = WorkerStateReader()

    assert reader.legacy_path == (
        fixture_home / ".cache" / "conductor" / "native-cli" / "sessions.json"
    )
    assert reader.legacy_path != native / "sessions.json"
    # The operator's session is not read.
    assert reader.legacy_keys() == ()


def test_a_store_without_a_path_stays_inside_this_test_s_home(tmp_path):
    """The store's own default must not be the operator's home.

    The native-CLI session store works out its default path when its
    module is imported, so the ``HOME`` of that moment is baked into a
    constant. A store built without a path reads that home -- the
    operator's -- and sees the sessions of a machine this test knows
    nothing about. The isolation above points that default at this test's
    own home, and this is the guard for it: a manager or a store built
    without an explicit path stays inside this test's home.
    """
    sessions = pytest.importorskip("src.llms.native_cli.sessions")
    fixture_home = Path(os.environ["HOME"])
    assert fixture_home == tmp_path / "isolated-home"
    assert sessions.DEFAULT_SESSION_PATH.is_relative_to(fixture_home)
    # ambient-store-default: this test is about the default path itself
    store = sessions.NativeCliSessionStore()
    assert store.path.is_relative_to(fixture_home)


def test_a_manager_without_a_store_does_not_count_the_operators_sessions(tmp_path):
    """The operator's session of the same provider must not be counted.

    The legacy map of the operator's home holds a session of the provider
    this test is about. A manager built without a store reads the home of
    whoever started the run, so it counts that session as a second one --
    and the test then fails on a machine that has used Conductor and
    passes on one that has not. The isolation above points the default at
    this test's own home, so the count stays right either way.
    """
    session_manager = pytest.importorskip("src.llms.native_cli.session_manager")

    operator = tmp_path / "operator-home"
    native = operator / ".cache" / "conductor" / "native-cli"
    native.mkdir(parents=True)
    (native / "sessions.json").write_text(
        json.dumps({"thread:other:kimi": "synthetic-session"}), encoding="utf-8"
    )

    state = tmp_path / "worker-sessions.json"
    state.write_text(
        json.dumps(
            {"lines": {"thread:abc:kimi": {"provider": "kimi", "session_id": "s-1"}}}
        ),
        encoding="utf-8",
    )

    # No store is given: the default is what the isolation has to cover.
    # ambient-store-default: this test is about the default path itself
    manager = session_manager.WorkerSessionManager(path=state)
    # The store it fell back on is not the operator's: the default points
    # at this test's own home, so the operator's session cannot be
    # counted. The second assertion says the same thing about the count.
    assert not manager.store.path.is_relative_to(operator)
    assert manager.legacy_keys() == ()


def test_the_workers_handler_runs(monkeypatch, tmp_path):
    # The legacy session map of the real installation is not this test's:
    # this module's fixture points HOME at a directory of this test's own.
    snapshot_path = tmp_path / "codexbar-snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(_row("kimi"))), encoding="utf-8")
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "workers",
            "--snapshot",
            str(snapshot_path),
            "--session-state",
            str(tmp_path / "sessions.json"),
        ]
    )
    result = args.handler(args)
    assert [row["provider"] for row in result["workers"]] == ["kimi"]


def test_the_refresh_is_opt_in(monkeypatch, tmp_path, capsys):
    calls = []

    def _fake_refresh(path, **kwargs):
        calls.append(str(path))

    monkeypatch.setattr(
        "conductor_cli.codexbar_snapshot.refresh_snapshot", _fake_refresh
    )
    # The legacy session map of the real installation is not this test's:
    # the package's conftest points HOME at a directory of this test's own.
    snapshot_path = tmp_path / "codexbar-snapshot.json"
    exit_code = cli.main(["workers", "--refresh", "--snapshot", str(snapshot_path)])
    assert exit_code == 0
    assert calls == [str(snapshot_path)]


def test_the_cli_prints_the_documented_json_shape(monkeypatch, tmp_path, capsys):
    # The legacy session map of the real installation is not this test's:
    # the package's conftest points HOME at a directory of this test's own.
    snapshot_path = tmp_path / "codexbar-snapshot.json"
    snapshot_path.write_text(json.dumps(_snapshot(_row("kimi"))), encoding="utf-8")
    exit_code = cli.main(
        [
            "workers",
            "--json",
            "--snapshot",
            str(snapshot_path),
            "--session-state",
            str(tmp_path / "sessions.json"),
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == ["workers"]
    assert payload["workers"][0]["provider"] == "kimi"


# ----------------------------------------------------------------------
# the second review round: the listing does not change the state, and the
# quota counters belong to the worker
# ----------------------------------------------------------------------


def _real_manager(tmp_path, lines=None):
    """A real session manager over a throwaway state file."""
    from src.llms.native_cli.session_manager import WorkerSessionManager
    from src.llms.native_cli.sessions import NativeCliSessionStore

    rich = tmp_path / "worker-sessions.json"
    rich.write_text(
        json.dumps({"version": 1, "lines": lines or {}}) + "\n", encoding="utf-8"
    )
    legacy = tmp_path / "sessions.json"
    legacy.write_text("{}\n", encoding="utf-8")
    manager = WorkerSessionManager(
        path=rich,
        store=NativeCliSessionStore(legacy),
        handoff_dir=tmp_path / "handoffs",
    )
    return manager, rich, legacy


def test_a_success_on_another_line_of_the_worker_covers_the_quota_failure(tmp_path):
    # The counters belong to the worker, not to the line. A worker is one
    # provider, and a turn that gets through on one of its lines says the
    # whole worker is working again -- so a quota failure on one line is
    # covered by a success on another one.
    manager, _, _ = _real_manager(tmp_path)
    manager.record_failure("thread:a:kimi", "quota_exhausted")

    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is True

    manager.record_turn("thread:b:kimi", session_id="sess-b")

    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("kimi", used=10.0)),
        now=NOW,
    )
    assert _row_of(result, "kimi")["exhausted"] is False


def test_the_listing_does_not_finish_a_pending_rotation(tmp_path, monkeypatch):
    # The listing only reports. A manager that opens the state file finishes
    # a rotation that was interrupted -- so the listing must not build one
    # that may: the state file, the legacy map and the archive are all left
    # exactly as they were.
    from src.llms.native_cli import session_manager as session_manager_module
    from src.llms.native_cli.sessions import NativeCliSessionStore

    key = "thread:pending:kimi"
    archive = tmp_path / "handoffs" / "pending.md"
    rich = tmp_path / "worker-sessions.json"
    rich.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    key: {
                        "provider": "kimi",
                        "session_id": "session-old",
                        "turns": 1,
                        "success_seq": 1,
                        "rotation_pending": {
                            "key": key,
                            "old_id": "session-old",
                            "new_id": "rot-fixed",
                            "archive_path": str(archive),
                            "reason": "active_rotation",
                            "rotations": 1,
                        },
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    legacy = tmp_path / "sessions.json"
    legacy.write_text(json.dumps({key: "session-old"}) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        session_manager_module,
        "NativeCliSessionStore",
        lambda path=None: NativeCliSessionStore(legacy),
    )
    monkeypatch.setattr(session_manager_module, "DEFAULT_HANDOFF_DIR", tmp_path / "handoffs")

    before = rich.read_bytes()
    workers_result(snapshot={"providers": []}, session_path=rich, now=NOW)

    assert rich.read_bytes() == before
    assert not archive.exists()
    assert json.loads(legacy.read_text(encoding="utf-8")) == {key: "session-old"}


def test_the_listing_reports_a_pending_rotation_without_finishing_it(
    tmp_path, monkeypatch
):
    # A rotation that was interrupted left its intent on the disk. The
    # listing says so -- "pending_rotation": true -- and does NOT finish it:
    # this command only reports.
    from src.llms.native_cli import session_manager as session_manager_module
    from src.llms.native_cli.sessions import NativeCliSessionStore

    key = "thread:pending:kimi"
    archive = tmp_path / "handoffs" / "pending.md"
    rich = tmp_path / "worker-sessions.json"
    rich.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    key: {
                        "provider": "kimi",
                        "session_id": "session-old",
                        "turns": 1,
                        "rotation_pending": {
                            "key": key,
                            "old_id": "session-old",
                            "new_id": "rot-fixed",
                            "archive_path": str(archive),
                            "reason": "active_rotation",
                            "rotations": 1,
                        },
                    }
                },
                "workers": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    legacy = tmp_path / "sessions.json"
    legacy.write_text(json.dumps({key: "session-old"}) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        session_manager_module,
        "NativeCliSessionStore",
        lambda path=None: NativeCliSessionStore(legacy),
    )
    monkeypatch.setattr(session_manager_module, "DEFAULT_HANDOFF_DIR", tmp_path / "handoffs")

    result = workers_result(snapshot={"providers": []}, session_path=rich, now=NOW)

    row = _row_of(result, "kimi")
    assert row["pending_rotation"] is True
    assert not archive.exists()
    assert "rotation_pending" in json.loads(rich.read_text(encoding="utf-8"))["lines"][key]


def test_the_listing_leaves_the_state_files_alone_down_to_the_mtime(
    tmp_path, monkeypatch
):
    # The listing only reports: the state file, the legacy map and their
    # mtimes are all the same after it as before it.
    from src.llms.native_cli import session_manager as session_manager_module
    from src.llms.native_cli.sessions import NativeCliSessionStore

    key = "thread:a:kimi"
    rich = tmp_path / "worker-sessions.json"
    rich.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {key: {"provider": "kimi", "session_id": "s1", "turns": 2}},
                "workers": {"kimi": {"success_seq": 2}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    legacy = tmp_path / "sessions.json"
    legacy.write_text(json.dumps({key: "s1"}) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        session_manager_module,
        "NativeCliSessionStore",
        lambda path=None: NativeCliSessionStore(legacy),
    )

    before = (rich.read_bytes(), rich.stat().st_mtime_ns)
    before_legacy = (legacy.read_bytes(), legacy.stat().st_mtime_ns)

    workers_result(snapshot={"providers": []}, session_path=rich, now=NOW)

    assert (rich.read_bytes(), rich.stat().st_mtime_ns) == before
    assert (legacy.read_bytes(), legacy.stat().st_mtime_ns) == before_legacy


# ----------------------------------------------------------------------
# one worker, however the files spell it
# ----------------------------------------------------------------------


def test_two_spellings_of_one_worker_are_one_worker_and_not_exhausted(tmp_path):
    # "step" and "stepfun" are two spellings of one provider. The worker
    # succeeds five times, runs out of quota, and then succeeds once more
    # under the other spelling: it is working again, so it is not exhausted.
    manager, _, _ = _real_manager(tmp_path)
    for _ in range(5):
        manager.record_turn("thread:t1:step", session_id="sess-a")
    manager.record_failure("thread:t1:step", "quota_exhausted")
    manager.record_turn("thread:t2:stepfun", session_id="sess-b")

    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("stepfun")),
        context_windows=_windows("stepfun"),
        now=NOW,
    )
    row = _row_of(result, "stepfun")
    assert row["sessions"] == 2
    assert row["exhausted"] is False


def test_two_spellings_of_one_worker_with_unequal_counts_are_one_worker(tmp_path):
    # The counts are not equal, and the worker is still one worker: the
    # success under the second spelling is what says the quota came back.
    manager, _, _ = _real_manager(tmp_path)
    manager.record_turn("thread:t1:step", session_id="sess-a")
    manager.record_turn("thread:t1:step", session_id="sess-a")
    manager.record_failure("thread:t1:step", "quota_exhausted")
    manager.record_turn("thread:t2:stepfun", session_id="sess-b")

    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("stepfun")),
        context_windows=_windows("stepfun"),
        now=NOW,
    )
    row = _row_of(result, "stepfun")
    assert row["exhausted"] is False
    # One row, and no second row for the other spelling.
    assert [r["provider"] for r in result["workers"]] == ["stepfun"]


def test_two_spellings_interleaved_in_both_orders_are_one_worker(tmp_path):
    # The other order: the failure first, then a success under the first
    # spelling, then another under the second.
    manager, _, _ = _real_manager(tmp_path)
    manager.record_failure("thread:t1:step", "quota_exhausted")
    manager.record_turn("thread:t2:stepfun", session_id="sess-b")

    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("stepfun")),
        context_windows=_windows("stepfun"),
        now=NOW,
    )
    row = _row_of(result, "stepfun")
    assert row["exhausted"] is False
    assert [r["provider"] for r in result["workers"]] == ["stepfun"]


def test_a_state_file_with_two_alias_records_merges_them(tmp_path):
    # A file written before the identity was normalised may hold a record
    # for "step" and one for "stepfun". They are one worker: their
    # successes add up and the newer quota marker wins.
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    state.write_text(
        json.dumps(
            {
                "version": 1,
                "lines": {
                    "thread:t1:step": {"provider": "step", "session_id": "sess-a"},
                    "thread:t2:stepfun": {"provider": "stepfun", "session_id": "sess-b"},
                },
                "workers": {
                    "step": {"success_seq": 5, "quota_failed_at_seq": 3},
                    "stepfun": {"success_seq": 2, "quota_failed_at_seq": None},
                },
            }
        ),
        encoding="utf-8",
    )
    legacy.write_text(json.dumps({}), encoding="utf-8")
    session_manager = pytest.importorskip("src.llms.native_cli.session_manager")
    sessions = pytest.importorskip("src.llms.native_cli.sessions")
    manager = session_manager.WorkerSessionManager(
        path=state, store=sessions.NativeCliSessionStore(path=legacy)
    )

    record = manager.current("thread:t1:step")
    assert record.success_seq == 7, "the successes of both spellings add up"
    assert record.quota_failed_at_seq == 3, "the newer quota marker wins"

    result = workers_result(
        manager=manager,
        snapshot=_snapshot(_row("stepfun")),
        context_windows=_windows("stepfun"),
        now=NOW,
    )
    assert [r["provider"] for r in result["workers"]] == ["stepfun"]
    assert _row_of(result, "stepfun")["exhausted"] is False


def test_the_table_shows_a_pending_rotation(tmp_path):
    manager = _Manager(
        records={"thread:t1:kimi": _Record()},
        ratios={"thread:t1:kimi": 0.25},
        keys=("thread:t1:kimi",),
    )
    result = workers_result(
        manager=manager, snapshot=_snapshot(), context_windows=_windows("kimi"), now=NOW
    )
    assert result["workers"], "the worker is not listed"
    result["workers"][0]["pending_rotation"] = True
    printed = []
    import io
    from contextlib import redirect_stdout

    with redirect_stdout(io.StringIO()) as buffer:
        render_workers(result, json_mode=False)
    printed = buffer.getvalue()
    assert "PENDING" in printed
    assert "yes" in printed
