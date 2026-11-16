"""dsh adapter: MCP servers live in ``.dsh/cordis.patch.yml`` patch ops.

All values below are synthetic. Every test runs under ``isolated_home``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from src.mcp_hub import (
    McpHubError,
    Registry,
    Server,
    apply_all,
    diff_all,
    import_existing,
    rollback,
    save_registry,
)
from src.mcp_hub.adapters.dsh import SPEC, DshCodec, native
from src.mcp_hub.agents import agent_path
from src.mcp_hub.render import _apply_codec
from src.mcp_hub.store import load_registry

PLUGIN = "@deepseek-ai/dsh-mcp-client"

HEAD = """# user header comment — keep me
- id: some-plugin-row
  config: {level: 'debug'}   # inline keep
- insert:
    # a comment inside an insert
    - id: unrelated-row
      name: '@deepseek-ai/dsh-other-plugin'
      config:
        flag: true
"""
MCP_HAND = """    - id: fake-alpha-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: alpha
        transport: stdio
        command: fake-alpha-bin
        args: ['--flag', 'x']
        toolCallTimeoutMs: 600000
    - id: mcp-beta
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: beta
        transport: streamable-http
        url: http://127.0.0.1:39999/mcp
        failOnStartupError: true
"""
TAIL = """# tail comment
- id: some-group
  insert:
    - id: grouped-row
      name: '@deepseek-ai/dsh-third'
"""
SEED = HEAD + MCP_HAND + TAIL


def _path(home: Path) -> Path:
    return agent_path(home, "dsh")


def _write(home: Path, text: str, mode: int = 0o600) -> Path:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _register(home: Path, *servers: Server) -> None:
    registry = load_registry(home)
    for server in servers:
        registry.servers[server.name] = server
    save_registry(home, Registry(servers=registry.servers))


def _dsh_server(name: str = "gamma", **kw) -> Server:
    kw.setdefault("command", "fake-gamma")
    kw.setdefault("args", ["serve"])
    return Server(name=name, targets=("dsh",), **kw)


def _loaded(path: Path) -> list:
    data = yaml.safe_load(path.read_text(encoding="utf-8").replace("!!js", "!!str"))
    assert isinstance(data, list) and data
    return data


def _servers(home: Path) -> dict:
    return DshCodec().parse(_path(home).read_text(encoding="utf-8"))


def test_spec_targets_cordis_patch_not_mcp_json(isolated_home: Path) -> None:
    assert _path(isolated_home) == isolated_home / ".dsh" / "cordis.patch.yml"
    legacy = isolated_home / ".dsh" / "mcp.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"mcpServers": {"legacy": {"command": "x"}}}\n')
    before = legacy.read_bytes()
    assert "legacy" not in import_existing(isolated_home)
    _register(isolated_home, _dsh_server())
    apply_all(isolated_home, apply=True)
    assert legacy.read_bytes() == before
    assert "gamma" in _servers(isolated_home)


def test_import_then_diff_is_empty(isolated_home: Path) -> None:
    _write(isolated_home, SEED)
    imported = import_existing(isolated_home)
    assert {"alpha", "beta"} <= set(imported)
    registry = load_registry(isolated_home)
    assert registry.servers["alpha"].command == "fake-alpha-bin"
    assert registry.servers["alpha"].args == ("--flag", "x")
    assert registry.servers["beta"].transport == "http"
    assert registry.servers["beta"].url == "http://127.0.0.1:39999/mcp"
    view = diff_all(isolated_home)["dsh"]
    assert view["unified_diff"] == ""
    apply_all(isolated_home, apply=True)
    assert _path(isolated_home).read_text(encoding="utf-8") == SEED


def test_apply_adds_server_preserving_everything_else(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED, mode=0o640)
    _register(isolated_home, _dsh_server(env={"TOKEN": "${FAKE_TOKEN}"}))
    assert diff_all(isolated_home)["dsh"]["unified_diff"]
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(SEED)
    data = _loaded(path)
    assert len(data) == 4
    servers = _servers(isolated_home)
    assert servers["gamma"]["command"] == "fake-gamma"
    assert servers["gamma"]["env"] == {"TOKEN": "${FAKE_TOKEN}"}
    assert "!!js process.env.FAKE_TOKEN" in text
    assert diff_all(isolated_home)["dsh"]["unified_diff"] == ""

    rollback(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED
    assert path.stat().st_mode & 0o777 == 0o640


def test_owned_rewrite_keeps_extras_and_neighbours(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    import_existing(isolated_home)
    registry = load_registry(isolated_home)
    alpha = registry.servers["alpha"]
    registry.servers["alpha"] = Server(
        name="alpha",
        command="fake-alpha-v2",
        args=list(alpha.args),
        targets=alpha.targets,
        overrides=dict(alpha.overrides),
    )
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(HEAD)
    assert text.endswith(TAIL)
    beta_block = MCP_HAND.split("    - id: mcp-beta", 1)[1]
    assert "    - id: mcp-beta" + beta_block in text
    entry = next(
        item
        for op in _loaded(path)
        for item in op.get("insert") or []
        if item.get("config", {}).get("serverName") == "alpha"
    )
    assert entry["id"] == "fake-alpha-mcp"
    assert entry["config"]["toolCallTimeoutMs"] == 600000
    assert entry["config"]["command"] == "fake-alpha-v2"


def test_remove_deletes_only_that_item(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    import_existing(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("alpha")
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    alpha_block = MCP_HAND.split("    - id: mcp-beta", 1)[0]
    assert text == SEED.replace(alpha_block, "")
    assert set(_servers(isolated_home)) == {"beta"}


def test_removing_last_managed_never_leaves_empty_file(isolated_home: Path) -> None:
    path = _write(isolated_home, "# only a comment\n")
    _register(isolated_home, _dsh_server())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _servers(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("gamma")
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# only a comment\n")
    assert yaml.safe_load(text) == []
    assert _servers(isolated_home) == {}


def test_removing_last_item_of_user_op_keeps_valid_list(isolated_home: Path) -> None:
    only = "- insert:\n" + MCP_HAND.split("    - id: mcp-beta", 1)[0]
    path = _write(isolated_home, "# hdr\n" + only)
    import_existing(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("alpha")
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    data = _loaded(path)
    assert data == [{"insert": []}]
    assert path.read_text(encoding="utf-8").startswith("# hdr\n- insert:\n")


def test_hand_written_entry_is_not_claimed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    _register(isolated_home, _dsh_server("alpha", command="hub-alpha"))
    result = diff_all(isolated_home)["dsh"]
    assert any("refusing to claim" in item["reason"] for item in result["skipped"])
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_plaintext_env_is_not_copied(isolated_home: Path) -> None:
    secret = "sk-" "fake-plaintext-0000"
    seed = SEED.replace(
        "        toolCallTimeoutMs: 600000\n",
        f"        env:\n          API_KEY: {secret}\n        toolCallTimeoutMs: 600000\n",
    )
    _write(isolated_home, seed)
    import_existing(isolated_home)
    stored = (isolated_home / ".conductor" / "mcp" / "servers.toml").read_text()
    assert secret not in stored
    assert load_registry(isolated_home).servers["alpha"].env == {"API_KEY": "${API_KEY}"}
    with pytest.raises(McpHubError):
        Server(name="x", command="c", env={"K": "plain"}, targets=("dsh",))


def test_hostile_values_cannot_inject_yaml(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    hostile = 'x": 1\n- id: evil # } ] \' "'
    with pytest.raises(McpHubError):
        Server(name=hostile, command="c", targets=("dsh",))
    with pytest.raises(McpHubError):
        Server(name="h", command="a\nb", targets=("dsh",))
    _register(
        isolated_home,
        _dsh_server(
            "hostile",
            command='fake: "cmd" # not a comment',
            args=["- id: injected", "{a: b}", "'q'", '"dq"', "a\tb", "\u2028x"],
        ),
    )
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(SEED)
    servers = _servers(isolated_home)
    assert servers["hostile"]["command"] == 'fake: "cmd" # not a comment'
    assert servers["hostile"]["args"][0] == "- id: injected"
    assert servers["hostile"]["args"][-1] == "\u2028x"
    ids = [
        item.get("id")
        for op in _loaded(path)
        for item in op.get("insert") or []
    ]
    assert "injected" not in ids


def test_flow_style_insert_is_refused_without_writing(isolated_home: Path) -> None:
    flow = (
        "# flow\n- insert: [{id: a-mcp, name: '@deepseek-ai/dsh-mcp-client', "
        "config: {serverName: a, transport: stdio, command: x}}]\n"
    )
    path = _write(isolated_home, flow)
    import_existing(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("a")
    save_registry(isolated_home, registry)
    with pytest.raises(McpHubError):
        DshCodec().edit(flow, {}, {"a"})
    try:
        apply_all(isolated_home, apply=True)
    except McpHubError:
        pass
    assert path.read_text(encoding="utf-8") == flow


def test_anchor_and_multidoc_refused() -> None:
    codec = DshCodec()
    anchored = (
        "- insert:\n    - &e\n      id: a-mcp\n      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config: {serverName: a, transport: stdio, command: x}\n"
    )
    with pytest.raises(McpHubError):
        codec.edit(anchored, {}, {"a"})
    with pytest.raises(McpHubError):
        codec.parse("- insert: []\n---\n- insert: []\n")


def test_top_level_mapping_refused(isolated_home: Path) -> None:
    text = "mcpServers:\n  a:\n    command: x\n"
    path = _write(isolated_home, text)
    with pytest.raises(McpHubError):
        DshCodec().parse(text)
    _register(isolated_home, _dsh_server())
    try:
        apply_all(isolated_home, apply=True)
    except McpHubError:
        pass
    assert path.read_text(encoding="utf-8") == text


def test_codec_never_returns_empty_or_mapping() -> None:
    codec = DshCodec()
    body = native(
        {
            "transport": "stdio",
            "command": "c",
            "args": [],
            "url": "",
            "env": {},
            "enabled": True,
        }
    )
    fresh = codec.fresh({"a": body})
    assert isinstance(yaml.safe_load(fresh), list)
    gone = codec.edit(fresh, {}, {"a"})
    assert yaml.safe_load(gone) == []
    assert codec.edit("[]\n", {"b": body}, set()).count("- insert:") == 1
    out, changed = _apply_codec("dsh", "", {"a": body}, set())
    assert changed and isinstance(yaml.safe_load(out), list)


def test_disabled_entry_round_trips(isolated_home: Path) -> None:
    seed = SEED.replace(
        "    - id: mcp-beta\n", "    - id: mcp-beta\n      disabled: true\n"
    )
    _write(isolated_home, seed)
    import_existing(isolated_home)
    beta = load_registry(isolated_home).servers["beta"]
    assert beta.effective("dsh")["enabled"] is False
    assert diff_all(isolated_home)["dsh"]["unified_diff"] == ""
    apply_all(isolated_home, apply=True)
    assert _path(isolated_home).read_text(encoding="utf-8") == seed


def test_sse_and_long_names_are_skipped(isolated_home: Path) -> None:
    _write(isolated_home, SEED)
    _register(
        isolated_home,
        _dsh_server("s1", transport="sse", url="http://127.0.0.1:1/sse", command=""),
        _dsh_server("n" * 40),
    )
    skipped = {item["server"] for item in diff_all(isolated_home)["dsh"]["skipped"]}
    assert {"s1", "n" * 40} <= skipped
    assert SPEC.codec is not None


def test_hand_written_js_conditions_survive_a_rewrite(isolated_home: Path) -> None:
    from src.mcp_hub import Registry, Server, apply_all, import_existing, save_registry

    path = isolated_home / ".dsh" / "cordis.patch.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "- insert:\n"
        "    - id: a-mcp\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      disabled: !!js process.env.OFF === '1'\n"
        "      config:\n"
        "        serverName: a\n"
        "        transport: stdio\n"
        "        command: old\n"
        "        args: []\n"
        "        env:\n"
        "          T: !!js process.env.T || 'x'\n",
        encoding="utf-8",
    )
    import_existing(isolated_home)
    from src.mcp_hub import diff_all

    assert diff_all(isolated_home)["dsh"]["unified_diff"] == ""
    registry = Registry(
        servers={
            "a": Server(name="a", command="new", env={"T": "${T}"}, targets=("dsh",)),
        }
    )
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    assert '"new"' in text
    assert "process.env.OFF" in text
    assert "|| 'x'" in text


def test_crlf_file_stays_crlf(isolated_home: Path) -> None:
    from src.mcp_hub import Registry, Server, apply_all, save_registry

    path = isolated_home / ".dsh" / "cordis.patch.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"# keep\r\n- insert: []\r\n")
    save_registry(
        isolated_home,
        Registry(servers={"n": Server(name="n", command="c", targets=("dsh",))}),
    )
    apply_all(isolated_home, apply=True)
    data = path.read_bytes()
    assert b"serverName" in data
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert data.startswith(b"# keep\r\n- insert: []\r\n")
