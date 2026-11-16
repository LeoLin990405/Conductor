"""Integration tests for ``UserDataBackend`` against a real Postgres.

Validates that profile preference saves preserve the server-managed
``other_preference`` column and serialize with dashboard preference writes.

Every write goes through the backend, as the agent's Write tool
(``awrite_text`` after a Read) or the file mount (``awrite_versioned``) makes
it, so the lock, the version check and the commit are the ones that ship.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ptc_agent.agent.backends.user_data import UserDataBackend
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.server.database import user as user_db
from src.server.services import user_data_io as io
from src.server.services.profile_files import (
    PREFERENCE_FILE,
    PreferenceFile,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

PROFILE = "/work/.agents/user/profile/"


class _StubSandbox:
    """The backend reads and writes rows, never the sandbox's files."""

    def normalize_path(self, p): return p
    def virtualize_path(self, p): return p
    def validate_path(self, p): return True
    @property
    def filesystem_config(self): return None


def _backend(user_id: str) -> UserDataBackend:
    return UserDataBackend(
        user_id=user_id,
        call=CallContext(),
        sandbox_backend=_StubSandbox(),  # type: ignore[arg-type]
        root_prefix=PROFILE,
    )


async def _read(backend: UserDataBackend, filename: str) -> None:
    """The Read a Write is checked against, of the whole file."""
    assert await backend.aread_range(PROFILE + filename) is not None


async def _write(backend: UserDataBackend, filename: str, content: dict) -> Any:
    return await backend.awrite_text(PROFILE + filename, io.serialize_json(content))


async def _save(user_id: str, filename: str, content: dict) -> None:
    """Read then Write, as the agent's tools save."""
    backend = _backend(user_id)
    await _read(backend, filename)
    await _write(backend, filename, content)


# ---------------------------------------------------------------------------
# Preferences: other_preference preservation
# ---------------------------------------------------------------------------


class TestPreferenceApply:
    async def test_first_insert_seeds_other_preference_empty(
        self, seed_user, patched_get_db_connection,
    ):
        user_id = seed_user["user_id"]
        await _save(
            user_id,
            PREFERENCE_FILE,
            {"agent_preference": {"default_model": "claude-sonnet"}},
        )

        row = await io.fetch_preferences_for_user(user_id)
        assert row is not None
        assert row["agent_preference"] == {"default_model": "claude-sonnet"}
        # other_preference seeded to empty object on first insert
        assert row["other_preference"] == {}

    async def test_update_preserves_other_preference(
        self, seed_user, patched_get_db_connection, test_db_pool,
    ):
        """Server-managed `other_preference` survives an agent edit."""
        user_id = seed_user["user_id"]

        # First insert (agent path)
        await _save(
            user_id, PREFERENCE_FILE, {"agent_preference": {"tone": "brief"}}
        )

        # Server-managed state survives while an agent-visible field changes.
        await user_db.upsert_user_preferences(
            user_id, other_preference={"onboarding_step": 3}
        )

        # Agent edits its slice
        await _save(
            user_id,
            PREFERENCE_FILE,
            {"agent_preference": {"tone": "concise"}},
        )

        row = await io.fetch_preferences_for_user(user_id)
        assert row["agent_preference"] == {"tone": "concise"}
        # other_preference must be untouched
        assert row["other_preference"] == {"onboarding_step": 3}


# ---------------------------------------------------------------------------
# Dashboard preference writes against a save in flight
# ---------------------------------------------------------------------------


async def _preference_race(user_id: str):
    await _save(user_id, PREFERENCE_FILE, {"agent_preference": {"tone": "low"}})

    def crud():
        return user_db.upsert_user_preferences(
            user_id, other_preference={"onboarding_step": 3}
        )

    def landed(row):
        assert (row["agent_preference"], row["other_preference"]) == (
            {"tone": "high"}, {"onboarding_step": 3},
        )

    written = {"agent_preference": {"tone": "high"}}
    return PreferenceFile, PREFERENCE_FILE, written, crud, landed


class TestPreferenceCrudWriteDuringSave:
    async def test_crud_write_is_not_lost(
        self, seed_user, patched_get_db_connection, monkeypatch,
    ):
        """A dashboard or tool write that comes while a save holds its read
        lands after the save rather than under it.

        The save starts the CRUD write right after its read and waits up to
        half a second for it. A CRUD write outside the profile lock commits
        in that window, and the save's diff, planned from the rows it read,
        then writes the old values back over it.
        """
        user_id = seed_user["user_id"]
        file_cls, filename, written, crud, landed = await _preference_race(user_id)
        real_fetch = file_cls.fetch
        started: list[asyncio.Task] = []

        async def fetch(self, user_id, conn=None):
            rows = await real_fetch(self, user_id, conn)
            if conn is not None and not started:
                started.append(asyncio.create_task(crud()))
                await asyncio.wait(started, timeout=0.5)
            return rows

        monkeypatch.setattr(file_cls, "fetch", fetch)
        await _save(user_id, filename, written)
        await started[0]

        async with patched_get_db_connection() as conn:
            landed(await real_fetch(file_cls(), user_id, conn))
