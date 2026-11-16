"""Stable JSON result shape for native browser capabilities."""

from __future__ import annotations

from typing import Any


def success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def failure(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"code": code, "message": message, "hint": hint}}
