"""ChatAnthropic variant that uses OAuth Bearer auth (auth_token) instead of API key (X-Api-Key)."""

from functools import cached_property
from typing import Any

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput

# Required by Anthropic's API for OAuth subscription tokens to access Sonnet/Opus.
_CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

# A header-auth provider (DeepSeek's x-dsh-auth-token) still has to satisfy the
# Anthropic SDK's standard-auth-header check. The platform does not treat this
# placeholder as an API key when its own header is present.
_PLACEHOLDER_API_KEY = "EMPTY"


class ChatAnthropicOAuth(ChatAnthropic):
    """ChatAnthropic that sends ``Authorization: Bearer <token>`` instead of ``X-Api-Key``.

    The Anthropic Python SDK supports two auth modes:
      - ``api_key``   → ``X-Api-Key`` header  (standard API keys)
      - ``auth_token`` → ``Authorization: Bearer`` header  (OAuth tokens)

    LangChain's ``ChatAnthropic`` only passes ``api_key``.  This subclass
    intercepts ``_client_params`` so the OAuth token goes through ``auth_token``,
    keeps a non-secret placeholder in ``api_key`` on the constructed client (the
    SDK still requires a standard auth header to build a request, and this also
    prevents a fallback to the ``ANTHROPIC_API_KEY`` environment variable), and
    prepends the Claude Code identity to the system prompt (required for
    Sonnet/Opus access).

    ``include_identity`` exists for the other OAuth providers behind an
    Anthropic-compatible endpoint (Kimi Code, GLM). Their endpoint accepts the
    subscription bearer but is not Anthropic's first-party surface, so the
    Claude Code identity block is neither required nor truthful there; the
    manifest opts out per provider and the bearer auth mode is kept.
    """

    include_identity: bool = True

    # A provider that authenticates with a bare custom header (DeepSeek's
    # ``x-dsh-auth-token``) instead of Bearer or X-Api-Key. The token arrives
    # as ``api_key`` from the OAuth resolver; ``api_key`` is nulled on the
    # constructed client and the header is carried in the default headers.
    auth_header: str | None = None

    @property
    def _client_params(self) -> dict[str, Any]:
        # ChatAnthropic caches its params; copy before moving the credential so
        # the sync client and the async client each see the real token once and
        # neither build inherits the placeholder from the other.
        params = dict(super()._client_params)
        if self.auth_header:
            # The real credential moves out of the standard api-key slot into
            # the provider's declared header; the slot keeps a non-secret
            # placeholder so the SDK's auth validation still passes.
            token = params.get("api_key")
            headers = dict(params.get("default_headers") or {})
            headers[self.auth_header] = str(token or "")
            params["default_headers"] = headers
            params["api_key"] = _PLACEHOLDER_API_KEY
            return params
        # Move the value from api_key → auth_token so the SDK sends Bearer auth
        token = params.pop("api_key", None)
        params["api_key"] = None
        params["auth_token"] = token
        return params

    def _drop_api_key(self, client: Any) -> None:
        if not self.auth_header:
            client.api_key = None
            return
        # Header-auth clients are constructed with the placeholder already in
        # the standard slot (see ``_client_params``), so the real token never
        # reaches X-Api-Key or a mutable default-headers copy.
        client.api_key = _PLACEHOLDER_API_KEY

    @cached_property
    def _client(self) -> anthropic.Client:
        client = super()._client
        # Bearer mode nulls api_key so auth_headers sends only the declared
        # scheme; header-auth mode keeps the non-secret placeholder.
        self._drop_api_key(client)
        return client

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        client = super()._async_client
        self._drop_api_key(client)
        return client

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: dict,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if not self.include_identity:
            return payload
        # Prepend Claude Code identity — required for Sonnet/Opus with OAuth tokens.
        system = payload.get("system")
        identity_block = {"type": "text", "text": _CLAUDE_CODE_IDENTITY}
        if isinstance(system, list):
            payload["system"] = [identity_block, *system]
        elif isinstance(system, str) and system:
            payload["system"] = [identity_block, {"type": "text", "text": system}]
        else:
            payload["system"] = [identity_block]
        return payload
