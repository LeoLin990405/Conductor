"""Tests for the review_merge configuration section.

The worktree root is machine-shaped and shared with the CLI through
``CONDUCTOR_WT_ROOT``; the test command and timeout are operator knobs for the
server-side merge rehearsal.
"""

from __future__ import annotations

import pytest

from src.config.models import ReviewMergeConfig
from src.config.settings import get_review_merge_config


class TestReviewMergeConfig:
    def test_defaults(self):
        config = ReviewMergeConfig()
        assert config.worktree_root == "~/.conductor/worktrees"
        assert config.test_command == ""
        assert config.test_timeout_seconds == 3600.0
        assert config.output_tail_chars == 2000
        assert config.codex_command == "codex"
        assert config.codex_timeout_seconds == 900.0
        assert config.local_hosts == ["127.0.0.1", "::1", "localhost"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"worktree_root": ""},
            {"test_timeout_seconds": 0},
            {"test_timeout_seconds": 100_000},
            {"codex_timeout_seconds": 100_000},
            {"output_tail_chars": -1},
            {"output_tail_chars": 100_000},
            {"test_command": "x" * 5000},
            {"codex_command": ""},
            {"codex_timeout_seconds": 0},
            {"codex_timeout_seconds": 100000},
            {"local_hosts": []},
            {"local_hosts": ["conductor.local/path"]},
            {"local_hosts": ["has space"]},
            {"local_hosts": ["http://127.0.0.1"]},
        ],
    )
    def test_rejects_out_of_range_values(self, kwargs):
        with pytest.raises(ValueError):
            ReviewMergeConfig(**kwargs)


class TestEnvironmentOverrides:
    def test_worktree_root_is_shared_with_the_cli(self, monkeypatch):
        # A path that looks like a real root; not resolved here.
        monkeypatch.setenv("CONDUCTOR_WT_ROOT", "/srv/conductor-wt")
        assert get_review_merge_config().worktree_root == "/srv/conductor-wt"

    def test_test_command_override(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_MERGE_TEST_CMD", "pytest -q tests/unit")
        assert get_review_merge_config().test_command == "pytest -q tests/unit"

    def test_timeout_is_clamped_and_invalid_values_ignored(self, monkeypatch):
        baseline = get_review_merge_config().test_timeout_seconds
        monkeypatch.setenv("CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS", "999999")
        assert get_review_merge_config().test_timeout_seconds == 7200.0
        monkeypatch.setenv("CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS", "soon")
        assert get_review_merge_config().test_timeout_seconds == baseline
        monkeypatch.setenv("CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS", "0")
        assert get_review_merge_config().test_timeout_seconds == 1.0

    def test_codex_command_and_timeout_overrides(self, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_CODEX_COMMAND", "/opt/codex/bin/codex")
        monkeypatch.setenv("CONDUCTOR_CODEX_TIMEOUT_SECONDS", "120")
        config = get_review_merge_config()
        assert config.codex_command == "/opt/codex/bin/codex"
        assert config.codex_timeout_seconds == 120.0
        monkeypatch.setenv("CONDUCTOR_CODEX_TIMEOUT_SECONDS", "999999")
        assert get_review_merge_config().codex_timeout_seconds == 3600.0
        monkeypatch.setenv("CONDUCTOR_CODEX_TIMEOUT_SECONDS", "soon")
        assert get_review_merge_config().codex_timeout_seconds == 900.0

    def test_local_hosts_override_is_validated(self, monkeypatch):
        monkeypatch.setenv(
            "CONDUCTOR_MERGE_LOCAL_HOSTS", "127.0.0.1, Conductor.Local "
        )
        assert get_review_merge_config().local_hosts == [
            "127.0.0.1",
            "conductor.local",
        ]
        # A malformed host list is ignored, not partially applied.
        monkeypatch.setenv("CONDUCTOR_MERGE_LOCAL_HOSTS", "conductor.local/path")
        assert get_review_merge_config().local_hosts == [
            "127.0.0.1",
            "::1",
            "localhost",
        ]

    def test_blank_overrides_are_ignored(self, monkeypatch):
        before = get_review_merge_config()
        monkeypatch.setenv("CONDUCTOR_WT_ROOT", "  ")
        monkeypatch.setenv("CONDUCTOR_MERGE_TEST_CMD", " ")
        monkeypatch.setenv("CONDUCTOR_MERGE_TEST_TIMEOUT_SECONDS", "")
        monkeypatch.setenv("CONDUCTOR_CODEX_COMMAND", " ")
        monkeypatch.setenv("CONDUCTOR_CODEX_TIMEOUT_SECONDS", "")
        monkeypatch.setenv("CONDUCTOR_MERGE_LOCAL_HOSTS", " ")
        assert get_review_merge_config() == before
