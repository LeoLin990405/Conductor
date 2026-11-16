"""Small local checks for the acceptance runner's network fixture and CLI."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/acceptance_parallel.py"
SPEC = importlib.util.spec_from_file_location("acceptance_parallel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
acceptance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acceptance
SPEC.loader.exec_module(acceptance)


@pytest.mark.enable_socket
def test_mock_github_serves_source_check_routes_and_records_real_http_calls():
    with acceptance.MockGitHub() as github:
        with urllib.request.urlopen(f"{github.base_url}/user", timeout=2) as response:
            user = json.loads(response.read())
        with urllib.request.urlopen(
            f"{github.base_url}/repos/acceptance/project", timeout=2
        ) as response:
            repo = json.loads(response.read())
        comment_url = f"{github.base_url}/repos/acceptance/project/issues/42/comments"
        comment_request = urllib.request.Request(
            comment_url,
            data=json.dumps({"body": "accepted <!-- conductor-autonomy:fixture-id -->"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(comment_request, timeout=2) as response:
            posted = json.loads(response.read())
        with urllib.request.urlopen(comment_url, timeout=2) as response:
            comments = json.loads(response.read())

    assert user["login"] == "conductor-acceptance"
    assert repo["full_name"] == "acceptance/project"
    assert posted["id"] == 1
    assert any("conductor-autonomy:fixture-id" in comment["body"] for comment in comments)
    assert any(
        request["path"] == "/repos/acceptance/project/issues/42/comments"
        for request in github.requests
    )
    assert {
        "/user",
        "/repos/acceptance/project",
        "/repos/acceptance/project/issues/42/comments",
    } <= {request["path"] for request in github.requests}


def test_acceptance_script_help_does_not_contact_a_backend():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--base-url" in result.stdout
    assert "--live" in result.stdout
    assert "--keep-resources" in result.stdout


def test_api_error_allowlists_run_conflict_and_keeps_run_id_without_body_text(monkeypatch):
    body = json.dumps(
        {"detail": "run abc123 changed while coordinator was working"}
    ).encode()

    def reject(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://localhost/api/v1/main-agent/runs",
            409,
            "Conflict",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    client = acceptance.ApiClient("http://localhost", user_id="test", bearer_token=None)

    with pytest.raises(acceptance.ApiError) as raised:
        client.request("POST", "/api/v1/main-agent/runs", {"goal": "private prompt"})

    assert raised.value.conflict_kind == "run_revision_conflict"
    assert raised.value.conflict_run_id == "abc123"
    assert "private prompt" not in str(raised.value)


def test_api_error_does_not_retain_unrecognized_conflict_detail(monkeypatch):
    body = json.dumps({"detail": "private prompt and credential-looking text"}).encode()

    def reject(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://localhost/api/v1/main-agent/runs",
            409,
            "Conflict",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    client = acceptance.ApiClient("http://localhost", user_id="test", bearer_token=None)

    with pytest.raises(acceptance.ApiError) as raised:
        client.request("POST", "/api/v1/main-agent/runs", {"goal": "private prompt"})

    assert raised.value.conflict_kind is None
    assert raised.value.conflict_run_id is None
    assert "private prompt" not in str(raised.value)


def test_api_error_classifies_only_the_structured_coordinator_busy_code(monkeypatch):
    body = json.dumps(
        {"detail": {"code": "coordinator_busy", "message": "safe retry message"}}
    ).encode()

    def reject(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "http://localhost/api/v1/main-agent/runs/abc/tick",
            409,
            "Conflict",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    client = acceptance.ApiClient("http://localhost", user_id="test", bearer_token=None)

    with pytest.raises(acceptance.ApiError) as raised:
        client.request("POST", "/api/v1/main-agent/runs/abc/tick")

    assert raised.value.conflict_kind == "coordinator_busy"
    assert "safe retry message" not in str(raised.value)


def test_tick_retries_only_structured_coordinator_busy(monkeypatch):
    calls = 0

    class Client:
        def request(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise acceptance.ApiError(
                    "POST", "/tick", 409, conflict_kind="coordinator_busy"
                )
            return {"status": "queued"}

    monkeypatch.setattr(acceptance.time, "sleep", lambda _delay: None)
    result = acceptance._tick_run(Client(), "run-1", timeout=1)

    assert result == {"status": "queued"}
    assert calls == 2


def test_tick_does_not_retry_other_conflicts(monkeypatch):
    calls = 0

    class Client:
        def request(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise acceptance.ApiError(
                "POST", "/tick", 409, conflict_kind="run_revision_conflict"
            )

    with pytest.raises(acceptance.ApiError):
        acceptance._tick_run(Client(), "run-1", timeout=1)

    assert calls == 1


def test_mock_reply_wait_rejects_a_clarification_post_counting_as_final_reply():
    final_replies = [f"final reply {index}" for index in range(5)]
    posts = [
        {"body": {"body": "Please clarify the request."}},
        *({"body": {"body": reply}} for reply in final_replies[:-1]),
    ]

    assert len(posts) == len(final_replies)
    assert not acceptance._all_replies_posted(posts, final_replies)
    assert acceptance._all_replies_posted(
        [*posts, {"body": {"body": final_replies[-1]}}], final_replies
    )
