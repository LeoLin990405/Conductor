"""Shared fixtures: an isolated HOME so no test ever touches the real one."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)
    monkeypatch.delenv("CONDUCTOR_SKILLS_CONFIG", raising=False)
    return home


@pytest.fixture
def repo(home: Path) -> Path:
    from src.skills_hub.config import load_config

    return load_config(home=home).repo
