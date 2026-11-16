from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import github
from src.conductor_cli.client import CLIError


class VaultClient:
    def __init__(self, secrets: list[dict[str, str]] | None = None) -> None:
        self.secrets = secrets or []
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, *, json: dict | None = None):
        self.calls.append((method, path, json))
        if method == "GET":
            return {"secrets": self.secrets}
        return {"name": github.GITHUB_SECRET_NAME}


def test_auth_creates_vault_secret_without_printing_token(monkeypatch, capsys):
    token = "ghp-test-token-never-print"
    monkeypatch.setattr(github.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(github.getpass, "getpass", lambda _: token)
    client = VaultClient()

    result = github.run(argparse.Namespace(github_command="auth"), client)

    assert result == {"token_set": True}
    assert client.calls == [
        ("GET", "/api/v1/mcp/vault/secrets", None),
        (
            "POST",
            "/api/v1/mcp/vault/secrets",
            {
                "value": token,
                "name": "GITHUB_TOKEN",
                "description": "GitHub personal access token for Conductor CLI",
            },
        ),
    ]
    assert token not in capsys.readouterr().out
    assert all("github.com" not in call[1] for call in client.calls)


def test_auth_updates_existing_vault_secret(monkeypatch):
    monkeypatch.setattr(github.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(github.getpass, "getpass", lambda _: "replacement-token")
    client = VaultClient([{"name": "GITHUB_TOKEN"}])

    github.run(argparse.Namespace(github_command="auth"), client)

    assert client.calls[-1] == (
        "PUT",
        "/api/v1/mcp/vault/secrets/GITHUB_TOKEN",
        {"value": "replacement-token"},
    )


def test_auth_refuses_non_interactive_prompt(monkeypatch):
    monkeypatch.setattr(github.sys.stdin, "isatty", lambda: False)
    client = VaultClient()

    with pytest.raises(CLIError, match="interactive terminal"):
        github.run(argparse.Namespace(github_command="auth"), client)
    assert client.calls == []


def test_auth_refuses_password_prompt_fallback(monkeypatch):
    monkeypatch.setattr(github.sys.stdin, "isatty", lambda: True)

    def fallback(_prompt: str) -> str:
        raise github.getpass.GetPassWarning("echo fallback")

    monkeypatch.setattr(github.getpass, "getpass", fallback)
    client = VaultClient()

    with pytest.raises(CLIError, match="without terminal echo"):
        github.run(argparse.Namespace(github_command="auth"), client)
    assert client.calls == []


@pytest.mark.parametrize("token", ["x" * 4097, "ghp-bad\ntoken", "ghp-bad\x7ftoken"])
def test_auth_rejects_invalid_token_before_vault_write(monkeypatch, token):
    monkeypatch.setattr(github.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(github.getpass, "getpass", lambda _: token)
    client = VaultClient()

    with pytest.raises(CLIError, match="invalid characters or length"):
        github.run(argparse.Namespace(github_command="auth"), client)
    assert client.calls == []


def test_auth_sanitizes_store_error_that_contains_token(monkeypatch):
    token = "ghp-secret-do-not-print"
    monkeypatch.setattr(github.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(github.getpass, "getpass", lambda _: token)

    class FailingClient(VaultClient):
        def request(self, method: str, path: str, *, json: dict | None = None):
            if method == "POST":
                raise CLIError(f"response echoed {token}")
            return super().request(method, path, json=json)

    with pytest.raises(CLIError) as error:
        github.run(argparse.Namespace(github_command="auth"), FailingClient())
    assert token not in str(error.value)


def test_status_ignores_gh_token_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "sentinel-from-environment")
    client = VaultClient()

    result = github.run(argparse.Namespace(github_command="status"), client)

    assert result["token_set"] is False
    assert "sentinel-from-environment" not in repr(result)
    assert client.calls == [("GET", "/api/v1/mcp/vault/secrets", None)]


def test_status_reports_presence_and_truthful_scope_limit():
    client = VaultClient([{"name": "OTHER"}, {"name": "GITHUB_TOKEN"}])

    result = github.run(argparse.Namespace(github_command="status"), client)

    assert result == {
        "token_set": True,
        "scopes_check": {
            "status": "unavailable",
            "reason": "api_does_not_expose_scopes",
        },
    }
    assert client.calls == [("GET", "/api/v1/mcp/vault/secrets", None)]


def test_status_reports_unset_token():
    client = VaultClient([{"name": "OTHER"}])

    result = github.run(argparse.Namespace(github_command="status"), client)

    assert result["token_set"] is False
    assert result["scopes_check"]["status"] == "unavailable"


@pytest.mark.parametrize("response", [None, {}, {"secrets": [None]}, {"secrets": "bad"}])
def test_malformed_vault_response_fails_closed(monkeypatch, response):
    class MalformedClient:
        def request(self, *_args, **_kwargs):
            return response

    with pytest.raises(CLIError, match="invalid vault response"):
        github.run(argparse.Namespace(github_command="status"), MalformedClient())
