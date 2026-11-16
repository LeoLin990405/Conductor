"""A rejected DeepSeek account token becomes a sign-in-required error."""

from src.server.services.runs.stream_errors import (
    error_event_data,
    sign_in_required_provider,
)


def _rejected(model: str = "deepseek-flash-account", status: int = 401) -> BaseException:
    exc = RuntimeError("Error code: 401 - unauthorized")
    exc.__model_resilience__ = {
        "model": model,
        "attempted_models": [
            {"model": model, "error": str(exc), "status_code": status, "attempts": 1}
        ],
    }
    return exc


def test_rejected_deepseek_account_token_names_the_provider():
    assert sign_in_required_provider(_rejected(), "oauth") == "deepseek-account"


def test_other_credentials_or_models_are_not_sign_in_required():
    assert sign_in_required_provider(_rejected(), "byok") is None
    assert sign_in_required_provider(_rejected("deepseek-flash"), "oauth") is None


def test_the_error_frame_carries_the_sign_in_flag_and_hint():
    data = error_event_data("thread-1", "failed", exc=_rejected(), credential_source="oauth")
    assert data["sign_in_required"] == "deepseek-account"
    assert data["status_code"] == 401
    assert data["hints"][0] == "sign_in"
    assert "api_key" not in data["hints"]
