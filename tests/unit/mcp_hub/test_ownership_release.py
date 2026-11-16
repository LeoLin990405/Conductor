"""Ownership release: apply deletes or disables the keys Conductor owns.

When a server is removed or disabled in the registry, the next apply gives up
the agent-config keys it owns: a removed server's key is deleted, and a
disabled server's key is set to ``enabled = false`` where the agent can hold
that field (otherwise deleted). Each release is a journaled ``released`` event
and keeps dry-run, snapshot and rollback. A key the hub does not own is never
touched, and a key the user recreates after a release stays theirs. Agent
config files live under a monkeypatched ``HOME``.
A release is journalled even when the user deleted the whole config file, and
it reaches a server map the file declares as one inline TOML table. A key whose
representation apply cannot edit keeps its ownership instead of being orphaned.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.mcp_hub import (
    Registry,
    Server,
    apply_all,
    diff_all,
    import_existing,
    rollback,
    save_registry,
)
from src.mcp_hub.errors import McpHubError
from src.mcp_hub.journal import entry_for, snapshot_ids
from src.mcp_hub.store import load_managed, load_registry


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    return root


def _json_servers(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("mcpServers") or {}
    return raw if isinstance(raw, dict) else {}


def _toml_servers(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data.get("mcp_servers") or {}
    return raw if isinstance(raw, dict) else {}


def _add_manual(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("mcpServers", {})["manual"] = {"command": "echo", "args": ["user"]}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _released_events(home: Path, agent: str) -> list[dict]:
    out: list[dict] = []
    for snapshot_id in snapshot_ids(home):
        found = entry_for(home, snapshot_id, agent)
        if found is not None and found.get("state") == "released":
            out.append(found)
    return out


def _publish(home: Path, server: Server) -> None:
    save_registry(home, Registry(servers={server.name: server}))
    applied = apply_all(home, apply=True)
    assert applied["applied"] is True


def test_remove_deletes_owned_keys_and_keeps_unowned(home: Path) -> None:
    server = Server(
        name="hubbed", command="echo", args=["managed"], targets=("claude", "cursor")
    )
    _publish(home, server)
    claude = home / ".claude.json"
    cursor = home / ".cursor" / "mcp.json"
    assert "hubbed" in _json_servers(claude)
    assert "hubbed" in _json_servers(cursor)
    _add_manual(claude)
    _add_manual(cursor)

    save_registry(home, Registry())
    released = apply_all(home, apply=True)

    assert released["applied"] is True
    assert "hubbed" not in _json_servers(claude)
    assert "hubbed" not in _json_servers(cursor)
    # A key the hub never owned survives untouched.
    assert "manual" in _json_servers(claude)
    assert "manual" in _json_servers(cursor)
    assert released["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert released["diffs"]["cursor"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]


def test_release_is_journaled_and_drops_ownership(home: Path) -> None:
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("claude",))
    _publish(home, server)
    assert load_managed(home)["claude"] == {"hubbed"}

    save_registry(home, Registry())
    apply_all(home, apply=True)

    events = _released_events(home, "claude")
    assert events, "a removed owned key must record a released event"
    latest = events[-1]
    assert latest["released"] == ["hubbed"]
    assert latest["owned"] == []
    assert load_managed(home)["claude"] == set()


def test_disable_sets_enabled_false_on_toml_agent(home: Path) -> None:
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("grok",))
    _publish(home, server)
    grok = home / ".grok" / "config.toml"
    assert _toml_servers(grok)["hubbed"]["enabled"] is True

    registry = load_registry(home)
    registry.servers["hubbed"].enabled = False
    save_registry(home, registry)
    result = apply_all(home, apply=True)

    body = _toml_servers(grok)["hubbed"]
    assert body["enabled"] is False
    assert body["command"] == "echo"
    assert result["diffs"]["grok"]["released"] == [
        {"key": "hubbed", "action": "disable"}
    ]
    # A disabled key stays owned so a later enable can restore it.
    assert load_managed(home)["grok"] == {"hubbed"}


def test_disable_sets_enabled_false_on_json_agent(home: Path) -> None:
    server = Server(
        name="hubbed", command="echo", args=["managed"], targets=("minimax",)
    )
    _publish(home, server)
    minimax = home / ".minimax" / "mcp.json"
    assert _json_servers(minimax)["hubbed"]["enabled"] is True

    registry = load_registry(home)
    registry.servers["hubbed"].enabled = False
    save_registry(home, registry)
    apply_all(home, apply=True)

    assert _json_servers(minimax)["hubbed"]["enabled"] is False


def test_disable_deletes_when_agent_has_no_enabled_field(home: Path) -> None:
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("claude",))
    _publish(home, server)
    claude = home / ".claude.json"
    assert "hubbed" in _json_servers(claude)

    registry = load_registry(home)
    registry.servers["hubbed"].enabled = False
    save_registry(home, registry)
    result = apply_all(home, apply=True)

    assert "hubbed" not in _json_servers(claude)
    assert result["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]


def test_disable_then_enable_round_trips(home: Path) -> None:
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("grok",))
    _publish(home, server)
    grok = home / ".grok" / "config.toml"

    registry = load_registry(home)
    registry.servers["hubbed"].enabled = False
    save_registry(home, registry)
    apply_all(home, apply=True)
    assert _toml_servers(grok)["hubbed"]["enabled"] is False

    registry = load_registry(home)
    registry.servers["hubbed"].enabled = True
    save_registry(home, registry)
    apply_all(home, apply=True)
    assert _toml_servers(grok)["hubbed"]["enabled"] is True


def test_release_is_a_dry_run_until_apply(home: Path) -> None:
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("claude",))
    _publish(home, server)
    claude = home / ".claude.json"
    before = claude.read_bytes()
    journal_before = set(snapshot_ids(home))

    save_registry(home, Registry())
    preview = apply_all(home, apply=False)

    assert preview["claude"]["released"] == [{"key": "hubbed", "action": "delete"}]
    assert claude.read_bytes() == before
    assert set(snapshot_ids(home)) == journal_before


def test_release_snapshot_rolls_back(home: Path) -> None:
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("claude",))
    _publish(home, server)
    claude = home / ".claude.json"
    before = claude.read_bytes()
    assert "hubbed" in _json_servers(claude)

    save_registry(home, Registry())
    released = apply_all(home, apply=True)
    assert "hubbed" not in _json_servers(claude)
    snapshot = released["snapshot_id"]
    assert snapshot

    rolled = rollback(home, snapshot, apply=True)
    assert rolled["dry_run"] is False
    assert claude.read_bytes() == before
    assert "hubbed" in _json_servers(claude)
    assert entry_for(home, snapshot, "claude")["state"] == "rolled_back"


def test_noop_release_drops_stale_ownership(home: Path) -> None:
    """A key the user already deleted is released without a rewrite."""
    server = Server(name="hubbed", command="echo", args=["managed"], targets=("claude",))
    _publish(home, server)
    claude = home / ".claude.json"
    payload = json.loads(claude.read_text(encoding="utf-8"))
    del payload["mcpServers"]["hubbed"]
    claude.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    save_registry(home, Registry())
    result = apply_all(home, apply=True)
    assert result["snapshot_id"] is None
    assert load_managed(home)["claude"] == set()

    payload = json.loads(claude.read_text(encoding="utf-8"))
    payload["mcpServers"]["hubbed"] = {"command": "user-owned-tool", "args": []}
    claude.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    before = claude.read_bytes()
    apply_all(home, apply=True)

    assert claude.read_bytes() == before
    assert _json_servers(claude)["hubbed"]["command"] == "user-owned-tool"


def test_cli_remove_then_apply_releases_owned_key(home: Path) -> None:
    def run(argv: list[str]) -> dict:
        import contextlib
        import io

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(argv)
        assert code == 0
        return json.loads(stdout.getvalue())

    run(
        [
            "mcp",
            "add",
            "--name",
            "hubbed",
            "--command",
            "echo",
            "--arg",
            "managed",
            "--target",
            "claude",
            "--apply",
            "--json",
        ]
    )
    applied = run(["mcp", "apply", "--apply", "--json"])
    assert applied["data"]["applied"] is True
    claude = home / ".claude.json"
    assert "hubbed" in _json_servers(claude)

    run(["mcp", "remove", "hubbed", "--apply", "--json"])
    released = run(["mcp", "apply", "--apply", "--json"])
    assert released["data"]["applied"] is True
    assert released["data"]["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert "hubbed" not in _json_servers(claude)


def _write_inline_codex(home: Path, body: str) -> Path:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_release_is_journaled_when_the_config_file_is_gone(home: Path) -> None:
    """A deleted config file still records the release its removal implies.

    Publishing into a file that already existed, then deleting that file and
    the registry entry, used to leave ownership active: no bytes changed, so
    the no-op release was skipped and a later apply deleted the key the user
    recreated in a fresh file.
    """
    claude = home / ".claude.json"
    claude.write_text(
        json.dumps({"mcpServers": {"hand": {"command": "hand", "args": []}}}) + "\n",
        encoding="utf-8",
    )
    server = Server(
        name="hubbed", command="echo", args=["managed"], targets=("claude",)
    )
    _publish(home, server)
    assert "hubbed" in _json_servers(claude)

    claude.unlink()
    save_registry(home, Registry())
    result = apply_all(home, apply=True)

    assert result["applied"] is True
    assert result["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    events = _released_events(home, "claude")
    assert events, "a missing config file must not keep stale ownership"
    assert events[-1]["released"] == ["hubbed"]
    assert events[-1]["owned"] == []
    assert load_managed(home)["claude"] == set()

    claude.write_text(
        json.dumps({"mcpServers": {"hubbed": {"command": "user-owned", "args": []}}})
        + "\n",
        encoding="utf-8",
    )
    before = claude.read_bytes()
    apply_all(home, apply=True)

    assert claude.read_bytes() == before
    assert _json_servers(claude)["hubbed"]["command"] == "user-owned"


def test_release_removes_owned_key_from_inline_toml(home: Path) -> None:
    """An inline ``mcp_servers`` table loses its owned key and keeps the rest.

    codex accepts the map as one inline table. A read-only probe used to report
    no change while ownership was dropped, leaving the key in the file with
    nobody owning it.
    """
    codex = _write_inline_codex(
        home,
        'mcp_servers = { hubbed = { command = "echo", args = ["managed"] },'
        ' "../x" = { command = "other", args = [] } }\n',
    )
    before = codex.read_bytes()
    assert set(import_existing(home)) == {"hubbed"}
    assert load_managed(home)["codex"] == {"hubbed"}

    save_registry(home, Registry())
    preview = diff_all(home)
    assert preview["codex"]["released"] == [{"key": "hubbed", "action": "delete"}]
    assert preview["codex"]["unified_diff"], "the inline table must change"
    assert codex.read_bytes() == before

    result = apply_all(home, apply=True)

    assert result["applied"] is True
    body = _toml_servers(codex)
    assert set(body) == {"../x"}
    assert body["../x"]["command"] == "other"
    assert "../x" in codex.read_text(encoding="utf-8")
    assert result["diffs"]["codex"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert load_managed(home)["codex"] == set()
    events = _released_events(home, "codex")
    assert events[-1]["released"] == ["hubbed"]
    assert events[-1]["owned"] == []


def test_inline_toml_release_rolls_back(home: Path) -> None:
    """The inline release is a snapshot like any other publish."""
    codex = _write_inline_codex(
        home,
        'mcp_servers = { hubbed = { command = "echo", args = ["managed"] },'
        ' hand = { command = "hand", args = [] } }\n',
    )
    before = codex.read_bytes()
    assert set(import_existing(home)) == {"hubbed", "hand"}

    registry = load_registry(home)
    del registry.servers["hubbed"]
    save_registry(home, registry)
    released = apply_all(home, apply=True)
    assert released["snapshot_id"]
    assert set(_toml_servers(codex)) == {"hand"}

    rolled = rollback(home, released["snapshot_id"], apply=True)

    assert rolled["dry_run"] is False
    assert codex.read_bytes() == before
    assert set(_toml_servers(codex)) == {"hubbed", "hand"}
    assert entry_for(home, released["snapshot_id"], "codex")["state"] == "rolled_back"


def test_disable_rewrites_inline_toml_key_in_place(home: Path) -> None:
    """A disabled key in an inline table is flipped without breaking the TOML."""
    grok = home / ".grok" / "config.toml"
    grok.parent.mkdir(parents=True, exist_ok=True)
    grok.write_text(
        "mcp_servers = { hubbed = { command = \"echo\", args = [\"managed\"],"
        " enabled = true }, hand = { command = \"hand\" } }\n",
        encoding="utf-8",
    )
    assert set(import_existing(home)) == {"hubbed", "hand"}

    registry = load_registry(home)
    registry.servers["hubbed"].enabled = False
    save_registry(home, registry)
    result = apply_all(home, apply=True)

    body = _toml_servers(grok)
    assert body["hubbed"]["enabled"] is False
    assert body["hubbed"]["command"] == "echo"
    assert body["hand"]["command"] == "hand"
    assert result["diffs"]["grok"]["released"] == [
        {"key": "hubbed", "action": "disable"}
    ]
    assert load_managed(home)["grok"] == {"hubbed", "hand"}


def test_unremovable_inline_key_keeps_its_ownership(home: Path) -> None:
    """A representation apply cannot edit is not released out from under.

    ``mcp_servers.NAME = { ... }`` declares the same map without a header the
    editor can drop. The key stays owned and is reported as orphaned, so the
    next apply retries the release instead of orphaning it.
    """
    codex = _write_inline_codex(
        home, 'mcp_servers.hubbed = { command = "echo", args = ["managed"] }\n'
    )
    before = codex.read_bytes()
    assert set(import_existing(home)) == {"hubbed"}

    save_registry(home, Registry())
    result = apply_all(home, apply=True)

    assert codex.read_bytes() == before
    assert result["diffs"]["codex"]["released"] == []
    assert result["diffs"]["codex"]["orphaned"] == ["hubbed"]
    assert load_managed(home)["codex"] == {"hubbed"}
    assert not _released_events(home, "codex")


def test_inline_release_decodes_unicode_escapes_before_matching(home: Path) -> None:
    """A TOML unicode escape is decoded before a key is matched.

    ``"\\u0061"`` in an inline table is the key ``a``. Reading it as the literal
    text ``u0061`` made the editor treat an unowned entry as the owned key it
    was asked to drop, so removing one key deleted both.
    """
    server = Server(
        name="u0061", command="echo", args=["managed"], targets=("codex",)
    )
    _publish(home, server)
    assert load_managed(home)["codex"] == {"u0061"}

    codex = _write_inline_codex(
        home,
        'mcp_servers = { u0061 = { command = "echo", args = ["managed"] },'
        ' "\\u0061" = { command = "other", args = [] } }\n',
    )
    assert set(_toml_servers(codex)) == {"u0061", "a"}

    save_registry(home, Registry())
    result = apply_all(home, apply=True)

    body = _toml_servers(codex)
    # The unowned key, written as an escape, is still there.
    assert set(body) == {"a"}
    assert body["a"]["command"] == "other"
    assert result["diffs"]["codex"]["released"] == [
        {"key": "u0061", "action": "delete"}
    ]
    assert result["diffs"]["codex"]["orphaned"] == []
    assert load_managed(home)["codex"] == set()


def test_release_verifies_each_removal_before_releasing_ownership(home: Path) -> None:
    """A key the edit could not reach is not released out from under.

    A dotted ``mcp_servers.hubbed = { ... }`` declaration is not a table the
    editor drops. Removing it alongside a ``[mcp_servers.other]`` table used to
    report both released and clear ownership for both, while ``hubbed`` stayed
    active in the file with nobody owning it: a later apply could no longer
    clean it up.
    """
    codex = _write_inline_codex(
        home,
        'mcp_servers.hubbed = { command = "echo", args = ["managed"] }\n\n'
        '[mcp_servers.other]\ncommand = "other"\nargs = []\n',
    )
    assert set(import_existing(home)) == {"hubbed", "other"}
    assert load_managed(home)["codex"] == {"hubbed", "other"}

    save_registry(home, Registry())
    preview = diff_all(home)
    assert preview["codex"]["released"] == [{"key": "other", "action": "delete"}]
    assert preview["codex"]["orphaned"] == ["hubbed"]
    result = apply_all(home, apply=True)

    body = _toml_servers(codex)
    assert set(body) == {"hubbed"}
    assert body["hubbed"]["command"] == "echo"
    assert result["diffs"]["codex"]["released"] == [
        {"key": "other", "action": "delete"}
    ]
    assert result["diffs"]["codex"]["orphaned"] == ["hubbed"]
    assert load_managed(home)["codex"] == {"hubbed"}
    events = _released_events(home, "codex")
    assert events[-1]["released"] == ["other"]
    assert events[-1]["owned"] == ["hubbed"]


def test_noop_release_on_toml_agent_keeps_the_file(home: Path) -> None:
    """A TOML release whose key the user already deleted rewrites nothing.

    The remaining ``[mcp_servers.NAME]`` tables must survive byte for byte, and
    the stale ownership is still journalled away.
    """
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_text(
        '[mcp_servers.hubbed]\ncommand = "echo"\nargs = ["managed"]\n\n'
        '[mcp_servers.hand]\ncommand = "hand"\nargs = []\n',
        encoding="utf-8",
    )
    assert set(import_existing(home)) == {"hubbed", "hand"}
    codex.write_text(
        '[mcp_servers.hand]\ncommand = "hand"\nargs = []\n', encoding="utf-8"
    )
    before = codex.read_bytes()

    registry = load_registry(home)
    del registry.servers["hubbed"]
    save_registry(home, registry)
    result = apply_all(home, apply=True)

    assert result["snapshot_id"] is None
    assert codex.read_bytes() == before
    assert result["diffs"]["codex"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert load_managed(home)["codex"] == {"hand"}
    assert _released_events(home, "codex")[-1]["released"] == ["hubbed"]


def test_cli_apply_releases_inline_toml_key(home: Path) -> None:
    """``mcp apply --apply`` surfaces the release of an inline TOML key."""
    codex = _write_inline_codex(
        home,
        'mcp_servers = { hubbed = { command = "echo", args = ["managed"] },'
        ' hand = { command = "hand", args = [] } }\n',
    )

    def run(argv: list[str]) -> dict:
        import contextlib
        import io

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(argv)
        assert code == 0
        return json.loads(stdout.getvalue())

    run(["mcp", "import", "--apply", "--json"])
    run(["mcp", "remove", "hubbed", "--apply", "--json"])
    released = run(["mcp", "apply", "--apply", "--json"])

    assert released["data"]["applied"] is True
    assert released["data"]["diffs"]["codex"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert set(_toml_servers(codex)) == {"hand"}


def test_update_survives_a_bracket_header_inside_instructions(home: Path) -> None:
    """A ``[header]`` line inside a multi-line string is not a table header.

    A valid ``developer_instructions`` string may hold a line like ``[example]``.
    The TOML splitter used to read that line as a table header, cut the preamble
    mid-string so it no longer parsed, and then treat every scheduled key as one
    it could not reach: updating an enabled owned server deleted its table and
    suppressed the replacement instead of rewriting it in place. Splitting is
    string-aware, so the preamble stays parseable and the update lands.
    """
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True, exist_ok=True)
    instructions = "Follow this example:\n[example]\nThen add your servers.\n"
    codex.write_text(
        "developer_instructions = \"\"\"\n"
        + instructions
        + "\"\"\"\n\n"
        + '[mcp_servers.hubbed]\ncommand = "echo"\nargs = ["managed"]\n',
        encoding="utf-8",
    )
    assert set(import_existing(home)) == {"hubbed"}
    assert load_managed(home)["codex"] == {"hubbed"}

    registry = load_registry(home)
    registry.servers["hubbed"].command = "changed"
    save_registry(home, registry)
    result = apply_all(home, apply=True)

    assert result["applied"] is True
    body = _toml_servers(codex)
    # The owned server is updated in place, not dropped and lost.
    assert body["hubbed"]["command"] == "changed"
    assert body["hubbed"]["args"] == ["managed"]
    # The instruction string survives byte for byte and still parses as one.
    parsed = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert parsed["developer_instructions"] == instructions
    assert "[example]" in parsed["developer_instructions"]


def test_release_ignores_an_inline_map_after_an_escaped_triple_quote(home: Path) -> None:
    """An escaped triple quote does not end a multi-line string.

    Inside a basic multi-line string a backslash escapes the next byte, so a
    backslash followed by three quotes is one literal quote plus two more and
    the string keeps going. The scanner used to treat that run of quotes as the
    terminator, then read an example ``mcp_servers = { ... }`` map inside the
    instructions as a real preamble table and edited it while releasing an owned
    server, corrupting the instruction bytes while still producing valid TOML.
    """
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir(parents=True, exist_ok=True)
    instructions = (
        'Use this config: \\"""\n'
        'mcp_servers = { hubbed = { command = "example" } }\n'
        "More instructions.\n"
    )
    codex.write_text(
        "developer_instructions = \"\"\"\n"
        + instructions
        + "\"\"\"\n\n"
        + '[mcp_servers.hubbed]\ncommand = "echo"\nargs = ["managed"]\n',
        encoding="utf-8",
    )
    before = tomllib.loads(codex.read_text(encoding="utf-8"))[
        "developer_instructions"
    ]
    assert set(import_existing(home)) == {"hubbed"}
    assert load_managed(home)["codex"] == {"hubbed"}

    save_registry(home, Registry())
    result = apply_all(home, apply=True)

    after = tomllib.loads(codex.read_text(encoding="utf-8"))["developer_instructions"]
    # The instruction bytes are untouched, the example inline map included.
    assert after == before
    assert 'mcp_servers = { hubbed = { command = "example" } }' in after
    # The real owned server the registry dropped is still released.
    assert set(_toml_servers(codex)) == set()
    assert result["diffs"]["codex"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert load_managed(home)["codex"] == set()


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_apply_refuses_when_the_registry_file_is_missing(home: Path) -> None:
    """A deleted servers.toml is not an empty registry: nothing is released."""
    _publish(home, Server(name="hubbed", command="echo", args=["m"], targets=("claude",)))
    (home / ".conductor" / "mcp" / "servers.toml").unlink()
    before = _tree(home)

    with pytest.raises(McpHubError, match="servers.toml is missing"):
        apply_all(home, apply=True)

    assert _tree(home) == before
    assert "hubbed" in _json_servers(home / ".claude.json")
    assert not (home / ".conductor" / "mcp" / "apply.lock").exists()


def test_cli_apply_refuses_when_the_registry_file_is_missing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(home, Server(name="hubbed", command="echo", args=["m"], targets=("claude",)))
    (home / ".conductor" / "mcp" / "servers.toml").unlink()
    before = _tree(home)

    code = main(["mcp", "apply", "--apply"])

    assert code != 0
    assert "servers.toml is missing" in capsys.readouterr().err
    assert _tree(home) == before


def test_apply_with_an_empty_registry_file_still_releases(home: Path) -> None:
    _publish(home, Server(name="hubbed", command="echo", args=["m"], targets=("claude",)))
    (home / ".conductor" / "mcp" / "servers.toml").write_text("", encoding="utf-8")

    assert apply_all(home, apply=True)["applied"] is True
    assert "hubbed" not in _json_servers(home / ".claude.json")
