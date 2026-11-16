"""Judgment tests never touch the real HOME or inherit judgment env vars."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_judgment_env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("CONDUCTOR_JUDGMENT_"):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_JUDGMENT_RECORDS", str(tmp_path / "records.jsonl"))
    yield
