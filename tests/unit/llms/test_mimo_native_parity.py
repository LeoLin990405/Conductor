"""MiMo V2.6: 1M context, always-available thinking and image bounds.

Live-verified against the deployed MIMO_API_KEY: token-plan endpoint text calls
with thinking enabled/disabled, the 131072 cap, and base64 image input all work.
The DSH pi-ai catalog pins the same 1,048,576 context and 2000x2000 image bound.
"""

from src.llms.llm import LLM


def test_the_mimo_provider_and_intl_variant_are_declared():
    info = LLM.get_model_config().get_provider_info("mimo")
    assert info["sdk"] == "openai"
    assert info["base_url"] == "https://token-plan-sgp.xiaomimimo.com/v1"
    assert info["access_type"] == "coding_plan"
    oauth = LLM.get_model_config().get_provider_info("mimo-oauth")
    assert oauth["sdk"] == "openai"
    assert oauth["base_url"] == "https://api.xiaomimimo.com/v1"
    assert info["image_resize"] == {
        "max_width": 2000,
        "max_height": 2000,
        "max_bytes": 4_718_592,
        "jpeg_quality": 80,
    }


def test_the_mimo_rows_match_the_documented_capabilities():
    for model in ("mimo-v2.6-flash", "mimo-v2.6-pro"):
        entry = LLM.get_model_config().get_model_config(model)
        assert entry["context"] == 1_048_576
        assert entry["input_modalities"] == ["text", "image"]
        assert entry["parameters"]["max_completion_tokens"] == 131_072
        assert entry["reasoning"]["efforts"] == ["none", "high"]
        assert entry["reasoning"]["default"] == "high"
        assert entry["reasoning"]["on"] == {"extra_body.thinking.type": "enabled"}
        assert entry["reasoning"]["off"] == {"extra_body.thinking.type": "disabled"}
        assert entry["prompt_guidance"] == "lean"
