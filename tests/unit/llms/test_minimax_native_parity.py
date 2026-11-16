"""MiniMax M3: CN Messages endpoint, vision and request limits.

Live-verified against api.minimax.cn/anthropic: text, thinking on/off,
max_tokens=512000 and base64 image input all work with this deployment's key.
The DSH pi-ai catalog pins the same 2000x2000 / 4.5 MiB / JPEG-80 image bound.
"""

from src.llms.llm import LLM


def test_the_minimax_provider_uses_the_reachable_cn_messages_endpoint():
    info = LLM.get_model_config().get_provider_info("minimax")
    assert info["sdk"] == "anthropic"
    assert info["base_url"] == "https://api.minimax.cn/anthropic"
    assert info["region"] == "cn"
    intl = LLM.get_model_config().get_provider_info("minimax-intl")
    assert intl["base_url"] == "https://api.minimax.io/anthropic"
    assert intl["region"] == "intl"


def test_minimax_request_limits_match_the_catalog():
    m3 = LLM.get_model_config().get_model_config("minimax-m3")
    assert m3["context"] == 1_000_000
    assert m3["parameters"]["max_tokens"] == 512_000
    assert m3["input_modalities"] == ["text", "image", "video"]
    # Thinking is adaptive; off is an explicit mode switch.
    assert m3["reasoning"]["efforts"] == ["none", "high"]
    assert m3["reasoning"]["on"] == {"parameters.thinking.type": "adaptive"}
    assert m3["reasoning"]["off"] == {"parameters.thinking.type": "disabled"}

    flash = LLM.get_model_config().get_model_config("minimax-m3.1-flash")
    assert flash["context"] == 1_000_000
    assert flash["parameters"]["max_tokens"] == 128_000
    assert flash["reasoning"]["write"] == "parameters.output_config.effort"


def test_minimax_images_use_the_catalog_bounds():
    info = LLM.get_model_config().get_provider_info("minimax")
    assert info["image_resize"] == {
        "max_width": 2000,
        "max_height": 2000,
        "max_bytes": 4_718_592,
        "jpeg_quality": 80,
    }
