"""The Watchdog gets one persistent hidden thread, separate from the Router."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import watchdog_session as session


@pytest.mark.asyncio
async def test_an_existing_watchdog_session_is_reused():
    row = {"user_id": "u-1", "workspace_id": "w-1", "thread_id": "t-1"}
    dispatcher = AsyncMock()
    with patch(
        "src.server.services.autonomy.watchdog_session.session_db.get_watchdog_session",
        AsyncMock(return_value=row),
    ):
        got = await session.ensure_watchdog_session("u-1", dispatcher=dispatcher)
    assert got == row
    dispatcher.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_watchdog_session_is_created_in_home():
    saved = {"user_id": "u-1", "workspace_id": "home-1", "thread_id": "watch-1"}
    dispatcher = AsyncMock()
    dispatcher.create_thread.return_value = "watch-1"
    with (
        patch(
            "src.server.services.autonomy.watchdog_session.session_db.get_watchdog_session",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.watchdog_session.get_home_workspace_id",
            return_value="home-1",
        ),
        patch(
            "src.server.services.autonomy.watchdog_session.session_db.save_watchdog_session",
            AsyncMock(return_value=saved),
        ) as save,
    ):
        got = await session.ensure_watchdog_session("u-1", dispatcher=dispatcher)

    assert got == saved
    dispatcher.create_thread.assert_awaited_once()
    assert dispatcher.create_thread.await_args.kwargs["platform"] == "superagent_watchdog"
    save.assert_awaited_once_with("u-1", workspace_id="home-1", thread_id="watch-1")
