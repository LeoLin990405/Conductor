"""Swappable judgment backends: ``llm`` (v1) and ``local`` (interface only)."""

from .llm import LLMJudgmentBackend
from .local import LocalJudgmentBackend, unavailable_reason

__all__ = ["LLMJudgmentBackend", "LocalJudgmentBackend", "unavailable_reason"]
