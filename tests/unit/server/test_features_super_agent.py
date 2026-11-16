"""The Super-agent catalog entry is opt-in and kill-switchable."""

from src.config.features import FEATURES, FeatureGate


def test_super_agent_is_core_and_still_has_a_kill_switch():
    spec = FEATURES["super_agent"]
    assert spec.enabled is True
    assert spec.gate is FeatureGate.NONE
    assert spec.tradeoffs
