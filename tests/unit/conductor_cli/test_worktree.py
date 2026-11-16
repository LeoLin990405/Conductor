from pathlib import Path

import pytest

from src.conductor_cli.worktree import DEFAULT_WORKTREE_ROOT, resolve_ticket_worktree


def test_default_root_and_relative_path_use_operator_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".conductor" / "worktrees"
    selected = root / "repo"
    selected.mkdir(parents=True)

    assert resolve_ticket_worktree("repo") == selected.resolve()
    assert DEFAULT_WORKTREE_ROOT == Path("~/.conductor/worktrees")


def test_parent_traversal_cannot_escape_configured_root(tmp_path: Path):
    root = tmp_path / "worktrees"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="under the configured worktree root"):
        resolve_ticket_worktree("../outside", root)


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_or_non_string_explicit_path_is_rejected(value, tmp_path: Path):
    root = tmp_path / "worktrees"
    root.mkdir()

    with pytest.raises(ValueError, match="non-empty path"):
        resolve_ticket_worktree(value, root)
