"""N67: daemon Ticket Workers write Git objects only into a per-run quarantine.

The shared ``<common>/objects`` store stays read-only inside the sandbox; the
host moves the quarantined objects in after the run (``pack-objects |
index-pack --strict``) and keeps a quarantine it cannot move. Also N68's
ref-content checks (a text symref or a deleted Ticket ref is refused).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.conductor_cli import daemon_ticket_worktree as host_wt
from src.server.services.autonomy import object_quarantine as oq
from tests.unit.conftest import completion_report_json
from tests.unit import test_local_daemon_ticket_worktree as _base
from tests.unit.test_local_daemon_ticket_worktree import (
    FakeServer,
    _adapter,
    _claim,
    _git,
    _run_once,
    _sandbox_available,
    _ticket,
)

# The daemon worktree fixtures (repo, roots, isolation policy, platform).
dispatch_repo = _base.dispatch_repo
worktree_root = _base.worktree_root
local_isolation_policy = _base.local_isolation_policy
writable_worktree_platform = _base.writable_worktree_platform


def _quarantine_parent(repo: Path) -> Path:
    return (repo / ".git").resolve() / oq.QUARANTINE_PARENT


def _write_object(repo: Path, object_dir: Path, content: bytes) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_OBJECT_DIRECTORY=str(object_dir),
        GIT_ALTERNATE_OBJECT_DIRECTORIES=str((repo / ".git" / "objects").resolve()),
    )
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=content,
        capture_output=True,
        env=env,
        check=True,
    ).stdout.decode().strip()


def _linked_admin(repo: Path, tmp_path: Path, name: str = "wt") -> Path:
    _git(repo, "worktree", "add", "-q", "-b", f"conductor/ticket/{name}", str(tmp_path / name))
    return (repo / ".git" / "worktrees" / name).resolve()


# --- the sandbox probe: what main's store looks like after a hostile Worker ---


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _sandbox_available(),
    reason="the object store boundary is enforced by macOS sandbox-exec only",
)
async def test_sandboxed_worker_cannot_write_the_shared_object_store(
    dispatch_repo: Path,
    worktree_root: Path,
    local_isolation_policy: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The R3-2 sandbox granted ``objects/`` writable: a Worker could add a
    loose object, delete a pack main needs or write ``info/alternates``.
    Now each of those fails, and a normal commit still lands via the host's
    migration."""
    from src.llms.native_cli import isolation
    from src.llms.native_cli.isolation import prepare_process
    from src.server.services.autonomy import git_delivery as gd

    monkeypatch.setattr(
        isolation,
        "read_workspace_policy",
        lambda *_args: {
            "root": str(local_isolation_policy),
            "env": set(),
            "runtime_read_paths": tuple(gd._git_runtime_read_paths()),
        },
    )
    _git(dispatch_repo, "repack", "-a", "-d", "-q")
    common = (dispatch_repo / ".git").resolve()
    packs = sorted((common / "objects" / "pack").glob("*.pack"))
    assert packs
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(70)
    branch = f"conductor/ticket/{ticket_id}"
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-70"))
    results: dict[str, subprocess.CompletedProcess[str]] = {}
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        git = gd._git_binary()
        identity = ("-c", "user.name=w", "-c", "user.email=w@example.invalid")
        seen["env"] = dict(isolation.current_worker_isolation().git_env)
        (Path(cwd) / "work.txt").write_text("work\n", encoding="utf-8")
        steps = {
            "loose": ("/bin/sh", "-c", f'mkdir -p "{common}/objects/ee" && echo x > "{common}/objects/ee/{"e" * 38}"'),
            "pack_delete": ("/bin/rm", "-f", str(packs[0])),
            "alternates": ("/bin/sh", "-c", f'echo /tmp > "{common}/objects/info/alternates"'),
            "unquarantined_write": (
                "/usr/bin/env",
                "-u",
                "GIT_OBJECT_DIRECTORY",
                "-u",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "/bin/sh",
                "-c",
                f'echo unquarantined | "{git}" hash-object -w --stdin',
            ),
            "add": (git, "add", "work.txt"),
            "commit": (git, *identity, "commit", "-qm", "worker commit"),
        }
        for name, argv in steps.items():
            wrapped, env, run_cwd = prepare_process(argv, str(cwd), {})
            assert wrapped[0].endswith("sandbox-exec")
            results[name] = subprocess.run(
                wrapped, cwd=run_cwd, env=env, capture_output=True, text=True
            )
        return SimpleNamespace(
            text=completion_report_json("blocked", "need review", next_step="need review?")
        )

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True

    for name in ("loose", "pack_delete", "alternates", "unquarantined_write"):
        assert results[name].returncode != 0, name
    assert all(pack.exists() for pack in packs)
    assert not (common / "objects" / "info" / "alternates").exists()
    assert not (common / "objects" / "ee").exists()
    assert results["commit"].returncode == 0, results["commit"].stderr
    quarantine = Path(seen["env"]["GIT_OBJECT_DIRECTORY"])
    assert quarantine.parent == common / oq.QUARANTINE_PARENT
    assert seen["env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"].split(os.pathsep)[0] == str(
        common / "objects"
    )
    # Host side, no GIT_* env: the commit was moved into the real store and
    # the quarantine is gone.
    tip = _git(dispatch_repo, "rev-parse", branch)
    assert tip != base_sha
    assert _git(dispatch_repo, "cat-file", "-t", tip) == "commit"
    _git(dispatch_repo, "fsck", "--connectivity-only", "--no-dangling")
    assert not quarantine.exists()
    [report] = api.bodies("/report")
    assert "refused the Worker result" not in report["summary"]


@pytest.mark.asyncio
async def test_run_grants_the_quarantine_not_the_object_store(
    dispatch_repo: Path, worktree_root: Path
):
    from src.llms.native_cli import isolation

    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(71)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-71"))
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        current = isolation.current_worker_isolation()
        seen["dirs"] = current.runtime_write_paths
        seen["env"] = dict(current.git_env)
        seen["worker_env"] = isolation._minimal_env({}, current)
        return SimpleNamespace(text=completion_report_json("blocked", "wait", next_step="wait?"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    common = (dispatch_repo / ".git").resolve()
    assert common / "objects" not in seen["dirs"]
    assert not any(
        path == common / "objects" or (common / "objects").is_relative_to(path)
        for path in seen["dirs"]
    )
    quarantine = Path(seen["env"]["GIT_OBJECT_DIRECTORY"])
    assert quarantine in seen["dirs"]
    assert seen["worker_env"]["GIT_OBJECT_DIRECTORY"] == str(quarantine)
    assert "'gc.auto'='0'" in seen["worker_env"]["GIT_CONFIG_PARAMETERS"]
    # The empty quarantine was dropped after the run.
    assert not quarantine.exists()


@pytest.mark.asyncio
async def test_unmovable_quarantine_refuses_the_result_and_is_kept(
    dispatch_repo: Path, worktree_root: Path
):
    from src.llms.native_cli import isolation

    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(72)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-72"))
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        quarantine = Path(dict(isolation.current_worker_isolation().git_env)["GIT_OBJECT_DIRECTORY"])
        seen["q"] = quarantine
        _write_object(dispatch_repo, quarantine, b"worker object\n")
        (quarantine / "escape").symlink_to(dispatch_repo / ".git" / "objects")
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    # Not an ending: the checkout stays with the kept quarantine.
    assert report["state"] == "blocked"
    assert "could not be moved" in report["summary"]
    assert "symlink" in report["summary"]
    assert seen["q"].is_dir()  # kept, nothing removed through the symlink
    assert (dispatch_repo / ".git" / "objects").is_dir()
    assert api.bodies("/worktree/cleanup") == []
    assert host_wt.ticket_worktree_path(
        worktree_root, dispatch_repo, ticket_id, f"conductor/ticket/{ticket_id}"
    ).is_dir()


@pytest.mark.asyncio
async def test_cli_failure_still_moves_the_quarantined_objects(
    dispatch_repo: Path, worktree_root: Path
):
    from src.llms.native_cli import isolation

    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(74)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-74"))
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        quarantine = Path(dict(isolation.current_worker_isolation().git_env)["GIT_OBJECT_DIRECTORY"])
        seen["q"] = quarantine
        seen["oid"] = _write_object(dispatch_repo, quarantine, b"before the crash\n")
        raise RuntimeError("provider crashed")

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert _git(dispatch_repo, "cat-file", "-p", seen["oid"]) == "before the crash"
    assert not seen["q"].exists()


# --- the migration itself ---


def test_migrate_moves_objects_checks_the_tip_and_removes(dispatch_repo: Path, tmp_path: Path):
    common = (dispatch_repo / ".git").resolve()
    admin = _linked_admin(dispatch_repo, tmp_path)
    quarantine = oq.prepare(common, admin)
    oid = _write_object(dispatch_repo, quarantine.path, b"hello quarantine\n")
    assert subprocess.run(
        ["git", "cat-file", "-e", oid], cwd=dispatch_repo, env={"PATH": os.defpath}
    ).returncode != 0
    (quarantine.path / "info").mkdir(exist_ok=True)
    (quarantine.path / "info" / "alternates").write_text("/nonexistent\n")
    result = oq.migrate(common, quarantine.path)
    assert result.ok and result.moved == 1, result
    assert _git(dispatch_repo, "cat-file", "-p", oid) == "hello quarantine"
    assert not quarantine.path.exists()


def test_migrate_refuses_a_missing_tip_and_a_corrupt_object(dispatch_repo: Path, tmp_path: Path):
    common = (dispatch_repo / ".git").resolve()
    admin = _linked_admin(dispatch_repo, tmp_path)
    quarantine = oq.prepare(common, admin)
    result = oq.migrate(common, quarantine.path, tips=("f" * 40,))
    assert not result.ok and "tip" in result.reason
    assert quarantine.path.exists()
    corrupt = quarantine.path / "ab" / ("c" * 38)
    corrupt.parent.mkdir()
    corrupt.write_bytes(b"not zlib")
    result = oq.migrate(common, quarantine.path)
    assert not result.ok
    assert quarantine.path.exists()


def test_prepare_retries_leftovers_then_offers_kept_ones_as_alternates(
    dispatch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    common = (dispatch_repo / ".git").resolve()
    admin = _linked_admin(dispatch_repo, tmp_path)
    first = oq.prepare(common, admin)
    oid = _write_object(dispatch_repo, first.path, b"left behind\n")
    second = oq.prepare(common, admin)  # the next run moves the leftover first
    assert not first.path.exists() and second.kept == ()
    assert _git(dispatch_repo, "cat-file", "-p", oid) == "left behind"

    (second.path / "link").symlink_to(tmp_path)
    monkeypatch.setenv("CONDUCTOR_DAEMON_QUARANTINE_MAX_RETRIES", "2")
    third = oq.prepare(common, admin)
    assert third.kept == (second.path,)
    assert any("kept" in note for note in third.notes)
    assert second.path.name in third.worker_env()["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
    fourth = oq.prepare(common, admin)
    assert any("kept" in note for note in fourth.notes)
    fifth = oq.prepare(common, admin)
    assert any("needs an operator" in note for note in fifth.notes)
    assert second.path.exists()


def test_a_leftover_with_a_live_worker_is_never_moved(dispatch_repo: Path, tmp_path: Path):
    common = (dispatch_repo / ".git").resolve()
    admin = _linked_admin(dispatch_repo, tmp_path)
    first = oq.prepare(common, admin)
    oid = _write_object(dispatch_repo, first.path, b"still being written\n")
    oq.mark_live(first.path, [os.getpgrp()])
    second = oq.prepare(common, admin)
    assert first.path.is_dir()
    assert any("still running" in note for note in second.notes)
    assert subprocess.run(
        ["git", "cat-file", "-e", oid], cwd=dispatch_repo, env={"PATH": os.defpath}
    ).returncode != 0
    oq.mark_live(first.path, [])  # group unknown: stays live
    oq.prepare(common, admin)
    assert first.path.is_dir()
    oq.mark_live(first.path, [2**30])  # that group is gone
    oq.prepare(common, admin)
    assert not first.path.exists()
    assert _git(dispatch_repo, "cat-file", "-p", oid) == "still being written"


def test_migrate_drops_index_files_host_git_would_parse(dispatch_repo: Path, tmp_path: Path):
    common = (dispatch_repo / ".git").resolve()
    admin = _linked_admin(dispatch_repo, tmp_path)
    quarantine = oq.prepare(common, admin)
    oid = _write_object(dispatch_repo, quarantine.path, b"real object\n")
    (quarantine.path / "info").mkdir(exist_ok=True)
    (quarantine.path / "info" / "commit-graphs").mkdir()
    (quarantine.path / "info" / "commit-graph").write_bytes(b"garbage")
    (quarantine.path / "pack").mkdir(exist_ok=True)
    (quarantine.path / "pack" / "multi-pack-index").write_bytes(b"garbage")
    (quarantine.path / oid[:2] / "tmp_obj_x").write_bytes(b"temp")
    result = oq.migrate(common, quarantine.path)
    assert result.ok, result
    assert _git(dispatch_repo, "cat-file", "-p", oid) == "real object"


def test_prepare_moves_the_leftover_of_a_removed_checkout(dispatch_repo: Path, tmp_path: Path):
    common = (dispatch_repo / ".git").resolve()
    gone = _linked_admin(dispatch_repo, tmp_path, "gone")
    leftover = oq.prepare(common, gone)
    oid = _write_object(dispatch_repo, leftover.path, b"orphaned\n")
    _git(dispatch_repo, "worktree", "remove", "--force", str(tmp_path / "gone"))
    other = _linked_admin(dispatch_repo, tmp_path, "other")
    oq.prepare(common, other)
    assert not leftover.path.exists()
    assert _git(dispatch_repo, "cat-file", "-p", oid) == "orphaned"


def test_prepare_never_touches_a_dispatch_launch_quarantine(dispatch_repo: Path, tmp_path: Path):
    common = (dispatch_repo / ".git").resolve()
    admin = _linked_admin(dispatch_repo, tmp_path)
    parent = _quarantine_parent(dispatch_repo)
    parent.mkdir(exist_ok=True)
    launcher = parent / f"{admin.name}-20261111T000000"
    launcher.mkdir()
    _write_object(dispatch_repo, launcher, b"live dispatch launch\n")
    oq.prepare(common, admin)
    assert launcher.is_dir() and any(launcher.iterdir())


def test_prepare_needs_a_linked_worktree(dispatch_repo: Path):
    common = (dispatch_repo / ".git").resolve()
    with pytest.raises(oq.ObjectQuarantineError):
        oq.prepare(common, common)


def test_worker_isolation_accepts_only_object_quarantine_variables(tmp_path: Path):
    from src.llms.native_cli.base import NativeCliUnavailable
    from src.llms.native_cli.isolation import worker_isolation

    workdir = tmp_path / "w"
    workdir.mkdir()
    with pytest.raises(NativeCliUnavailable):
        with worker_isolation(
            workspace_id="w",
            root=tmp_path / "state",
            workdir=workdir,
            env_names=set(),
            git_env={"GIT_DIR": "/elsewhere"},
        ):
            pass


# --- N68: ref content ---


def _ref_fixture(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    common = tmp_path / "common"
    admin = common / "worktrees" / "t"
    admin.mkdir(parents=True)
    branch = "conductor/ticket/T"
    ref = common / "refs" / "heads" / branch
    ref.parent.mkdir(parents=True)
    (admin / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return common, admin, branch, ref


def test_a_text_symref_in_the_ticket_ref_is_refused(tmp_path: Path):
    common, admin, branch, ref = _ref_fixture(tmp_path)
    ref.write_text("a" * 40 + "\n")
    assert host_wt.ticket_ref_tamper_reason(common, admin, branch) == ""
    ref.write_text("ref: refs/heads/main\n")
    assert "plain object id" in host_wt.ticket_ref_tamper_reason(common, admin, branch)
    assert "plain object id" in host_wt.ticket_ref_tamper_reason(common, None, branch)
    assert host_wt.ticket_ref_oid(common, branch) == ""


def test_a_deleted_ticket_ref_is_refused_after_the_run(tmp_path: Path):
    common, admin, branch, ref = _ref_fixture(tmp_path)
    # Before a first commit a missing ref is an unborn branch, not tampering.
    assert host_wt.ticket_ref_tamper_reason(common, admin, branch) == ""
    assert "missing" in host_wt.ticket_ref_tamper_reason(
        common, admin, branch, require_ref=True
    )
    # A packed ref counts as present.
    (common / "packed-refs").write_text(f"{'b' * 40} refs/heads/{branch}\n")
    assert host_wt.ticket_ref_tamper_reason(common, admin, branch, require_ref=True) == ""
    assert host_wt.ticket_ref_oid(common, branch) == "b" * 40


@pytest.mark.asyncio
async def test_worker_deleting_its_ticket_ref_is_refused(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(73)
    branch = f"conductor/ticket/{ticket_id}"
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-73"))

    async def run(prompt, *, cwd=None, **_kwargs):
        (dispatch_repo / ".git" / "refs" / "heads" / branch).unlink()
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert "ref is missing" in report["summary"]


def test_quarantine_env_is_dropped_for_host_git_checks(tmp_path: Path, monkeypatch):
    """git_delivery's isolated reads run inside the Worker's isolation; they
    must read the real store, not the Worker's quarantine."""
    from src.server.services.autonomy import git_delivery as gd

    seen: dict[str, Any] = {}

    def fake_stream(argv, *, cwd, env, **_kwargs):
        seen["env"] = env
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(gd, "_stream_git_output", fake_stream)
    from src.llms.native_cli.isolation import worker_isolation

    workdir = tmp_path / "w"
    workdir.mkdir()
    _git(workdir, "init", "-q")
    with worker_isolation(
        workspace_id="w",
        root=tmp_path / "state",
        workdir=workdir,
        env_names=set(),
        git_env={"GIT_OBJECT_DIRECTORY": str(tmp_path / "q")},
    ):
        gd._run_git(str(workdir), ["status"], timeout=10)
    assert "GIT_OBJECT_DIRECTORY" not in seen["env"]
