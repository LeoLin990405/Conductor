
import pytest

from src.server.services.native_runtime.mcp import NativeMCPServer, _resolved_headers


@pytest.mark.asyncio
async def test_vault_headers_resolve(monkeypatch):
    async def secrets(user_id, names=None):
        assert user_id == "u"
        return {"TOKEN": "secret"}

    monkeypatch.setattr("src.server.database.user_vault_secrets.get_user_secrets_decrypted", secrets)
    headers = await _resolved_headers("u", {"Authorization": "Bearer ${vault:TOKEN}"})
    assert headers == {"Authorization": "Bearer secret"}


@pytest.mark.asyncio
async def test_missing_vault_header_is_dropped(monkeypatch):
    async def secrets(user_id, names=None):
        return {}

    monkeypatch.setattr("src.server.database.user_vault_secrets.get_user_secrets_decrypted", secrets)
    assert await _resolved_headers("u", {"Authorization": "Bearer ${vault:TOKEN}"}) is None


def test_server_dict_carries_headers():
    server = NativeMCPServer(name="remote", url="https://x/mcp", headers=(("Authorization", "Bearer t"),))
    assert server.as_dict()["headers"] == {"Authorization": "Bearer t"}


def test_codex_http_bearer_uses_env_var_not_argv(tmp_path):
    from src.llms.native_cli.codex import CodexExecAdapter

    adapter = CodexExecAdapter(
        binary="/opt/codex",
        read_only=False,
        mcp_servers=[
            {
                "name": "remote",
                "url": "https://x/mcp",
                "headers": {"Authorization": "Bearer secret-token"},
            }
        ],
    )
    invocation = adapter.build_invocation("hi", cwd=str(tmp_path))
    assert "secret-token" not in " ".join(invocation.argv)
    assert any("bearer_token_env_var" in part for part in invocation.argv)
    assert "secret-token" in invocation.env.values()
