"""Differential-harness fixture schema (Phase 1 skeleton).

A fixture is one recorded flow: the HTTP exchanges, SSE streams and DB snapshots
that a flow produced. The harness compares two fixtures (old path vs new path)
without contacting a provider; capture wiring (Playwright against the M2 stack)
is Phase 3/4 work, so the committed F1–F3 fixtures are **synthetic baselines**
built from the documented wire contracts and marked ``synthetic: true``.

Keeping the schema here (not inside the test suite) is deliberate: the same
loader/validator is used by the CLI, by the unit tests and — later — by the
capture script, so a fixture that validates in CI is a fixture the compare step
can read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The flows Phase 1 promises to compare (docs/refactor/0006-differential-harness.md).
FLOWS = ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8")


class FixtureError(ValueError):
    """A fixture file is missing a required field or has the wrong shape."""


@dataclass(frozen=True)
class Exchange:
    """One HTTP request/response pair, already redacted at capture time."""

    method: str
    path: str
    status: int
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "request": self.request,
            "response": self.response,
        }


@dataclass(frozen=True)
class SseEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "payload": self.payload}


@dataclass(frozen=True)
class SseStream:
    label: str
    events: tuple[SseEvent, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "events": [event.to_dict() for event in self.events]}


@dataclass(frozen=True)
class DbSnapshot:
    """Normalized rows of one table; ``keys`` define row identity for sorting."""

    table: str
    rows: tuple[dict[str, Any], ...]
    keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "rows": list(self.rows), "keys": list(self.keys)}


@dataclass(frozen=True)
class Fixture:
    flow: str
    revision: str
    app_revision: str = ""
    captured_at: str = ""
    synthetic: bool = True
    http: tuple[Exchange, ...] = ()
    sse: tuple[SseStream, ...] = ()
    db: tuple[DbSnapshot, ...] = ()
    ui: dict[str, Any] | None = None

    def validate(self) -> None:
        if self.flow not in FLOWS:
            raise FixtureError(f"unknown flow {self.flow!r}; known: {list(FLOWS)}")
        if not self.revision.strip():
            raise FixtureError("fixture needs a revision label")
        for exchange in self.http:
            if exchange.method.upper() != exchange.method:
                raise FixtureError(f"HTTP method must be uppercase: {exchange.method!r}")
            if not exchange.path.startswith("/"):
                raise FixtureError(f"HTTP path must be absolute: {exchange.path!r}")
            if not 100 <= int(exchange.status) <= 599:
                raise FixtureError(f"HTTP status out of range: {exchange.status!r}")
        for stream in self.sse:
            if not stream.label.strip():
                raise FixtureError("SSE stream needs a label")
            for event in stream.events:
                if not event.type.strip():
                    raise FixtureError("SSE event needs a type")
        for snapshot in self.db:
            if not snapshot.table.strip():
                raise FixtureError("DB snapshot needs a table")
            for row in snapshot.rows:
                if not isinstance(row, dict):
                    raise FixtureError(f"DB row must be an object: {row!r}")
                if snapshot.keys and not all(key in row for key in snapshot.keys):
                    raise FixtureError(
                        f"DB row is missing identity keys {snapshot.keys}: {row!r}"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow,
            "revision": self.revision,
            "app_revision": self.app_revision,
            "captured_at": self.captured_at,
            "synthetic": self.synthetic,
            "http": [exchange.to_dict() for exchange in self.http],
            "sse": [stream.to_dict() for stream in self.sse],
            "db": [snapshot.to_dict() for snapshot in self.db],
            "ui": self.ui,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _require(mapping: dict[str, Any], key: str, kind: type, where: str) -> Any:
    value = mapping.get(key)
    if not isinstance(value, kind):
        raise FixtureError(f"{where}: {key!r} must be {kind.__name__}")
    return value


def load_fixture(path: Path) -> Fixture:
    """Load and structurally validate one fixture file."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"{path}: cannot read fixture ({exc})") from exc
    if not isinstance(raw, dict):
        raise FixtureError(f"{path}: fixture root must be an object")
    where = path.name
    http = tuple(
        Exchange(
            method=_require(item, "method", str, where),
            path=_require(item, "path", str, where),
            status=int(_require(item, "status", int, where)),
            request=_require(item, "request", dict, where),
            response=_require(item, "response", dict, where),
        )
        for item in _require(raw, "http", list, where)
    )
    sse = tuple(
        SseStream(
            label=_require(stream, "label", str, where),
            events=tuple(
                SseEvent(
                    type=_require(event, "type", str, where),
                    payload=_require(event, "payload", dict, where),
                )
                for event in _require(stream, "events", list, where)
            ),
        )
        for stream in _require(raw, "sse", list, where)
    )
    db = tuple(
        DbSnapshot(
            table=_require(snapshot, "table", str, where),
            rows=tuple(_require(snapshot, "rows", list, where)),
            keys=tuple(_require(snapshot, "keys", list, where)),
        )
        for snapshot in _require(raw, "db", list, where)
    )
    fixture = Fixture(
        flow=_require(raw, "flow", str, where),
        revision=_require(raw, "revision", str, where),
        app_revision=raw.get("app_revision") or "",
        captured_at=raw.get("captured_at") or "",
        synthetic=bool(raw.get("synthetic", True)),
        http=http,
        sse=sse,
        db=db,
        ui=raw.get("ui"),
    )
    fixture.validate()
    return fixture
