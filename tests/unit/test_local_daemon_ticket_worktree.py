"""Local daemon creates, resumes and removes Ticket worktrees on its own host.

Every test drives the real ``Api`` request builders against an in-memory
server (``FakeServer._call``), so the assertions are on the exact HTTP bodies
the server would receive. Repositories are tmp ``git init`` repos; nothing
touches the network.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.conductor_cli import daemon_ticket_worktree as host_wt
from tests.unit.conftest import completion_report_json

PREFIX = "/api/v1/autonomy/daemon"


@pytest.fixture(autouse=True)
def local_isolation_policy(tmp_path, monkeypatch):
    from src.llms.native_cli import isolation

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "workspace-policy.json"
    policy.write_text("{}")
    os.chmod(policy, 0o600)
    monkeypatch.setenv("CONDUCTOR_WORKSPACE_POLICY", str(policy))
    monkeypatch.setenv("CONDUCTOR_ISOLATION_ROOT", str(tmp_path / "worker-state"))
    monkeypatch.setattr(
        isolation,
        "read_workspace_policy",
        lambda *_args: {"root": str(workspace), "env": set(), "runtime_read_paths": ()},
    )
    return workspace


@pytest.fixture(autouse=True)
def writable_worktree_platform(monkeypatch):
    """Per-file Git grants are macOS-only and a writable Ticket worktree is
    refused elsewhere. The fake adapters below never launch a process, so
    off macOS they run as if on it; the platform gate itself is tested
    explicitly (test_non_macos_writable_worktree_is_refused_before_checkout)."""
    import platform

    if platform.system() != "Darwin":
        monkeypatch.setattr(platform, "system", lambda: "Darwin")


def _commit_work(cwd: Path) -> None:
    """Deliver the Worker's work as a commit, as the completion contract asks."""
    _git(
        cwd,
        "-c",
        "user.name=worker",
        "-c",
        "user.email=worker@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "worker change",
    )


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


@pytest.fixture
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "isolation"))
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_ROOT", str(tmp_path / "locks"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Daemon Worktree")
    _git(repo, "config", "user.email", "daemon-worktree@example.invalid")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(repo))
    return repo


@pytest.fixture
def worktree_root(tmp_path: Path) -> Path:
    root = tmp_path / "daemon-worktrees"
    root.mkdir()
    return root


class FakeServer(daemon.Api):
    """The daemon HTTP API in memory: records each (method, path, body)."""

    def __init__(self, *claims: dict[str, Any]) -> None:
        super().__init__("http://fake.invalid", "token", timeout=5.0)
        self.claims = list(claims)
        self.calls: list[tuple[str, str, Any]] = []
        self.generation = 0
        self.report_error: Exception | None = None
        self.bind_error: Exception | None = None
        self.cleanup_errors: list[Exception | None] = []
        self.stop_ack_error: Exception | None = None

    def _call(self, method, path, body=None, *, timeout=None):
        self.calls.append((method, path, body))
        if path == f"{PREFIX}/claim":
            return self.claims.pop(0) if self.claims else {}
        if path == f"{PREFIX}/heartbeat":
            leases = (body or {}).get("leases") or []
            return {"leases": [{**lease, "renewed": True} for lease in leases]}
        if path.endswith("/status"):
            return {"status": "running"}
        if path.endswith("/report"):
            if self.report_error is not None:
                raise self.report_error
            return {"ok": True}
        if path.endswith("/worktree"):
            if self.bind_error is not None:
                raise self.bind_error
            self.generation += 1
            return {
                "worktree_path": body["worktree_path"],
                "worktree_generation": self.generation,
            }
        if path.endswith("/worktree/cleanup"):
            error = self.cleanup_errors.pop(0) if self.cleanup_errors else None
            if error is not None:
                raise error
            return {"ok": True, "pending": body["outcome"] == "failed"}
        if path.endswith("/stop/ack"):
            if self.stop_ack_error is not None:
                raise self.stop_ack_error
            return {"ok": True}
        if path.endswith("/output"):
            return {"ok": True}
        raise AssertionError(f"unexpected call {method} {path}")

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [body for _m, path, body in self.calls if path.endswith(suffix)]


def _claim(
    ticket_id: str,
    base_sha: str,
    *,
    lease_id: str = "lease-1",
    worktree_path: str = "",
    branch: str | None = None,
    duration: float = 90,
) -> dict[str, Any]:
    return {
        "ticket": {
            "ticket_id": ticket_id,
            "lease_id": lease_id,
            "lease_expires_at": "2099-01-01T00:00:00+00:00",
            "lease_duration_seconds": duration,
            "worker_provider": "codex",
            "workspace_id": "test-workspace",
            "status": "running",
            "worktree_path": worktree_path,
            "worktree": {
                "ticket_id": ticket_id,
                "branch": branch or f"conductor/ticket/{ticket_id}",
                "base_sha": base_sha,
                "base_branch": "origin/main",
                "worktree_path": worktree_path,
                "worktree_generation": 0,
            },
            "mandate": {},
        },
        "prompt": "work",
    }


def _adapter(run):
    return patch(
        "src.llms.native_cli.registry.get_adapter",
        return_value=SimpleNamespace(run=AsyncMock(side_effect=run)),
    )


async def _run_once(api: FakeServer, worktree_root: Path) -> Any:
    return await daemon.run_one(
        api, "codex", "/daemon/default", 10, worktree_root=str(worktree_root)
    )


def _journal(worktree_root: Path) -> list[dict[str, Any]]:
    return host_wt.list_cleanup_journal(worktree_root)


def _ticket(n: int) -> str:
    return f"{n:08x}-aaaa-bbbb-cccc-dddddddddddd"


@pytest.mark.asyncio
async def test_worktree_is_cut_under_daemon_root_not_the_server_path(
    dispatch_repo: Path, worktree_root: Path, tmp_path: Path
):
    """On core/main the claim's worktree spec is ignored: the CLI runs in the
    workspace root, nothing is bound and HEAD is not the pinned base."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    (dispatch_repo / "README").write_text("moved\n", encoding="utf-8")
    _git(dispatch_repo, "commit", "-qam", "origin/main moves after the pin")
    _git(dispatch_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # A server-side absolute path that happens to exist on this host too.
    server_path = tmp_path / "server" / "worktrees" / "ticket"
    server_path.mkdir(parents=True)
    ticket_id = _ticket(1)
    api = FakeServer(_claim(ticket_id, base_sha, worktree_path=str(server_path)))
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["cwd"] = Path(cwd)
        seen["head"] = _git(Path(cwd), "rev-parse", "HEAD")
        seen["branch"] = _git(Path(cwd), "branch", "--show-current")
        return SimpleNamespace(text=completion_report_json("blocked", "need input", next_step="need input?"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True

    assert seen["cwd"].is_relative_to(worktree_root.resolve())
    assert seen["cwd"] != server_path.resolve()
    assert seen["head"] == base_sha
    assert seen["branch"] == f"conductor/ticket/{ticket_id}"
    assert list(server_path.iterdir()) == []
    assert api.bodies("/worktree") == [
        {
            "lease_id": "lease-1",
            "worktree_path": str(seen["cwd"]),
            "branch": f"conductor/ticket/{ticket_id}",
            "base_sha": base_sha,
        }
    ]


@pytest.mark.asyncio
async def test_dirty_blocked_ticket_resumes_its_own_worktree_path(
    dispatch_repo: Path, worktree_root: Path, tmp_path: Path
):
    """On core/main the first run gets the workspace root, so no bound
    worktree_path exists and the second claim cannot resume one."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(2)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-1"))
    seen: list[Path] = []

    async def blocked(prompt, *, cwd=None, **_kwargs):
        seen.append(Path(cwd))
        (Path(cwd) / "wip.txt").write_text("half done\n", encoding="utf-8")
        return SimpleNamespace(text=completion_report_json("blocked", "need a decision", next_step="need a decision?"))

    with _adapter(blocked):
        assert await _run_once(api, worktree_root) is True
    first = seen[0]
    assert first.is_relative_to(worktree_root.resolve())
    assert (first / "wip.txt").exists()
    assert api.bodies("/report")[-1]["state"] == "blocked"
    assert api.bodies("/worktree/cleanup") == []
    assert _journal(worktree_root) == []

    # The server re-claims with the persisted path, which is exactly the path
    # this host derives for the Ticket and branch: the dirty checkout resumes.
    bound = api.bodies("/worktree")[0]["worktree_path"]
    api.claims.append(
        _claim(ticket_id, base_sha, lease_id="lease-2", worktree_path=bound)
    )

    async def resumed(prompt, *, cwd=None, **_kwargs):
        seen.append(Path(cwd))
        seen.append(Path(cwd) / "wip.txt")
        assert (Path(cwd) / "wip.txt").read_text(encoding="utf-8") == "half done\n"
        return SimpleNamespace(text=completion_report_json("blocked", "still waiting", next_step="still waiting?"))

    with _adapter(resumed):
        assert await _run_once(api, worktree_root) is True
    assert seen[1] == first
    assert api.bodies("/worktree")[1]["worktree_path"] == str(first)
    assert api.bodies("/worktree")[1]["lease_id"] == "lease-2"
    assert (first / "wip.txt").exists()


@pytest.mark.asyncio
async def test_successful_and_failed_removal_report_different_outcomes(
    dispatch_repo: Path, worktree_root: Path
):
    """On core/main there is no cleanup route call at all (and the r1-6 WIP
    sent the same report for both); here the bodies differ by outcome."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ok_id, locked_id = _ticket(3), _ticket(4)
    api = FakeServer(
        _claim(ok_id, base_sha, lease_id="lease-ok"),
        _claim(locked_id, base_sha, lease_id="lease-locked"),
    )
    paths: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        path = Path(cwd)
        if not paths:
            paths["ok"] = path
        else:
            paths["locked"] = path
            # A real removal failure: git refuses to remove a locked worktree.
            _git(dispatch_repo, "worktree", "lock", str(path))
        _commit_work(Path(cwd))
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
        assert await _run_once(api, worktree_root) is True

    cleanup = api.bodies("/worktree/cleanup")
    assert cleanup[0] == {
        "lease_id": "lease-ok",
        "worktree_path": str(paths["ok"]),
        "worktree_generation": 1,
        "outcome": "removed",
        "detail": "",
    }
    assert cleanup[1]["outcome"] == "failed"
    assert cleanup[1]["lease_id"] == "lease-locked"
    assert cleanup[1]["worktree_path"] == str(paths["locked"])
    assert cleanup[1]["worktree_generation"] == 2
    assert "DaemonTicketWorktreeError" in cleanup[1]["detail"]
    assert not paths["ok"].exists()
    assert paths["locked"].exists()
    # The cleanup report goes to its own route, after the one terminal report.
    reports = api.bodies("/report")
    assert [body["state"] for body in reports] == ["ready", "ready"]
    assert all("worktree_cleanup" not in body for body in reports)
    order = [path for _m, path, _b in api.calls if "/tickets/" in path]
    assert order.index(f"{PREFIX}/tickets/{ok_id}/report") < order.index(
        f"{PREFIX}/tickets/{ok_id}/worktree/cleanup"
    )


@pytest.mark.asyncio
async def test_failed_removal_is_journaled_and_drain_retries_and_reports_removed(
    dispatch_repo: Path, worktree_root: Path
):
    """On core/main nothing is created or journaled, so there is nothing to
    drain and no 'removed' outcome is ever posted."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(5)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-5"))
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        (Path(cwd) / "result.txt").write_text("work\n", encoding="utf-8")
        _git(dispatch_repo, "worktree", "lock", str(cwd))
        raise RuntimeError("broke")

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    path = seen["path"]
    assert path.exists()
    [entry] = _journal(worktree_root)
    assert entry["ticket_id"] == ticket_id
    assert entry["worktree_path"] == str(path)
    assert entry["worktree_generation"] == 1
    assert entry["attempts"] == 1
    assert api.bodies("/worktree/cleanup")[-1]["outcome"] == "failed"

    # Not due yet: the drain backs off and sends nothing.
    assert await daemon._drain_pending_worktree_cleanups(api, str(worktree_root)) == 0
    assert len(api.bodies("/worktree/cleanup")) == 1

    _git(dispatch_repo, "worktree", "unlock", str(path))
    later = time.time() + 3600
    assert (
        await daemon._drain_pending_worktree_cleanups(
            api, str(worktree_root), now=later
        )
        == 1
    )
    assert not path.exists()
    assert api.bodies("/worktree/cleanup")[-1] == {
        "lease_id": "lease-5",
        "worktree_path": str(path),
        "worktree_generation": 1,
        "outcome": "removed",
        "detail": "",
    }
    assert _journal(worktree_root) == []
    # Dirty files were archived before the forced removal, not discarded.
    assert any((worktree_root / "recovery-artifacts").rglob("*"))


@pytest.mark.asyncio
async def test_unsent_cleanup_outcome_is_retried_by_the_drain(
    dispatch_repo: Path, worktree_root: Path
):
    """A removal that worked but whose report was lost is re-reported."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(6)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-6"))
    api.cleanup_errors = [daemon.ApiError("down", 503, True)]
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        _commit_work(Path(cwd))
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    assert not seen["path"].exists()
    assert len(_journal(worktree_root)) == 1
    later = time.time() + 3600
    assert (
        await daemon._drain_pending_worktree_cleanups(
            api, str(worktree_root), now=later
        )
        == 1
    )
    assert [body["outcome"] for body in api.bodies("/worktree/cleanup")] == [
        "removed",
        "removed",
    ]
    assert _journal(worktree_root) == []


@pytest.mark.asyncio
async def test_cleanup_409_drops_the_journal_without_touching_the_checkout(
    dispatch_repo: Path, worktree_root: Path
):
    """On core/main there is no journal or cleanup route to answer 409."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(7)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-7"))
    api.cleanup_errors = [daemon.ApiError("stale", 409, False)]
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        _git(dispatch_repo, "worktree", "lock", str(cwd))
        _commit_work(Path(cwd))
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    assert api.bodies("/worktree/cleanup")[-1]["outcome"] == "failed"
    assert _journal(worktree_root) == []
    assert seen["path"].exists()


@pytest.mark.asyncio
async def test_stale_journal_never_removes_a_newer_generation_checkout(
    dispatch_repo: Path, worktree_root: Path
):
    """Generation 1's journal meets ownership generation 2 on the same path."""
    ticket_id = _ticket(8)
    path = worktree_root / "live-checkout"
    path.mkdir()
    host_wt.write_host_ownership(
        worktree_root,
        ticket_id=ticket_id,
        worktree_path=str(path),
        lease_id="lease-new",
        generation=2,
    )
    host_wt.write_cleanup_journal(
        worktree_root,
        ticket_id=ticket_id,
        worktree_path=str(path),
        worktree_generation=1,
        lease_id="lease-old",
    )
    api = FakeServer()
    assert await daemon._drain_pending_worktree_cleanups(api, str(worktree_root)) == 1
    assert path.exists()
    assert api.calls == []
    assert _journal(worktree_root) == []
    assert host_wt.read_host_ownership(worktree_root, ticket_id)["generation"] == 2


@pytest.mark.asyncio
async def test_failed_terminal_report_leaves_no_journal_and_no_removal(
    dispatch_repo: Path, worktree_root: Path
):
    """On core/main no checkout exists to keep. The r1-6 WIP removal ran from
    a journal regardless of whether the server saw the ending."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(9)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-9"))
    api.report_error = daemon.ApiError("rejected", 422, False)
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        (Path(cwd) / "wip.txt").write_text("keep\n", encoding="utf-8")
        _commit_work(Path(cwd))
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    assert len(api.bodies("/report")) == 1
    assert seen["path"].exists()
    assert (seen["path"] / "wip.txt").exists()
    assert _journal(worktree_root) == []
    assert api.bodies("/worktree/cleanup") == []
    # A later poll has nothing to remove either.
    assert (
        await daemon._drain_pending_worktree_cleanups(
            api, str(worktree_root), now=time.time() + 3600
        )
        == 0
    )
    assert seen["path"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["prepare", "bind"])
@pytest.mark.parametrize(
    "report_error",
    [daemon.ApiError("conflict", 409, False), daemon.LeaseLost("lease gone")],
    ids=["api-error", "lease-lost"],
)
async def test_finish_raising_in_prepare_block_sends_exactly_one_report(
    dispatch_repo: Path, worktree_root: Path, failure: str, report_error: Exception
):
    """On core/main the spec is ignored and the CLI runs; the r1-6 WIP's broad
    handler around prepare re-called finish() and sent a second report."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    if failure == "prepare":
        base_sha = "0" * 40  # well formed, absent from the repository
    ticket_id = _ticket(10)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-10"))
    api.report_error = report_error
    if failure == "bind":
        api.bind_error = daemon.ApiError("fenced", 409, False)
    run = AsyncMock()
    with _adapter(run):
        try:
            await _run_once(api, worktree_root)
        except (daemon.ApiError, daemon.LeaseLost):
            pass
    run.assert_not_awaited()
    reports = api.bodies("/report")
    assert len(reports) == 1
    assert reports[0]["state"] == "failed"
    assert "Ticket worktree" in reports[0]["summary"]
    assert api.bodies("/worktree/cleanup") == []
    assert _journal(worktree_root) == []
    # A checkout cut for a failed bind is not left behind.
    assert not [p for p in worktree_root.rglob("*") if (p / ".git").is_file()]


@pytest.mark.asyncio
async def test_bind_failure_fails_the_launch_with_one_failed_report(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(11), base_sha, lease_id="lease-11"))
    api.bind_error = daemon.ApiError("fenced", 409, False)
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert report["lease_id"] == "lease-11"
    assert "bind" in report["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "-x",
        "../escape",
        "conductor/ticket/a..b",
        "refs/heads/x",
        "user-feature",
        "conductor/ticket/",
        "conductor/ticket/x@{1}",
    ],
)
async def test_unsafe_server_branch_fails_the_launch(
    dispatch_repo: Path, worktree_root: Path, branch: str
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(12), base_sha, branch=branch))
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    assert api.bodies("/worktree") == []
    assert api.bodies("/report")[0]["state"] == "failed"
    listing = _git(dispatch_repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 1  # only the main checkout


@pytest.mark.asyncio
async def test_missing_dispatch_repo_fails_closed(
    worktree_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("CONDUCTOR_DISPATCH_REPO", raising=False)
    api = FakeServer(_claim(_ticket(13), "a" * 40))
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    assert api.bodies("/report")[0]["state"] == "failed"
    assert "no dispatch repository" in api.bodies("/report")[0]["summary"]


@pytest.mark.asyncio
async def test_claim_lease_fields_drive_heartbeat_bind_report_and_cleanup(
    dispatch_repo: Path, worktree_root: Path
):
    """On core/main bind and cleanup are never called with the lease."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(14)
    api = FakeServer(
        _claim(ticket_id, base_sha, lease_id="lease-claimed", duration=0.9)
    )

    async def run(prompt, *, cwd=None, **_kwargs):
        await asyncio.sleep(0.8)  # long enough for the 0.9s lease to renew
        _commit_work(Path(cwd))
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    beats = [body for body in api.bodies("/heartbeat") if body]
    assert beats
    assert all(
        body["leases"] == [{"ticket_id": ticket_id, "lease_id": "lease-claimed"}]
        for body in beats
    )
    assert api.bodies("/worktree")[0]["lease_id"] == "lease-claimed"
    assert api.bodies("/report")[0]["lease_id"] == "lease-claimed"
    assert api.bodies("/worktree/cleanup")[0]["lease_id"] == "lease-claimed"


async def _run_until_stopped(
    api: FakeServer, worktree_root: Path, *, process_gone: bool
) -> Path:
    seen: dict[str, Path] = {}
    started = asyncio.Event()

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        started.set()
        await asyncio.sleep(60)

    async def watch(_api, _ticket_id, _poll, _cli):
        await started.wait()
        return {"status": "cancelled", "stop": {"verification": "pending"}}

    tracker = SimpleNamespace(
        gone=Mock(return_value=process_gone), evidence=Mock(return_value={})
    )
    with (
        _adapter(run),
        patch.object(daemon, "_watch_remote_cancel", side_effect=watch),
        patch.object(daemon, "_PROCESS_GONE_GRACE", 0.1),
        patch("src.llms.native_cli.process.ProcessTracker", return_value=tracker),
    ):
        assert await _run_once(api, worktree_root) is True
    return seen["path"]


@pytest.mark.asyncio
async def test_verified_stop_removes_and_reports(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(15), base_sha, lease_id="lease-15"))
    path = await _run_until_stopped(api, worktree_root, process_gone=True)
    assert api.bodies("/stop/ack")[0]["process_gone"] is True
    assert api.bodies("/report") == []
    assert not path.exists()
    assert api.bodies("/worktree/cleanup")[0]["outcome"] == "removed"


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["process-alive", "ack-rejected"])
async def test_unverified_stop_keeps_the_checkout(
    dispatch_repo: Path, worktree_root: Path, why: str
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(16), base_sha, lease_id="lease-16"))
    if why == "ack-rejected":
        api.stop_ack_error = daemon.ApiError("stale", 409, False)
    path = await _run_until_stopped(
        api, worktree_root, process_gone=why == "ack-rejected"
    )
    assert path.exists()
    assert api.bodies("/worktree/cleanup") == []
    assert _journal(worktree_root) == []


@pytest.mark.asyncio
async def test_legacy_cwd_outside_workspace_is_rejected(
    dispatch_repo: Path, worktree_root: Path
):
    foreign = worktree_root / "other-workspace-checkout"
    foreign.mkdir()
    api = FakeServer(
        {
            "ticket": {
                "ticket_id": _ticket(18),
                "lease_id": "lease-cwd",
                "lease_duration_seconds": 30,
                "worker_provider": "codex",
                "workspace_id": "test-workspace",
                "cwd": str(foreign),
                "worktree": None,
            },
            "prompt": "work",
        }
    )
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert "Ticket cwd" in report["summary"]


# --- review round 2: sandbox grants, path/branch adoption, drain, journal ---


def _sandbox_available() -> bool:
    import platform
    import shutil

    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _sandbox_available(),
    reason="per-file Git write grants are enforced by macOS sandbox-exec only",
)
async def test_sandboxed_worker_commits_on_its_branch_but_cannot_move_main(
    dispatch_repo: Path,
    worktree_root: Path,
    local_isolation_policy: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Under the daemon's real worker_isolation (sandbox-exec), the Worker can
    commit on its Ticket branch, but refs/heads/main, a sibling Ticket branch
    and the worktree admin's commondir are not writable."""
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
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    sibling = "conductor/ticket/sibling"
    _git(dispatch_repo, "branch", sibling, base_sha)
    common = (dispatch_repo / ".git").resolve()
    ticket_id = _ticket(40)
    branch = f"conductor/ticket/{ticket_id}"
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-40"))
    results: dict[str, subprocess.CompletedProcess[str]] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        git = gd._git_binary()
        identity = ("-c", "user.name=w", "-c", "user.email=w@example.invalid")
        gitdir = Path(_git(Path(cwd), "rev-parse", "--absolute-git-dir")).resolve()
        (Path(cwd) / "work.txt").write_text("work\n", encoding="utf-8")
        steps = {
            "add": (git, "add", "work.txt"),
            "commit": (git, *identity, "commit", "-qm", "worker commit"),
            "main": (git, "update-ref", "refs/heads/main", "HEAD"),
            "sibling": (git, "update-ref", f"refs/heads/{sibling}", "HEAD"),
            "direct": (
                "/bin/sh",
                "-c",
                f'echo {"f" * 40} > "{common}/refs/heads/main"',
            ),
            "commondir": (
                "/bin/sh",
                "-c",
                f'echo /tmp > "{gitdir}/commondir"',
            ),
        }
        for name, argv in steps.items():
            wrapped, env, run_cwd = prepare_process(argv, str(cwd), {})
            assert wrapped[0].endswith("sandbox-exec")
            results[name] = subprocess.run(
                wrapped, cwd=run_cwd, env=env, capture_output=True, text=True
            )
        return SimpleNamespace(text=completion_report_json("blocked", "need review", next_step="need review?"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True

    assert results["add"].returncode == 0, results["add"].stderr
    assert results["commit"].returncode == 0, results["commit"].stderr
    for name in ("main", "sibling", "direct", "commondir"):
        assert results[name].returncode != 0, name
    assert _git(dispatch_repo, "rev-parse", "main") == base_sha
    assert _git(dispatch_repo, "rev-parse", sibling) == base_sha
    tip = _git(dispatch_repo, "rev-parse", branch)
    assert tip != base_sha
    assert _git(dispatch_repo, "log", "-1", "--format=%s", tip) == "worker commit"
    assert _git(dispatch_repo, "reflog", "-1", "--format=%gs", branch).startswith(
        "commit:"
    )


@pytest.mark.asyncio
async def test_write_grants_name_files_not_the_ref_store(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(41)
    prepared = await daemon.prepare_host_worktree(
        ticket_id=ticket_id,
        spec=_claim(ticket_id, base_sha)["ticket"]["worktree"],
        worktrees_root=worktree_root,
        repo_root=dispatch_repo,
    )
    assert prepared is not None
    common = (dispatch_repo / ".git").resolve()
    # N67: no directory grant at all; objects go to a per-run quarantine.
    assert not hasattr(daemon, "linked_worktree_git_write_paths")
    files = daemon.linked_worktree_git_write_files(
        prepared.path, branch=prepared.branch, repo_root=dispatch_repo
    )
    ref = common / "refs" / "heads" / prepared.branch
    assert ref in files and Path(f"{ref}.lock") in files
    assert common / "logs" / "refs" / "heads" / prepared.branch in files
    assert all(path.is_relative_to(common) for path in files)
    assert not any(
        path.name in {"main", "main.lock", "packed-refs", "config", "commondir", "gitdir"}
        for path in files
    )
    with pytest.raises(host_wt.DaemonTicketWorktreeError):
        daemon.linked_worktree_git_write_files(
            prepared.path, branch="main", repo_root=dispatch_repo
        )


@pytest.mark.asyncio
async def test_foreign_checkout_under_root_is_never_adopted(
    dispatch_repo: Path, worktree_root: Path
):
    """A user/dispatch checkout under the daemon root, named as the persisted
    path, is neither reused nor removed (the r1-6 daemon reused it)."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    foreign = worktree_root / "someone-elses-checkout"
    _git(dispatch_repo, "worktree", "add", "-q", "-b", "user-feature", str(foreign), base_sha)
    (foreign / "precious.txt").write_text("user work\n", encoding="utf-8")
    with pytest.raises(host_wt.DaemonTicketWorktreeError):
        await daemon.prepare_host_worktree(
            ticket_id="T1",
            spec={"ticket_id": "T1", "branch": "user-feature", "base_sha": base_sha},
            persisted_path=str(foreign),
            worktrees_root=worktree_root,
            repo_root=dispatch_repo,
        )
    # Even with a Ticket branch, a persisted path that is not this host's
    # derived path for the Ticket is ignored: a fresh checkout is cut there.
    prepared = await daemon.prepare_host_worktree(
        ticket_id="T1",
        spec={"ticket_id": "T1", "branch": "conductor/ticket/T1", "base_sha": base_sha},
        persisted_path=str(foreign),
        worktrees_root=worktree_root,
        repo_root=dispatch_repo,
    )
    assert prepared is not None and prepared.created
    assert prepared.path == host_wt.ticket_worktree_path(
        worktree_root, dispatch_repo, "T1", "conductor/ticket/T1"
    )
    assert (foreign / "precious.txt").read_text(encoding="utf-8") == "user work\n"
    assert _git(foreign, "branch", "--show-current") == "user-feature"


@pytest.mark.asyncio
async def test_another_tickets_checkout_is_not_reused(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    a = await daemon.prepare_host_worktree(
        ticket_id="A",
        spec={"ticket_id": "A", "branch": "conductor/ticket/A", "base_sha": base_sha},
        worktrees_root=worktree_root,
        repo_root=dispatch_repo,
    )
    assert a is not None
    (a.path / "a-work.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(host_wt.DaemonTicketWorktreeError):
        await daemon.prepare_host_worktree(
            ticket_id="B",
            spec={"ticket_id": "B", "branch": "conductor/ticket/A", "base_sha": base_sha},
            persisted_path=str(a.path),
            worktrees_root=worktree_root,
            repo_root=dispatch_repo,
        )
    assert (a.path / "a-work.txt").exists()
    listing = _git(dispatch_repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 2  # main checkout + A only


@pytest.mark.asyncio
async def test_existing_branch_attaches_only_when_it_descends_from_base(
    dispatch_repo: Path, worktree_root: Path, tmp_path: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    # Descends: the branch has one commit on top of base_sha (an earlier,
    # removed checkout of the same Ticket).
    _git(dispatch_repo, "branch", "conductor/ticket/ok", base_sha)
    scratch = tmp_path / "scratch-ok"
    _git(dispatch_repo, "worktree", "add", "-q", str(scratch), "conductor/ticket/ok")
    (scratch / "x").write_text("x\n", encoding="utf-8")
    _git(scratch, "add", "x")
    _git(scratch, "commit", "-qm", "earlier attempt")
    _git(dispatch_repo, "worktree", "remove", str(scratch))
    tip = _git(dispatch_repo, "rev-parse", "conductor/ticket/ok")
    ok = await daemon.prepare_host_worktree(
        ticket_id="OK",
        spec={"ticket_id": "OK", "branch": "conductor/ticket/ok", "base_sha": base_sha},
        worktrees_root=worktree_root,
        repo_root=dispatch_repo,
    )
    assert ok is not None and _git(ok.path, "rev-parse", "HEAD") == tip

    # Does not descend: the branch sits on the old base, the pin moved on.
    _git(dispatch_repo, "branch", "conductor/ticket/stale", base_sha)
    (dispatch_repo / "README").write_text("moved\n", encoding="utf-8")
    _git(dispatch_repo, "commit", "-qam", "main moves")
    new_base = _git(dispatch_repo, "rev-parse", "HEAD")
    with pytest.raises(host_wt.DaemonTicketWorktreeError, match="descend"):
        await daemon.prepare_host_worktree(
            ticket_id="STALE",
            spec={
                "ticket_id": "STALE",
                "branch": "conductor/ticket/stale",
                "base_sha": new_base,
            },
            worktrees_root=worktree_root,
            repo_root=dispatch_repo,
        )
    stale_path = host_wt.ticket_worktree_path(
        worktree_root, dispatch_repo, "STALE", "conductor/ticket/stale"
    )
    assert not stale_path.exists()
    assert _git(dispatch_repo, "rev-parse", "conductor/ticket/stale") == base_sha


@pytest.mark.asyncio
async def test_pending_cleanup_drain_runs_while_the_lease_is_renewed(
    dispatch_repo: Path, worktree_root: Path
):
    """A slow drain at claim time must not let the new lease expire: it runs
    after the lease is registered, so heartbeats cover it."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(42)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-42", duration=0.9))
    during: list[int] = []

    async def slow_drain(_api, _root, **_kwargs):
        before = len(api.bodies("/heartbeat"))
        await asyncio.sleep(0.8)
        during.append(
            sum(
                1
                for body in api.bodies("/heartbeat")[before:]
                if body
                and {"ticket_id": ticket_id, "lease_id": "lease-42"}
                in body.get("leases", [])
            )
        )
        return 0

    async def run(prompt, *, cwd=None, **_kwargs):
        return SimpleNamespace(text=completion_report_json("blocked", "wait", next_step="wait?"))

    with (
        _adapter(run),
        patch.object(daemon, "_drain_pending_worktree_cleanups", side_effect=slow_drain),
    ):
        assert await _run_once(api, worktree_root) is True
    assert during and during[0] >= 1
    assert api.bodies("/report")[0]["state"] == "blocked"


def test_cleanup_journal_keeps_one_entry_per_generation(worktree_root: Path):
    for generation in (1, 2):
        host_wt.write_cleanup_journal(
            worktree_root,
            ticket_id="T",
            worktree_path=f"/x/{generation}",
            worktree_generation=generation,
            lease_id=f"lease-{generation}",
        )
    entries = sorted(_journal(worktree_root), key=lambda e: e["worktree_generation"])
    assert [e["worktree_generation"] for e in entries] == [1, 2]
    assert host_wt.read_cleanup_journal(worktree_root, "T", 1)["worktree_path"] == "/x/1"
    host_wt.drop_cleanup_journal(worktree_root, "T", generation=1)
    assert host_wt.read_cleanup_journal(worktree_root, "T", 1) is None
    assert host_wt.read_cleanup_journal(worktree_root, "T", 2)["lease_id"] == "lease-2"


def test_journal_and_ownership_writes_fsync_file_and_directory(
    worktree_root: Path, monkeypatch: pytest.MonkeyPatch
):
    import stat

    synced: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(host_wt.os, "fsync", recording_fsync)
    host_wt.write_cleanup_journal(
        worktree_root,
        ticket_id="T",
        worktree_path="/x",
        worktree_generation=3,
        lease_id="l",
    )
    assert synced == ["file", "dir"]
    synced.clear()
    host_wt.write_host_ownership(
        worktree_root, ticket_id="T", worktree_path="/x", lease_id="l", generation=3
    )
    assert synced == ["file", "dir"]
    journal = host_wt.journal_path(worktree_root, "T", 3)
    assert journal.stat().st_mode & 0o777 == 0o600


# --- review round 3: exact file grants, ref tampering, removal wait, refusals ---


@pytest.mark.asyncio
async def test_run_isolation_carries_exact_file_grants_not_a_private_swap(
    dispatch_repo: Path, worktree_root: Path
):
    """The Ticket ref is granted through worker_isolation's own
    runtime_write_files (no private _CONTEXT swap), and the macOS profile
    names it with a literal rule, never a subpath one."""
    from src.llms.native_cli import isolation

    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(50)
    branch = f"conductor/ticket/{ticket_id}"
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-50"))
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        current = isolation.current_worker_isolation()
        seen["files"] = current.runtime_write_files
        seen["dirs"] = current.runtime_write_paths
        seen["profile"] = isolation._mac_profile(
            current.workdir, current.home, current.root / "tmp"
        )
        return SimpleNamespace(text=completion_report_json("blocked", "wait", next_step="wait?"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    common = (dispatch_repo / ".git").resolve()
    ref = common / "refs" / "heads" / branch
    assert ref in seen["files"]
    assert common / "logs" / "refs" / "heads" / branch in seen["files"]
    assert not any(path.is_relative_to(common / "refs") for path in seen["dirs"])
    assert f'(literal "{ref}")' in seen["profile"]
    assert f'(subpath "{ref}")' not in seen["profile"]
    # No private isolation state is touched by the daemon any more.
    import inspect

    assert "_CONTEXT" not in inspect.getsource(daemon)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _sandbox_available(),
    reason="per-file Git write grants are enforced by macOS sandbox-exec only",
)
@pytest.mark.parametrize("swap", ["symlink", "directory"])
async def test_ticket_ref_swapped_inside_the_sandbox_refuses_the_result(
    dispatch_repo: Path,
    worktree_root: Path,
    local_isolation_policy: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap: str,
):
    """rv11/r2daemon/probe_objects.py: under the real sandbox a Worker may
    replace its granted ref name with a symlink to refs/heads/main (or a
    directory). The daemon lstats the ref after the run and refuses the
    result instead of trusting (or committing through) it."""
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
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(51)
    branch = f"conductor/ticket/{ticket_id}"
    common = (dispatch_repo / ".git").resolve()
    ref = common / "refs" / "heads" / branch
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-51"))
    results: dict[str, subprocess.CompletedProcess[str]] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        git = gd._git_binary()
        identity = ("-c", "user.name=w", "-c", "user.email=w@example.invalid")
        (Path(cwd) / "work.txt").write_text("work\n", encoding="utf-8")
        if swap == "symlink":
            tamper = f'rm "{ref}" && ln -s ../../../main "{ref}"'
        else:
            tamper = f'rm "{ref}" && mkdir "{ref}"'
        steps = {
            "add": (git, "add", "work.txt"),
            "commit": (git, *identity, "commit", "-qm", "worker commit"),
            "tamper": ("/bin/sh", "-c", tamper),
            # Below the swapped-in directory nothing is granted (literal rule).
            "below": ("/bin/sh", "-c", f'echo {"f" * 40} > "{ref}/evil"'),
        }
        for name, argv in steps.items():
            wrapped, env, run_cwd = prepare_process(argv, str(cwd), {})
            assert wrapped[0].endswith("sandbox-exec")
            results[name] = subprocess.run(
                wrapped, cwd=run_cwd, env=env, capture_output=True, text=True
            )
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True

    assert results["commit"].returncode == 0, results["commit"].stderr
    assert results["tamper"].returncode == 0, results["tamper"].stderr
    assert results["below"].returncode != 0
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert "refused the Worker result" in report["summary"]
    assert swap in report["summary"]
    assert _git(dispatch_repo, "rev-parse", "main") == base_sha
    assert not (ref / "evil").exists()


def test_ticket_ref_tamper_reason_flags_symlink_dir_hardlink_and_head(tmp_path: Path):
    common = tmp_path / "common"
    admin = common / "worktrees" / "t"
    admin.mkdir(parents=True)
    branch = "conductor/ticket/T"
    ref = common / "refs" / "heads" / branch
    reflog = common / "logs" / "refs" / "heads" / branch
    ref.parent.mkdir(parents=True)
    reflog.parent.mkdir(parents=True)
    main = common / "refs" / "heads" / "main"
    main.write_text("a" * 40 + "\n")
    ref.write_text("b" * 40 + "\n")
    reflog.write_text("log\n")
    (admin / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    assert host_wt.ticket_ref_tamper_reason(common, admin, branch) == ""

    ref.unlink()
    ref.symlink_to(main)
    assert "symlink" in host_wt.ticket_ref_tamper_reason(common, admin, branch)
    ref.unlink()
    ref.mkdir()
    assert "directory" in host_wt.ticket_ref_tamper_reason(common, admin, branch)
    ref.rmdir()
    os.link(main, ref)
    assert "hard-linked" in host_wt.ticket_ref_tamper_reason(common, admin, branch)
    ref.unlink()
    ref.write_text("b" * 40 + "\n")
    reflog.unlink()
    reflog.symlink_to(main)
    assert "reflog" in host_wt.ticket_ref_tamper_reason(common, admin, branch)
    reflog.unlink()
    (admin / "HEAD").write_text("ref: refs/heads/main\n")
    assert "HEAD" in host_wt.ticket_ref_tamper_reason(common, admin, branch)


def _slow_tracker(gone_after: int | None) -> SimpleNamespace:
    """A Worker tree that reports alive for ``gone_after`` polls (None: forever)."""
    polls = {"n": 0}

    def gone() -> bool:
        polls["n"] += 1
        return gone_after is not None and polls["n"] > gone_after

    return SimpleNamespace(gone=gone, evidence=Mock(return_value={}), polls=polls)


@pytest.mark.asyncio
async def test_cli_error_removal_waits_for_the_worker_tree_to_exit(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(52), base_sha, lease_id="lease-52"))
    tracker = _slow_tracker(gone_after=3)
    seen: dict[str, Path] = {}
    removal_polls: list[int] = []
    real_remove = daemon.remove_ticket_worktree

    async def remove(**kwargs):
        removal_polls.append(tracker.polls["n"])
        return await real_remove(**kwargs)

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        raise RuntimeError("cli crashed")

    with (
        _adapter(run),
        patch.object(daemon, "_PROCESS_GONE_GRACE", 2.0),
        patch.object(daemon, "remove_ticket_worktree", side_effect=remove),
        patch("src.llms.native_cli.process.ProcessTracker", return_value=tracker),
    ):
        assert await _run_once(api, worktree_root) is True
    assert api.bodies("/report")[0]["state"] == "failed"
    # Removal ran only after the tree was reported gone.
    assert removal_polls and removal_polls[0] > 3
    assert not seen["path"].exists()
    assert api.bodies("/worktree/cleanup")[0]["outcome"] == "removed"


@pytest.mark.asyncio
async def test_cli_error_with_a_surviving_worker_defers_removal(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(53)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-53"))
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        raise RuntimeError("cli crashed")

    with (
        _adapter(run),
        patch.object(daemon, "_PROCESS_GONE_GRACE", 0.1),
        patch(
            "src.llms.native_cli.process.ProcessTracker",
            return_value=_slow_tracker(gone_after=None),
        ),
    ):
        assert await _run_once(api, worktree_root) is True
    assert api.bodies("/report")[0]["state"] == "failed"
    assert seen["path"].exists()
    assert api.bodies("/worktree/cleanup") == []
    [entry] = _journal(worktree_root)
    assert entry["ticket_id"] == ticket_id
    assert entry["next_attempt_at"] > time.time()


@pytest.mark.asyncio
async def test_cancelled_run_removal_waits_for_the_worker_tree(
    dispatch_repo: Path, worktree_root: Path
):
    """Daemon task cancelled mid-run: the cancelled report is accepted, but a
    Worker tree still alive keeps the checkout (removal deferred)."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(54), base_sha, lease_id="lease-54"))
    started = asyncio.Event()
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        started.set()
        await asyncio.sleep(60)

    with (
        _adapter(run),
        patch.object(daemon, "_PROCESS_GONE_GRACE", 0.1),
        patch(
            "src.llms.native_cli.process.ProcessTracker",
            return_value=_slow_tracker(gone_after=None),
        ),
    ):
        task = asyncio.create_task(_run_once(api, worktree_root))
        await asyncio.wait_for(started.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert api.bodies("/report")[0]["summary"].startswith("Local daemon was cancelled")
    assert seen["path"].exists()
    assert api.bodies("/worktree/cleanup") == []
    assert len(_journal(worktree_root)) == 1


@pytest.mark.asyncio
async def test_diverged_resume_branch_blocks_with_the_operator_fix(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(55)
    branch = f"conductor/ticket/{ticket_id}"
    _git(dispatch_repo, "switch", "-q", "--orphan", "elsewhere")
    (dispatch_repo / "o").write_text("o\n", encoding="utf-8")
    _git(dispatch_repo, "add", "o")
    _git(dispatch_repo, "commit", "-qm", "unrelated")
    _git(dispatch_repo, "branch", branch, "HEAD")
    _git(dispatch_repo, "switch", "-q", "main")
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-55"))
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    [report] = api.bodies("/report")
    assert report["state"] == "blocked"
    assert "no longer descends" in report["summary"]
    assert f"git branch -D {branch}" in report["summary"]
    assert branch in report["question"]
    assert "new Ticket" in report["question"]
    assert api.bodies("/worktree") == []
    assert api.bodies("/worktree/cleanup") == []


@pytest.mark.asyncio
async def test_non_macos_writable_worktree_is_refused_before_checkout(
    dispatch_repo: Path, worktree_root: Path, monkeypatch: pytest.MonkeyPatch
):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(56), base_sha, lease_id="lease-56"))
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert "per-file Git write grants" in report["summary"]
    assert "macOS" in report["summary"]
    assert api.bodies("/worktree") == []
    assert [p for p in worktree_root.rglob("*") if p.is_dir() and (p / ".git").exists()] == []


# --- Review round 4 (line D) -------------------------------------------------


def _http_error(code: int, retry_after: str | None):
    import email.message
    import io
    import urllib.error

    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "http://fake.invalid/x", code, "busy", headers, io.BytesIO(b"")
    )


def test_retry_after_parsing_is_bounded_and_ignores_garbage():
    from email.utils import format_datetime
    from datetime import datetime, timezone

    assert daemon._parse_retry_after("40") == 40.0
    assert daemon._parse_retry_after(" 0 ") == 0.0
    assert daemon._parse_retry_after("100000") == daemon._RETRY_AFTER_MAX
    assert daemon._parse_retry_after("9" * 5000) == daemon._RETRY_AFTER_MAX
    for garbage in (None, "", "soon", "-5", "1.5", "4e2", "١٢"):
        assert daemon._parse_retry_after(garbage) is None, garbage
    now = 1_900_000_000.0
    when = format_datetime(datetime.fromtimestamp(now + 120, timezone.utc), usegmt=True)
    assert daemon._parse_retry_after(when, now=now) == pytest.approx(120.0)
    past = format_datetime(datetime.fromtimestamp(now - 120, timezone.utc), usegmt=True)
    assert daemon._parse_retry_after(past, now=now) == 0.0


@pytest.mark.parametrize(
    ("header", "low", "high"),
    [
        ("40", 40.0, 40.0),
        ("100000", 300.0, 300.0),
        ("garbage", 0.0, daemon._BACKOFF_MAX),
        (None, 0.0, daemon._BACKOFF_MAX),
    ],
)
@pytest.mark.asyncio
async def test_claim_503_waits_at_least_the_servers_retry_after(header, low, high):
    """A 503 from the claim endpoint with Retry-After: 40 must not be retried
    after the daemon's own (<= 5 s) backoff. The sleep is patched, so the
    test asserts the requested wait, not wall time."""
    api = daemon.Api("http://fake.invalid", "token", timeout=5.0)
    response = Mock()
    response.status = 200
    response.read.return_value = b'{"ticket": null}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch.object(
            daemon.urllib.request,
            "urlopen",
            side_effect=[_http_error(503, header), response],
        ),
        patch.object(daemon.asyncio, "sleep", side_effect=fake_sleep),
    ):
        payload = await daemon._retry_call(api.claim, "claim-1")
    assert payload == {"ticket": None}
    [waited] = sleeps
    assert low <= waited <= high


@pytest.mark.asyncio
async def test_retry_after_is_still_bounded_by_the_lease_deadline():
    calls = {"n": 0}

    def flaky(*_args, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise daemon.ApiError("busy", 503, True, retry_after=200.0)
        return "ok"

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    deadline = time.monotonic() + 30.0
    with patch.object(daemon.asyncio, "sleep", side_effect=fake_sleep):
        assert await daemon._retry_call(flaky, deadline=deadline) == "ok"
    assert 0 < sleeps[0] <= 30.0


def _worker_commits_then_detaches(dispatch_repo: Path, cwd: Path) -> str:
    (cwd / "work.txt").write_text("work\n", encoding="utf-8")
    _git(cwd, "add", "work.txt")
    _git(cwd, "commit", "-qm", "branch work")
    _git(cwd, "switch", "-q", "--detach")
    _git(cwd, "commit", "-q", "--allow-empty", "-m", "detached work")
    return _git(cwd, "rev-parse", "HEAD")


@pytest.mark.asyncio
async def test_detached_head_after_run_blocks_and_keeps_the_checkout(
    dispatch_repo: Path, worktree_root: Path
):
    """rv11/r3daemon/test_probe_r3_items.py (switch-detach-commit): a Worker
    that leaves a plain detached HEAD is blocked, not refused, and its checkout
    (with the only copy of the detached commits) is kept."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(90)
    branch = f"conductor/ticket/{ticket_id}"
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-90"))
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        seen["head"] = _worker_commits_then_detaches(dispatch_repo, Path(cwd))
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    assert report["state"] == "blocked"
    assert "detached HEAD" in report["summary"]
    question = report["question"]
    assert f"git switch {branch}" in question
    assert "git switch -c" in question
    assert f"git merge {seen['head']}" in question
    for discarding in ("reset", "branch -f", "branch -D", "checkout -f", "--discard"):
        assert discarding not in question
        assert discarding not in report["summary"]
    assert seen["path"].exists()
    assert _git(seen["path"], "rev-parse", "HEAD") == seen["head"]
    assert api.bodies("/worktree/cleanup") == []
    assert _journal(worktree_root) == []
    assert _git(dispatch_repo, "rev-parse", "main") == base_sha

    # Resumed before the operator acted: blocked again, the Worker never runs.
    bound = api.bodies("/worktree")[0]["worktree_path"]
    api.claims.append(_claim(ticket_id, base_sha, lease_id="lease-91", worktree_path=bound))
    idle = AsyncMock()
    with _adapter(idle):
        assert await _run_once(api, worktree_root) is True
    idle.assert_not_awaited()
    assert api.bodies("/report")[-1]["state"] == "blocked"
    assert f"git switch {branch}" in api.bodies("/report")[-1]["question"]
    assert seen["path"].exists()

    # The operator follows the question; the next resume runs on the branch
    # and the detached commit is on it.
    _git(seen["path"], "switch", "-q", "-c", "saved-detached")
    _git(seen["path"], "switch", "-q", branch)
    _git(seen["path"], "merge", "-q", "--ff-only", "saved-detached")
    api.claims.append(_claim(ticket_id, base_sha, lease_id="lease-92", worktree_path=bound))
    ran = AsyncMock(return_value=SimpleNamespace(text=completion_report_json("blocked", "wait", next_step="wait?")))
    with _adapter(ran):
        assert await _run_once(api, worktree_root) is True
    ran.assert_awaited_once()
    assert _git(dispatch_repo, "rev-parse", branch) == seen["head"]


@pytest.mark.asyncio
async def test_detached_at_base_after_run_is_blocked_not_refused(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(93)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-93"))
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        _git(Path(cwd), "checkout", "-q", base_sha)
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    assert report["state"] == "blocked"
    assert "refused" not in report["summary"]
    assert seen["path"].exists()
    assert api.bodies("/worktree/cleanup") == []


@pytest.mark.parametrize("head", ["other-ref", "symlink"])
@pytest.mark.asyncio
async def test_head_naming_another_ref_or_symlinked_stays_refused(
    dispatch_repo: Path, worktree_root: Path, head: str
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(94 if head == "other-ref" else 95)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-94"))

    async def run(prompt, *, cwd=None, **_kwargs):
        admin = Path(_git(Path(cwd), "rev-parse", "--absolute-git-dir"))
        if head == "other-ref":
            (admin / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        else:
            target = admin / "HEAD.target"
            target.write_text(base_sha + "\n", encoding="utf-8")
            (admin / "HEAD").unlink()
            (admin / "HEAD").symlink_to(target)
        return SimpleNamespace(text=completion_report_json(summary="done"))

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert "refused the Worker result" in report["summary"]


def test_tamper_reason_accepts_only_a_plain_detached_head(tmp_path: Path):
    common = tmp_path / "common"
    admin = common / "worktrees" / "t"
    admin.mkdir(parents=True)
    branch = "conductor/ticket/T"
    head = admin / "HEAD"
    head.write_text("c" * 40 + "\n")
    assert "HEAD" in host_wt.ticket_ref_tamper_reason(common, admin, branch)
    assert host_wt.ticket_ref_tamper_reason(common, admin, branch, allow_detached=True) == ""
    assert host_wt.detached_head_commit(admin) == "c" * 40
    head.write_text("ref: refs/heads/main\n")
    assert host_wt.detached_head_commit(admin) == ""
    assert "HEAD" in host_wt.ticket_ref_tamper_reason(
        common, admin, branch, allow_detached=True
    )
    target = admin / "elsewhere"
    target.write_text("c" * 40 + "\n")
    head.unlink()
    head.symlink_to(target)
    assert host_wt.detached_head_commit(admin) == ""
    assert "symlink" in host_wt.ticket_ref_tamper_reason(
        common, admin, branch, allow_detached=True
    )


@pytest.mark.asyncio
async def test_cli_error_on_a_detached_head_keeps_the_unique_commits(
    dispatch_repo: Path, worktree_root: Path
):
    """The CLI-error path has no result to block, but the accepted failed
    ending must not remove the only checkout that carries detached commits."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(96)
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-96"))
    seen: dict[str, Any] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        seen["head"] = _worker_commits_then_detaches(dispatch_repo, Path(cwd))
        raise RuntimeError("cli crashed")

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    assert api.bodies("/report")[0]["state"] == "failed"
    assert seen["path"].exists()
    assert _git(seen["path"], "rev-parse", "HEAD") == seen["head"]
    [cleanup] = api.bodies("/worktree/cleanup")
    assert cleanup["outcome"] == "failed"
    assert "detached HEAD" in cleanup["detail"]
    assert len(_journal(worktree_root)) == 1


def test_diverged_branch_hint_never_resets_the_branch_onto_the_base():
    branch = "conductor/ticket/T"
    base = "a" * 40
    message = str(host_wt.TicketBranchDivergedError("T", branch, base))
    question = host_wt.diverged_branch_question(branch)
    for text in (message, question):
        assert "branch -f" not in text
        assert "reset" not in text
        assert "rebase" in text.lower()
        assert "new Ticket" in text
    assert f"git rebase {base} {branch}" in message
    # Deleting is offered only after the commits are saved elsewhere.
    assert message.index(f"git branch <backup-name> {branch}") < message.index(
        f"git branch -D {branch}"
    )


@pytest.mark.asyncio
async def test_diverged_resume_report_offers_no_discarding_command(
    dispatch_repo: Path, worktree_root: Path
):
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(97)
    branch = f"conductor/ticket/{ticket_id}"
    _git(dispatch_repo, "switch", "-q", "--orphan", "elsewhere")
    (dispatch_repo / "o").write_text("o\n", encoding="utf-8")
    _git(dispatch_repo, "add", "o")
    _git(dispatch_repo, "commit", "-qm", "unrelated")
    _git(dispatch_repo, "branch", branch, "HEAD")
    _git(dispatch_repo, "switch", "-q", "main")
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-97"))
    with _adapter(AsyncMock()):
        assert await _run_once(api, worktree_root) is True
    [report] = api.bodies("/report")
    assert report["state"] == "blocked"
    for field in ("summary", "question"):
        assert "branch -f" not in report[field]
        assert "rebase" in report[field].lower()


@pytest.mark.asyncio
async def test_symlinked_ticket_ref_is_not_resumed(
    dispatch_repo: Path, worktree_root: Path
):
    """rv11/r3daemon/probe_c (symref-resume): after a CLI-error run swapped the
    Ticket ref for a symlink to refs/heads/main, a resume must not check main
    out as the Ticket branch."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(98)
    branch = f"conductor/ticket/{ticket_id}"
    _git(dispatch_repo, "branch", branch, base_sha)
    ref = dispatch_repo / ".git" / "refs" / "heads" / branch
    assert ref.is_file()
    ref.unlink()
    ref.symlink_to("refs/heads/main")
    api = FakeServer(_claim(ticket_id, base_sha, lease_id="lease-98"))
    run = AsyncMock()
    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    run.assert_not_awaited()
    [report] = api.bodies("/report")
    assert report["state"] == "failed"
    assert api.bodies("/worktree") == []
    assert [p for p in worktree_root.rglob("*") if p.is_dir() and (p / ".git").exists()] == []
    with pytest.raises(host_wt.DaemonTicketWorktreeError, match="symlink"):
        await daemon.prepare_host_worktree(
            ticket_id=ticket_id,
            spec={"ticket_id": ticket_id, "branch": branch, "base_sha": base_sha},
            worktrees_root=str(worktree_root),
        )


@pytest.mark.asyncio
async def test_journal_drain_waits_for_a_surviving_worker_process(
    dispatch_repo: Path, worktree_root: Path
):
    """rv11/r3daemon/probe_sf1.py (bg-grandchild-long, term-ignoring-writer):
    a deferred removal journals the Worker processes, and the drain (which has
    no tracker) keeps the checkout while any of them is alive."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    ticket_id = _ticket(99)
    prepared = await daemon.prepare_host_worktree(
        ticket_id=ticket_id,
        spec={"ticket_id": ticket_id, "branch": f"conductor/ticket/{ticket_id}", "base_sha": base_sha},
        worktrees_root=str(worktree_root),
    )
    survivor = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        tracker = SimpleNamespace(
            gone=lambda: False,
            evidence=lambda: {
                "processes": [
                    # The reaped leader (pid may be reissued: only its group counts)
                    {"pid": 999_999, "pgid": None, "exited": True, "alive": False},
                    {"pid": survivor.pid, "pgid": survivor.pid, "exited": False, "alive": True},
                ]
            },
        )
        api = FakeServer()
        binding = {"path": str(prepared.path), "generation": 1, "lease_id": "lease-99"}
        with patch.object(daemon, "_PROCESS_GONE_GRACE", 0.0):
            assert (
                await daemon._cleanup_after_accepted_ending(
                    api, str(worktree_root), ticket_id, binding, tracker=tracker
                )
                == "deferred"
            )
        [entry] = _journal(worktree_root)
        assert {row["pid"] for row in entry["processes"]} == {999_999, survivor.pid}
        later = time.time() + 3600
        assert await daemon._drain_pending_worktree_cleanups(api, str(worktree_root), now=later) == 0
        assert prepared.path.exists()
        assert api.bodies("/worktree/cleanup") == []
        [entry] = _journal(worktree_root)
        assert entry["attempts"] == 2
        assert entry["processes"]
    finally:
        survivor.kill()
        survivor.wait()
    later = time.time() + 7200
    assert await daemon._drain_pending_worktree_cleanups(api, str(worktree_root), now=later) == 1
    assert not prepared.path.exists()
    assert api.bodies("/worktree/cleanup")[-1]["outcome"] == "removed"
    assert _journal(worktree_root) == []


@pytest.mark.asyncio
async def test_cli_error_on_a_detached_head_the_branch_carries_is_removed(
    dispatch_repo: Path, worktree_root: Path
):
    """Detached at a commit the Ticket branch already has (checkout <base>):
    nothing would be lost, so the accepted ending removes the checkout."""
    base_sha = _git(dispatch_repo, "rev-parse", "HEAD")
    api = FakeServer(_claim(_ticket(100), base_sha, lease_id="lease-100"))
    seen: dict[str, Path] = {}

    async def run(prompt, *, cwd=None, **_kwargs):
        seen["path"] = Path(cwd)
        (Path(cwd) / "work.txt").write_text("work\n", encoding="utf-8")
        _git(Path(cwd), "add", "work.txt")
        _git(Path(cwd), "commit", "-qm", "branch work")
        _git(Path(cwd), "checkout", "-q", base_sha)
        raise RuntimeError("cli crashed")

    with _adapter(run):
        assert await _run_once(api, worktree_root) is True
    assert not seen["path"].exists()
    assert api.bodies("/worktree/cleanup")[-1]["outcome"] == "removed"
    assert _journal(worktree_root) == []


@pytest.fixture(autouse=True)
def _current_server_claims(daemon_claims_have_completion_contract):
    """Claims here stand for a server that sends the completion contract."""
