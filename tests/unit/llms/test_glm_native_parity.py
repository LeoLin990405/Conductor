"""GLM-5.3: documented 1M context, always-on thinking, max effort default.

No Z.ai key is available in this deployment, so this is documentation + DSH
pi-ai catalog alignment; the live call remains a documented gap. The official
docs say GLM-5.3 always thinks (low/high/max, default max) and cannot disable
thinking; GLM-5.3-Flash is the native multimodal sibling with the same window.
"""

import pytest

from src.llms.llm import LLM


@pytest.mark.parametrize(
    "model",
    ["glm-5.3", "glm-5.3-flash", "glm-5.3-cn", "glm-5.3-flash-cn", "glm-5.3-cn-coding",
     "glm-5.3-cn-oauth", "glm-5.3-flash-cn-oauth", "glm-5.3-oauth"],
)
def test_glm_53_rows_are_one_million_context_and_max_by_default(model):
    entry = LLM.get_model_config().get_model_config(model)
    assert entry["context"] == 1_000_000
    assert entry["parameters"]["max_tokens"] == 131_072
    assert entry["reasoning"]["efforts"] == ["low", "high", "max"]
    assert entry["reasoning"]["default"] == "max"
    # Always-on thinking: no off patch, and the request enables it.
    assert "off" not in entry["reasoning"]
    assert entry["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": False}


def test_glm_53_flash_declares_the_multimodal_inputs():
    entry = LLM.get_model_config().get_model_config("glm-5.3-flash")
    kwargs = entry["input_modalities"]
    assert "text" in kwargs and "image" in kwargs and "pdf" in kwargs


def test_zai_providers_carry_the_adapter_image_bounds():
    info = LLM.get_model_config().get_provider_info("z-ai")
    assert info["image_resize"] == {
        "max_width": 2000,
        "max_height": 2000,
        "max_bytes": 4_718_592,
        "jpeg_quality": 80,
    }
    assert LLM.get_model_config().get_provider_info("z-ai-cn")["image_resize"] == info["image_resize"]
