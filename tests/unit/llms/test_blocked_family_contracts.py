"""Request-shape contracts for families whose live key is unavailable.

Kimi and Qwen are aligned from official docs + the DSH catalog, but this
deployment cannot call them. These tests still pin the exact wire shape each
client builds, so a manifest edit that silently drops the effort surface, the
output cap or the route cannot pass unnoticed until a key appears.
"""

from langchain_core.messages import HumanMessage

from src.llms.llm import create_llm


def _payload(model: str, effort: str | None) -> dict:
    llm = create_llm(
        model,
        api_key="test-key",
        **({"reasoning_effort": effort} if effort else {}),
    )
    return llm._get_request_payload([HumanMessage("hi")])


def test_kimi_k3_messages_route_carries_effort_and_the_131k_cap():
    payload = _payload("kimi-k3", "low")
    assert payload["model"] == "kimi-k3"
    assert payload["max_tokens"] == 131_072
    # Kimi's Messages API takes output_config.effort; thinking is always on.
    assert payload["output_config"] == {"effort": "low"}


def test_kimi_coding_route_carries_its_effort_extra_body():
    payload = _payload("kimi-k3-coding", "max")
    # Kimi Code's official model id for the K3 alias is the short k3.
    assert payload["model"] == "k3"
    assert payload["max_tokens"] == 131_072
    assert payload["extra_body"] == {"reasoning_effort": "max"}


def test_qwen38_uses_the_responses_reasoning_effort_and_131k_output():
    llm = create_llm("qwen3.8-max", api_key="test-key", reasoning_effort="xhigh")
    assert llm.use_responses_api is True
    payload = llm._get_request_payload([HumanMessage("hi")])
    assert payload["model"] == "qwen3.8-max"
    # Responses API spells the output cap max_output_tokens.
    assert payload["max_output_tokens"] == 131_072
    assert payload["reasoning"] == {"effort": "xhigh"}


def test_qwen37_plus_has_no_effort_surface_and_a_65k_cap():
    payload = _payload("qwen3.7-plus", None)
    assert payload["model"] == "qwen3.7-plus"
    assert payload["max_output_tokens"] == 65_536
    assert payload.get("reasoning") is None
