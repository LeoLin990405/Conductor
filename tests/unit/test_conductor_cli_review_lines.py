"""Local CLI review preflight plus the server-backed queue payloads.

The queue itself moved to the processor's HTTP API; these tests pin the CLI
side: a self-review still fails before the round trip, and every call sends the
line names, provider and worktree root the server needs to enforce the rest.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.conductor_cli import review
from src.conductor_cli.client import CLIError, json_dumps_safe, sanitize_terminal


class RecordingClient:
    def __init__(self, *, post_result=None, get_result=None, error=None):
        self.calls: list[tuple] = []
        self.post_result = post_result if post_result is not None else {"ok": True}
        self.get_result = get_result if get_result is not None else {"reviews": []}
        self.error = error

    def post(self, path: str, json=None):
        if self.error is not None:
            raise self.error
        self.calls.append(("POST", path, json))
        return self.post_result

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        return self.get_result


def _line(root, name):
    path = root / name
    path.mkdir()
    (path / ".git").mkdir()
    return path


@pytest.fixture
def fleet_root(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    _line(root, "line-a")
    _line(root, "line-b")
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(root))
    return root


def test_local_cli_sanitizes_terminal_controls():
    assert sanitize_terminal("\x1b[2J\x1b]52;c;ZXZpbA==\x07safe") == "safe"


def test_local_cli_json_output_escapes_c1_controls():
    output = json_dumps_safe({"lines": ["\x9b2J\x9d52\x9c"]})
    assert "\x9b" not in output
    assert "\\u009b" in output


def test_review_refuses_self_review_before_the_api(fleet_root):
    client = RecordingClient(error=AssertionError("the API must not be called"))
    with pytest.raises(CLIError, match="refusing self-review"):
        review.handle_run(
            SimpleNamespace(line="line-a", by="line-a", provider="codex"), client
        )
    assert client.calls == []


def test_review_refuses_unknown_lines_before_the_api(fleet_root):
    client = RecordingClient(error=AssertionError("the API must not be called"))
    with pytest.raises(CLIError, match="Unknown line"):
        review.handle_run(
            SimpleNamespace(line="nope", by="line-b", provider="codex"), client
        )
    assert client.calls == []


def test_review_run_posts_line_reviewer_provider_and_root(fleet_root):
    record = {"id": "r-1", "line": "line-a", "state": "queued"}
    client = RecordingClient(post_result=record)

    out = review.handle_run(
        SimpleNamespace(line="line-a", by="line-b", provider="codex"), client
    )

    assert out == record
    assert client.calls == [
        (
            "POST",
            "/api/v1/reviews",
            {
                "line": "line-a",
                "reviewer": "line-b",
                "provider": "codex",
                "root": str(fleet_root.resolve()),
            },
        )
    ]


def test_review_status_gets_the_queue_with_the_optional_line_filter(fleet_root):
    client = RecordingClient(get_result={"reviews": [{"id": "r-1"}]})

    out = review.handle_status(SimpleNamespace(line="line-a"), client)

    assert out == {"reviews": [{"id": "r-1"}]}
    assert client.calls == [
        ("GET", "/api/v1/reviews", {"limit": 100, "line": "line-a"})
    ]


def test_review_status_is_empty_on_a_fresh_queue(fleet_root):
    client = RecordingClient()
    assert review.handle_status(SimpleNamespace(line=None), client) == {"reviews": []}
    assert client.calls == [("GET", "/api/v1/reviews", {"limit": 100})]


def test_review_verdict_posts_the_verdict_and_note(fleet_root):
    client = RecordingClient(post_result={"id": "r-1", "verdict": "merge"})

    out = review.handle_verdict(
        SimpleNamespace(
            review_id="11111111-1111-1111-1111-111111111111",
            verdict="MERGE",
            note="tests reviewed",
        ),
        client,
    )

    assert out["verdict"] == "merge"
    method, path, payload = client.calls[0]
    assert (method, path) == (
        "POST",
        "/api/v1/reviews/11111111-1111-1111-1111-111111111111/verdict",
    )
    assert payload == {
        "verdict": "merge",
        "note": "tests reviewed",
        "root": str(fleet_root.resolve()),
    }


@pytest.mark.parametrize("review_id", ["", "..", "../other", "1111/2222", "x" * 65])
def test_review_verdict_rejects_ids_that_are_not_review_ids(fleet_root, review_id):
    client = RecordingClient()
    with pytest.raises(CLIError, match="not a review id"):
        review.handle_verdict(
            SimpleNamespace(review_id=review_id, verdict="merge", note=""), client
        )
    assert client.calls == []


def test_review_requires_a_running_server(fleet_root):
    with pytest.raises(CLIError, match="requires a running Conductor server"):
        review.handle_status(SimpleNamespace(line=None), None)
