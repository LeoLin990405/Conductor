"""Apply safety: no write on dry run, symlink refusal, lock, atomic replace."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from src.mcp_hub import McpHubError, Registry, Server, apply_all, diff_all, save_registry
from src.mcp_hub import apply as apply_mod


def _server(home: Path) -> None:
    save_registry(
        home,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed",
                    command="uvx",
                    args=["pkg"],
                    targets=("cursor",),
                )
            }
        ),
    )


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    _server(tmp_path)
    target = tmp_path / ".cursor" / "mcp.json"
    before = list(tmp_path.rglob("*"))
    preview = apply_all(tmp_path, apply=False)
    assert preview == diff_all(tmp_path)
    assert preview["cursor"]["unified_diff"]
    assert not target.exists()
    after = list(tmp_path.rglob("*"))
    assert {p.relative_to(tmp_path) for p in after} == {p.relative_to(tmp_path) for p in before}


def test_symlink_config_outside_home_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    leaked = outside / "claude.json"
    original = b'{"userID": "keep", "mcpServers": {}}\n'
    leaked.write_bytes(original)
    (home / ".claude.json").symlink_to(leaked)
    save_registry(
        home,
        Registry(servers={"hubbed": Server(name="hubbed", command="uvx", targets=("claude",))}),
    )
    with pytest.raises(McpHubError, match="symlink"):
        apply_all(home, apply=True)
    assert leaked.read_bytes() == original


def test_parent_symlink_outside_home_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".cursor").symlink_to(outside)
    save_registry(
        home,
        Registry(servers={"hubbed": Server(name="hubbed", command="uvx", targets=("cursor",))}),
    )
    with pytest.raises(McpHubError, match="symlink"):
        apply_all(home, apply=True)
    assert not (outside / "mcp.json").exists()


def test_interrupted_replace_keeps_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server(tmp_path)
    target = tmp_path / ".cursor" / "mcp.json"
    target.parent.mkdir(parents=True)
    original = b'{"mcpServers": {"hand": {"command": "echo"}}}\n'
    target.write_bytes(original)

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected before replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError, match="injected"):
        apply_all(tmp_path, apply=True)
    assert target.read_bytes() == original


def test_concurrent_apply_one_side_errors(tmp_path: Path) -> None:
    _server(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        entered.set()
        assert release.wait(5)

    apply_mod.hold_lock_hook = hold
    errors: list[BaseException] = []
    done = threading.Event()

    def first() -> None:
        try:
            apply_all(tmp_path, apply=True)
        except BaseException as exc:  # noqa: BLE001 - the test records either outcome
            errors.append(exc)
        finally:
            done.set()

    def second() -> None:
        assert entered.wait(5)
        try:
            apply_all(tmp_path, apply=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        left = threading.Thread(target=first)
        right = threading.Thread(target=second)
        left.start()
        right.start()
        right.join(5)
        release.set()
        left.join(5)
        assert not left.is_alive()
        assert not right.is_alive()
    finally:
        apply_mod.hold_lock_hook = None
        release.set()
    assert len(errors) == 1
    assert isinstance(errors[0], McpHubError)
    assert "in progress" in str(errors[0])
    written = (tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    assert "hubbed" in written
    assert written.count("{") == written.count("}")
