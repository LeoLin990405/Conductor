"""The committed Router model is the DeepSeek-V4.1-Flash account route.

The deployment must not silently fall back to `step-3.7-flash` (the local
account default from the earlier smoke setup) or to the API-key route: the
committed `config.yaml` overrides only Router turns with
`deepseek-flash-account`, which resolves to model `deepseek-flash` on the
`deepseek-account` provider and needs the user's account sign-in.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.config.core import load_infrastructure_config
from src.llms.llm import LLM

ROOT = Path(__file__).resolve().parents[3]
ROUTER_MODEL = "deepseek-flash-account"


@pytest.fixture(scope="module")
def committed_config():
    """config.yaml as the repository ships it, not the unit-test defaults."""
    load_infrastructure_config.cache_clear()
    try:
        yield load_infrastructure_config(str(ROOT / "config.yaml"))
    finally:
        load_infrastructure_config.cache_clear()


def test_committed_config_pins_the_router_model(committed_config):
    assert committed_config.autonomy_router_model == ROUTER_MODEL


def test_router_model_is_the_deepseek_account_route():
    entry = LLM.get_model_config().get_model_config(ROUTER_MODEL)

    # The account route reuses the V4.1 Flash model id and differs only in
    # auth: `deepseek-account` sends the DeepSeek platform account token.
    assert entry["model_id"] == "deepseek-flash"
    assert entry["provider"] == "deepseek-account"
    assert entry["display_name"].startswith("DeepSeek-V4.1-Flash")
    assert entry["visible"] is True
    assert entry["context"] == 1_000_000


def test_router_model_is_not_the_placeholder_or_the_api_key_route():
    assert ROUTER_MODEL != "step-3.7-flash"
    # `deepseek-flash` is the deliberate API-key fallback, not the committed
    # account-route selection.
    assert ROUTER_MODEL != "deepseek-flash"


def test_settings_accessor_reads_the_committed_value(committed_config):
    from src.config.settings import get_autonomy_router_model

    with patch(
        "src.config.settings.get_infrastructure_config", return_value=committed_config
    ):
        assert get_autonomy_router_model() == ROUTER_MODEL
