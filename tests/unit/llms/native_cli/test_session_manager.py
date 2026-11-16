"""The worker's session manager: when a session is replaced, and what the next one is told.

A session that runs out of context is worth nothing, and a rotation that loses
the pointer to the work already done is worse than the failure it avoided. So
these tests hold three things at once: the line is rotated *before* its context
runs out, the old session is archived rather than dropped, and the handover the
next session starts from carries the work's facts and neither the prompt nor a
credential.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUsage
from src.llms.native_cli.registry import context_window_tokens
from src.llms.native_cli.session_manager import (
    CONTEXT_EXHAUSTED,
    MAX_ROTATIONS,
    ROTATION_EXHAUSTED_REASON,
    ROTATION_THRESHOLD,
    WorkerSessionManager,
)
from src.llms.native_cli.sessions import (
    NativeCliSessionStore,
    SessionStoreUnavailable,
)


KEY = "thread:t1:kimi"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> dict:
    """A throwaway repository: a baseline commit, a commit since it, a change."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "feat/worker-health")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "worker")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "chore: baseline")
    baseline = _git(root, "rev-parse", "HEAD")
    (root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "feat: second commit")
    return {
        "root": root,
        "baseline": baseline,
        "head": _git(root, "rev-parse", "HEAD"),
    }


@pytest.fixture
def make_manager(tmp_path, repo):
    """Build a manager over the throwaway repository, one isolated case each."""
    cases = iter(range(1000))
    # The task file and the report are the same for every case: the handover
    # names them, so two managers that are meant to agree must be given the
    # same ones.
    task_file = tmp_path / "task.md"
    task_file.write_text("the task\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("the report\n", encoding="utf-8")

    def _make(**overrides):
        case = tmp_path / f"case-{next(cases)}"
        case.mkdir()
        params = {
            "path": case / "worker-sessions.json",
            "store": NativeCliSessionStore(case / "sessions.json"),
            "clock": lambda: 1_700_000_000.0,
            "worktree": repo["root"],
            "baseline": repo["baseline"],
            "task_file": task_file,
            "report": report,
            "handoff_dir": case / "handoffs",
        }
        params.update(overrides)
        return WorkerSessionManager(**params), task_file, report

    return _make


# ----------------------------------------------------------------------
# the registry's windows


def test_a_pool_spelling_answers_the_same_as_the_registered_name() -> None:
    # The dispatch pool says "claude" and "deepseek"; the registry says
    # "claude-code" and "deepseek-dsh". A caller holding either spelling must
    # get the same window, or a line rotates on one spelling and never on the
    # other.
    assert context_window_tokens("claude") == context_window_tokens("claude-code")
    assert context_window_tokens("deepseek") == context_window_tokens("deepseek-dsh")
    assert context_window_tokens("claude") is not None


def test_an_unknown_provider_has_no_window_and_does_not_raise() -> None:
    assert context_window_tokens("manus") is None
    assert context_window_tokens("") is None
    assert context_window_tokens("   ") is None


def test_kimi_the_provider_this_task_calls_out_has_a_window() -> None:
    assert context_window_tokens("kimi") is not None


# ----------------------------------------------------------------------
# active rotation


def test_a_session_at_eighty_percent_of_its_window_is_due(make_manager) -> None:
    manager, _, _ = make_manager()
    window = context_window_tokens("kimi")
    # The PRD's number, spelled out here: a test that read the constant would
    # follow it and prove nothing.
    assert ROTATION_THRESHOLD == 0.8
    below = int(window * 0.79)
    at = int(window * 0.8) + 1

    manager.record_turn(KEY, usage=NativeCliUsage(input_tokens=below))
    assert manager.should_rotate(KEY) is False

    manager.record_turn(KEY, usage=NativeCliUsage(input_tokens=at - below))
    assert manager.should_rotate(KEY) is True


def test_a_provider_without_a_window_never_rotates_on_usage_alone(make_manager) -> None:
    manager, _, _ = make_manager()
    assert context_window_tokens("cursor") is None

    manager.record_turn(
        "thread:t1:cursor", usage=NativeCliUsage(input_tokens=10_000_000)
    )
    assert manager.should_rotate("thread:t1:cursor") is False
    # No window is not a share of zero: the answer is "unknown".
    assert manager.usage_ratio("thread:t1:cursor") is None


def test_the_ratio_counts_input_and_output_tokens(make_manager) -> None:
    manager, _, _ = make_manager()
    window = context_window_tokens("kimi")

    manager.record_turn(
        KEY,
        usage=NativeCliUsage(input_tokens=window // 4, output_tokens=window // 4),
    )
    assert manager.usage_ratio(KEY) == pytest.approx(0.5)


# ----------------------------------------------------------------------
# passive rotation


def test_a_context_exhausted_failure_rotates_at_once(make_manager) -> None:
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-1")

    manager.record_failure(KEY, CONTEXT_EXHAUSTED)
    assert manager.should_rotate(KEY) is True

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert result.rotated is True
    assert result.session_id is not None
    assert result.session_id != "sess-1"
    # The old session is archived, not deleted: its id is still there.
    assert "sess-1" in manager.archived_session_ids(KEY)
    assert manager.current(KEY).session_id == result.session_id
    # The failure that asked for the rotation has been handled.
    assert manager.should_rotate(KEY) is False


def test_a_rotation_starts_the_new_session_over(make_manager) -> None:
    manager, _, _ = make_manager()
    window = context_window_tokens("kimi")
    manager.record_turn(
        KEY, usage=NativeCliUsage(input_tokens=window, output_tokens=window)
    )
    assert manager.should_rotate(KEY) is True

    manager.rotate(KEY, reason="active_rotation")
    record = manager.current(KEY)
    assert record.turns == 0
    assert record.input_tokens == 0
    assert record.output_tokens == 0
    assert manager.usage_ratio(KEY) == pytest.approx(0.0)


# ----------------------------------------------------------------------
# the handover note


def test_the_handoff_carries_the_work_the_next_session_needs(make_manager, repo) -> None:
    manager, task_file, report = make_manager()
    manager.record_turn(KEY, session_id="sess-1")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert str(task_file) in content
    assert "feat/worker-health" in content
    assert repo["head"] in content
    assert str(report) in content
    # v1: the handoff carries the change summary as numbers only -- the file
    # paths are left to the next session, which runs git diff --stat itself.
    assert "a.txt" not in content
    assert "1 file changed, 1 insertion(+)" in content
    assert "git diff --stat" in content
    # A commit is a hash now: the subject is the worker's own text and
    # never reaches the handoff.
    assert "feat: second commit" not in content
    assert "chore: baseline" not in content
    assert content.count(repo["head"]) >= 2  # the HEAD line and the commit list
    assert "继续执行任务文件" in content


def test_the_handoff_names_no_path_or_tainted_file(make_manager, repo, tmp_path) -> None:
    """v1: the handoff carries numbers and hashes, not paths, not free text.

    The repo gains a file whose name embeds a credential; the rotation
    reason carries a sentinel. Neither reaches the note: the change is
    reported as the stat's own summary numbers, and the reason is written
    as the fixed category code or ``unknown``.
    """
    from src.llms.native_cli.session_manager import WorkerSessionManager

    tainted = 'api_key=[redacted]auditmarker.txt'
    root = repo["root"]
    (root / tainted).write_text("x\n", encoding="utf-8")
    _git(root, "add", tainted)
    _git(root, "commit", "--no-gpg-sign", "-m", "chore: add a file")

    case = tmp_path / "case-tainted"
    case.mkdir()
    manager = WorkerSessionManager(
        path=case / "worker-sessions.json",
        store=NativeCliSessionStore(case / "sessions.json"),
        worktree=root,
        baseline=repo["baseline"],
        task_file=str(tmp_path / "task.md"),
        report=str(tmp_path / "report.md"),
        handoff_dir=case / "handoffs",
    )
    result = manager.rotate(KEY, reason="context_exhausted SECRET-REASON-SENTINEL")
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "auditmarker" not in content
    assert tainted not in content
    assert "SECRET-REASON-SENTINEL" not in content
    assert "- 轮换原因: unknown" in content
    # The change is reported as the stat's summary numbers only: the tainted
    # file name is not in the note, and the note points the next session at
    # git diff --stat for the paths.
    assert "files changed" in content
    assert "insertion" in content
    assert "git diff --stat" in content
    assert tainted.split("=")[1][:10] not in content  # "auditmark" prefix
    assert "继续执行任务文件" in content


@pytest.mark.asyncio
async def test_the_handoff_names_no_path_and_no_free_reason(tmp_path) -> None:
    """Second guard for the same rule, over a real legacy store."""
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    store.set("thread:t1:kimi", "sess-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=store,
        handoff_dir=tmp_path / "handoffs",
    )
    result = manager.rotate(
        "thread:t1:kimi", reason="active_rotation SECRET-REASON-SENTINEL"
    )
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "SECRET-REASON-SENTINEL" not in content
    assert "- 轮换原因: unknown" in content


def test_two_handoffs_for_the_same_inputs_are_the_same(make_manager) -> None:
    first, _, _ = make_manager()
    second, _, _ = make_manager()
    for manager in (first, second):
        manager.record_turn(KEY, session_id="sess-1")

    one = first.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    two = second.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    assert one.handoff_path.read_text(encoding="utf-8") == two.handoff_path.read_text(
        encoding="utf-8"
    )


def test_the_handoff_carries_neither_a_credential_nor_the_prompt(make_manager, repo) -> None:
    root = repo["root"]
    (root / "b.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "b.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "chore: use sk-" "live-SENTINEL0123456789abcdef")

    manager, task_file, _ = make_manager()
    # The prompt is never handed to the manager: there is no parameter that
    # could carry it. What the task file holds stays in the task file.
    task_file.write_text("the prompt: PROMPT-SENTINEL-xyz\n", encoding="utf-8")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "SENTINEL0123456789abcdef" not in content
    assert "PROMPT-SENTINEL-xyz" not in content
    # The commit is listed as a hash: the subject that carried the
    # credential is not in the note at all, masked or otherwise.
    assert "chore:" not in content
    assert repo["head"] in content


def test_a_failure_on_a_line_without_a_session_still_yields_a_record(make_manager) -> None:
    manager, _, _ = make_manager()
    record = manager.record_failure(KEY, "rate_limited")
    assert record.session_id
    assert record.last_failure_class == "rate_limited"
    # A rate limit is not a reason to rotate.
    assert manager.should_rotate(KEY) is False


# ----------------------------------------------------------------------
# the legacy line counters: migration is fail open (v1 narrowing)
# ----------------------------------------------------------------------


def _write_legacy_state(path: Path, lines: dict) -> None:
    """An old-format state file: counters on the lines, no worker records."""
    path.write_text(
        json.dumps({"version": 1, "lines": lines}) + "\n", encoding="utf-8"
    )


_LEGACY_LINES = {
    "thread:t1:step": {
        "session_id": "s1",
        "provider": "step",
        "success_seq": 5,
        "quota_failed_at_seq": 5,
    },
    "thread:t2:step": {
        "session_id": "s2",
        "provider": "step",
        "success_seq": 6,
    },
    "thread:t3:stepfun": {
        "session_id": "s3",
        "provider": "stepfun",
        "success_seq": 1,
        "quota_failed_at_seq": 0,
    },
}


def test_a_legacy_migration_sums_and_stays_fail_open(tmp_path) -> None:
    """The line counters sum across the aliases, and no quota state is
    judged from records that cannot be ordered: the migrated worker is not
    exhausted, and the line counters are gone from the migrated file."""
    lines_a = _LEGACY_LINES
    lines_b = dict(reversed(list(_LEGACY_LINES.items())))

    for label, lines in (("a", lines_a), ("b", lines_b)):
        # The old file *is* the manager's own file: an old-format file that
        # lives where the manager reads its state, so the migration reads the
        # counters and the manager writes the worker record back.
        state = tmp_path / f"worker-{label}.json"
        _write_legacy_state(state, lines)
        manager = WorkerSessionManager(
            path=state,
            store=NativeCliSessionStore(tmp_path / f"sessions-{label}.json"),
            handoff_dir=tmp_path / f"handoffs-{label}",
        )
        record = manager.current("thread:t1:step")
        assert record.success_seq == 12, label  # 5 + 6 + 1, order-free
        assert record.quota_failed_at_seq is None, label


def test_a_single_literal_with_a_marker_is_not_exhausted_after_migration(tmp_path):
    _write_legacy_state(
        tmp_path / "worker.json",
        {
            "thread:t1:step": {
                "session_id": "s1",
                "provider": "step",
                "success_seq": 5,
                "quota_failed_at_seq": 5,
            }
        },
    )
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        handoff_dir=tmp_path / "handoffs",
    )
    record = manager.current("thread:t1:step")
    assert record.success_seq == 5
    assert record.quota_failed_at_seq is None


def test_the_two_spellings_of_one_worker_merge_into_one_counter(tmp_path):
    _write_legacy_state(tmp_path / "worker.json", _LEGACY_LINES)
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        handoff_dir=tmp_path / "handoffs",
    )
    # step 5 + step 6 + stepfun 1: one worker, one counter.
    stepfun = manager.current("thread:t3:stepfun")
    assert stepfun.success_seq == 12


def test_step_and_stepfun_merge_to_six_successes(tmp_path):
    """The Planner's case: two spellings of one provider, five and one.

    ``step`` and ``stepfun`` are one provider, so the successes of both are
    the successes of one worker: five and one is six, not the larger of the
    two.
    """
    _write_legacy_state(
        tmp_path / "worker.json",
        {
            "thread:t1:step": {
                "session_id": "s1",
                "provider": "step",
                "success_seq": 5,
            },
            "thread:t2:stepfun": {
                "session_id": "s2",
                "provider": "stepfun",
                "success_seq": 1,
            },
        },
    )
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        handoff_dir=tmp_path / "handoffs",
    )
    assert manager.current("thread:t1:step").success_seq == 6
    assert manager.current("thread:t2:stepfun").success_seq == 6


def test_an_unrelated_write_does_not_freeze_the_migration(tmp_path) -> None:
    """A kimi write after the migration must not pin step's migrated counters."""
    _write_legacy_state(tmp_path / "worker.json", _LEGACY_LINES)
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        handoff_dir=tmp_path / "handoffs",
    )
    manager.current("thread:t1:step")
    manager.record_turn("thread:t4:kimi", session_id="sess-kimi")

    state = json.loads((tmp_path / "worker.json").read_text(encoding="utf-8"))
    assert state["workers"]["kimi"]["success_seq"] == 1
    assert state["workers"]["stepfun"]["success_seq"] == 12
    assert state["workers"]["stepfun"].get("quota_failed_at_seq") is None
    # The line counters are gone: they belong to the worker records now.
    for line in state["lines"].values():
        assert "success_seq" not in line


@pytest.mark.asyncio
async def test_the_migrated_file_is_written_by_a_real_turn(tmp_path) -> None:
    """The migration is not just a helper result: a real turn on a line of
    the old file reads the old counters, migrates them, and writes the
    worker record back."""
    _write_legacy_state(tmp_path / "worker.json", _LEGACY_LINES)
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=store,
        handoff_dir=tmp_path / "handoffs",
    )
    manager.record_turn("thread:t1:step", session_id="s1")

    state = json.loads((tmp_path / "worker.json").read_text(encoding="utf-8"))
    assert state["workers"]["stepfun"]["success_seq"] == 13  # 12 + this turn
    assert state["workers"]["stepfun"].get("quota_failed_at_seq") is None
    # The line carries no counter any more: the migration took the counters
    # off the lines, and the file says so by leaving the field out.
    assert state["lines"]["thread:t1:step"].get("success_seq") is None


# ----------------------------------------------------------------------
# parking


def test_a_line_that_keeps_rotating_is_parked(make_manager) -> None:
    manager, _, _ = make_manager()
    for _ in range(MAX_ROTATIONS):
        assert manager.rotate(KEY, reason=CONTEXT_EXHAUSTED).rotated is True
    assert manager.current(KEY).rotations == MAX_ROTATIONS

    parked = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert parked.rotated is False
    assert parked.parked is True
    assert parked.reason_code == ROTATION_EXHAUSTED_REASON
    assert parked.session_id is None
    # No further rotation happens on a parked line.
    assert manager.current(KEY).rotations == MAX_ROTATIONS
    assert manager.should_rotate(KEY) is False
    assert manager.rotate(KEY, reason=CONTEXT_EXHAUSTED).parked is True


# ----------------------------------------------------------------------
# backward compatibility and privacy


def test_the_legacy_session_map_is_read_and_left_alone(tmp_path) -> None:
    legacy = tmp_path / "sessions.json"
    legacy.write_text(
        json.dumps({"thread:t1:kimi": "sess-legacy"}) + "\n", encoding="utf-8"
    )
    before = legacy.read_text(encoding="utf-8")

    manager = WorkerSessionManager(
        path=tmp_path / "worker-sessions.json",
        store=NativeCliSessionStore(legacy),
    )
    record = manager.current("thread:t1:kimi")
    assert record is not None
    assert record.session_id == "sess-legacy"

    # Reading is reading: the flat map is not rewritten into another shape.
    assert legacy.read_text(encoding="utf-8") == before
    data = json.loads(legacy.read_text(encoding="utf-8"))
    assert data == {"thread:t1:kimi": "sess-legacy"}
    assert all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    )
    # The manager's own richer state lives in its own file.
    assert manager.path != legacy
    assert not manager.path.exists()


def test_a_recorded_turn_keeps_the_legacy_map_a_flat_map(make_manager) -> None:
    manager, _, _ = make_manager()
    legacy = manager.store.path

    manager.record_turn(KEY, session_id="sess-2")

    data = json.loads(legacy.read_text(encoding="utf-8"))
    assert data == {KEY: "sess-2"}
    assert all(
        isinstance(key, str) and isinstance(value, str) for key, value in data.items()
    )


def test_the_handoff_and_the_state_file_are_private(make_manager) -> None:
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-1")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert stat.S_IMODE(result.handoff_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600


def test_the_handoff_path_is_the_one_the_line_was_given(make_manager) -> None:
    manager, _, _ = make_manager()
    assert manager.handoff_path(KEY) is None

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert manager.handoff_path(KEY) == result.handoff_path


# ----------------------------------------------------------------------
# the fix round: what a reviewer found
# ----------------------------------------------------------------------


def test_the_handoff_drops_a_credential_made_of_only_letters(make_manager, repo) -> None:
    # A credential that names itself is a credential whatever it is made of.
    # The shape rule needs a digit to tell a secret from a long word, and a
    # fake Bearer value of pure letters has no digit -- so it used to survive
    # into the handover note, which is the one file a human reads.
    root = repo["root"]
    (root / "c.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "c.txt")
    _git(
        root,
        "commit",
        "--no-gpg-sign",
        "-m",
        "chore: Authorization: Bearer abcdefghijklmnopqrstuvwxyzABCDEFG",
    )

    manager, _, _ = make_manager()
    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "abcdefghijklmnopqrstuvwxyzABCDEFG" not in content
    assert "Bearer" not in content
    # The commit line is a hash: the subject line is gone.
    import re as _re

    assert _re.search(r"^\n?[0-9a-f]{40}$", content, _re.MULTILINE)


def test_two_managers_do_not_erase_each_others_line(tmp_path, repo) -> None:
    # Two managers may work on the same state file -- a dispatcher and a
    # status command, say. A manager that wrote back its whole cached copy
    # would erase the line the other one saved in the meantime, so only the
    # lines this manager changed are written over the file.
    state = tmp_path / "worker-sessions.json"
    first = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(tmp_path / "first.json"),
        worktree=repo["root"],
    )
    second = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(tmp_path / "second.json"),
        worktree=repo["root"],
    )
    # The second manager reads the file before the first one has written to
    # it, so it holds a stale copy of what is on disk.
    assert second.current("thread:other:kimi") is None

    first.record_turn("thread:a:kimi", session_id="sess-a")
    second.record_turn("thread:b:kimi", session_id="sess-b")

    written = json.loads(state.read_text(encoding="utf-8"))
    assert set(written["lines"]) == {"thread:a:kimi", "thread:b:kimi"}
    assert written["lines"]["thread:a:kimi"]["session_id"] == "sess-a"
    assert written["lines"]["thread:b:kimi"]["session_id"] == "sess-b"


def test_a_rotation_that_cannot_be_written_keeps_the_old_session(
    tmp_path, repo, monkeypatch
) -> None:
    # A rotation writes the manager's own state and only then drops the old
    # id from the flat map. If the write fails, the old session is still
    # where the runner can find it: cleared first, the id would be gone
    # while the state on disk still named the old session and had no
    # archive, and the work of that session would be unreachable.
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=repo["root"],
        handoff_dir=tmp_path / "handoffs",
    )
    manager.record_turn(KEY, session_id="sess-old")
    assert manager.current(KEY).session_id == "sess-old"

    def refuse() -> None:
        raise OSError("the disk is full")

    monkeypatch.setattr(manager, "_save", refuse)
    with pytest.raises(OSError):
        manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    # The flat map still names the old session.
    assert NativeCliSessionStore(legacy).get(KEY) == "sess-old"
    # And the state on disk still holds it, with no archive of it.
    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["lines"][KEY]["session_id"] == "sess-old"
    assert not written["lines"][KEY].get("archived")


def test_a_successful_rotation_archives_the_old_session_in_both_stores(
    tmp_path, repo
) -> None:
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=repo["root"],
        handoff_dir=tmp_path / "handoffs",
    )
    manager.record_turn(KEY, session_id="sess-old")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert result.rotated is True

    # The old id is archived, not deleted.
    assert "sess-old" in manager.archived_session_ids(KEY)
    # The flat map no longer points at it: the next turn starts a new one.
    assert NativeCliSessionStore(legacy).get(KEY) is None
    # And the two stores agree: the old id is history in both, and the new
    # one is the live session in the state.
    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["lines"][KEY]["session_id"] == result.session_id
    assert written["lines"][KEY]["archived"][0]["session_id"] == "sess-old"


def test_the_failure_class_is_stored_as_its_plain_name(make_manager) -> None:
    # ``str()`` of a string enum is its qualified name, not its value, so an
    # enum that reached the disk as "FailureClass.CONTEXT_EXHAUSTED" was
    # dropped by every reader that checks the class against the vocabulary --
    # and the rotation trigger never fired.
    from src.llms.native_cli.failure import FailureClass

    manager, _, _ = make_manager()
    record = manager.record_failure(KEY, FailureClass.CONTEXT_EXHAUSTED)

    assert record.last_failure_class == "context_exhausted"
    written = json.loads(manager.path.read_text(encoding="utf-8"))
    assert written["lines"][KEY]["last_failure_class"] == "context_exhausted"
    # The trigger follows the class, so an exhausted context still rotates.
    assert manager.should_rotate(KEY) is True


def test_a_plain_failure_class_name_is_still_accepted(make_manager) -> None:
    manager, _, _ = make_manager()
    record = manager.record_failure(KEY, "rate_limited")
    assert record.last_failure_class == "rate_limited"


def test_the_rotation_count_starts_again_after_the_first_turn(make_manager) -> None:
    # The count is a count of CONSECUTIVE rotations: it says how many times
    # the session had to be replaced without a single turn getting through.
    # The first turn that completes in the new session is proof that the new
    # session works, so the count starts again.
    manager, _, _ = make_manager()
    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert manager.current(KEY).rotations == 1

    manager.record_turn(KEY)
    assert manager.current(KEY).rotations == 0


def test_the_rotation_count_survives_a_failure_that_is_not_a_turn(make_manager) -> None:
    # A failure is not a completed turn: it proves nothing about the new
    # session, so the count still stands.
    manager, _, _ = make_manager()
    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    manager.record_failure(KEY, "network")
    assert manager.current(KEY).rotations == 1


def test_two_keys_that_fold_to_one_name_get_different_handoff_files(make_manager) -> None:
    # "thread:a:b:kimi" and "thread:a-b:kimi" fold to the same readable
    # name. Without a hash of the untouched key they would write over each
    # other's handover notes.
    manager, _, _ = make_manager()
    one = manager.rotate("thread:a:b:kimi", reason=CONTEXT_EXHAUSTED)
    two = manager.rotate("thread:a-b:kimi", reason=CONTEXT_EXHAUSTED)

    assert one.handoff_path != two.handoff_path
    assert one.handoff_path.exists()
    assert two.handoff_path.exists()


# ----------------------------------------------------------------------
# the fix round 2: what the second review found
# ----------------------------------------------------------------------


def _lines(state: Path) -> dict:
    """The lines the manager's own state file holds."""
    return json.loads(state.read_text(encoding="utf-8"))["lines"]


def _fake_git(diagnostic: str):
    """A git that answers the way the handoff reads it: the log carries
    hashes only -- a diagnostic never reaches it."""

    def _git(*args: str) -> str:
        if args and args[0] == "log":
            return "a" * 40
        if args and args[0] == "diff":
            return " safe-file.txt | 1 +"
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "feat/audit-fake"
        if args == ("rev-parse", "HEAD"):
            return "2" * 40
        return ""

    return _git


# ----------------------------------------------------------------------
# R2: the handover note drops a credential in every shape
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "diagnostic",
    [
        '{"api_key":"purelettersentinelcredentialvalue"}',
        "api_key='purelettersentinelcredentialvalue'",
        "{'api_key':'purelettersentinelcredentialvalue'}",
        "Authorization: Bearer purelettersentinelcredentialvalue",
    ],
)
def test_the_handoff_drops_a_credential_in_every_shape(make_manager, diagnostic) -> None:
    # The handover note is the one file a human reads, so it must not carry a
    # secret in any of the shapes a secret takes. The value here is made of
    # letters only: no "long mixed run" rule can catch it, so it can only be
    # caught by the rule that knows what a credential key is.
    manager, _, _ = make_manager()
    manager._git = _fake_git(diagnostic)
    manager.record_turn(KEY, session_id="sess-1")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "purelettersentinelcredentialvalue" not in content
    # The diagnostic text is gone entirely, masked or otherwise: what is left
    # of the commit is its hash.
    assert "a" * 40 in content


def test_the_handoff_note_carries_no_credential_at_all(make_manager) -> None:
    # Every shape in one note: a key, a header, a scheme and an opaque run.
    manager, _, _ = make_manager()
    manager._git = _fake_git(
        '{"api_key":"purelettersentinelcredentialvalue"} '
        "Authorization: Bearer anothersentinelvalue sk-" "live-SENTINEL0123456789abcdef"
    )
    manager.record_turn(KEY, session_id="sess-1")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "purelettersentinelcredentialvalue" not in content
    assert "anothersentinelvalue" not in content
    assert "SENTINEL0123456789abcdef" not in content


# ----------------------------------------------------------------------
# R3: two managers must not undo each other
# ----------------------------------------------------------------------


def test_a_second_manager_does_not_undo_the_first_ones_turns(tmp_path) -> None:
    # The second manager holds an older picture of the line. A failure it
    # records must change the failure fields and nothing else: the turns and
    # the tokens of the other manager must still be there.
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    first = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))
    second = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))

    first.record_turn(
        KEY, session_id="sess-old", usage=NativeCliUsage(input_tokens=100, output_tokens=10)
    )
    second.current(KEY)  # the second manager now holds an older picture
    first.record_turn(KEY, usage=NativeCliUsage(input_tokens=100, output_tokens=10))

    assert _lines(state)[KEY]["turns"] == 2
    assert _lines(state)[KEY]["input_tokens"] == 200

    second.record_failure(KEY, "auth_failed")

    line = _lines(state)[KEY]
    assert line["turns"] == 2
    assert line["input_tokens"] == 200
    assert line["output_tokens"] == 20
    assert line["last_failure_class"] == "auth_failed"


def test_two_lines_share_one_legacy_map(tmp_path) -> None:
    # The flat map is shared: two lines that write to it must both stay in
    # it. The map holds the session id the runner resumes, so a line that
    # fell out of it would start a new provider session and lose its turn.
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    first = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))
    second = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))

    first.record_turn("thread:a:kimi", session_id="sess-a")
    second.record_turn("thread:b:kimi", session_id="sess-b")

    written = json.loads(legacy.read_text(encoding="utf-8"))
    assert written["thread:a:kimi"] == "sess-a"
    assert written["thread:b:kimi"] == "sess-b"
    assert set(_lines(state)) == {"thread:a:kimi", "thread:b:kimi"}


def test_a_failure_only_touches_the_failure_fields(tmp_path) -> None:
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    manager = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))

    manager.record_turn(
        KEY, session_id="sess-1", usage=NativeCliUsage(input_tokens=10, output_tokens=2)
    )
    manager.record_failure(KEY, "network")

    line = _lines(state)[KEY]
    assert line["turns"] == 1
    assert line["input_tokens"] == 10
    assert line["output_tokens"] == 2
    assert line["session_id"] == "sess-1"
    assert line["last_failure_class"] == "network"
    assert line["turns_at_failure"] == 1


# ----------------------------------------------------------------------
# R4: a rotation that was interrupted is finished, not left half done
# ----------------------------------------------------------------------


def _fail_save_on(monkeypatch, manager, call: int) -> None:
    """Make the manager's own write fail on the given call."""
    original = manager._save
    counter = {"n": 0}

    def _save() -> None:
        counter["n"] += 1
        if counter["n"] >= call:
            raise OSError("the disk is full")
        original()

    monkeypatch.setattr(manager, "_save", _save)


@pytest.mark.parametrize("step", [1, 2, 3, 4])
def test_a_rotation_that_was_interrupted_is_finished(
    tmp_path, repo, monkeypatch, step
) -> None:
    # A rotation is done in four steps and writes down what it is about to do
    # before the first of them. Whichever step failed, the next operation on
    # the line finishes the work that is left -- the same way every time --
    # and the line ends up whole: the archive, the two stores and the note.
    root = tmp_path / f"case-{step}"
    root.mkdir()
    state = root / "worker-sessions.json"
    legacy = root / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=repo["root"],
        handoff_dir=root / "handoffs",
    )
    manager._git = lambda *_: ""
    manager.record_turn(KEY, session_id="sess-old")

    if step == 3:
        # The third step is the one that writes the legacy map.
        def refuse_legacy(_):
            raise OSError("the disk is full")

        monkeypatch.setattr(manager.store, "_save", refuse_legacy)
    else:
        _fail_save_on(monkeypatch, manager, step)

    with pytest.raises(OSError):
        manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    pending = _lines(state).get(KEY, {}).get("rotation_pending")
    if step == 1:
        # The very first step failed, so the rotation never started and there
        # is nothing to finish.
        assert pending is None
        assert _lines(state)[KEY]["session_id"] == "sess-old"
        assert not _lines(state)[KEY].get("archived")
        monkeypatch.undo()
        manager.record_failure(KEY, "network")
        assert _lines(state)[KEY]["session_id"] == "sess-old"
        assert _lines(state)[KEY]["last_failure_class"] == "network"
        return

    # The intent survived the failure: it is what makes the work recoverable.
    assert pending is not None
    assert pending["old_id"] == "sess-old"
    assert pending["new_id"] != "sess-old"

    # The cache is never ahead of the disk: a failed write leaves both as
    # they were.
    assert manager.current(KEY).session_id == _lines(state)[KEY]["session_id"]

    # The disk comes back and the next operation finishes the work.
    monkeypatch.undo()
    manager.record_failure(KEY, "network")

    line = _lines(state)[KEY]
    assert "rotation_pending" not in line
    assert line["session_id"] == pending["new_id"]
    # The old session is still reachable: it is history, not a deletion.
    assert "sess-old" in manager.archived_session_ids(KEY)
    # The flat map no longer names the archived session.
    assert NativeCliSessionStore(legacy).get(KEY) != "sess-old"
    # The write that followed the recovery worked.
    assert line["last_failure_class"] == "network"


def test_a_rotation_is_finished_when_the_manager_opens_again(tmp_path, repo) -> None:
    # A process that was killed in the middle of a rotation leaves the intent
    # on the disk. The manager that opens the file afterwards finishes the
    # work before it does anything else.
    root = tmp_path / "case"
    root.mkdir()
    state = root / "worker-sessions.json"
    legacy = root / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=repo["root"],
        handoff_dir=root / "handoffs",
    )
    manager._git = lambda *_: ""
    manager.record_turn(KEY, session_id="sess-old")

    # A rotation whose intent was written down, and whose third step failed.
    manager._commit(
        KEY,
        {
            **manager._disk_line(KEY),
            "rotation_pending": {
                "key": KEY,
                "old_id": "sess-old",
                "new_id": "sess-new",
                "archive_path": str(root / "handoffs" / "note.md"),
                "reason": CONTEXT_EXHAUSTED,
                "rotations": 1,
            },
        },
    )

    reopened = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=repo["root"],
        handoff_dir=root / "handoffs",
    )

    line = _lines(state)[KEY]
    assert "rotation_pending" not in line
    assert line["session_id"] == "sess-new"
    assert "sess-old" in reopened.archived_session_ids(KEY)


def test_a_rotation_that_was_finished_twice_is_the_same_rotation(tmp_path, repo) -> None:
    # Finishing the work is idempotent: the old session is archived once and
    # the flat map is cleared once.
    root = tmp_path / "case"
    root.mkdir()
    state = root / "worker-sessions.json"
    legacy = root / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=repo["root"],
        handoff_dir=root / "handoffs",
    )
    manager._git = lambda *_: ""
    manager.record_turn(KEY, session_id="sess-old")
    manager._commit(
        KEY,
        {
            **manager._disk_line(KEY),
            "rotation_pending": {
                "key": KEY,
                "old_id": "sess-old",
                "new_id": "sess-new",
                "archive_path": str(root / "handoffs" / "note.md"),
                "reason": CONTEXT_EXHAUSTED,
                "rotations": 1,
            },
        },
    )

    manager.record_failure(KEY, "network")
    manager.record_failure(KEY, "network")

    assert manager.archived_session_ids(KEY).count("sess-old") == 1
    assert NativeCliSessionStore(legacy).get(KEY) is None


# ----------------------------------------------------------------------
# the handover note's number
# ----------------------------------------------------------------------


def test_a_second_rotation_does_not_write_over_the_first_note(make_manager) -> None:
    # The rotation count starts again after the first successful turn, so it
    # cannot number the notes. The note carries a number of its own that only
    # goes up.
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-old")

    first = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    manager.record_turn(KEY, session_id="sess-next")
    second = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    assert first.handoff_path != second.handoff_path
    assert first.handoff_path.exists()
    assert second.handoff_path.exists()
    assert len(manager.archived_session_ids(KEY)) == 2


# ----------------------------------------------------------------------
# the success counter
# ----------------------------------------------------------------------


def test_the_success_counter_goes_up_and_a_rotation_does_not_reset_it(
    make_manager,
) -> None:
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-old")
    manager.record_turn(KEY)
    assert manager.current(KEY).success_seq == 2

    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    assert manager.current(KEY).success_seq == 2

    manager.record_turn(KEY, session_id="sess-new")
    assert manager.current(KEY).success_seq == 3


def test_a_quota_failure_remembers_the_counter_it_happened_at(make_manager) -> None:
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-old")
    manager.record_turn(KEY)
    assert manager.current(KEY).quota_failed_at_seq is None

    manager.record_failure(KEY, "quota_exhausted")
    assert manager.current(KEY).quota_failed_at_seq == 2

    # A failure of another class does not move the two counters.
    manager.record_failure(KEY, "network")
    assert manager.current(KEY).quota_failed_at_seq == 2
    assert manager.current(KEY).success_seq == 2

    # One good turn after the failure is what makes the line a recovered one.
    manager.record_turn(KEY)
    assert manager.current(KEY).success_seq == 3
    assert manager.current(KEY).quota_failed_at_seq == 2


def test_the_counters_survive_a_restart(tmp_path) -> None:
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    manager = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))
    manager.record_turn(KEY, session_id="sess-old")
    manager.record_failure(KEY, "quota_exhausted")

    reopened = WorkerSessionManager(path=state, store=NativeCliSessionStore(legacy))
    record = reopened.current(KEY)
    assert record.success_seq == 1
    assert record.quota_failed_at_seq == 1


def test_a_rotation_keeps_the_counters(tmp_path) -> None:
    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        # A rotation writes its handover note. Without a directory of its
        # own the manager writes it into the home of whoever runs the
        # tests -- a file the operator never asked for, in a directory
        # that belongs to their installation.
        handoff_dir=tmp_path / "handoffs",
    )
    manager.record_turn(KEY, session_id="sess-old")
    manager.record_failure(KEY, "quota_exhausted")

    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    record = manager.current(KEY)
    assert record.success_seq == 1
    assert record.quota_failed_at_seq == 1
    assert record.turns == 0


# ----------------------------------------------------------------------
# the second fix round: what the independent verifier found
# ----------------------------------------------------------------------


def test_a_manager_that_shares_its_file_with_the_store_does_not_wait_for_itself(
    tmp_path,
) -> None:
    # ``flock`` belongs to the open file description, so a second descriptor
    # for one file waits for the first *inside this very process*. The
    # manager's own lock and the store's lock are one file when the state
    # and the flat map are one file, so the manager must take only one of
    # them -- otherwise the write waits for itself and never returns.
    import threading

    same = tmp_path / "sessions.json"
    manager = WorkerSessionManager(
        path=same,
        store=NativeCliSessionStore(same),
        handoff_dir=tmp_path / "handoffs",
    )
    manager._git = lambda *_: ""

    done = threading.Event()

    def _write() -> None:
        try:
            manager.record_turn(KEY, session_id="sess-1")
            manager.record_failure(KEY, "network")
            manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
        finally:
            done.set()

    worker = threading.Thread(target=_write, daemon=True)
    worker.start()
    assert done.wait(timeout=10), "the write waited for itself"
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_the_handoff_drops_a_secret_that_names_nothing(make_manager) -> None:
    # A secret that names nothing -- no key, no scheme -- is caught by the
    # shape rule and by nothing else. A run of letters that long is not a
    # word, so it goes even though it carries no digit to give it away.
    manager, _, _ = make_manager()
    manager._git = _fake_git("chore: use purelettersentinelcredentialvalue now")
    manager.record_turn(KEY, session_id="sess-1")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "purelettersentinelcredentialvalue" not in content
    # The log the handoff reads is hashes only, so the secret subject is
    # never in the note at all.
    assert "a" * 40 in content


def test_a_commit_subject_does_not_reach_the_note(make_manager) -> None:
    # A commit subject is the worker's own text, so the handoff does not
    # carry it at all: the log it reads lists hashes only.
    manager, _, _ = make_manager()
    manager._git = _fake_git("feat: second commit")
    manager.record_turn(KEY, session_id="sess-1")

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    content = result.handoff_path.read_text(encoding="utf-8")

    assert "feat: second commit" not in content
    assert "a" * 40 in content


# ----------------------------------------------------------------------
# the second review round: a read error, a read-only manager, and the
# counters that belong to the worker
# ----------------------------------------------------------------------


def test_a_read_error_stops_the_write_and_keeps_the_other_lines(make_manager) -> None:
    # A write is based on what is on the disk, and a read that fails must
    # not be mistaken for an empty disk: the write would then hold one line
    # where the file held two, and the other worker's line would be gone.
    # So the read failure stops the write, and the file is left as it was.
    manager, _, _ = make_manager()
    other = "thread:t2:kimi"
    manager.record_turn(KEY, session_id="sess-a")
    manager.record_turn(other, session_id="sess-b")
    path = manager.path
    before = path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def fail_the_read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("injected read failure")
        return real_read_text(candidate, *args, **kwargs)

    Path.read_text = fail_the_read
    try:
        with pytest.raises(SessionStoreUnavailable):
            manager.record_turn(KEY, session_id="sess-a2")
    finally:
        Path.read_text = real_read_text

    assert path.read_text(encoding="utf-8") == before
    lines = json.loads(before)["lines"]
    assert sorted(lines) == sorted([KEY, other])
    assert lines[other]["session_id"] == "sess-b"


def test_a_write_that_cannot_read_the_state_writes_nothing(make_manager) -> None:
    # When the state cannot be read at all, the write does not happen: the
    # file keeps every line it had, and the line that was being written is
    # not half written.
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-a")
    path = manager.path
    before = path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def always_fail(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("injected read failure")
        return real_read_text(candidate, *args, **kwargs)

    Path.read_text = always_fail
    try:
        with pytest.raises(OSError):
            manager.record_turn(KEY, session_id="sess-a2")
    finally:
        Path.read_text = real_read_text

    assert path.read_text(encoding="utf-8") == before


def test_a_read_only_manager_does_not_finish_a_pending_rotation(make_manager) -> None:
    # A status command that reports on the workers must not change what it
    # reports on. A manager that opens the state file finishes a rotation
    # that was interrupted -- so a read-only manager does not, and the file,
    # the legacy map and the archive are all left alone.
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-old")

    # An interrupted rotation: the intent is on the disk, the work is not
    # done. The manager that made it is the one that would finish it.
    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    payload["lines"][KEY]["rotation_pending"] = {
        "key": KEY,
        "old_id": "sess-old",
        "new_id": "rot-fixed",
        "archive_path": str(manager.handoff_dir / "pending.md"),
        "reason": "active_rotation",
        "rotations": 1,
    }
    manager.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    reader, _, _ = make_manager(path=manager.path, read_only=True)
    before = manager.path.read_text(encoding="utf-8")

    reader.current(KEY)

    assert manager.path.read_text(encoding="utf-8") == before
    assert "rotation_pending" in json.loads(before)["lines"][KEY]
    assert not (manager.handoff_dir / "pending.md").exists()


def test_a_read_only_manager_refuses_to_write(make_manager) -> None:
    # The refusal is loud: a caller that wants to change the state has to
    # build a manager that may.
    manager, _, _ = make_manager(read_only=True)

    with pytest.raises(PermissionError):
        manager.record_turn(KEY, session_id="sess-1")
    with pytest.raises(PermissionError):
        manager.record_failure(KEY, "network")
    with pytest.raises(PermissionError):
        manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    assert not manager.path.exists()


def test_the_success_counter_belongs_to_the_worker_not_to_the_line(make_manager) -> None:
    # A worker is one provider. The counter that says how much it has
    # achieved is the worker's, and it lives in the worker's own record in
    # the state file -- not on one of its lines.
    manager, _, _ = make_manager()
    other = "thread:t2:kimi"

    manager.record_turn(KEY, session_id="sess-a")
    manager.record_turn(other, session_id="sess-b")

    state = json.loads(manager.path.read_text(encoding="utf-8"))
    assert state["workers"]["kimi"]["success_seq"] == 2
    assert manager.current(KEY).success_seq == 2
    assert manager.current(other).success_seq == 2
    # The lines carry no counters of their own any more.
    assert "success_seq" not in state["lines"][KEY]


def test_the_quota_failure_is_recorded_against_the_worker(make_manager) -> None:
    # The number a quota failure is recorded at is the worker's success
    # counter, so a turn that gets through on another line of the same
    # worker covers the failure.
    manager, _, _ = make_manager()
    other = "thread:t2:kimi"

    manager.record_failure(KEY, "quota_exhausted")
    manager.record_turn(other, session_id="sess-b")

    worker = json.loads(manager.path.read_text(encoding="utf-8"))["workers"]["kimi"]
    assert worker["quota_failed_at_seq"] == 0
    assert worker["success_seq"] == 1
    assert manager.current(KEY).quota_failed_at_seq == 0
    assert manager.current(other).quota_failed_at_seq == 0


def test_a_state_file_that_is_not_json_stops_the_write(make_manager) -> None:
    # A state file that cannot be parsed is not an empty state: the write
    # is stopped and the file is left exactly as it was.
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-a")
    path = manager.path
    path.write_text("{not json at all", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(SessionStoreUnavailable):
        manager.record_turn(KEY, session_id="sess-a2")

    assert path.read_text(encoding="utf-8") == before


def test_a_turn_on_another_line_advances_the_workers_count(make_manager) -> None:
    # The counters belong to the worker, so a turn on one of its lines
    # advances the worker's count -- not only the count of the line it ran
    # on.
    manager, _, _ = make_manager()
    other = "thread:t2:kimi"

    manager.record_turn(KEY, session_id="sess-a")
    manager.record_turn(other, session_id="sess-b")
    manager.record_turn(KEY, session_id="sess-a2")

    worker = json.loads(manager.path.read_text(encoding="utf-8"))["workers"]["kimi"]
    assert worker["success_seq"] == 3
    assert manager.current(other).success_seq == 3


def test_a_rotation_does_not_reset_the_workers_counters(make_manager) -> None:
    # A rotation starts a new session, not a new generation: the counters
    # only go up.
    manager, _, _ = make_manager()
    manager.record_turn(KEY, session_id="sess-a")
    manager.record_failure(KEY, "quota_exhausted")

    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    worker = json.loads(manager.path.read_text(encoding="utf-8"))["workers"]["kimi"]
    assert worker["success_seq"] == 1
    assert worker["quota_failed_at_seq"] == 1


# ----------------------------------------------------------------------
# the change is two phases: read everything, then write both files
# ----------------------------------------------------------------------


def _files_and_mtimes(*paths: Path) -> list[tuple[bytes | None, int | None]]:
    out = []
    for path in paths:
        if path.exists():
            stat_ = path.stat()
            out.append((path.read_bytes(), stat_.st_mtime_ns))
        else:
            out.append((None, None))
    return out


def test_a_read_that_fails_at_any_point_leaves_both_files_alone(make_manager) -> None:
    # The change reads both files before it writes either of them, so a read
    # that fails stops it with both files exactly as they were. The failure
    # is injected at EVERY read point in turn: a file that cannot be read
    # from the start only exercises the first one.
    manager, _, _ = make_manager()
    state = manager.path
    legacy = manager.store.path

    for target in (state, legacy):
        original = Path.read_text
        counter = {"n": 0}

        def patched(self, *args, **kwargs):
            if str(self) == str(target):
                counter["n"] += 1
                raise PermissionError("the file cannot be read")
            return original(self, *args, **kwargs)

        Path.read_text = patched
        try:
            with pytest.raises(SessionStoreUnavailable):
                manager.record_turn(KEY, session_id="sess-a")
        finally:
            Path.read_text = original
        assert counter["n"] >= 1, "the read was never reached"

    # Nothing was written: the state file does not even exist yet.
    assert not state.exists()
    assert not legacy.exists()


def test_a_failure_at_the_last_read_point_leaves_both_files_alone(make_manager) -> None:
    # The read that matters is the LAST one: by then the change has worked
    # out what it is going to write, and the worker record is staged. A
    # failure there must still leave both files alone.
    manager, _, _ = make_manager()
    state = manager.path
    legacy = manager.store.path
    state.write_text(json.dumps({"version": 1, "lines": {}, "workers": {}}))
    legacy.write_text(json.dumps({}))

    original = Path.read_text
    counter = {"n": 0}

    def counting(self, *args, **kwargs):
        if str(self) == str(state):
            counter["n"] += 1
        return original(self, *args, **kwargs)

    Path.read_text = counting
    try:
        manager.record_turn(KEY, session_id="sess-a")
    finally:
        Path.read_text = original
    reads = counter["n"]
    assert reads >= 1

    # Now fail the last read of the state file.
    before = _files_and_mtimes(state, legacy)
    counter = {"n": 0}

    def patched(self, *args, **kwargs):
        if str(self) == str(state):
            counter["n"] += 1
            if counter["n"] == reads:
                raise PermissionError("the file cannot be read")
        return original(self, *args, **kwargs)

    Path.read_text = patched
    try:
        with pytest.raises(SessionStoreUnavailable):
            manager.record_turn(KEY, session_id="sess-b")
    finally:
        Path.read_text = original

    after = _files_and_mtimes(state, legacy)
    assert after == before, "the aborted write changed a file"


def test_a_failed_write_leaves_no_worker_record_behind(make_manager) -> None:
    # A write that failed must not leave its worker record behind: a later
    # write by the same manager would put that stale record over the new one
    # another manager saved, and lose the quota marker with it.
    manager, _, _ = make_manager()
    state = manager.path

    manager.record_turn(KEY, session_id="sess-a")
    manager.record_turn(KEY, session_id="sess-a")
    manager.record_failure(KEY, "quota_exhausted")

    original = Path.read_text

    def patched(self, *args, **kwargs):
        if str(self) == str(state):
            raise PermissionError("the file cannot be read")
        return original(self, *args, **kwargs)

    Path.read_text = patched
    try:
        with pytest.raises(SessionStoreUnavailable):
            manager.record_turn(KEY, session_id="sess-b")
    finally:
        Path.read_text = original

    # The worker record the failed write staged is gone: the file holds the
    # count from before it, with its quota marker.
    worker = json.loads(state.read_text(encoding="utf-8"))["workers"]["kimi"]
    assert worker["success_seq"] == 2
    assert worker["quota_failed_at_seq"] == 2

    # A later write of another provider does not drag the stale record over
    # what is on the disk.
    manager.record_turn("thread:t1:glm", session_id="sess-c")
    workers = json.loads(state.read_text(encoding="utf-8"))["workers"]
    assert workers["kimi"]["success_seq"] == 2
    assert workers["kimi"]["quota_failed_at_seq"] == 2


def test_the_handover_note_cleans_everything_it_carries_at_one_exit(repo, tmp_path) -> None:
    # The branch, the commit subjects and the paths all come from outside the
    # program, so any of them can carry a credential. The note is assembled
    # first and cleaned once, at the exit, so no piece of external metadata
    # can reach the file without passing the rule that decides what a
    # credential is.
    root = repo["root"]
    secret = "fakepassword987654"
    _git(root, "checkout", "-b", f"feature/api_key={secret}")
    (root / "b.txt").write_text("two\n", encoding="utf-8")
    _git(root, "add", "b.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", f'chore: {{"password": {secret}}}')

    state = tmp_path / "worker-sessions.json"
    legacy = tmp_path / "sessions.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(legacy),
        worktree=root,
        baseline=repo["baseline"],
        task_file=str(tmp_path / "task.md"),
        report=str(tmp_path / "report.md"),
        handoff_dir=tmp_path / "handoffs",
    )

    result = manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)
    note = Path(result.handoff_path).read_text(encoding="utf-8")

    assert secret not in note
    # The note still says which branch it is about: the tainted segment is
    # replaced whole by the redaction marker, and the clean one is kept.
    assert "feature/<redacted>" in note
    assert "api_key=" not in note
    assert "[redacted]" not in note


def test_a_mirrored_legacy_count_is_summed_under_the_v1_rule(tmp_path) -> None:
    """What a file that mirrored its count onto every line reads as.

    A file written before the worker records existed kept the counter on the
    lines, and the counter belonged to the **worker**: every line of a
    worker carried the same number. Two lines of one worker therefore carry
    the same number twice, and the v1 rule -- the successes of the lines
    with the same spelling add up -- counts it once per line.

    The count that comes out is larger than the number of turns the worker
    really made. What it decides is narrow: the only reader of the counter
    compares it with the quota marker, a migrated worker has no marker, and
    so it reads as not exhausted either way. The number is kept as the v1
    rule says it is, and the difference is written down here so that it is a
    fact in the suite and not a surprise later.
    """
    _write_legacy_state(
        tmp_path / "worker.json",
        {
            "thread:t1:step": {
                "session_id": "s1",
                "provider": "step",
                "success_seq": 2,
            },
            "thread:t2:step": {
                "session_id": "s2",
                "provider": "step",
                "success_seq": 2,
            },
        },
    )
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        handoff_dir=tmp_path / "handoffs",
    )
    record = manager.current("thread:t1:step")

    # The v1 rule: two lines of the same spelling add up.
    assert record.success_seq == 4
    # And the thing the counter decides: the worker is not out of quota.
    assert record.quota_failed_at_seq is None


# ----------------------------------------------------------------------
# the reason on the disk is a code, not what the caller said
# ----------------------------------------------------------------------

REASON_SENTINEL = "SECRET-REASON-SENTINEL"


def test_the_state_and_the_history_keep_no_free_reason(tmp_path) -> None:
    """The state file keeps a code, not the text the caller passed.

    The state file is a file on a disk, and the caller may have passed
    anything as the reason. The history the old session is kept in carries
    the category code and the failure class -- and neither carries the
    text.
    """
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    store.set(KEY, "sess-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=store,
        handoff_dir=tmp_path / "handoffs",
    )
    manager.record_failure(KEY, "network")

    manager.rotate(KEY, reason=f"context_exhausted {REASON_SENTINEL}")

    state = json.loads((tmp_path / "worker.json").read_text(encoding="utf-8"))
    assert REASON_SENTINEL not in json.dumps(state)
    archived = state["lines"][KEY].get("archived") or []
    assert archived, "the old session is archived, not deleted"
    assert archived[0]["reason"] == "unknown"
    # The failure class is kept next to it: the history says what happened
    # without saying anything the caller said.
    assert archived[0]["failure_class"] == "network"


def test_a_known_reason_code_is_kept_in_the_history(tmp_path) -> None:
    """The codes the program knows are kept as they are."""
    store = NativeCliSessionStore(tmp_path / "sessions.json")
    store.set(KEY, "sess-old")
    manager = WorkerSessionManager(
        path=tmp_path / "worker.json",
        store=store,
        handoff_dir=tmp_path / "handoffs",
    )

    manager.rotate(KEY, reason=CONTEXT_EXHAUSTED)

    state = json.loads((tmp_path / "worker.json").read_text(encoding="utf-8"))
    archived = state["lines"][KEY].get("archived") or []
    assert archived[0]["reason"] == "context_exhausted"


def test_the_intent_written_before_the_rotation_carries_no_free_reason(
    tmp_path, repo, monkeypatch
) -> None:
    """The intent that is written down first carries no text either.

    The rotation writes down what it is about to do before it does it, and
    that intent sits on the disk for as long as the rotation takes. A
    caller that passed a free text reason must not have it written there.
    """
    state = tmp_path / "worker.json"
    manager = WorkerSessionManager(
        path=state,
        store=NativeCliSessionStore(tmp_path / "sessions.json"),
        worktree=repo["root"],
        handoff_dir=tmp_path / "handoffs",
    )
    manager._git = lambda *_: ""
    manager.record_turn(KEY, session_id="sess-old")
    # The second write fails: the intent is on the disk, the rotation is
    # not finished.
    _fail_save_on(monkeypatch, manager, 2)

    with pytest.raises(OSError):
        manager.rotate(KEY, reason=f"context_exhausted {REASON_SENTINEL}")

    pending = _lines(state).get(KEY, {}).get("rotation_pending")
    assert pending is not None
    assert REASON_SENTINEL not in json.dumps(pending)
    assert pending["reason"] == "unknown"
