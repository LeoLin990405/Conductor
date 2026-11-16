"""StepFun reasoning models: documented context, output cap and effort dial.

The modality rows are verified against a live StepFun call (a base64 MP4 on
``step-5-preview``/``step-3.7-flash``) — see docs/VIDEO-INPUT.md. No
STEPFUN_API_KEY is required to run this test; it reads the manifest only.
"""

from src.llms.llm import LLM


def test_step_5_preview_matches_the_model_page():
    entry = LLM.get_model_config().get_model_config("step-5-preview")
    # The Step Plan /models endpoint reports max_input_tokens 1,024,000.
    assert entry["context"] == 1_024_000
    assert entry["parameters"]["max_tokens"] == 65_536  # 64k documented output
    # Text, image and video: the model page lists all three as native inputs.
    assert entry["input_modalities"] == ["text", "image", "video"]
    assert entry["reasoning"]["efforts"] == ["low", "medium", "high"]
    assert entry["reasoning"]["default"] == "medium"
    assert entry["reasoning"]["write"] == "parameters.reasoning_effort"


def test_step_37_flash_matches_the_model_page():
    entry = LLM.get_model_config().get_model_config("step-3.7-flash")
    assert entry["context"] == 262_144
    assert entry["input_modalities"] == ["text", "image", "video"]
    assert entry["reasoning"]["efforts"] == ["low", "medium", "high"]
    assert entry["reasoning"]["default"] == "medium"
