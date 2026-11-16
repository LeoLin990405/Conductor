"""Anthropic-compatible subscription endpoints are not all Anthropic.

The app reaches several subscription endpoints through an Anthropic-shaped
client. Only Claude's own OAuth surface requires the ``Authorization: Bearer``
auth mode *and* the Claude Code identity system block; Kimi Code speaks bearer
without the identity, and Zhipu mints a standard coding-plan key that travels as
``x-api-key``. These tests pin the provider-declared opt-outs and the payload
that actually reaches the SDK, so a future "cleanup" of the OAuth path cannot
silently put Anthropic's identity block on a competitor's request or move a
minted key onto the wrong header.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from src.llms.extension import ChatAnthropicOAuth
from src.llms.llm import LLM, ModelConfig


def _payload(client: ChatAnthropic) -> dict:
    return client._get_request_payload([HumanMessage(content="hi")])


def _platform_payload(client: ChatAnthropic) -> dict:
    return client._get_request_payload(
        [SystemMessage(content="base prompt"), HumanMessage(content="hi")]
    )


class TestProviderDeclarations:
    def test_claude_oauth_keeps_identity_and_bearer(self):
        info = ModelConfig().get_provider_info("claude-oauth")
        assert info["access_type"] == "oauth"
        assert info.get("oauth_identity", True) is True
        assert info.get("oauth_auth", "bearer") == "bearer"

    def test_kimi_oauth_is_bearer_without_the_claude_identity(self):
        info = ModelConfig().get_provider_info("kimi-oauth")
        assert info["access_type"] == "oauth"
        assert info["oauth_identity"] is False
        # Bearer is the default auth mode; only the identity opt-out is declared.
        assert info.get("oauth_auth", "bearer") == "bearer"

    def test_zhipu_oauth_uses_the_minted_key_as_x_api_key(self):
        for provider in ("z-ai-oauth", "z-ai-cn-oauth"):
            info = ModelConfig().get_provider_info(provider)
            assert info["access_type"] == "oauth"
            assert info["oauth_auth"] == "x-api-key"
            assert info["oauth_identity"] is False


class TestClientConstruction:
    def test_kimi_builds_the_bearer_client_without_identity(self):
        client = LLM("kimi-k3-oauth", api_key="oauth-token").get_llm()
        assert isinstance(client, ChatAnthropicOAuth)
        assert client.include_identity is False
        # The OAuth wrapper still moves the token to the bearer slot. Read the
        # params once: the property pops the key it returns, so a second access
        # would report an empty token.
        params = client._client_params
        assert params["api_key"] is None
        assert params["auth_token"] == "oauth-token"

    def test_zhipu_builds_a_plain_anthropic_client(self):
        client = LLM("glm-5.3-cn-oauth", api_key="minted-key").get_llm()
        assert type(client) is ChatAnthropic
        assert client.anthropic_api_key.get_secret_value() == "minted-key"


class TestPayloadIdentity:
    def test_kimi_payload_carries_no_claude_code_identity(self):
        client = ChatAnthropicOAuth(
            model="kimi-k3", api_key="oauth-token", max_tokens=1024, include_identity=False
        )
        payload = _platform_payload(client)
        system = payload["system"]
        texts = [system] if isinstance(system, str) else [block["text"] for block in system]
        assert texts == ["base prompt"]
        assert "Claude Code" not in " ".join(texts)

    def test_claude_default_still_prepends_the_identity(self):
        client = ChatAnthropicOAuth(model="claude-opus-5", api_key="oat", max_tokens=1024)
        payload = _platform_payload(client)
        texts = [block["text"] for block in payload["system"]]
        assert texts[0].startswith("You are Claude Code")
        assert texts[-1] == "base prompt"
