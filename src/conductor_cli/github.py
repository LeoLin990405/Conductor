"""GitHub CLI authentication through the user's encrypted vault."""

from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from typing import Any

from .client import CLIError

GITHUB_SECRET_NAME = "GITHUB_TOKEN"
_VAULT_PATH = "/api/v1/mcp/vault/secrets"


def add_commands(subparsers: Any) -> None:
    """Register the GitHub authentication and status commands."""
    github = subparsers.add_parser("github", help="Manage GitHub authentication")
    commands = github.add_subparsers(dest="github_command", required=True)
    commands.add_parser("auth", help="Store a GitHub token in the Conductor vault")
    commands.add_parser("status", help="Show GitHub token and scope-check availability")
    github.set_defaults(handler=run)


def run(args: argparse.Namespace, client: Any) -> dict[str, Any]:
    if args.github_command == "auth":
        return _auth(client)
    if args.github_command == "status":
        return _status(client)
    raise ValueError("Unsupported GitHub command")


def _auth(client: Any) -> dict[str, bool]:
    if not sys.stdin.isatty():
        raise CLIError("GitHub authentication requires an interactive terminal")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("GitHub personal access token: ").strip()
    except (getpass.GetPassWarning, EOFError):
        raise CLIError("Could not read GitHub token without terminal echo") from None
    if not token:
        raise CLIError("GitHub token must not be empty")
    if len(token) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in token):
        raise CLIError("GitHub token has invalid characters or length")

    vault = client.request("GET", _VAULT_PATH)
    secrets = _vault_secrets(vault)
    exists = any(
        isinstance(secret, dict) and secret.get("name") == GITHUB_SECRET_NAME
        for secret in secrets
    )
    method = "PUT" if exists else "POST"
    path = f"{_VAULT_PATH}/{GITHUB_SECRET_NAME}" if exists else _VAULT_PATH
    body = {"value": token}
    if not exists:
        body.update(
            name=GITHUB_SECRET_NAME,
            description="GitHub personal access token for Conductor CLI",
        )
    # Do not retain or print the token after this request returns.
    try:
        client.request(method, path, json=body)
    except CLIError:
        raise CLIError("Could not store GitHub token in the Conductor vault") from None
    del token, body
    return {"token_set": True}


def _status(client: Any) -> dict[str, Any]:
    vault = client.request("GET", _VAULT_PATH)
    secrets = _vault_secrets(vault)
    token_set = any(
        isinstance(secret, dict) and secret.get("name") == GITHUB_SECRET_NAME
        for secret in secrets
    )
    return {
        "token_set": token_set,
        "scopes_check": {
            "status": "unavailable",
            "reason": "api_does_not_expose_scopes",
        },
    }


def _vault_secrets(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or not isinstance(response.get("secrets"), list):
        raise CLIError("Conductor returned an invalid vault response")
    secrets = response["secrets"]
    if any(not isinstance(secret, dict) or not isinstance(secret.get("name"), str) for secret in secrets):
        raise CLIError("Conductor returned an invalid vault response")
    return secrets
