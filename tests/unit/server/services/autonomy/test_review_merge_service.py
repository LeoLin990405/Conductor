"""Merge service safety properties: path resolution, local gate, Codex review.

The router wiring is covered by the API and end-to-end tests; here the
service's own invariants are pinned without an HTTP round trip: the root/path
boundary, the local-only privilege layers, the Codex review parsing/failures,
the rehearsal pinning and the parent fence on the landed merge.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config.models import ReviewMergeConfig
from src.server.services.autonomy import review_merge as service
from tests.unit.server.review_merge_git import (
    GitCommandError,
    add_worktree,
    commit_file,
    git,
    head,
    init_repo,
    worktree_count,
)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    root = tmp_path / "fleet"
    root.mkdir()
    repo = init_repo(root / "repo", {"base.txt": "base\n"})
    git(repo, "branch", "integrate/r2")
    add_worktree(repo, root / "target", "integrate/r2")
    add_worktree(repo, root / "line", "feat/line", create_branch=True)
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(root))
    return SimpleNamespace(
        root=root, repo=repo, target=root / "target", line=root / "line"
    )


def _prepare(fleet):
    return service._prepare_merge(
        line="line", into="integrate/r2", claimed_root=str(fleet.root)
    )


def _review(**overrides):
    base = {
        "review_id": "review-1",
        "line": "line",
        "reviewer": "repo",
        "reviewer_provider": "codex",
        "state": "completed",
        "verdict": "merge",
        "head_sha": "a" * 40,
        "note": "",
    }
    base.update(overrides)
    return base


def _codex_ok(monkeypatch):
    calls: list[dict] = []

    def fake_review(**kwargs):
        calls.append(kwargs)
        return "merge"

    monkeypatch.setattr(service, "_run_codex_merge_review", fake_review)
    return calls


def _fake_run(monkeypatch, *, action=None, result=(True, "green", False)):
    def fake_run_merge(argv: list[str], *, cwd, line_path=None):
        if action is not None:
            action()
        return result

    monkeypatch.setattr(service, "_run_merge", fake_run_merge)


# ---------------------------------------------------------------------------
# Root and path boundary
# ---------------------------------------------------------------------------


def test_resolve_line_rejects_traversal_and_separators(fleet):
    for name in ("", ".", "..", "../escape", "a/b", "/abs", "a\\b"):
        with pytest.raises(service.ReviewMergeError) as error:
            service.resolve_line(fleet.root, name)
        assert error.value.code == "line_not_found"


def test_resolve_line_accepts_a_worktree_directory_with_spaces(fleet):
    space = fleet.root / "line with space"
    space.mkdir()
    (space / ".git").mkdir()

    assert service.resolve_line(fleet.root, "line with space") == space.resolve()


def test_resolve_line_rejects_a_symlink_that_escapes_the_root(fleet, tmp_path):
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (fleet.root / "escape").symlink_to(outside)

    with pytest.raises(service.ReviewMergeError) as error:
        service.resolve_line(fleet.root, "escape")

    assert error.value.code == "line_not_found"


def test_claimed_root_must_match_the_configured_root(fleet, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(service.ReviewMergeError) as error:
        service.resolve_request_root(str(other))
    assert error.value.code == "worktree_root_mismatch"
    assert str(fleet.root.resolve()) in error.value.message


def test_target_symlink_outside_the_root_is_refused(fleet, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    git(outside, "init", "-q")
    (outside / "f.txt").write_text("x\n", encoding="utf-8")
    git(outside, "add", "-A")
    git(outside, "commit", "-q", "-m", "outside")
    git(outside, "checkout", "-q", "-b", "integrate/r2")
    import shutil

    shutil.rmtree(fleet.target)
    (fleet.root / "target").symlink_to(outside)

    with pytest.raises(service.ReviewMergeError) as error:
        service.resolve_target_worktree(fleet.root, "integrate/r2")

    assert error.value.code == "target_worktree_outside_root"


# ---------------------------------------------------------------------------
# Local-only privilege
# ---------------------------------------------------------------------------


def test_local_gate_accepts_a_loopback_peer_and_local_host(monkeypatch):
    monkeypatch.setattr(
        service, "get_review_merge_config", lambda: ReviewMergeConfig()
    )
    assert service.is_local_merge_request(
        peer="127.0.0.1", hostname="127.0.0.1", headers={}
    )
    assert service.is_local_merge_request(
        peer="::1", hostname="localhost", headers={}
    )


def test_local_gate_refuses_a_remote_peer():
    assert not service.is_local_merge_request(
        peer="203.0.113.7", hostname="127.0.0.1", headers={}
    )
    assert not service.is_local_merge_request(
        peer=None, hostname="localhost", headers={}
    )


def test_local_gate_refuses_a_forwarding_header(monkeypatch):
    monkeypatch.setattr(
        service, "get_review_merge_config", lambda: ReviewMergeConfig()
    )
    for name in ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip"):
        assert not service.is_local_merge_request(
            peer="127.0.0.1",
            hostname="127.0.0.1",
            headers={name: "203.0.113.7"},
        )


def test_local_gate_refuses_a_public_host(monkeypatch):
    monkeypatch.setattr(
        service, "get_review_merge_config", lambda: ReviewMergeConfig()
    )
    assert not service.is_local_merge_request(
        peer="127.0.0.1", hostname="conductor.example.com", headers={}
    )


def test_local_gate_honours_an_explicitly_granted_host(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(local_hosts=["127.0.0.1", "conductor.local"]),
    )
    assert service.is_local_merge_request(
        peer="127.0.0.1", hostname="conductor.local", headers={}
    )
    # The peer must still be loopback.
    assert not service.is_local_merge_request(
        peer="203.0.113.7", hostname="conductor.local", headers={}
    )


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_prepare_refuses_a_detached_line(fleet):
    git(fleet.line, "checkout", "-q", "--detach")
    with pytest.raises(service.ReviewMergeError) as error:
        _prepare(fleet)
    assert error.value.code == "line_detached"


def test_prepare_refuses_a_dirty_line(fleet):
    (fleet.line / "dirty.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(service.ReviewMergeError) as error:
        _prepare(fleet)
    assert error.value.code == "line_dirty"


def test_prepare_refuses_a_dirty_target(fleet):
    (fleet.target / "dirty.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(service.ReviewMergeError) as error:
        _prepare(fleet)
    assert error.value.code == "target_dirty"


def test_prepare_refuses_a_missing_target_worktree(fleet):
    git(fleet.target, "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(service.ReviewMergeError) as error:
        _prepare(fleet)
    assert error.value.code == "target_worktree_missing"


def test_prepare_refuses_an_invalid_branch_name(fleet):
    with pytest.raises(service.ReviewMergeError) as error:
        service._prepare_merge(
            line="line", into="--head", claimed_root=str(fleet.root)
        )
    assert error.value.code == "invalid_target_branch"


# ---------------------------------------------------------------------------
# Merge-time Codex review
# ---------------------------------------------------------------------------


def test_parse_codex_verdict_requires_it_on_the_final_line():
    assert service._parse_codex_verdict("findings\nVERDICT: MERGE") == "merge"
    assert service._parse_codex_verdict("VERDICT: BLOCK\n") == "block"
    # Only the last non-empty line counts; earlier draft verdicts are ignored.
    assert (
        service._parse_codex_verdict("VERDICT: MERGE\nstill checking\nVERDICT: BLOCK")
        == "block"
    )
    # A trailing explanation means the final line is not a verdict.
    assert service._parse_codex_verdict("VERDICT: MERGE\nthanks!") is None
    assert service._parse_codex_verdict("no verdict here") is None
    assert service._parse_codex_verdict("VERDICT: maybe") is None


def test_codex_prompt_is_bounded_and_control_free():
    prompt = service._codex_prompt(
        line="line\nwith\x00controls" + "x" * 1000,
        into="integrate/r2",
        target_head="a" * 40,
    )
    assert "VERDICT: MERGE" in prompt
    assert "VERDICT: BLOCK" in prompt
    assert "\x00" not in prompt
    assert "linewithcontrols" in prompt
    assert len(prompt) < 2000


def test_codex_argv_rejects_an_unparseable_command(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(codex_command="'unclosed"),
    )
    with pytest.raises(service.ReviewMergeError) as error:
        service._codex_argv()
    assert error.value.code == "codex_command_invalid"


def test_codex_config_error_creates_no_temp_file(monkeypatch, tmp_path):
    import tempfile as tempfile_module

    before = set(Path(tempfile_module.gettempdir()).glob("conductor-codex-final-*"))
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(codex_command="'unclosed"),
    )
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_command_invalid"
    after = set(Path(tempfile_module.gettempdir()).glob("conductor-codex-final-*"))
    assert after == before


def _codex_run(
    monkeypatch,
    *,
    returncode=0,
    stdout="",
    stderr="",
    final_message=None,
    error=None,
    timed_out=False,
):
    """Fake the codex subprocess, writing --output-last-message when present."""

    def fake_process(argv, *, cwd, env, timeout):
        assert argv[1:4] == ["exec", "--sandbox", "read-only"]
        if error is not None:
            raise error
        if "--output-last-message" in argv:
            path = Path(argv[argv.index("--output-last-message") + 1])
            path.write_text(
                stdout if final_message is None else final_message,
                encoding="utf-8",
            )
        return returncode, stdout, stderr, timed_out

    monkeypatch.setattr(service, "_run_process", fake_process)


def _codex_review(tmp_path):
    return service._run_codex_merge_review(
        worktree=tmp_path,
        line="line",
        into="integrate/r2",
        target_head="a" * 40,
    )


def test_codex_review_requires_merge_verdict(monkeypatch, tmp_path):
    _codex_run(monkeypatch, stdout="reviewed\nVERDICT: MERGE\n")
    assert _codex_review(tmp_path) == "merge"


def test_codex_review_reports_a_block(monkeypatch, tmp_path):
    _codex_run(monkeypatch, stdout="finding: x\nVERDICT: BLOCK\n")
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_blocked"
    assert error.value.kind == "refused"
    assert "output_tail" not in error.value.detail
    assert "final_message_tail" not in error.value.detail


def test_codex_review_ignores_incidental_verdict_text(monkeypatch, tmp_path):
    # A verdict quoted in stdout/diagnostics must never gate a merge.
    _codex_run(
        monkeypatch,
        stdout="...\nVERDICT: MERGE\n",
        final_message="I could not finish the review.",
    )
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_verdict_missing"


def test_codex_verdict_is_read_from_the_end_of_a_large_final_message(
    monkeypatch, tmp_path
):
    # The file is larger than any prefix cap; a BLOCK at the end must win over
    # an earlier draft MERGE in the retained prefix.
    _codex_run(
        monkeypatch,
        final_message="draft: VERDICT: MERGE\n" + "x" * 70000 + "\nVERDICT: BLOCK\n",
    )
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_blocked"


def test_codex_timeout_removes_the_final_answer_file(monkeypatch, tmp_path):
    import tempfile as tempfile_module

    private_temp = tmp_path / "private-temp"
    private_temp.mkdir(mode=0o700)
    private_temp.chmod(0o700)
    monkeypatch.setenv("TMPDIR", str(private_temp))
    monkeypatch.setattr(tempfile_module, "tempdir", None)
    assert Path(tempfile_module.gettempdir()) == private_temp

    before = set(Path(tempfile_module.gettempdir()).glob("conductor-codex-final-*"))
    _codex_run(monkeypatch, timed_out=True)
    with pytest.raises(service.ReviewMergeError):
        _codex_review(tmp_path)
    after = set(Path(tempfile_module.gettempdir()).glob("conductor-codex-final-*"))
    assert after == before


def test_a_truncated_tail_cannot_manufacture_a_verdict(monkeypatch, tmp_path):
    # One huge line: after tail truncation its retained prefix starts mid-line,
    # so it must be discarded instead of parsed as a complete final line.
    _codex_run(monkeypatch, final_message="draft VERDICT: MERGE " + "x" * 70000)
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_verdict_missing"


def test_codex_review_reports_a_missing_verdict(monkeypatch, tmp_path):
    _codex_run(monkeypatch, stdout="I could not decide")
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_verdict_missing"


def test_codex_review_reports_a_nonzero_exit(monkeypatch, tmp_path):
    _codex_run(monkeypatch, returncode=2, stderr="auth failed")
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_failed"
    assert error.value.detail["exit_code"] == 2
    assert "output_tail" not in error.value.detail


def test_codex_review_reports_a_timeout(monkeypatch, tmp_path):
    _codex_run(monkeypatch, timed_out=True)
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_timeout"


def test_codex_review_reports_a_missing_binary(monkeypatch, tmp_path):
    _codex_run(monkeypatch, error=OSError("no codex"))
    with pytest.raises(service.ReviewMergeError) as error:
        _codex_review(tmp_path)
    assert error.value.code == "codex_unavailable"
    assert "reason" in error.value.detail


# ---------------------------------------------------------------------------
# Rehearsal pinning (a branch is mutable while tests/Codex run)
# ---------------------------------------------------------------------------


def test_rehearsal_refuses_when_the_line_advanced_while_tests_ran(fleet, monkeypatch):
    state = _prepare(fleet)
    target_before = head(fleet.target)

    def advance_line() -> None:
        commit_file(fleet.line, "moved.txt", "moved\n", "advance during tests")

    _fake_run(monkeypatch, action=advance_line)
    _codex_ok(monkeypatch)
    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)

    assert error.value.code == "line_advanced"
    assert head(fleet.target) == target_before
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_refuses_when_the_target_advanced_while_tests_ran(
    fleet, monkeypatch
):
    state = _prepare(fleet)

    def advance_target() -> None:
        commit_file(fleet.target, "target.txt", "target\n", "advance during tests")

    _fake_run(monkeypatch, action=advance_target)
    _codex_ok(monkeypatch)
    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)

    assert error.value.code == "target_advanced"
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_refuses_red_tests_without_touching_the_target(fleet, monkeypatch):
    state = _prepare(fleet)
    target_before = head(fleet.target)
    config = ReviewMergeConfig(output_tail_chars=50)
    monkeypatch.setattr(service, "get_review_merge_config", lambda: config)
    codex_calls = _codex_ok(monkeypatch)
    _fake_run(
        monkeypatch,
        result=(False, "FAILED test_x.py::test_conflict " + "x" * 200, False),
    )

    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)

    assert error.value.code == "tests_failed"
    assert error.value.detail["output_tail"]
    assert len(error.value.detail["output_tail"]) <= 50
    # A red rehearsal never reaches the Codex review.
    assert codex_calls == []
    assert head(fleet.target) == target_before
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_reports_a_test_timeout_as_structured_error(fleet, monkeypatch):
    state = _prepare(fleet)
    _fake_run(monkeypatch, result=(False, "", True))
    _codex_ok(monkeypatch)

    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)

    assert error.value.code == "tests_timeout"
    assert error.value.kind == "failed"
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_reports_a_conflict_before_running_tests(fleet, monkeypatch):
    commit_file(fleet.target, "conflict.txt", "target\n", "target edit")
    commit_file(fleet.line, "conflict.txt", "line\n", "line edit")
    state = _prepare(fleet)
    target_before = head(fleet.target)
    ran: list[bool] = []

    def tracking_run(argv: list[str], *, cwd, line_path=None):
        ran.append(True)
        return True, "", False

    monkeypatch.setattr(service, "_run_merge", tracking_run)
    codex_calls = _codex_ok(monkeypatch)

    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)

    assert error.value.code == "merge_conflict"
    assert error.value.detail["conflicts"] == ["conflict.txt"]
    assert error.value.detail["target_branch_untouched"] is True
    assert ran == []
    assert codex_calls == []
    assert head(fleet.target) == target_before
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_propagates_a_codex_block(fleet, monkeypatch):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)

    def blocked(**kwargs):
        raise service.ReviewMergeError("codex_blocked", "blocked", kind="refused")

    monkeypatch.setattr(service, "_run_codex_merge_review", blocked)
    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)
    assert error.value.code == "codex_blocked"
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_refuses_when_tests_modify_the_worktree(fleet, monkeypatch):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)

    def dirty_run(argv: list[str], *, cwd, line_path=None):
        (Path(cwd) / "work.txt").write_text("modified by tests\n", encoding="utf-8")
        return True, "", False

    monkeypatch.setattr(service, "_run_merge", dirty_run)
    _codex_ok(monkeypatch)

    with pytest.raises(service.ReviewMergeError) as error:
        service._rehearse(state, None)

    assert error.value.code == "rehearsal_modified"
    assert worktree_count(fleet.repo) == 3


def test_rehearsal_returns_the_tested_commit(fleet, monkeypatch):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)
    _codex_ok(monkeypatch)

    rehearsal = service._rehearse(state, None)

    assert rehearsal.line_head == head(fleet.line)
    assert rehearsal.target_head == head(fleet.target)
    assert rehearsal.merge_commit != head(fleet.target)
    assert len(rehearsal.merge_commit) == 40
    assert rehearsal.codex_verdict == "merge"


# ---------------------------------------------------------------------------
# Landing fence (ref-only CAS)
# ---------------------------------------------------------------------------


def test_land_moves_the_named_branch_ref_only(fleet, monkeypatch):
    """The branch lands the exact rehearsed commit; the worktree is untouched."""
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)
    _codex_ok(monkeypatch)
    rehearsal = service._rehearse(state, None)

    result = service._land(state, rehearsal)

    assert result["merged"] is True
    assert result["commit"] == rehearsal.merge_commit
    assert result["landed_ref"] == "refs/heads/integrate/r2"
    assert result["worktree_sync_required"] is True
    assert result["codex_verdict"] == "merge"
    # The ref is exactly the rehearsed merge commit, with the tested parents.
    assert (
        git(fleet.target, "rev-parse", "refs/heads/integrate/r2")
        == rehearsal.merge_commit
    )
    parents = git(fleet.target, "rev-list", "--parents", "-n", "1", "HEAD").split()[
        1:
    ]
    assert parents == [state.target_head, state.line_head]
    # The worktree was not rewritten by the server; the caller must sync it.
    assert not (fleet.target / "work.txt").exists()
    assert git(fleet.target, "status", "--porcelain") != ""
    # A local sync completes the landing for the checkout.
    git(fleet.target, "reset", "--hard")
    assert (fleet.target / "work.txt").read_text(encoding="utf-8") == "work\n"
    assert git(fleet.target, "status", "--porcelain") == ""


def test_land_refuses_when_the_line_is_already_merged(fleet, monkeypatch):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)
    _codex_ok(monkeypatch)
    service._land(state, service._rehearse(state, None))
    git(fleet.target, "reset", "--hard")
    already = _prepare(fleet)
    already_rehearsal = service._rehearse(already, None)
    assert already_rehearsal.merge_commit == already.target_head

    with pytest.raises(service.ReviewMergeError) as error:
        service._land(already, already_rehearsal)

    assert error.value.code == "already_merged"


def test_land_cas_refuses_when_the_target_advanced_and_preserves_the_writer(
    fleet, monkeypatch
):
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)
    _codex_ok(monkeypatch)
    rehearsal = service._rehearse(state, None)
    advanced = commit_file(
        fleet.target, "concurrent.txt", "concurrent\n", "concurrent work"
    )

    with pytest.raises(service.ReviewMergeError) as error:
        service._land(state, rehearsal)

    assert error.value.code == "target_advanced"
    # The CAS changed nothing; the concurrent commit is still the branch tip.
    assert (
        git(fleet.target, "rev-parse", "refs/heads/integrate/r2") == advanced
    )
    assert (fleet.target / "concurrent.txt").is_file()


def test_land_is_independent_of_what_the_target_worktree_has_checked_out(
    fleet, monkeypatch
):
    """A concurrent checkout cannot redirect a ref CAS on the named branch."""
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)
    _codex_ok(monkeypatch)
    rehearsal = service._rehearse(state, None)
    other_before = head(fleet.target)
    git(fleet.target, "checkout", "-q", "-b", "other")

    result = service._land(state, rehearsal)

    assert result["merged"] is True
    assert result["landed_ref"] == "refs/heads/integrate/r2"
    # The named branch landed the reviewed commit; the other checkout was not
    # touched (and the server does not claim the worktree is synced).
    assert (
        git(fleet.target, "rev-parse", "refs/heads/integrate/r2")
        == rehearsal.merge_commit
    )
    assert head(fleet.target) == other_before
    assert git(fleet.target, "branch", "--show-current") == "other"
    assert not (fleet.target / "work.txt").exists()


# ---------------------------------------------------------------------------
# Audit/error hygiene and test command plumbing
# ---------------------------------------------------------------------------


def test_audit_error_detail_drops_raw_process_output():
    error = service.ReviewMergeError(
        "tests_failed",
        "red",
        kind="failed",
        detail={
            "output_tail": "secret-ish output",
            "final_message_tail": "reviewer text",
            "output": "more output",
            "exit_code": 1,
        },
    )
    payload = service._audit_error_detail(error)
    assert payload["detail"] == {"exit_code": 1}
    assert "output_tail" not in payload["detail"]
    assert "final_message_tail" not in payload["detail"]


def test_test_command_is_bounded_and_credential_scrubbed(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(output_tail_chars=200),
    )
    tail = service._tail("token=sk-" "abcdefgh12345678 secret sk-" "zyxwvuts98765432")
    assert "sk-" "abcdefgh12345678" not in tail
    assert "sk-" "zyxwvuts98765432" not in tail
    assert len(tail) <= 200


def test_truncation_cannot_expose_a_credential_at_the_boundary(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(output_tail_chars=40),
    )
    # Sanitizing must happen before the length cut: a credential that would be
    # split by the cut is redacted first, so no fragment of it is exposed.
    credential = "sk-" + "a" * 40
    text = "x" * 30 + " api_key=" + credential + " " + "y" * 40
    tail = service._tail(text)
    assert "sk-" not in tail
    assert "a" * 40 not in tail
    assert len(tail) <= 40


def test_run_merge_reports_a_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(test_timeout_seconds=1.0),
    )
    monkeypatch.setattr(
        service, "_run_process", lambda *args, **kwargs: (-1, "", "", True)
    )
    assert service._run_merge(["pytest"], cwd=tmp_path) == (False, "", True)


def test_run_merge_reports_a_missing_executable(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(service, "_run_process", boom)
    with pytest.raises(service.ReviewMergeError) as error:
        service._run_merge(["/nonexistent/pytest"], cwd=tmp_path)
    assert error.value.code == "test_command_failed"


def test_import_guard_removes_the_line_checkout_from_sys_path(tmp_path):
    import sys

    leak_root = Path(__file__).resolve().parents[5]
    worktree = tmp_path / "rehearsal"
    worktree.mkdir()
    probe = (
        "import os, sys; p = os.path.abspath(os.environ"
        "['CONDUCTOR_REHEARSAL_LEAK_PREFIX']); "
        "paths = [os.path.abspath(e or '.') for e in sys.path]; "
        "src = any(x == os.path.join(p, 'src') for x in paths); "
        "venv = any(x.startswith(p + os.sep) and (os.sep + '.venv' + os.sep) in x "
        "for x in paths); "
        "print(src, venv)"
    )

    ok, output, timed_out = service._run_merge(
        [sys.executable, "-c", probe], cwd=worktree, line_path=leak_root
    )

    assert ok is True and timed_out is False
    # The checkout's source path is gone, while its virtualenv stays usable.
    assert output.strip().endswith("False True")


def test_tail_redacts_inherited_environment_secrets(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "pg-secret-value-123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "a" * 36)
    text = (
        "failing output: PGPASSWORD=pg-secret-value-123 "
        "token=ghp_" + "a" * 36
    )

    tail = service._tail(text)

    assert "pg-secret-value-123" not in tail
    assert "ghp_" + "a" * 36 not in tail
    assert "[REDACTED" in tail


def test_test_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
    import sys
    import time

    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(test_timeout_seconds=1.0),
    )
    child_script = (
        "import subprocess, sys, time; p = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(60)']); "
        "open('child.pid', 'w').write(str(p.pid)); time.sleep(60)"
    )

    ok, _output, timed_out = service._run_merge(
        [sys.executable, "-c", child_script], cwd=tmp_path
    )

    assert ok is False and timed_out is True
    pid = int((tmp_path / "child.pid").read_text(encoding="utf-8"))
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_zero_output_cap_means_no_truncation(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig().model_copy(update={"output_tail_chars": 0}),
    )
    text = "\n".join(f"line-{index}" for index in range(41))

    tail = service._tail(text)

    assert tail == text
    assert "line-0" in tail


def test_relative_interpreter_path_is_anchored_to_the_line_worktree(tmp_path):
    """Old ``--test-cmd '.venv/bin/python …'`` keeps working from the rehearsal."""
    worktree = tmp_path / "rehearsal"
    line = tmp_path / "line"
    worktree.mkdir()
    (line / ".venv" / "bin").mkdir(parents=True)
    (line / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    argv = service._resolve_test_argv(
        [".venv/bin/python", "-m", "pytest", "-q"],
        line_path=line,
        worktree=worktree,
    )

    assert argv[0] == str(line / ".venv" / "bin" / "python")
    assert argv[1:] == ["-m", "pytest", "-q"]


def test_repo_relative_test_script_stays_in_the_rehearsal_worktree(tmp_path):
    worktree = tmp_path / "rehearsal"
    line = tmp_path / "line"
    (worktree / "scripts").mkdir(parents=True)
    (worktree / "scripts" / "check.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (line / "scripts").mkdir(parents=True)
    (line / "scripts" / "check.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    argv = service._resolve_test_argv(
        ["scripts/check.sh"], line_path=line, worktree=worktree
    )

    assert argv == ["scripts/check.sh"]


def test_absolute_and_bare_test_commands_are_unchanged(tmp_path):
    worktree = tmp_path / "rehearsal"
    line = tmp_path / "line"
    worktree.mkdir()
    line.mkdir()
    for command in (["/usr/bin/python3", "-V"], ["pytest", "-q"]):
        assert (
            service._resolve_test_argv(command, line_path=line, worktree=worktree)
            == command
        )


def test_configured_test_command_splits_into_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(
        service,
        "get_review_merge_config",
        lambda: ReviewMergeConfig(test_command=".venv/bin/python -m pytest -q"),
    )
    assert service._test_command(tmp_path, None) == [
        ".venv/bin/python",
        "-m",
        "pytest",
        "-q",
    ]


def test_tail_redacts_short_env_secret(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "shortpw")
    tail = service._tail("failing command echoed shortpw in its output\n")
    assert "shortpw" not in tail
    assert "[REDACTED]" in tail


def test_hidden_index_flags_are_detected(fleet):
    path = fleet.line / "base.txt"
    path.write_text("modified\n", encoding="utf-8")
    git(fleet.line, "update-index", "--assume-unchanged", "base.txt")
    assert service._has_hidden_index_flags(fleet.line) is True
    # The plain status is clean even though the worktree content differs.
    assert service._is_dirty(fleet.line) is False


def test_tail_redacts_token_before_overlapping_short_secret(monkeypatch):
    token = "ghp_" + "x" * 30
    monkeypatch.setenv("PGPASSWORD", "x")
    monkeypatch.setenv("GITHUB_TOKEN", token)
    tail = service._tail(f"command echoed {token}\n")
    assert token not in tail
    assert "[REDACTED]" in tail


def test_tail_redacts_openai_style_token_before_short_env_secret(monkeypatch):
    token = "sk-" + "Q" * 32
    monkeypatch.setenv("PGPASSWORD", "sk")
    tail = service._tail(f"command echoed {token}\n")
    assert token not in tail
    assert "Q" * 16 not in tail


def test_land_does_not_follow_a_symbolic_target_ref(fleet, monkeypatch):
    """A symref swapped in before landing must not redirect the update."""
    commit_file(fleet.line, "work.txt", "work\n", "line work")
    state = _prepare(fleet)
    _fake_run(monkeypatch)
    _codex_ok(monkeypatch)
    rehearsal = service._rehearse(state, None)
    git(fleet.target, "checkout", "-q", "-b", "other")
    git(fleet.target, "symbolic-ref", "refs/heads/integrate/r2", "refs/heads/other")

    result = service._land(state, rehearsal)

    assert result["commit"] == rehearsal.merge_commit
    assert git(fleet.target, "rev-parse", "refs/heads/integrate/r2") == rehearsal.merge_commit
    with pytest.raises(GitCommandError):
        git(fleet.target, "symbolic-ref", "refs/heads/integrate/r2")


def test_import_guard_removes_the_server_checkout_without_a_line_venv(
    monkeypatch, tmp_path
):
    import sys

    server_root = tmp_path / "server"
    package = server_root / "leaky_pkg.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE = 'stale'\n", encoding="utf-8")
    line = tmp_path / "line"
    line.mkdir()
    worktree = tmp_path / "rehearsal"
    worktree.mkdir()
    module_file = (
        server_root / "src/server/services/autonomy/review_merge.py"
    )
    module_file.parent.mkdir(parents=True)
    monkeypatch.setattr(service, "__file__", str(module_file))
    monkeypatch.setenv("PYTHONPATH", str(server_root))
    probe = (
        "import importlib.util; print("
        "importlib.util.find_spec('leaky_pkg') is None)"
    )

    ok, output, timed_out = service._run_merge(
        [sys.executable, "-c", probe], cwd=worktree, line_path=line
    )

    assert ok is True and timed_out is False
    assert output.strip().endswith("True")
