"""Doubao on the ARK Plan route.

Live-verified with the deployment's VOLCENGINE_PLAN_API_KEY: text and base64
image input both answer through the plan endpoint. The public ARK docs site is
not fetchable from this environment, so the 128K context stays as the plan
route's recorded value rather than being raised from third-party claims.
"""

from src.llms.llm import LLM


def test_the_plan_route_is_the_configured_doubao_endpoint():
    info = LLM.get_model_config().get_provider_info("doubao-plan")
    assert info["sdk"] == "openai"
    assert info["base_url"] == "https://ark.cn-beijing.volces.com/api/plan/v3"
    assert info["access_type"] == "coding_plan"
    assert info["env_key"] == "VOLCENGINE_PLAN_API_KEY"


def test_seed_21_rows_declare_text_and_image():
    for model in ("doubao-seed-2-1-pro", "doubao-seed-2-1-turbo", "doubao-seed-2-1-lite"):
        entry = LLM.get_model_config().get_model_config(model)
        assert entry["context"] == 128_000
        assert entry["input_modalities"] == ["text", "image"]
        assert entry["prompt_guidance"] == "lean"
        # ARK Plan accepts the deep-thinking switch; the surface mirrors MiMo.
        assert entry["reasoning"]["efforts"] == ["none", "high"]
        assert entry["reasoning"]["on"] == {"extra_body.thinking.type": "enabled"}
        assert entry["reasoning"]["off"] == {"extra_body.thinking.type": "disabled"}
