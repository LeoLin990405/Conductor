"""The product's per-model Swarm capability table."""

from src.llms import swarm_capabilities


def test_auto_router_is_explicitly_unsupported():
    # The router's selected model is unknown before the turn starts.
    assert swarm_capabilities.supports_swarm("auto") is False


def test_a_catalog_model_follows_the_default():
    assert swarm_capabilities.supports_swarm("deepseek-flash") is True
    assert swarm_capabilities.supports_swarm("unknown-model") is True
