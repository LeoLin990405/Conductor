"""NativeReviewBackend runs each cross-review inside a private writable scratch."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.llms.native_cli.process import merged_env
from src.llms.native_cli.review_scratch import current_review_scratch
from src.server.services.dispatch import NativeReviewBackend

pytestmark = pytest.mark.asyncio


class FakeAdapter:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.seen: list = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        scratch = current_review_scratch()
        env = merged_env(None)
        self.seen.append(
            (scratch, env.get("TMPDIR"), scratch.root.is_dir() if scratch else None)
        )
        return SimpleNamespace(text="LGTM")


@pytest.fixture
def adapter(monkeypatch) -> FakeAdapter:
    fake = FakeAdapter()
    monkeypatch.setattr(
        "src.llms.native_cli.get_read_only_adapter", lambda model, **_kw: fake
    )
    monkeypatch.setattr(
        "src.server.services.dispatch._disabled_by_operator", lambda model: False
    )
    return fake


async def test_review_runs_inside_a_scratch_and_removes_it(tmp_path, adapter) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base = tmp_path / "runs" / "review-scratch"
    backend = NativeReviewBackend(cwd=repo, timeout_seconds=5.0, scratch_dir=base)

    assert await backend.review("codex", "PROMPT") == "LGTM"

    (prompt,) = adapter.prompts
    ((scratch, tmpdir, existed),) = adapter.seen
    assert scratch is not None and existed
    # The note leads; the review prompt (and its closing reply reminder) stays last.
    assert prompt.endswith("\n\nPROMPT")
    assert str(scratch.root) in prompt and "read-only" in prompt
    assert tmpdir == str(scratch.tmp)
    assert scratch.root.parent == base.resolve()
    assert not scratch.root.exists()
    assert os.listdir(base) == []
    assert current_review_scratch() is None


async def test_review_without_scratch_dir_is_unchanged(tmp_path, adapter) -> None:
    backend = NativeReviewBackend(cwd=tmp_path, timeout_seconds=5.0)
    await backend.review("codex", "PROMPT")
    assert adapter.prompts == ["PROMPT"]
    assert adapter.seen[0][0] is None


async def test_overlapping_scratch_falls_back_with_a_warning(tmp_path, adapter, caplog) -> None:
    backend = NativeReviewBackend(
        cwd=tmp_path, timeout_seconds=5.0, scratch_dir=Path(tmp_path) / "inside"
    )
    with caplog.at_level(logging.WARNING):
        assert await backend.review("codex", "PROMPT") == "LGTM"
    assert adapter.prompts == ["PROMPT"]
    assert adapter.seen[0][0] is None
    assert "review scratch refused" in caplog.text
    assert not (tmp_path / "inside").exists()
