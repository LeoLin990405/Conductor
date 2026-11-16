"""Server-backed ``conductor merge`` handler.

The pinning and rehearsal moved into the server service (covered by
``tests/unit/server/services/autonomy/test_review_merge_service.py`` and the
end-to-end API test); the CLI keeps its flags and only posts the request.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.conductor_cli import merge
from src.conductor_cli.client import CLIError


class RecordingClient:
    def __init__(self, *, result=None):
        self.calls: list[tuple] = []
        self.timeouts: list[float | None] = []
        self.result = result if result is not None else {"merged": True}

    def post(self, path: str, json=None, *, timeout: float | None = None):
        self.calls.append(("POST", path, json))
        self.timeouts.append(timeout)
        return self.result


def _line(root, name):
    path = root / name
    path.mkdir()
    (path / ".git").mkdir()
    return path


@pytest.fixture
def fleet_root(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    _line(root, "line")
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(root))
    return root


def test_merge_posts_the_request_and_returns_the_server_result(fleet_root):
    result = {
        "merged": True,
        "line": "line",
        "branch": "feat/line",
        "into": "integrate/r2",
        "commit": "c" * 40,
        "tested_sha": "a" * 40,
        "tests": "green",
        "pushed": False,
    }
    client = RecordingClient(result=result)

    out = merge.handle_merge(
        SimpleNamespace(line="line", into="integrate/r2", test_cmd=None), client
    )

    assert out == result
    assert client.calls == [
        (
            "POST",
            "/api/v1/merges",
            {
                "line": "line",
                "into": "integrate/r2",
                "root": str(fleet_root.resolve()),
            },
        )
    ]
    # The merge request outlives the client's default 30s API timeout.
    assert client.timeouts[0] == merge.merge_timeout()
    assert client.timeouts[0] >= 3600


def test_merge_forwards_the_operator_test_command(fleet_root):
    client = RecordingClient()

    merge.handle_merge(
        SimpleNamespace(line="line", into="integrate/r2", test_cmd="pytest -q"),
        client,
    )

    assert client.calls[0][2]["test_cmd"] == "pytest -q"


def test_merge_refuses_unknown_lines_before_the_api(fleet_root):
    client = RecordingClient()
    with pytest.raises(CLIError, match="Unknown line"):
        merge.handle_merge(
            SimpleNamespace(line="nope", into="integrate/r2", test_cmd=None), client
        )
    assert client.calls == []


def test_merge_timeout_is_configurable_and_clamped(monkeypatch):
    # The default covers the maximum server budget (7200s tests + 3600s Codex).
    assert merge.merge_timeout() == 11_100.0
    monkeypatch.setenv("CONDUCTOR_MERGE_TIMEOUT_SECONDS", "1800")
    assert merge.merge_timeout() == 1800.0
    monkeypatch.setenv("CONDUCTOR_MERGE_TIMEOUT_SECONDS", "5")
    assert merge.merge_timeout() == 60.0
    monkeypatch.setenv("CONDUCTOR_MERGE_TIMEOUT_SECONDS", "soon")
    assert merge.merge_timeout() == 11_100.0


def test_merge_requires_a_running_server(fleet_root):
    with pytest.raises(CLIError, match="requires a running Conductor server"):
        merge.handle_merge(
            SimpleNamespace(line="line", into="integrate/r2", test_cmd=None), None
        )
