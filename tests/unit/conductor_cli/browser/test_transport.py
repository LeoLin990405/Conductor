from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from src.conductor_cli.client import CLIError
from src.conductor_cli.browser import transport


def test_profile_path_is_agent_scoped_and_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    profile = transport.profile_for_agent("agent_7")

    assert profile == tmp_path / ".conductor" / "agent-browser" / "agent_7"
    for path in (profile.parent.parent, profile.parent, profile):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.stat().st_uid == os.getuid()


@pytest.mark.parametrize("agent", ["", "../escape", "a/b", ".", "two words", "a" * 65])
def test_profile_rejects_agent_path_traversal(agent: str) -> None:
    with pytest.raises(CLIError):
        transport.validate_agent(agent)


def test_profile_refuses_symlinked_agent_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    external = tmp_path / "outside"
    external.mkdir(mode=0o700)
    base = tmp_path / ".conductor" / "agent-browser"
    base.mkdir(parents=True, mode=0o700)
    (base / "agent").symlink_to(external, target_is_directory=True)

    with pytest.raises(CLIError):
        transport.profile_for_agent("agent")


class FakeSocket:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, path: str) -> None:
        self.path = path

    def sendall(self, payload: bytes) -> None:
        self.request = json.loads(payload)

    def recv(self, size: int) -> bytes:
        response, self.response = self.response, b""
        return response

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def test_request_uses_private_unix_socket_and_receives_bounded_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeSocket(b'{"status":"ready"}\n')
    socket_path = Path("/fake/control.sock")

    def connect(path: Path, timeout: float) -> FakeSocket:
        fake.settimeout(timeout)
        fake.connect(str(path))
        return fake

    monkeypatch.setattr(transport, "_socket_path", lambda _profile: socket_path)
    monkeypatch.setattr(transport, "_connect", connect)

    response = transport._request(tmp_path, {"command": "ping"}, timeout=3)

    assert response == {"status": "ready"}
    assert fake.timeout == 3
    assert fake.request == {"command": "ping"}
    assert fake.path == str(socket_path)


def test_request_rejects_non_json_or_oversized_responses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(transport, "_socket_path", lambda _profile: Path("/fake/control.sock"))
    for response in (b"not-json\n", b"x" * (transport.MAX_MESSAGE_BYTES + 1)):
        monkeypatch.setattr(transport, "_connect", lambda path, timeout, payload=response: FakeSocket(payload))
        with pytest.raises(CLIError, match="invalid response"):
            transport._request(tmp_path, {"command": "ping"})


def test_request_rejects_oversized_request_before_connect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    connected = False

    def connect(_path: Path, _timeout: float):
        nonlocal connected
        connected = True
        raise AssertionError("oversized request must not connect")

    monkeypatch.setattr(transport, "_connect", connect)
    with pytest.raises(CLIError, match="too large"):
        transport._request(tmp_path, {"command": "run", "steps": ["x" * transport.MAX_MESSAGE_BYTES]})
    assert connected is False


def test_owner_startup_timeout_terminates_only_spawned_child_and_sets_trusted_import_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / ".conductor" / "agent-browser" / "agent"
    profile.mkdir(parents=True, mode=0o700)
    process = type(
        "Child",
        (),
        {
            "returncode": None,
            "poll": lambda self: self.returncode,
            "terminate": lambda self: setattr(self, "returncode", 0),
            "kill": lambda self: setattr(self, "returncode", -9),
            "wait": lambda self, timeout=None: self.returncode,
        },
    )()
    captured: dict[str, object] = {}

    def spawn(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return process

    def unavailable(_profile: Path, _request: dict, timeout: float = 10.0):
        raise CLIError("not ready")

    monkeypatch.setattr(transport, "_request", unavailable)
    monkeypatch.setattr(transport, "_socket_path", lambda _profile: Path("/fake/control.sock"))
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    with pytest.raises(CLIError, match="did not become ready"):
        transport.ensure_owner(profile, startup_timeout=0, popen_factory=spawn)

    assert process.returncode == 0
    assert captured["args"][2] == "conductor_cli.browser.owner"
    assert captured["cwd"] == str(Path(transport.__file__).resolve().parents[3])
    assert captured["env"]["PYTHONPATH"] == str(Path(transport.__file__).resolve().parents[2])
    assert "LANGSMITH_API_KEY" not in captured["env"]
