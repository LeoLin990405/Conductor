"""The native capability routes reject requests without a valid token."""

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def native_client():
    from src.server.app.skills import native_router as native_skills_router

    app = FastAPI()
    app.include_router(native_skills_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_native_skills_requires_token(native_client):
    resp = await native_client.get("/api/v1/native/skills")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_native_skill_content_requires_token(native_client):
    resp = await native_client.get("/api/v1/native/skills/chart-annotation")
    assert resp.status_code == 401
