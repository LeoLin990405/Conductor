"""Subscription-login OAuth clients come from the environment (public build)."""

from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException

from src.server.services import oauth_client_config

ALL_SETTINGS = [env for _label, envs in oauth_client_config.ROUTE_SETTINGS.values() for env in envs]

MODULES = {
    "gemini": ("src.server.services.gemini_oauth", ["GEMINI_CLIENT_ID", "GEMINI_CLIENT_SECRET"]),
    "claude": ("src.server.services.claude_oauth", ["CLAUDE_CLIENT_ID"]),
    "codex": ("src.server.services.codex_oauth", ["CODEX_CLIENT_ID"]),
    "grok": ("src.server.services.grok_oauth", ["GROK_CLIENT_ID"]),
    "kimi": ("src.server.services.kimi_oauth", ["KIMI_CLIENT_ID"]),
}


@pytest.fixture
def unset(monkeypatch):
    for env in ALL_SETTINGS:
        monkeypatch.delenv(env, raising=False)


def test_require_raises_not_configured_with_the_variable(unset):
    with pytest.raises(oauth_client_config.OAuthClientNotConfigured) as err:
        oauth_client_config.require("CONDUCTOR_GEMINI_OAUTH_CLIENT_ID", "Gemini")
    assert err.value.env == "CONDUCTOR_GEMINI_OAUTH_CLIENT_ID"
    assert "CONDUCTOR_GEMINI_OAUTH_CLIENT_ID" in str(err.value)


@pytest.mark.parametrize("login", sorted(oauth_client_config.ROUTE_SETTINGS))
def test_initiate_guard_is_a_clean_503_when_unset(unset, login):
    with pytest.raises(HTTPException) as err:
        oauth_client_config.require_for_route(login)
    assert err.value.status_code == 503
    assert err.value.detail["type"] == "oauth_client_not_configured"
    assert err.value.detail["missing"] == list(oauth_client_config.ROUTE_SETTINGS[login][1])


@pytest.mark.parametrize("login", sorted(oauth_client_config.ROUTE_SETTINGS))
def test_initiate_guard_passes_when_set(monkeypatch, login):
    for env in oauth_client_config.ROUTE_SETTINGS[login][1]:
        monkeypatch.setenv(env, "test-value")
    oauth_client_config.require_for_route(login)


@pytest.mark.parametrize("login", sorted(MODULES))
def test_module_constants_read_the_environment(monkeypatch, unset, login):
    module = importlib.import_module(MODULES[login][0])
    for const in MODULES[login][1]:
        with pytest.raises(oauth_client_config.OAuthClientNotConfigured):
            getattr(module, const)
        monkeypatch.setenv(getattr(module, const + "_ENV"), "test-" + const.lower())
        assert getattr(module, const) == "test-" + const.lower()


def test_gemini_authorize_url_is_not_built_without_a_client(unset):
    from src.server.services import gemini_oauth

    with pytest.raises(oauth_client_config.OAuthClientNotConfigured):
        gemini_oauth.build_authorize_url("verifier", "challenge")
