"""GET /api/v1/fleet-skills — read-only per-agent skill inventory."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.skills_hub import manager


def make_skill(repo: Path, name: str) -> None:
    skill = repo / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\nbody\n", encoding="utf-8"
    )


@pytest_asyncio.fixture
async def fleet_client():
    from src.server.app.fleet_skills import router

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_inventory_lists_repo_and_per_agent_skills(
    fleet_client, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)
    repo = home / ".conductor" / "skills"
    make_skill(repo, "alpha")
    manager.install(repo, [("zcode", home / ".zcode" / "skills")], ["alpha"])

    resp = await fleet_client.get("/api/v1/fleet-skills")

    assert resp.status_code == 200
    body = resp.json()
    assert body["repo"] == str(repo)
    assert [s["name"] for s in body["skills"]] == ["alpha"]
    zcode = body["agents"]["zcode"]
    assert zcode["dir"] == str(home / ".zcode" / "skills")
    assert [e["name"] for e in zcode["entries"]] == ["alpha"]
    assert zcode["entries"][0]["managed"] is True


@pytest.mark.asyncio
async def test_inventory_agent_filter(fleet_client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)

    resp = await fleet_client.get("/api/v1/fleet-skills", params={"agent": "claude"})

    assert resp.status_code == 200
    assert list(resp.json()["agents"]) == ["claude"]


@pytest.mark.asyncio
async def test_inventory_rejects_unknown_agent(fleet_client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)

    resp = await fleet_client.get("/api/v1/fleet-skills", params={"agent": "nobody"})

    assert resp.status_code == 404
    assert "unknown agents" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_inventory_omits_skill_paths_and_sources(
    fleet_client, tmp_path, monkeypatch
):
    # Every authenticated user on a shared host can call this endpoint; the
    # response must not name per-skill absolute paths or import sources (a
    # git URL can embed a token).
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)
    repo = home / ".conductor" / "skills"
    make_skill(repo, "alpha")
    from src.skills_hub.core import load_manifest, write_manifest

    manifest = load_manifest(repo)
    manifest["alpha"] = {
        "agents": [],
        "source": "https://user:secret-token@example.com/fleet-skills.git",
        "source_ref": "main",
    }
    write_manifest(repo, manifest)

    resp = await fleet_client.get("/api/v1/fleet-skills")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["skills"][0]) == {"name", "description", "installed"}
    assert body["skills"][0]["name"] == "alpha"
    assert "secret-token" not in resp.text


@pytest.mark.asyncio
async def test_endpoint_is_read_only(fleet_client):
    # The dashboard surface must not expose any mutating verb.
    resp = await fleet_client.post("/api/v1/fleet-skills", json={"skill": "x"})
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_inventory_requires_authenticated_identity(fleet_client, monkeypatch):
    # Platform mode must 401 an anonymous caller like every other dashboard
    # route; the response names local directories and import sources.
    import src.server.utils.api as api_utils

    monkeypatch.setattr(api_utils, "HOST_MODE", "platform")

    resp = await fleet_client.get("/api/v1/fleet-skills")

    assert resp.status_code == 401
