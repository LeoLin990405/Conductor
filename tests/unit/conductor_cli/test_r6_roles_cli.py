"""R6 L1: ``conductor roles`` shows and edits the role registry through the API."""

from __future__ import annotations

from typing import Any

import pytest

from src.conductor_cli.cli import build_parser


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str, *, params: dict | None = None, timeout=None) -> Any:
        self.calls.append(("GET", path, params))
        return {"ok": True}

    def request(self, method: str, path: str, *, params=None, json=None, timeout=None) -> Any:
        self.calls.append((method, path, json))
        return {"ok": True}


def _run(argv: list[str]) -> _Client:
    args = build_parser().parse_args(argv)
    client = _Client()
    args.handler(args, client)
    return client


def test_show_and_audit() -> None:
    assert _run(["roles", "show"]).calls == [("GET", "/api/v1/roles", None)]
    assert _run(["roles", "audit", "--role", "worker", "--limit", "5"]).calls == [
        ("GET", "/api/v1/roles/audit", {"limit": 5, "role": "worker"})
    ]


def test_set_builds_the_update_body(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-token")
    monkeypatch.setenv("AUTH_USER_ID", "leo")
    client = _run(
        [
            "roles", "set", "worker",
            "--candidates", "stepfun,kimi@2.5",
            "--default", "stepfun",
            "--constraint", "requires_session_resume=true",
            "--constraint", "excluded=deepseek,minimax",
            "--reason", "web line",
            "--expected-revision", "3",
        ]
    )
    method, path, body = client.calls[0]
    assert (method, path) == ("PUT", "/api/v1/roles/worker")
    assert body == {
        "reason": "web line",
        "candidates": ["stepfun", "kimi@2.5"],
        "default": "stepfun",
        "constraints": {"requires_session_resume": True, "excluded": ["deepseek", "minimax"]},
        "expected_revision": 3,
    }


def test_set_requires_a_reason() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["roles", "set", "worker", "--default", "kimi"])


def test_set_without_the_operator_credential_refuses(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("CONDUCTOR_REPO_ROOT", str(tmp_path))
    with pytest.raises(SystemExit, match="operator credential"):
        _run(["roles", "set", "worker", "--default", "kimi", "--reason", "r"])
