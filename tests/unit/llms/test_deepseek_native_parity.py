"""DeepSeek conformance: the wire facts the official harness pins.

One file per model family, so "native parity" is a test result rather than a
claim. Sources: DeepSeek's docs (thinking mode, Anthropic API, pricing).
"""

import pytest
from langchain_core.messages import AIMessage

from src.llms.llm import LLM
from src.llms.reasoning_lineage import tool_turns_need_reasoning
from src.llms.reasoning_payload import deepseek_native_payload
from src.llms.token_counter import extract_token_usage

MODELS = ("deepseek-flash", "deepseek-v4-pro")


def test_the_provider_is_the_official_messages_route():
    info = LLM.get_model_config().get_provider_info("deepseek")
    assert info["sdk"] == "anthropic"
    assert info["base_url"] == "https://api.deepseek.com/anthropic"
    # Historical assistant reasoning must be replayed on tool-carrying requests.
    assert info["tool_turns_need_reasoning"] is True


@pytest.mark.parametrize("model", MODELS)
def test_the_thinking_surface_is_exactly_deepseek_s(model):
    """low/high/max go to output_config.effort; off only flips thinking.type."""
    reasoning = LLM.get_model_config().get_model_config(model)["reasoning"]
    assert reasoning["efforts"] == ["none", "low", "high", "max"]
    assert reasoning["default"] == "high"
    assert reasoning["write"] == "parameters.output_config.effort"
    assert reasoning["on"] == {"parameters.thinking.type": "enabled"}
    assert reasoning["off"] == {"parameters.thinking.type": "disabled"}


@pytest.mark.parametrize("model", MODELS)
def test_the_request_cap_is_the_harness_default(model):
    """DSH ships DEFAULT_MAX_TOKENS=256000; 384K stays a model capability."""
    entry = LLM.get_model_config().get_model_config(model)
    assert entry["parameters"]["max_tokens"] == 256_000
    catalog = LLM.get_model_config().get_model_info("deepseek", model)
    assert catalog["max_output"] == 384_000


def test_reasoning_replay_is_requested_for_the_route():
    assert tool_turns_need_reasoning("deepseek") is True


def test_empty_text_and_bad_arguments_follow_the_adapter():
    empty = {"type": "text", "text": ""}
    tool_use = {"type": "tool_use", "id": "tu_9", "name": "lookup", "input": {}}
    msg = AIMessage(
        content=[empty, tool_use],
        tool_calls=[],
        invalid_tool_calls=[{"name": "lookup", "args": "{bad", "id": "tu_9", "type": "tool_call"}],
    )
    out = deepseek_native_payload(msg)
    assert out.content == [tool_use]
    assert out.tool_calls == [{"name": "lookup", "args": {}, "id": "tu_9", "type": "tool_call"}]
    assert out.invalid_tool_calls == []


class _UsageResponse:
    usage_metadata = {
        "input_tokens": 1000,
        "output_tokens": 50,
        "total_tokens": 1050,
        "input_token_details": {"cache_read": 900},
    }


def test_cache_read_usage_is_accounted_for():
    """DeepSeek reports cache hits; the loop must count them, not drop them."""
    info = extract_token_usage(_UsageResponse())
    assert info["input_tokens"] == 1000
    assert info["cached_tokens"] == 900


def test_the_stream_idle_budget_matches_the_official_harness():
    """DSH bounds a DeepSeek stream at 300s; our Anthropic route reads it."""
    assert LLM.get_model_config().get_provider_info("deepseek")["request_timeout_seconds"] == 300
    assert LLM.get_model_config().get_provider_info("deepseek-account")["request_timeout_seconds"] == 300
    from src.llms.llm import create_llm

    client = create_llm("deepseek-flash", api_key="test-key")
    assert client.default_request_timeout == 300.0


@pytest.mark.asyncio
async def test_the_account_model_catalog_matches_the_observed_shape(monkeypatch):
    """GET /models at the inference origin, x-dsh-auth-token, cached per token."""
    from src.server.services import deepseek_oauth as ds

    calls = []

    class _Resp:
        status_code = 200
        def json(self):
            return {"data": [{"id": "deepseek-flash", "context_window": 1000000}]}

    class _Client:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False
        async def get(self, url, headers=None):
            calls.append((url, dict(headers or {})))
            return _Resp()

    async def no_cache():
        return None

    class _Cache:
        enabled = False
        client = None

    monkeypatch.setattr(ds.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("src.utils.cache.redis_cache.get_cache_client", lambda: _Cache())

    rows = await ds.fetch_account_models("acct-token")
    assert rows == [{"id": "deepseek-flash", "context_window": 1000000}]
    assert calls[0][0] == "https://api.deepseek.com/models"
    assert calls[0][1]["x-dsh-auth-token"] == "acct-token"


def test_file_references_ride_the_files_beta_header():
    """A Messages request carrying file ids must announce the beta."""
    from src.llms.llm import create_llm

    headers = LLM.get_model_config().get_provider_info("deepseek")["default_headers"]
    assert headers["anthropic-beta"] == "files-api-2025-04-14"
    client = create_llm("deepseek-flash", api_key="test-key")
    assert client._client_params["default_headers"].get("anthropic-beta") == "files-api-2025-04-14"


def test_flash_declares_in_history_system_prompt_updates():
    """The harness relies on DeepSeek reading the latest system message."""
    info = LLM.get_model_config().get_provider_info("deepseek")
    assert info["anthropic_profile"]["mid_conversation_system_messages"] is True


def test_thinking_blocks_survive_the_native_tool_history_serialization():
    """The live route accepted thinking + tool_use + tool_result replay; the
    serializer must keep that thinking block rather than stripping it."""
    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": "call the tool", "signature": "sig-1"},
            {"type": "tool_use", "id": "tu_1", "name": "lookup", "input": {"q": "x"}},
        ],
        tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "tu_1", "type": "tool_call"}],
    )
    out = deepseek_native_payload(msg)
    assert out.content[0] == {"type": "thinking", "thinking": "call the tool", "signature": "sig-1"}
    assert out.content[1]["type"] == "tool_use"
