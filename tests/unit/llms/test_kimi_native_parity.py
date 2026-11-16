"""Kimi K3: the facts its Messages API docs and the DSH catalog pin."""

import pytest

from src.llms.llm import LLM

CASES = {
    "kimi-k3": {"provider": "moonshot", "model_id": "kimi-k3", "write": "parameters.output_config.effort", "context": 1_000_000},
    "kimi-k3-coding": {"provider": "moonshot-coding", "model_id": "k3", "write": "extra_body.reasoning_effort", "context": 262_144},
    "kimi-k3-oauth": {"provider": "kimi-oauth", "model_id": "k3", "write": "extra_body.reasoning_effort", "context": 262_144},
    "kimi-k3.1-pro-coding": {"provider": "moonshot-coding", "model_id": "k3.1-pro", "write": "extra_body.reasoning_effort", "context": 262_144},
    "kimi-k3.1-pro-oauth": {"provider": "kimi-oauth", "model_id": "k3.1-pro", "write": "extra_body.reasoning_effort", "context": 262_144},
    "kimi-k3.1-pro-highspeed-coding": {"provider": "moonshot-coding", "model_id": "k3.1-pro-highspeed", "write": "extra_body.reasoning_effort", "context": 262_144},
    "kimi-k3.1-pro-highspeed-oauth": {"provider": "kimi-oauth", "model_id": "k3.1-pro-highspeed", "write": "extra_body.reasoning_effort", "context": 262_144},
    "kimi-for-coding-coding": {"provider": "moonshot-coding", "model_id": "kimi-for-coding", "write": "extra_body.reasoning_effort", "context": 1_048_576, "max_tokens": 32_768},
    "kimi-for-coding-oauth": {"provider": "kimi-oauth", "model_id": "kimi-for-coding", "write": "extra_body.reasoning_effort", "context": 1_048_576, "max_tokens": 32_768},
}


@pytest.mark.parametrize("model,expected", CASES.items())
def test_the_kimi_rows_match_the_documented_model(model, expected):
    entry = LLM.get_model_config().get_model_config(model)
    assert entry["provider"] == expected["provider"]
    assert entry["model_id"] == expected["model_id"]
    assert entry["context"] == expected["context"]
    assert entry["input_modalities"] == ["text", "image"]
    # K3 always thinks; low/high/max are the only levels and max is default.
    assert entry["reasoning"]["efforts"] == ["low", "high", "max"]
    assert entry["reasoning"]["default"] == "max"
    assert entry["reasoning"]["write"] == expected["write"]
    # Messages API requires max_tokens; the coding adapter ships 131072.
    assert entry["parameters"]["max_tokens"] == expected.get("max_tokens", 131_072)
    assert entry["prompt_guidance"] == "lean"


def test_the_moonshot_provider_pins_the_kimi_messages_endpoint_and_image_bounds():
    info = LLM.get_model_config().get_provider_info("moonshot")
    assert info["sdk"] == "anthropic"
    assert info["base_url"] == "https://api.moonshot.ai/anthropic"
    assert info["image_resize"] == {
        "max_width": 2000,
        "max_height": 2000,
        "max_bytes": 4_718_592,
        "jpeg_quality": 80,
    }
