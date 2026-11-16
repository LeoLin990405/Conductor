"""Codex receives Conductor's shipped MCP servers as documented -c overrides."""

from __future__ import annotations

from src.config.runtime.core import MCPServerConfig
from src.server.services.mcp_config import Origin
from src.server.services.native_runtime.mcp import (
    NativeMCPServer,
    _absolute_builtin_args,
    _bridgeable,
    codex_mcp_overrides,
)


def test_bundled_uv_entrypoint_becomes_cwd_independent():
    config = MCPServerConfig(
        name="cn_price",
        transport="stdio",
        command="uv",
        args=["run", "python", "plugins/cn-market/cn_price_mcp_server.py"],
    )
    args = _absolute_builtin_args(config)
    assert args[0:2] == ("run", "--directory")
    assert args[3] == "python"
    assert args[4].endswith("/plugins/cn-market/cn_price_mcp_server.py")


def test_codex_overrides_are_toml_arrays_and_quoted_strings():
    servers = [
        NativeMCPServer(
            name="cn_price",
            command="uv",
            args=("run", "--directory", "/repo", "python", "/repo/x.py"),
        )
    ]
    overrides = codex_mcp_overrides(servers)
    assert overrides[0] == 'mcp_servers.cn_price.command="uv"'
    assert overrides[1].startswith("mcp_servers.cn_price.args=[")
    assert '"run"' in overrides[1] and '"/repo/x.py"' in overrides[1]


def test_user_stdio_without_secrets_is_bridgeable():
    config = MCPServerConfig(
        name="user_tools",
        transport="stdio",
        command="/usr/local/bin/user-mcp",
        source="user",
    )
    assert _bridgeable(config, origin=Origin.USER, oauth_bound=False) is True


def test_user_server_with_env_or_oauth_is_not_bridgeable():
    with_env = MCPServerConfig(
        name="secret_tools",
        transport="stdio",
        command="/usr/local/bin/user-mcp",
        env={"TOKEN": "x"},
        source="user",
    )
    assert _bridgeable(with_env, origin=Origin.USER, oauth_bound=False) is False
    clean = MCPServerConfig(
        name="oauth_tools",
        transport="stdio",
        command="/usr/local/bin/user-mcp",
        source="user",
    )
    assert _bridgeable(clean, origin=Origin.USER, oauth_bound=True) is False
