"""Qwen3.8 / 3.7: official Model Studio limits and reasoning dial.

No DASHSCOPE_API_KEY is available in this deployment, so this is docs + DSH
token-plan catalog alignment; the live call remains a documented gap.
"""

import pytest

from src.llms.llm import LLM

QWEN38 = ["qwen3.8-max", "qwen3.8-flash", "qwen3.8-max-intl", "qwen3.8-flash-intl"]


@pytest.mark.parametrize("model", QWEN38)
def test_qwen38_rows_match_the_model_studio_limits(model):
    entry = LLM.get_model_config().get_model_config(model)
    assert entry["context"] == 1_000_000
    assert entry["parameters"]["max_tokens"] == 131_072
    # Model Studio exposes low/medium/xhigh only; xhigh is the default.
    assert entry["reasoning"]["efforts"] == ["low", "medium", "xhigh"]
    assert entry["reasoning"]["default"] == "xhigh"
    assert entry["reasoning"]["write"] == "parameters.reasoning.effort"
    assert entry["input_modalities"] == ["text", "image"]


@pytest.mark.parametrize("model", ["qwen3.7-plus", "qwen3.7-plus-intl"])
def test_qwen37_plus_has_no_effort_dial_and_a_65k_cap(model):
    entry = LLM.get_model_config().get_model_config(model)
    assert entry["parameters"]["max_tokens"] == 65_536
    # Mixed-mode thinking defaults on; the API exposes no effort knob.
    assert "reasoning" not in entry


def test_dashscope_providers_carry_the_image_bounds():
    info = LLM.get_model_config().get_provider_info("dashscope")
    assert info["image_resize"] == {
        "max_width": 2000,
        "max_height": 2000,
        "max_bytes": 4_718_592,
        "jpeg_quality": 80,
    }
    assert LLM.get_model_config().get_provider_info("dashscope-intl")["image_resize"] == info["image_resize"]
