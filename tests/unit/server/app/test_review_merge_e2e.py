"""End-to-end merge through the HTTP API against real git worktrees.

These tests create a throwaway repository with real commits and worktrees (no
network), drive the review queue and the merge endpoint, and assert on the
repository state the API leaves behind: the rehearsal worktree, the mandatory
Codex review, the real merge commit, and the untouched target on every failure
path. The Codex reviewer itself is scripted here — it is the only network
dependency of a real merge, and the service tests cover its command handling.
"""

from __future__ import annotations

import shlex
import sys
from types import SimpleNamespace

import pytest

from tests.unit.server.review_merge_git import (
    branch,
    commit_file,
    git,
    has_merge_head,
    head,
    parents,
    worktree_count,
)

pytestmark = pytest.mark.asyncio

REVIEWS = "/api/v1/reviews"
MERGES = "/api/v1/merges"

_CHECK_SCRIPT = """\
import subprocess
import sys
from pathlib import Path

expected = sys.argv[1:]
missing = [name for name in expected if not Path(name).exists()]
if missing:
    print(f"missing after merge: {missing}", file=sys.stderr)
    sys.exit(2)
parents = subprocess.run(
    ["git", "rev-list", "--parents", "-n", "1", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.split()[1:]
if len(parents) != 2:
    print(f"HEAD is not a merge commit: {parents}", file=sys.stderr)
    sys.exit(3)
if Path("base.txt").read_text() != "line change\\n":
    print("merged tree does not carry the line change", file=sys.stderr)
    sys.exit(4)
print("rehearsal green")
"""


@pytest.fixture
def codex_merge(monkeypatch):
    """Script the mandatory Codex review so the tests stay off the network."""
    from src.server.services.autonomy import review_merge as service

    calls: list[dict] = []
    state = {"verdict": "merge"}

    def fake_review(**kwargs):
        calls.append(kwargs)
        if state["verdict"] == "raise":
            raise service.ReviewMergeError(
                "codex_failed",
                "The Codex pre-merge review exited with status 2.",
                remediation="Fix the Codex invocation and retry.",
                status_code=409,
                kind="failed",
                detail={"exit_code": 2, "output_tail": "not logged in"},
            )
        if state["verdict"] == "block":
            raise service.ReviewMergeError(
                "codex_blocked",
                "The Codex pre-merge review returned BLOCK; the target branch was not touched.",
                remediation="Address the findings and retry.",
                status_code=409,
                kind="refused",
                detail={"output_tail": "finding: x"},
            )
        return state["verdict"]

    monkeypatch.setattr(service, "_run_codex_merge_review", fake_review)
    return SimpleNamespace(calls=calls, state=state)


async def _reviewed_line(client, fleet) -> dict:
    """Queue a review, land a Codex MERGE verdict on the current line head."""
    created = await client.post(
        REVIEWS,
        json={
            "line": "line",
            "reviewer": "repo",
            "provider": "codex",
            "root": str(fleet.root),
        },
    )
    assert created.status_code == 201, created.text
    verdict = await client.post(
        f"{REVIEWS}/{created.json()['id']}/verdict",
        json={"verdict": "merge", "note": "end-to-end", "root": str(fleet.root)},
    )
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["head_sha"] == head(fleet.line)
    return verdict.json()


def _check_command(*expected: str) -> str:
    return " ".join(
        [shlex.quote(sys.executable), "merge_check.py", *map(shlex.quote, expected)]
    )


async def test_merge_rehearses_in_a_worktree_then_lands_a_real_merge(
    client, fleet, review_store, codex_merge
):
    # The line and the target each add a file after the branch point, so the
    # rehearsal only passes if it really tested the merge of both sides.
    commit_file(fleet.target, "target_only.txt", "target\n", "target work")
    commit_file(fleet.line, "merge_check.py", _CHECK_SCRIPT, "add merge check")
    commit_file(fleet.line, "line_only.txt", "line\n", "line work")
    commit_file(fleet.line, "base.txt", "line change\n", "change base")
    request = await _reviewed_line(client, fleet)
    target_base = head(fleet.target)
    worktrees_before = worktree_count(fleet.repo)

    response = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": _check_command("base.txt", "line_only.txt", "target_only.txt"),
            "root": str(fleet.root),
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merged"] is True
    assert body["line"] == "line"
    assert body["branch"] == "feat/line"
    assert body["into"] == "integrate/r2"
    assert body["tests"] == "green"
    assert body["pushed"] is False
    assert body["tested_in_temporary_worktree"] is True
    assert body["codex_verdict"] == "merge"
    assert body["tested_sha"] == request["head_sha"]
    assert body["landed_ref"] == "refs/heads/integrate/r2"
    assert body["worktree_sync_required"] is True

    # The ref landed the exact rehearsed merge commit; the server deliberately
    # does not rewrite the target worktree, so the caller syncs it.
    assert body["commit"] == head(fleet.target)
    assert git(fleet.target, "rev-parse", "refs/heads/integrate/r2") == body["commit"]
    assert branch(fleet.target) == "integrate/r2"
    assert len(parents(fleet.target)) == 2
    assert not (fleet.target / "line_only.txt").exists()
    git(fleet.target, "reset", "--hard")
    assert (fleet.target / "line_only.txt").is_file()
    assert (fleet.target / "target_only.txt").is_file()
    assert (fleet.target / "base.txt").read_text(encoding="utf-8") == "line change\n"
    assert git(fleet.target, "status", "--porcelain") == ""
    assert not has_merge_head(fleet.target)

    # The rehearsal worktree is gone: no leaked /tmp registration or checkout.
    assert worktree_count(fleet.repo) == worktrees_before

    # The Codex review ran against the rehearsal merge, not the final commit.
    assert len(codex_merge.calls) == 1
    assert codex_merge.calls[0]["line"] == "line"
    assert codex_merge.calls[0]["into"] == "integrate/r2"
    assert codex_merge.calls[0]["target_head"] == target_base
    assert codex_merge.calls[0]["target_head"] != body["commit"]

    # The durable audit has an intent row before the result row.
    actions = [row["action"] for row in review_store.audit]
    assert actions.index("merge_started") < actions.index("merge_merged")
    started = next(
        row for row in review_store.audit if row["action"] == "merge_started"
    )
    assert started["detail"]["tested_sha"] == body["tested_sha"]
    assert started["detail"]["merge_commit"] == body["commit"]
    assert started["detail"]["codex_verdict"] == "merge"
    merged = [row for row in review_store.audit if row["action"] == "merge_merged"]
    assert len(merged) == 1
    assert merged[0]["outcome"] == "ok"
    assert merged[0]["target_branch"] == "integrate/r2"
    assert merged[0]["detail"]["commit"] == body["commit"]
    assert merged[0]["detail"]["landed_ref"] == "refs/heads/integrate/r2"
    assert merged[0]["detail"]["worktree_sync_required"] is True
    assert merged[0]["detail"]["tested_in_temporary_worktree"] is True


async def test_red_tests_refuse_the_merge_and_leave_the_target_untouched(
    client, fleet, review_store, codex_merge
):
    commit_file(
        fleet.line,
        "merge_check.py",
        "import sys\nprint('red on purpose', file=sys.stderr)\nsys.exit(9)\n",
        "add a red check",
    )
    await _reviewed_line(client, fleet)
    target_before = head(fleet.target)

    response = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": _check_command(),
            "root": str(fleet.root),
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "tests_failed"
    assert "not touched" in detail["message"]
    assert "re-record" in detail["remediation"] or "review" in detail["remediation"]
    assert detail["detail"]["output_tail"]
    # The test output tail never carries the whole environment, and the target
    # branch is exactly where it was before the attempt.
    assert head(fleet.target) == target_before
    assert not (fleet.target / "merge_check.py").exists()
    assert git(fleet.target, "status", "--porcelain") == ""
    assert worktree_count(fleet.repo) == 3
    # A red rehearsal never reaches the Codex review.
    assert codex_merge.calls == []

    failed = [row for row in review_store.audit if row["action"] == "merge_failed"]
    assert len(failed) == 1
    assert failed[0]["detail"]["code"] == "tests_failed"
    assert "output_tail" not in failed[0]["detail"]["detail"]


async def test_a_conflict_returns_a_structured_error_and_no_merge(
    client, fleet, review_store, codex_merge
):
    commit_file(fleet.target, "conflict.txt", "target side\n", "target conflict edit")
    commit_file(fleet.line, "conflict.txt", "line side\n", "line conflict edit")
    await _reviewed_line(client, fleet)
    target_before = head(fleet.target)

    response = await client.post(
        MERGES,
        json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "merge_conflict"
    assert detail["detail"]["conflicts"] == ["conflict.txt"]
    assert detail["detail"]["target_branch_untouched"] is True
    assert "conflict" in detail["remediation"]
    assert head(fleet.target) == target_before
    assert branch(fleet.target) == "integrate/r2"
    assert git(fleet.target, "status", "--porcelain") == ""
    assert worktree_count(fleet.repo) == 3
    assert codex_merge.calls == []

    failed = [row for row in review_store.audit if row["action"] == "merge_failed"]
    assert len(failed) == 1
    assert failed[0]["detail"]["code"] == "merge_conflict"


async def test_codex_block_refuses_the_merge(client, fleet, review_store, codex_merge):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    await _reviewed_line(client, fleet)
    target_before = head(fleet.target)
    codex_merge.state["verdict"] = "block"

    response = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": "true",
            "root": str(fleet.root),
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "codex_blocked"
    assert "not touched" in detail["message"]
    assert head(fleet.target) == target_before
    assert worktree_count(fleet.repo) == 3
    refused = [row for row in review_store.audit if row["action"] == "merge_refused"]
    assert refused and refused[-1]["detail"]["code"] == "codex_blocked"
    assert refused[-1]["detail"]["detail"] == {}


async def test_codex_failure_refuses_the_merge(
    client, fleet, review_store, codex_merge
):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    await _reviewed_line(client, fleet)
    target_before = head(fleet.target)
    codex_merge.state["verdict"] = "raise"

    response = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": "true",
            "root": str(fleet.root),
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "codex_failed"
    assert head(fleet.target) == target_before
    failed = [row for row in review_store.audit if row["action"] == "merge_failed"]
    assert failed and failed[-1]["detail"]["code"] == "codex_failed"
    assert failed[-1]["detail"]["detail"] == {"exit_code": 2}


async def test_already_merged_line_is_refused_without_a_second_commit(
    client, fleet, review_store, codex_merge
):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    await _reviewed_line(client, fleet)
    first = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": "true",
            "root": str(fleet.root),
        },
    )
    assert first.status_code == 200, first.text
    merged_head = head(fleet.target)
    git(fleet.target, "reset", "--hard")

    second = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": "true",
            "root": str(fleet.root),
        },
    )

    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_merged"
    assert head(fleet.target) == merged_head
    assert len(parents(fleet.target)) == 2


async def test_the_test_command_runs_in_the_rehearsal_worktree(
    client, fleet, codex_merge
):
    """Tests execute against the rehearsal worktree, never the line worktree."""
    isolation_script = """\
import sys
from pathlib import Path

cwd = Path.cwd()
if not cwd.name.startswith("conductor-merge-"):
    print(f"tests ran outside the rehearsal worktree: {cwd}", file=sys.stderr)
    sys.exit(7)
# The rehearsal tree and its src/ must lead sys.path so editable installs of
# the project cannot shadow the merged sources with the line's checkout.
head = [str(Path(entry).resolve()) if entry else str(cwd.resolve()) for entry in sys.path[:5]]
if str(cwd.resolve()) not in head or str((cwd / "src").resolve()) not in head:
    print(f"rehearsal sources are not early on sys.path: {sys.path[:6]!r}", file=sys.stderr)
    sys.exit(8)
print(cwd)
"""
    commit_file(
        fleet.line, "isolation_check.py", isolation_script, "add isolation check"
    )
    line_head = head(fleet.line)
    await _reviewed_line(client, fleet)

    response = await client.post(
        MERGES,
        json={
            "line": "line",
            "into": "integrate/r2",
            "test_cmd": " ".join(
                [shlex.quote(sys.executable), "isolation_check.py"]
            ),
            "root": str(fleet.root),
        },
    )

    assert response.status_code == 200, response.text
    # The line branch itself is not merged into and not checked out elsewhere:
    # its HEAD is still the reviewed commit.
    assert head(fleet.line) == line_head
    assert branch(fleet.line) == "feat/line"
    assert not has_merge_head(fleet.line)
    assert len(parents(fleet.target)) == 2
