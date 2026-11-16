"""Auto is a routing choice; client construction receives a concrete model."""

from types import SimpleNamespace

import pytest

from src.server.services.llm import auto_model
from src.server.services.llm.config import ModelSelection, apply_auto_model


@pytest.mark.parametrize("slot,field", [("primary", "name"), ("background", "background")])
def test_auto_resolves_before_building_clients(monkeypatch, slot, field):
    monkeypatch.setenv("LLM_AUTO_CHAIN", "deepseek-flash,kimi-k3-coding")
    monkeypatch.setattr(auto_model, "_model_available", lambda model: model == "deepseek-flash")
    config = SimpleNamespace(llm=SimpleNamespace(
        name="auto", background="auto", compaction="auto", fallback=[],
    ))
    selection = apply_auto_model(
        config, ModelSelection(effective_model="auto", model_field="preferred_model"), slot,
    )

    assert selection.effective_model == "deepseek-flash"
    assert getattr(config.llm, field) == "deepseek-flash"
    assert config.llm.compaction == "deepseek-flash"
    assert config.llm.fallback == ["kimi-k3-coding"]


def test_concrete_selection_is_not_replaced(monkeypatch):
    config = SimpleNamespace(llm=SimpleNamespace(name="chosen-model"))
    selection = ModelSelection(effective_model="chosen-model", model_field="preferred_model")
    monkeypatch.setattr(auto_model, "resolve_auto_model", lambda: pytest.fail("unexpected auto routing"))
    assert apply_auto_model(config, selection, "primary") is selection
