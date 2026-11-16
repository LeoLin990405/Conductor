"""Red-team probes for the MCP hub.

A probe passes only when the attack is blocked. Do not loosen an assertion
to match a vulnerable result. Every path stays under tmp_path.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from src.mcp_hub import (
    McpHubError,
    Registry,
    Server,
    apply_all,
    render_agent,
    rollback,
    save_registry,
)

_SECRET = "sk-" "live-secret"
_SIBLING = "honest-user"
_FIXTURE = """{
  "userID": "honest-user",
  "mcpServers": {
    "kept": {
      "type": "stdio",
      "command": "echo",
      "args": ["kept"]
    }
  }
}
"""
_EVIL_NAMES = (
    'ok"\n[mcp_servers.evil]',
    "a]\nuserID=pwned",
)


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process_home = tmp_path / "process-home"
    process_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(process_home))


def _keys(node: object, found: set[str] | None = None) -> set[str]:
    if found is None:
        found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key))
            _keys(value, found)
    elif isinstance(node, list):
        for item in node:
            _keys(item, found)
    return found


def _assert_sibling(text: str, evil: str) -> None:
    data = json.loads(text)
    assert data["userID"] == _SIBLING
    assert evil not in _keys(data)


def _write_fixture(home: Path, mode: int = 0o644) -> Path:
    path = home / ".claude.json"
    path.write_text(_FIXTURE, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _prepare(home: Path, mode: int = 0o644) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = _write_fixture(home, mode)
    server = Server(name="ok", command="echo", args=["added"])
    save_registry(home, Registry(servers={"ok": server}))
    return path


def _files_containing(root: Path, needle: str) -> list[str]:
    raw = needle.encode("utf-8")
    hits: list[str] = []
    if not root.exists():
        return hits
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if raw in path.read_bytes():
                hits.append(str(path))
    return hits


def test_hostile_server_names_do_not_change_sibling_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    for index, name in enumerate(_EVIL_NAMES):
        home = tmp_path / f"home-{index}"
        home.mkdir()
        path = _write_fixture(home)
        before = path.read_bytes()
        try:
            server = Server(name=name, command="echo", args=["x"])
        except McpHubError:
            assert path.read_bytes() == before
            _assert_sibling(path.read_text(encoding="utf-8"), name)
            continue
        save_registry(home, Registry(servers={server.name: server}))
        view = render_agent(home, "claude")
        rendered = view.get("rendered")
        if isinstance(rendered, str):
            _assert_sibling(rendered, name)
        apply_all(home, apply=True)
        _assert_sibling(path.read_text(encoding="utf-8"), name)


def test_apply_refuses_config_symlink_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.json"
    payload = _FIXTURE.encode("utf-8")
    outside.write_bytes(payload)
    os.chmod(outside, 0o600)
    (home / ".claude.json").symlink_to(outside)
    server = Server(name="ok", command="echo", args=["added"])
    save_registry(home, Registry(servers={"ok": server}))
    before = outside.read_bytes()

    with pytest.raises(McpHubError):
        apply_all(home, apply=True)

    assert outside.read_bytes() == before
    assert (home / ".claude.json").is_symlink()


def test_plaintext_env_secret_never_lands_in_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _write_fixture(home)

    with pytest.raises(McpHubError):
        Server(name="ok", command="echo", args=["x"], env={"TOKEN": _SECRET})

    server = Server(name="ok", command="echo", args=["x"], env={"TOKEN": "${TOKEN}"})
    server.env["TOKEN"] = _SECRET
    registry = Registry(servers={"ok": server})
    try:
        save_registry(home, registry)
    except McpHubError:
        pass
    try:
        view = render_agent(home, "claude", registry)
    except McpHubError:
        view = {}
    rendered = view.get("rendered") or ""
    diff = view.get("unified_diff") or ""
    assert _SECRET not in rendered
    assert _SECRET not in diff
    try:
        apply_all(home, apply=True)
    except McpHubError:
        pass

    assert _files_containing(home, _SECRET) == []
    assert _files_containing(tmp_path / "process-home", _SECRET) == []


def test_concurrent_apply_all_does_not_tear_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    twin = tmp_path / "twin"
    expected_path = _prepare(twin)
    apply_all(twin, apply=True)
    expected = expected_path.read_bytes()

    home = tmp_path / "home"
    path = _prepare(home)
    original = path.read_bytes()
    assert original != expected
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(10)
            apply_all(home, apply=True)
        except McpHubError as exc:
            errors.append(exc)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not any(thread.is_alive() for thread in threads)
    got = path.read_bytes()
    assert got in {original, expected}, got
    parsed = json.loads(got)
    assert isinstance(parsed, dict)
    assert parsed["userID"] == _SIBLING
    assert got.count(b"{") >= 1
    assert original + expected not in got
    assert expected + original not in got


def test_rollback_restores_exact_bytes_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_home(monkeypatch, tmp_path)
    home = tmp_path / "home"
    path = _prepare(home, mode=0o640)
    original = path.read_bytes()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o640

    result = apply_all(home, apply=True)

    assert path.read_bytes() != original
    rollback(home, result.get("snapshot_id"), apply=True)
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == mode
