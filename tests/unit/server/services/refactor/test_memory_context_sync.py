"""Sync helpers from memory_context: the token estimator's CJK/latin rule.

Kept out of ``test_memory_context.py`` because that module is marked ``asyncio``
for the selection tests.
"""

from __future__ import annotations

from src.server.services.refactor.memory_context import estimate_tokens


class TestEstimateTokens:
    def test_latin_is_chars_over_four(self):
        assert estimate_tokens("a" * 40) == 10

    def test_cjk_is_one_per_char(self):
        assert estimate_tokens("天气预报") == 4

    def test_mixed_and_minimum(self):
        assert estimate_tokens("天气 report-42.md") >= 4
        assert estimate_tokens("") == 1
