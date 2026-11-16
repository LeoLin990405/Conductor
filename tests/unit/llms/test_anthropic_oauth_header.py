"""ChatAnthropicOAuth's custom-header auth mode (DeepSeek account route).

The DeepSeek account token rides in ``x-dsh-auth-token``, not Bearer and not
X-Api-Key. The client must construct, drop X-Api-Key, and send the declared
header on both sync and async clients.
"""

from src.llms.extension.anthropic_oauth import ChatAnthropicOAuth


def _client() -> ChatAnthropicOAuth:
    return ChatAnthropicOAuth(
        model="deepseek-flash",
        api_key="acct-token",
        base_url="https://api.deepseek.com/anthropic",
        auth_header="x-dsh-auth-token",
        include_identity=False,
    )


def test_params_carry_the_declared_header_not_bearer():
    params = _client()._client_params
    assert params.get("default_headers", {}).get("x-dsh-auth-token") == "acct-token"
    # The real token is replaced before the SDK client is constructed.
    assert params.get("api_key") == "EMPTY"
    assert "auth_token" not in params


def test_sync_client_drops_api_key_and_sends_the_header():
    client = _client()
    sdk = client._client
    # The real token is never placed in the standard API-key slot.
    assert sdk.api_key == "EMPTY"
    assert sdk.default_headers.get("x-dsh-auth-token") == "acct-token"
    assert sdk.default_headers.get("X-Api-Key") == "EMPTY"


def test_async_client_drops_api_key_and_sends_the_header():
    client = _client()
    sdk = client._async_client
    assert sdk.api_key == "EMPTY"
    assert sdk.default_headers.get("x-dsh-auth-token") == "acct-token"
    assert sdk.default_headers.get("X-Api-Key") == "EMPTY"


def test_requests_pass_sdk_auth_validation_and_carry_the_header():
    """Both clients must send the real token, not the placeholder.

    The SDK rejects a request when no standard auth header is set; the
    placeholder keeps validation green while the provider credential rides in
    x-dsh-auth-token. The sync and async clients are built from a cached parent
    params dict, so each must still see the real token once.
    """
    from anthropic._models import FinalRequestOptions

    options = FinalRequestOptions(
        method="post",
        url="/v1/messages",
        headers={},
        json_data={
            "model": "deepseek-flash",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    # Both build orders matter: the parent params are cached per instance, so
    # a mutation leak is only visible when the second client is constructed
    # after the first.
    for order in ((0, 1), (1, 0)):
        client = _client()
        clients = (client._client, client._async_client)
        for index in order:
            request = clients[index]._build_request(options)
            assert request.headers["x-dsh-auth-token"] == "acct-token"
            assert request.headers["x-api-key"] == "EMPTY"
