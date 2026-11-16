"""Small site adapters over the browser's safe semantic snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit


class AdapterError(ValueError):
    """A safe adapter selection or argument error."""


@dataclass(frozen=True)
class AdapterAction:
    name: str
    summary: str
    schema: dict[str, Any]


class SiteAdapter(Protocol):
    name: str
    summary: str
    domains: tuple[str, ...]
    actions: tuple[AdapterAction, ...]

    def matches(self, url: str) -> bool: ...

    def validate(self, action: str, args: dict[str, Any]) -> None: ...

    def execute(self, action: str, args: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]: ...


def _nodes(snapshot: dict[str, Any]):
    def walk(items: Any):
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            yield item
            yield from walk(item.get("children"))

    for frame in snapshot.get("frames", []):
        if isinstance(frame, dict) and frame.get("status") == "ok":
            yield from walk(frame.get("items"))


def _text(node: dict[str, Any]) -> str:
    value = node.get("text") or node.get("accessible_name")
    return value[:400] if isinstance(value, str) else ""


class DocsAdapter:
    name = "docs"
    summary = "Read headings and visible links from documentation pages."
    domains = ("docs.example.test",)
    actions = (
        AdapterAction("read", "Return page title and concise semantic content.", {"type": "object", "properties": {}, "additionalProperties": False}),
        AdapterAction("headings", "Return visible headings.", {"type": "object", "properties": {}, "additionalProperties": False}),
        AdapterAction("links", "Return visible link labels.", {"type": "object", "properties": {}, "additionalProperties": False}),
    )

    def matches(self, url: str) -> bool:
        return _host(url) in self.domains

    def validate(self, action: str, args: dict[str, Any]) -> None:
        _validate_action(self.actions, action, args)

    def execute(self, action: str, args: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        self.validate(action, args)
        nodes = list(_nodes(snapshot))
        headings = [_text(node) for node in nodes if _role(node) == "heading" and _text(node)]
        links = [_text(node) for node in nodes if _role(node) == "link" and _text(node)]
        if action == "headings":
            return {"url": snapshot.get("url", ""), "headings": headings[:40]}
        if action == "links":
            return {"url": snapshot.get("url", ""), "links": links[:40]}
        return {"url": snapshot.get("url", ""), "title": snapshot.get("title", ""), "headings": headings[:40], "links": links[:40]}


class CatalogAdapter:
    name = "catalog"
    summary = "Read visible item names from a catalog page."
    domains = ("catalog.example.test",)
    actions = (
        AdapterAction("read", "Return the page title and visible catalog item labels.", {"type": "object", "properties": {}, "additionalProperties": False}),
        AdapterAction("items", "Return visible catalog item labels.", {"type": "object", "properties": {}, "additionalProperties": False}),
    )

    def matches(self, url: str) -> bool:
        return _host(url) in self.domains

    def validate(self, action: str, args: dict[str, Any]) -> None:
        _validate_action(self.actions, action, args)

    def execute(self, action: str, args: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        self.validate(action, args)
        items = [
            _text(node)
            for node in _nodes(snapshot)
            if _role(node) in {"listitem", "article"} and _text(node)
        ]
        if action == "items":
            return {"url": snapshot.get("url", ""), "items": items[:40]}
        return {"url": snapshot.get("url", ""), "title": snapshot.get("title", ""), "items": items[:40]}


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _role(node: dict[str, Any]) -> str:
    value = node.get("role", "")
    return value if isinstance(value, str) else ""


def _validate_action(actions: tuple[AdapterAction, ...], action: str, args: dict[str, Any]) -> None:
    if action not in {item.name for item in actions}:
        raise AdapterError("Unknown site action")
    if args:
        raise AdapterError("This site action does not accept arguments")


DEFAULT_ADAPTERS: tuple[SiteAdapter, ...] = (DocsAdapter(), CatalogAdapter())
