"""DeepSeek's two API models, as the agent stack must see them.

Both rows are declared against DeepSeek's Anthropic-compatible endpoint
(https://api.deepseek.com/anthropic): thinking mode is on by default, the graded
dial is ``output_config.effort`` and the mode switch is ``thinking.type``.
DeepSeek-V4.1-Flash also accepts images; the V4 Pro flagship is text-only.
"""

import pytest

from src.llms.llm import LLM
from src.llms.pricing_utils import find_model_pricing
from src.llms.swarm_capabilities import supports_swarm

CASES = {
    "deepseek-flash": {"name": "DeepSeek-V4.1-Flash", "modalities": ["text", "image"]},
    "deepseek-v4-pro": {"name": "DeepSeek-V4-Pro", "modalities": ["text"]},
}


@pytest.fixture(scope="module")
def config():
    return LLM.get_model_config()


@pytest.mark.parametrize("model,expected", CASES.items())
def test_the_agent_row_matches_deepseek_s_api(model, expected, config):
    entry = config.get_model_config(model)
    assert entry["model_id"] == model
    assert entry["provider"] == "deepseek"
    assert entry["display_name"] == expected["name"]
    assert entry["visible"] is True
    # DeepSeek's published limits: 1M context, 384K max output. The agent
    # asks for 256K per request, the cap the official harness ships, leaving
    # the model's own 384K ceiling to the provider catalog row.
    assert entry["context"] == 1_000_000
    assert entry["parameters"]["max_tokens"] == 256_000
    assert entry["input_modalities"] == expected["modalities"]
    # Thinking is on by default at high effort; the surface is the one the
    # Anthropic-compatible endpoint documents.
    reasoning = entry["reasoning"]
    assert reasoning["efforts"] == ["none", "low", "high", "max"]
    assert reasoning["default"] == "high"
    assert reasoning["write"] == "parameters.output_config.effort"
    assert reasoning["on"] == {"parameters.thinking.type": "enabled"}
    assert reasoning["off"] == {"parameters.thinking.type": "disabled"}
    # DeepSeek reasons well without the fuller scaffolding: keep prompts lean.
    assert entry["prompt_guidance"] == "lean"


@pytest.mark.parametrize("model", list(CASES))
def test_the_provider_catalog_prices_and_lists_the_model(model):
    row = LLM.get_model_config().get_model_info("deepseek", model)
    assert row is not None
    assert row["context_window"] == 1_000_000
    assert row["max_output"] == 384_000
    assert row["is_reasoning"] is True
    pricing = find_model_pricing(model, provider="deepseek")
    assert pricing is not None


@pytest.mark.parametrize("model", list(CASES))
def test_both_models_can_drive_swarm_and_are_not_the_auto_router(model):
    assert supports_swarm(model) is True
    assert model != "auto"
