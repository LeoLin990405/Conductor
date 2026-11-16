"""Opt-in API tests for workspace session persistence.

Set ``PTC_CLI_TEST_SERVER_URL`` to an isolated Conductor test server to run
these tests. They create and delete temporary workspaces on that server.
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("PTC_CLI_TEST_SERVER_URL"),
    reason="PTC_CLI_TEST_SERVER_URL not set; live workspace tests are opt-in",
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    return home


class TestSessionPersistenceReal:
    """Test persistence against an explicitly selected isolated API server."""

    @pytest.mark.asyncio
    async def test_workspace_id_is_persisted_on_create(self, fake_home):
        from ptc_cli.agent.lifecycle import create_api_session
        from ptc_cli.agent.persistence import delete_persisted_session, load_persisted_session

        agent_name = f"audit-persist-{uuid.uuid4().hex[:12]}"
        client, workspace_id, reused = await create_api_session(
            agent_name,
            server_url=os.environ["PTC_CLI_TEST_SERVER_URL"],
        )

        try:
            assert reused is False
            persisted = load_persisted_session(agent_name)
            assert persisted is not None
            assert persisted["workspace_id"] == workspace_id
        finally:
            delete_persisted_session(agent_name)
            await client.delete_workspace(workspace_id)
            await client.close()

    @pytest.mark.asyncio
    async def test_second_session_reuses_the_persisted_workspace(self, fake_home):
        from ptc_cli.agent.lifecycle import create_api_session
        from ptc_cli.agent.persistence import delete_persisted_session

        agent_name = f"audit-reuse-{uuid.uuid4().hex[:12]}"
        server_url = os.environ["PTC_CLI_TEST_SERVER_URL"]
        first_client, workspace_id, first_reused = await create_api_session(
            agent_name,
            server_url=server_url,
        )
        second_client = None

        try:
            second_client, reused_id, reused = await create_api_session(
                agent_name,
                server_url=server_url,
            )
            assert first_reused is False
            assert reused is True
            assert reused_id == workspace_id
        finally:
            delete_persisted_session(agent_name)
            if second_client is not None:
                await second_client.delete_workspace(workspace_id)
                await second_client.close()
            else:
                await first_client.delete_workspace(workspace_id)
            await first_client.close()
