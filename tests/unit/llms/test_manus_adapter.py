"""Manus adapter: remote task lifecycle without network in tests."""

from __future__ import annotations

import httpx
import pytest

import src.llms.manus as manus
from src.llms.native_cli.base import NativeCliError, NativeCliUnavailable
from src.llms.manus import ManusAdapter, load_manus_api_key


@pytest.mark.asyncio
async def test_manus_create_poll_and_result(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key", poll_interval=0.01, poll_timeout_seconds=5)
    calls: list[tuple[str, str]] = []

    async def fake_request(method, path, payload=None):
        calls.append((method, path))
        if path == "/v2/task.create":
            assert payload["message"]["content"].endswith("research")
            assert payload["message"]["content"].startswith("Research only:")
            assert payload["interactive_mode"] is False
            assert payload["share_visibility"] == "private"
            return {"ok": True, "task_id": "task-1", "task_url": "https://manus.im/app/task-1"}
        if path.startswith("/v2/task.listMessages"):
            return {
                "ok": True,
                "messages": [
                    {"type": "status_update", "status_update": {"agent_status": "stopped"}},
                    {"type": "assistant_message", "assistant_message": {"content": "final answer"}},
                    {"type": "status_update", "status_update": {"agent_status": "running"}},
                ],
                "has_more": False,
            }
        if path.startswith("/v2/task.detail"):
            return {"ok": True, "task": {"status": "stopped", "has_running_background_jobs": False}}
        raise AssertionError(path)

    monkeypatch.setattr(adapter, "_request", fake_request)
    result = await adapter.run("research")
    assert result.text == "final answer"
    assert result.session_id == "task-1"
    assert result.usage.total_tokens == 0
    assert adapter.last_task_url == "https://manus.im/app/task-1"
    assert calls[0] == ("POST", "/v2/task.create")
    assert "order=desc" in calls[1][1]


@pytest.mark.asyncio
async def test_manus_message_pagination_reaches_final_status(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key")
    calls: list[str] = []

    async def fake_request(method, path, payload=None):
        calls.append(path)
        if "cursor=" not in path:
            return {
                "ok": True,
                "messages": [{"type": "assistant_message", "assistant_message": {"content": "last"}}],
                "has_more": True,
                "next_cursor": "page-2",
            }
        return {
            "ok": True,
            "messages": [
                {"type": "status_update", "status_update": {"agent_status": "stopped"}},
                {"type": "assistant_message", "assistant_message": {"content": "first"}},
            ],
            "has_more": False,
        }

    monkeypatch.setattr(adapter, "_request", fake_request)
    events = await adapter._messages("task-2", 10**20)
    assert len(events) == 3
    assert "cursor=page-2" in calls[1]
    assert manus._status(events) == "stopped"
    assert manus._assistant_text(events) == "first\nlast"


@pytest.mark.asyncio
async def test_manus_waiting_is_not_reported_as_complete(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key", poll_timeout_seconds=5)

    async def fake_request(method, path, payload=None):
        if path == "/v2/task.create":
            return {"ok": True, "task_id": "task-2"}
        return {"ok": True, "messages": [{"type": "status_update", "status_update": {"agent_status": "waiting"}}]}

    monkeypatch.setattr(adapter, "_request", fake_request)
    with pytest.raises(NativeCliError, match="waiting"):
        await adapter.run("research")


@pytest.mark.asyncio
async def test_manus_does_not_complete_without_detail_background_status(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key", poll_interval=0.01, poll_timeout_seconds=30)
    adapter._poll_timeout_seconds = 0.05

    async def fake_request(method, path, payload=None):
        if path == "/v2/task.create":
            return {"ok": True, "task_id": "task-3"}
        if "task.listMessages" in path:
            return {"ok": True, "messages": [{"type": "status_update", "status_update": {"agent_status": "stopped"}}]}
        return {"ok": True, "task": {"status": "stopped"}}

    monkeypatch.setattr(adapter, "_request", fake_request)
    with pytest.raises(NativeCliError, match="timed out"):
        await adapter.run("research")


@pytest.mark.asyncio
async def test_manus_cancel_calls_task_stop(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key")
    adapter.last_task_id = "task-4"
    seen: list[str] = []

    async def fake_request(method, path, payload=None):
        seen.append(path)
        assert payload == {"task_id": "task-4"}
        return {"ok": True}

    monkeypatch.setattr(adapter, "_request", fake_request)
    await adapter.cancel_remote()
    await adapter.cancel_remote()
    assert seen == ["/v2/task.stop"]


@pytest.mark.asyncio
async def test_manus_credit_balance_returns_official_data_only(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key")

    async def fake_request(method, path, payload=None):
        assert method == "GET"
        assert path == "/v2/usage.availableCredits"
        return {"ok": True, "request_id": "req-fake", "data": {"total_credits": 123, "periodic_credits": 100}}

    monkeypatch.setattr(adapter, "_request", fake_request)
    assert await adapter.available_credits() == {"amount": 123, "currency": "CREDITS"}
    assert await adapter.get_credit_balance() == {"amount": 123, "currency": "CREDITS"}


@pytest.mark.asyncio
async def test_manus_credit_balance_unknown_on_invalid_response_or_request_error(monkeypatch):
    adapter = ManusAdapter(api_key="fake-test-key")

    async def invalid_response(method, path, payload=None):
        return {"ok": True, "data": {"total_credits": True}}

    monkeypatch.setattr(adapter, "_request", invalid_response)
    assert await adapter.available_credits() is None

    async def failed_request(method, path, payload=None):
        raise NativeCliError("sanitized fake error")

    monkeypatch.setattr(adapter, "_request", failed_request)
    assert await adapter.available_credits() is None


def test_manus_loader_reads_only_secure_owned_env_file(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("# ignored\nexport MANUS_API_KEY='fake file key'\n", encoding="utf-8")
    env_file.chmod(0o600)
    assert load_manus_api_key(env_file) == "fake file key"

    env_file.chmod(0o640)
    assert load_manus_api_key(env_file) is None


def test_manus_key_availability_checks_metadata_without_reading_contents(tmp_path, monkeypatch):
    env_dir = tmp_path / ".config" / "manus"
    env_dir.mkdir(parents=True)
    env_file = env_dir / "env"
    env_file.write_text("MANUS_API_KEY=fake file key\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setattr(manus.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(manus.Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    assert manus.manus_api_key_available()


def test_manus_loader_requires_regular_non_symlink_and_never_executes_shell(tmp_path):
    env_file = tmp_path / "env"
    marker = tmp_path / "executed"
    env_file.write_text(f"MANUS_API_KEY=$(touch {marker})\n", encoding="utf-8")
    env_file.chmod(0o600)
    assert load_manus_api_key(env_file) == f"$(touch {marker})"
    assert not marker.exists()

    link = tmp_path / "link"
    link.symlink_to(env_file)
    assert load_manus_api_key(link) is None


def test_explicit_test_key_skips_local_key_loader(monkeypatch):
    def forbidden_loader():
        raise AssertionError("explicit test keys must not read the local key file")

    monkeypatch.setattr(manus, "load_manus_api_key", forbidden_loader)
    adapter = ManusAdapter(api_key="fake-test-key", base_url="http://fake.invalid")
    assert adapter.binary == "manus-api"


def test_explicit_empty_key_skips_local_key_loader(monkeypatch):
    def forbidden_loader():
        raise AssertionError("explicit empty keys must not read the local key file")

    monkeypatch.setattr(manus, "load_manus_api_key", forbidden_loader)
    adapter = ManusAdapter(api_key="")
    with pytest.raises(NativeCliUnavailable, match="Manus API key"):
        _ = adapter.binary


@pytest.mark.asyncio
async def test_manus_http_errors_never_reflect_body_or_key(monkeypatch):
    secret = "fake-test-secret-do-not-leak"
    adapter = ManusAdapter(api_key=secret)

    class Response:
        status_code = 403

        @staticmethod
        def json():
            return {"ok": False, "error": {"message": secret}}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["x-manus-api-key"] == secret

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(manus.httpx, "AsyncClient", Client)
    with pytest.raises(NativeCliError) as exc_info:
        await adapter._request("GET", "/v2/test")
    assert secret not in str(exc_info.value)
    assert "message" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_manus_transport_exception_suppresses_secret_chain(monkeypatch):
    secret = "fake-test-secret-do-not-leak"
    adapter = ManusAdapter(api_key=secret)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            raise httpx.ConnectError(secret)

    monkeypatch.setattr(manus.httpx, "AsyncClient", Client)
    with pytest.raises(NativeCliError) as exc_info:
        await adapter._request("GET", "/v2/test")
    assert secret not in str(exc_info.value)
    assert exc_info.value.__suppress_context__
    assert exc_info.value.__cause__ is None
