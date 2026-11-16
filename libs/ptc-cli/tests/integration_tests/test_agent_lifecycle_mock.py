"""Mocked integration tests for the API workspace lifecycle."""

from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def api_client(monkeypatch):
    """Use an in-memory client so lifecycle tests never contact the server."""
    from ptc_cli.agent import lifecycle

    client = Mock()
    client.workspace_id = None
    client.create_workspace = AsyncMock(return_value={"workspace_id": "workspace-new"})
    client.get_workspace = AsyncMock(return_value={"status": "running"})
    client.start_workspace = AsyncMock()
    client.close = AsyncMock()
    monkeypatch.setattr(
        lifecycle,
        "SSEStreamClient",
        lambda **_kwargs: client,
    )
    return client


class TestCreateApiSession:
    """Test workspace creation, reuse and session persistence."""

    @pytest.mark.asyncio
    async def test_new_workspace_is_persisted(self, api_client, monkeypatch):
        from ptc_cli.agent import lifecycle

        save = Mock()
        monkeypatch.setattr(lifecycle, "load_persisted_session", Mock(return_value=None))
        monkeypatch.setattr(lifecycle, "save_persisted_session", save)

        client, workspace_id, reused = await lifecycle.create_api_session("agent-a")

        assert client is api_client
        assert workspace_id == "workspace-new"
        assert api_client.workspace_id == workspace_id
        assert reused is False
        api_client.create_workspace.assert_awaited_once_with(name="cli-agent-a")
        save.assert_called_once_with("agent-a", workspace_id)

    @pytest.mark.asyncio
    async def test_persisted_workspace_is_reused_and_recency_updated(self, api_client, monkeypatch):
        from ptc_cli.agent import lifecycle

        update = Mock()
        monkeypatch.setattr(
            lifecycle,
            "load_persisted_session",
            Mock(return_value={"workspace_id": "workspace-existing"}),
        )
        monkeypatch.setattr(lifecycle, "update_session_last_used", update)

        client, workspace_id, reused = await lifecycle.create_api_session("agent-a")

        assert client is api_client
        assert workspace_id == "workspace-existing"
        assert api_client.workspace_id == workspace_id
        assert reused is True
        api_client.get_workspace.assert_awaited_once_with(workspace_id)
        api_client.create_workspace.assert_not_awaited()
        update.assert_called_once_with("agent-a")

    @pytest.mark.asyncio
    async def test_stopped_persisted_workspace_is_started(self, api_client, monkeypatch):
        from ptc_cli.agent import lifecycle

        api_client.get_workspace.return_value = {"status": "stopped"}
        monkeypatch.setattr(
            lifecycle,
            "load_persisted_session",
            Mock(return_value={"workspace_id": "workspace-stopped"}),
        )
        update = Mock()
        monkeypatch.setattr(lifecycle, "update_session_last_used", update)

        _client, workspace_id, reused = await lifecycle.create_api_session("agent-a")

        assert workspace_id == "workspace-stopped"
        assert reused is True
        api_client.start_workspace.assert_awaited_once_with(workspace_id)
        update.assert_called_once_with("agent-a")

    @pytest.mark.asyncio
    async def test_missing_persisted_workspace_is_deleted_then_recreated(self, api_client, monkeypatch):
        from ptc_cli.agent import lifecycle

        api_client.get_workspace.return_value = None
        persisted = {"workspace_id": "workspace-gone"}
        delete = Mock()
        save = Mock()
        monkeypatch.setattr(lifecycle, "load_persisted_session", Mock(return_value=persisted))
        monkeypatch.setattr(lifecycle, "delete_persisted_session", delete)
        monkeypatch.setattr(lifecycle, "save_persisted_session", save)

        _client, workspace_id, reused = await lifecycle.create_api_session("agent-a")

        assert workspace_id == "workspace-new"
        assert reused is False
        delete.assert_called_once_with("agent-a")
        save.assert_called_once_with("agent-a", workspace_id)

    @pytest.mark.asyncio
    async def test_persist_session_false_forces_a_new_workspace(self, api_client, monkeypatch):
        from ptc_cli.agent import lifecycle

        load = Mock()
        save = Mock()
        monkeypatch.setattr(lifecycle, "load_persisted_session", load)
        monkeypatch.setattr(lifecycle, "save_persisted_session", save)

        _client, workspace_id, reused = await lifecycle.create_api_session(
            "agent-a", persist_session=False
        )

        assert workspace_id == "workspace-new"
        assert reused is False
        load.assert_not_called()
        save.assert_not_called()
        api_client.create_workspace.assert_awaited_once_with(name="cli-agent-a")
