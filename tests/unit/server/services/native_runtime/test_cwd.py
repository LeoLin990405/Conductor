"""Native cwd is a host-security boundary, not a free-form path."""

from __future__ import annotations

import pytest

from src.server.services.native_runtime import cwd
from src.server.services.native_runtime.cwd import resolve_native_cwd


def test_none_keeps_the_private_default():
    assert resolve_native_cwd(None) is None
    assert resolve_native_cwd("  ") is None


def test_existing_home_directory_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    assert resolve_native_cwd(str(project)) == str(project.resolve())


def test_outside_allowed_roots_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    home.mkdir()
    monkeypatch.setattr(cwd, "_allowed_roots", lambda: [home.resolve()])
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="must be inside"):
        resolve_native_cwd(str(outside))


def test_missing_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ValueError, match="not an existing directory"):
        resolve_native_cwd(str(tmp_path / "missing"))
