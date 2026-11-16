"""Isolated HOME for instructions-hub tests. Never the operator HOME."""

from __future__ import annotations

from pathlib import Path

import pytest

# The managed header, its digest, and the wrapper around the body. The size
# budget applies to the file on disk, so the tests need its length.
HEADER_BYTES = len("<!-- managed_by=conductor source=") + 12 + len(" -->\n\n")
FRONT_MATTER_BYTES = len("---\nalwaysApply: true\n---\n\n")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "KIMI_CODE_HOME",
        "GROK_HOME",
        "CURSOR_CONFIG_DIR",
        "MINIMAX_DATA_DIR",
        "STEP_CODING_AGENT_DIR",
        "DSH_HOME",
        "DSH_AGENTS_HOME",
        "OPENCODE_CONFIG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def make_library(tmp_path: Path):
    """Build a fragment library with its own profiles."""

    def _make(
        fragments: dict[str, str],
        profiles: dict[str, dict[str, object]],
        *,
        name: str = "lib",
    ) -> Path:
        library = tmp_path / name
        (library / "fragments").mkdir(parents=True)
        (library / "profiles").mkdir(parents=True)
        for stem, text in fragments.items():
            (library / "fragments" / f"{stem}.md").write_text(text, encoding="utf-8")
        for stem, data in profiles.items():
            (library / "profiles" / f"{stem}.toml").write_text(
                _profile_toml(data), encoding="utf-8"
            )
        return library

    return _make


def _profile_toml(data: dict[str, object]) -> str:
    names = data.get("fragments") or []
    lines = ["fragments = [" + ", ".join(repr(str(item)) for item in names) + "]"]
    extra = data.get("extra") or {}
    if extra:
        pairs = ", ".join(
            f"{key} = [" + ", ".join(repr(str(item)) for item in values) + "]"
            for key, values in dict(extra).items()
        )
        lines.append(f"extra = {{ {pairs} }}")
    return "\n".join(lines) + "\n"
