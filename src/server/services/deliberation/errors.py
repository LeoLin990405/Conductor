"""Deliberation failures. ``code`` is the stable client-facing reason."""

from __future__ import annotations


class DeliberationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
