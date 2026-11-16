"""Git delivery accepts the macOS /var and /tmp alias spellings (P0 sandbox)."""

import sys
from pathlib import Path

import pytest

from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    WorkspaceDeliveryLock,
    _valid_task6_worktree_add,
    _valid_task6_worktree_remove,
)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases")


def _alias(path: Path) -> Path:
    text = str(path)
    for name in ("var", "tmp"):
        if text.startswith(f"/private/{name}/"):
            return Path(text.replace(f"/private/{name}", f"/{name}", 1))
    pytest.skip("tmp_path is not under a macOS system alias")


def _user_link(tmp_path: Path) -> Path:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    return link


def test_lock_root_spelled_through_var_alias_is_accepted(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    lock = WorkspaceDeliveryLock(
        str(work), lock_root=str(_alias(tmp_path) / "locks"), workspace_root=str(work), timeout=1
    )
    assert lock.lock_root == tmp_path / "locks"
    with lock:
        assert any((tmp_path / "locks").iterdir())


def test_lock_root_env_spelled_through_var_alias_is_accepted(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_ROOT", str(_alias(tmp_path) / "env-locks"))
    with WorkspaceDeliveryLock(str(work), workspace_root=str(work), timeout=1):
        pass
    assert (tmp_path / "env-locks").is_dir()


def test_lock_root_under_user_symlink_is_still_refused(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    link = _user_link(tmp_path)
    for base in (link, _alias(link)):
        lock = WorkspaceDeliveryLock(
            str(work), lock_root=str(base / "locks"), workspace_root=str(work), timeout=1
        )
        with pytest.raises(GitDeliveryError, match="unsafe"):
            lock.acquire()


def test_task6_worktree_target_spelled_through_var_alias_is_accepted(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    target = str(_alias(root) / "ticket-1")
    assert _valid_task6_worktree_add(("worktree", "add", target, "main"), 2, root)
    assert _valid_task6_worktree_remove(("worktree", "remove", target), 2, root)


def test_task6_worktree_target_under_user_symlink_is_still_refused(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    (root / "real").mkdir()
    (root / "link").symlink_to(root / "real")
    for base in (root, _alias(root)):
        target = str(base / "link" / "ticket-1")
        assert not _valid_task6_worktree_add(("worktree", "add", target, "main"), 2, root)
        assert not _valid_task6_worktree_remove(("worktree", "remove", target), 2, root)
