"""The new-commit verdict the completion contract reconciles against."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.server.services.autonomy.git_delivery import (
    GitSnapshot,
    capture_git_snapshot,
    safe_git_commit_delivery,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.invalid")
    if commit:
        (path / "a.txt").write_text("a\n", encoding="utf-8")
        _git(path, "add", "a.txt")
        _git(path, "commit", "-qm", "base")
    return path


def _commit(path: Path, name: str = "b.txt") -> None:
    (path / name).write_text("b\n", encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-qm", "work")


def test_snapshot_records_head_and_round_trips(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    snapshot = capture_git_snapshot(str(repo))

    assert snapshot.head == _git(repo, "rev-parse", "HEAD")
    assert snapshot.head_known is True
    restored = GitSnapshot.from_record(snapshot.to_record())
    assert restored is not None
    assert restored.head == snapshot.head
    assert restored.head_known is True


def test_a_new_commit_is_delivered(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    baseline = capture_git_snapshot(str(repo))
    _commit(repo)

    assert safe_git_commit_delivery(str(repo), baseline) == "delivered"


def test_an_unmoved_head_is_missing_even_when_clean(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    baseline = capture_git_snapshot(str(repo))

    assert safe_git_commit_delivery(str(repo), baseline) == "missing"


def test_a_reset_behind_the_baseline_is_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _commit(repo)
    baseline = capture_git_snapshot(str(repo))
    _git(repo, "reset", "-q", "--hard", "HEAD~1")

    assert safe_git_commit_delivery(str(repo), baseline) == "missing"


def test_a_first_commit_on_an_unborn_branch_is_delivered(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", commit=False)
    baseline = capture_git_snapshot(str(repo))
    assert baseline.head is None and baseline.head_known is True

    assert safe_git_commit_delivery(str(repo), baseline) == "missing"
    _commit(repo)
    assert safe_git_commit_delivery(str(repo), baseline) == "delivered"


def test_unchecked_baselines_neither_confirm_nor_refute(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    legacy = capture_git_snapshot(str(repo)).to_record()
    legacy.pop("head")
    legacy.pop("head_known")
    restored = GitSnapshot.from_record(legacy)
    assert restored is not None and restored.head_checked is False

    assert safe_git_commit_delivery(str(repo), None) == "not_checked"
    assert safe_git_commit_delivery(str(repo), restored) == "not_checked"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert safe_git_commit_delivery(str(plain), capture_git_snapshot(str(plain))) == (
        "not_checked"
    )


def test_an_unavailable_baseline_or_lost_repository_is_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    baseline = capture_git_snapshot(str(repo))

    assert safe_git_commit_delivery(str(repo), None, baseline_unavailable=True) == (
        "unknown"
    )
    other = _repo(tmp_path / "other")
    assert safe_git_commit_delivery(str(other), baseline) == "unknown"


def test_a_malformed_head_record_is_rejected() -> None:
    record = {
        "version": 1,
        "root_id": None,
        "gitdir_id": None,
        "entries": {},
        "head": "not-an-object-id",
    }
    assert GitSnapshot.from_record(record) is None


def test_an_unreadable_baseline_head_is_unknown_not_unchecked(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    record = capture_git_snapshot(str(repo)).to_record()
    record["head"] = None
    record["head_known"] = False
    restored = GitSnapshot.from_record(record)
    assert restored is not None
    assert restored.head_checked is True and restored.head_known is False

    assert safe_git_commit_delivery(str(repo), restored) == "unknown"


def test_a_head_without_head_known_is_rejected() -> None:
    record = {
        "version": 1,
        "root_id": None,
        "gitdir_id": None,
        "entries": {},
        "head": "a" * 40,
        "head_known": False,
    }
    assert GitSnapshot.from_record(record) is None


def test_the_origin_is_the_first_baseline_and_commits_since_it_count(tmp_path: Path) -> None:
    from src.server.services.autonomy.git_delivery import (
        delivery_origin_record,
        with_delivery_origin,
    )

    repo = _repo(tmp_path / "repo")
    first = capture_git_snapshot(str(repo))
    origin = delivery_origin_record(first, prior_run=False)
    assert origin == {
        "version": 1,
        "known": True,
        "head": first.head,
        "root_id": first.root_id,
    }
    _commit(repo)  # run 1 commits, then reports partial
    second = capture_git_snapshot(str(repo))  # run 2 re-baselines after it

    assert safe_git_commit_delivery(str(repo), second) == "missing"
    assert safe_git_commit_delivery(str(repo), with_delivery_origin(second, origin)) == (
        "delivered"
    )


def test_an_unknown_origin_makes_the_commit_check_unverified(tmp_path: Path) -> None:
    from src.server.services.autonomy.git_delivery import (
        delivery_origin_record,
        with_delivery_origin,
    )

    repo = _repo(tmp_path / "repo")
    late = capture_git_snapshot(str(repo))
    origin = delivery_origin_record(late, prior_run=True)
    assert origin == {"version": 1, "known": False}

    assert safe_git_commit_delivery(str(repo), with_delivery_origin(late, origin)) == (
        "unverified"
    )
    assert with_delivery_origin(late, None) is late


def test_an_origin_from_another_checkout_is_unverified(tmp_path: Path) -> None:
    from src.server.services.autonomy.git_delivery import (
        delivery_origin_record,
        with_delivery_origin,
    )

    first = _repo(tmp_path / "first")
    origin = delivery_origin_record(capture_git_snapshot(str(first)), prior_run=False)
    other = _repo(tmp_path / "other")
    baseline = capture_git_snapshot(str(other))

    assert safe_git_commit_delivery(
        str(other), with_delivery_origin(baseline, origin)
    ) == "unverified"


def test_a_first_run_without_a_baseline_records_an_unknown_origin() -> None:
    from src.server.services.autonomy.git_delivery import delivery_origin_record

    assert delivery_origin_record(None, prior_run=False) == {"version": 1, "known": False}


def test_a_pending_placeholder_alone_is_not_an_earlier_run() -> None:
    from src.server.services.autonomy.worker import mark_unknown_delivery_origin

    placeholder = {"version": 2, "snapshot": {"version": 1, "error": True}, "pending": True}
    mandate: dict = {"git_delivery_baseline": placeholder}
    mark_unknown_delivery_origin(mandate, placeholder)
    assert "git_delivery_origin" not in mandate

    earlier = {"version": 2, "snapshot": {"version": 1}, "pending": False}
    mark_unknown_delivery_origin(mandate, earlier)
    assert mandate["git_delivery_origin"] == {"version": 1, "known": False}

    known = {"version": 1, "known": True, "head": "a" * 40}
    mandate = {"git_delivery_origin": known}
    mark_unknown_delivery_origin(mandate, earlier)
    assert mandate["git_delivery_origin"] is known
