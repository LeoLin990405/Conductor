"""Structural comparison of two normalized fixtures.

The policy is the one in ``docs/refactor/0006-differential-harness.md``:

- protocol shape (methods, paths, statuses, SSE event types, dict keys, list
  lengths, DB row counts) must match exactly;
- numeric volatility (token counts, latencies) is tolerated within 5% (or ±1 for
  small values) and reported;
- free-text content differences are reported and tolerated — they are the one
  place a deterministic diff cannot decide, so they are surfaced for a reviewer
  instead of silently passing or failing;
- everything else is a failure.

Tolerated differences still show up in the report. A run is ``ok`` when there is
no untolerated difference, so "passes with 3 flagged diffs" is visible rather
than hidden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from scripts.refactor_diff.schema import Fixture

#: Keys whose value is prose. A deterministic comparison cannot judge these, so
#: a diff is flagged and tolerated for a reviewer looking at the report.
TEXT_KEYS = frozenset({"content", "text", "summary", "message", "reply", "delta"})

#: Numeric keys compared with a relative tolerance (see ``_numeric_tolerance``).
VOLATILE_NUMERIC_KEYS = frozenset(
    {
        "token_count",
        "tokens",
        "tokens_in",
        "tokens_out",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "duration_ms",
        "elapsed_ms",
        "timing_ms",
    }
)

#: SSE events that are transport noise rather than behavior.
IGNORED_SSE_TYPES = frozenset({"keepalive", "heartbeat"})

TOLERANCE_RATIO = 0.05


@dataclass(frozen=True)
class Difference:
    path: str
    kind: str  # protocol | data | text | tolerated
    old: Any
    new: Any

    @property
    def tolerated(self) -> bool:
        return self.kind in {"text", "tolerated"}


@dataclass
class DiffReport:
    flow: str
    old_revision: str
    new_revision: str
    differences: list[Difference] = field(default_factory=list)

    @property
    def failures(self) -> list[Difference]:
        return [diff for diff in self.differences if not diff.tolerated]

    @property
    def tolerated(self) -> list[Difference]:
        return [diff for diff in self.differences if diff.tolerated]

    @property
    def ok(self) -> bool:
        return not self.failures

    def render_markdown(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        lines = [
            f"### Refactor diff {self.flow}: {status}",
            "",
            f"- old: `{self.old_revision}`",
            f"- new: `{self.new_revision}`",
            f"- failures: {len(self.failures)}",
            f"- tolerated/flagged: {len(self.tolerated)}",
            "",
        ]
        if not self.differences:
            lines.append("No differences.")
            return "\n".join(lines) + "\n"
        lines.append("| kind | path | old | new |")
        lines.append("| --- | --- | --- | --- |")
        for diff in self.differences:
            old = _short(diff.old)
            new = _short(diff.new)
            lines.append(f"| {diff.kind} | `{diff.path}` | {old} | {new} |")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow": self.flow,
            "ok": self.ok,
            "old_revision": self.old_revision,
            "new_revision": self.new_revision,
            "differences": [
                {
                    "path": diff.path,
                    "kind": diff.kind,
                    "tolerated": diff.tolerated,
                    "old": diff.old,
                    "new": diff.new,
                }
                for diff in self.differences
            ],
        }


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "…"


def _numeric_tolerance(old: float, new: float) -> float:
    return max(1.0, TOLERANCE_RATIO * max(abs(old), abs(new)))


def _diff_value(path: str, key: str | None, old: Any, new: Any, report: DiffReport) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for missing in sorted(set(old) - set(new)):
            report.differences.append(Difference(f"{path}.{missing}", "protocol", old[missing], "<absent>"))
        for added in sorted(set(new) - set(old)):
            report.differences.append(Difference(f"{path}.{added}", "protocol", "<absent>", new[added]))
        for name in sorted(set(old) & set(new)):
            _diff_value(f"{path}.{name}", name, old[name], new[name], report)
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            report.differences.append(
                Difference(f"{path}.length", "protocol", len(old), len(new))
            )
            return
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            _diff_value(f"{path}[{index}]", key, old_item, new_item, report)
        return
    if isinstance(old, bool) or isinstance(new, bool):
        if old is not new:
            report.differences.append(Difference(path, "data", old, new))
        return
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        if old == new:
            return
        if key in VOLATILE_NUMERIC_KEYS and abs(old - new) <= _numeric_tolerance(float(old), float(new)):
            report.differences.append(Difference(path, "tolerated", old, new))
            return
        report.differences.append(Difference(path, "data", old, new))
        return
    if isinstance(old, str) and isinstance(new, str):
        if old == new:
            return
        kind = "text" if key in TEXT_KEYS else "data"
        report.differences.append(Difference(path, kind, old, new))
        return
    if old != new:
        report.differences.append(Difference(path, "data", old, new))


def _expected_identity(snapshot_keys: tuple[str, ...], row: dict[str, Any]) -> tuple:
    if snapshot_keys:
        return tuple(row.get(key) for key in snapshot_keys)
    return tuple(sorted((key, json.dumps(value, sort_keys=True)) for key, value in row.items()))


def compare_fixtures(old: Fixture, new: Fixture) -> DiffReport:
    """Compare two fixtures already normalized (see ``normalize.normalize_fixture``)."""
    report = DiffReport(flow=old.flow, old_revision=old.revision, new_revision=new.revision)
    if old.flow != new.flow:
        report.differences.append(Difference("flow", "protocol", old.flow, new.flow))
        return report

    if len(old.http) != len(new.http):
        report.differences.append(
            Difference("http.length", "protocol", len(old.http), len(new.http))
        )
    else:
        for index, (old_exchange, new_exchange) in enumerate(
            zip(old.http, new.http, strict=True)
        ):
            base = f"http[{index}]"
            for field_name in ("method", "path", "status"):
                if getattr(old_exchange, field_name) != getattr(new_exchange, field_name):
                    report.differences.append(
                        Difference(
                            f"{base}.{field_name}",
                            "protocol",
                            getattr(old_exchange, field_name),
                            getattr(new_exchange, field_name),
                        )
                    )
            _diff_value(f"{base}.request", None, old_exchange.request, new_exchange.request, report)
            _diff_value(f"{base}.response", None, old_exchange.response, new_exchange.response, report)

    old_streams = [
        SseStreamView(stream.label, [event for event in stream.events if event.type not in IGNORED_SSE_TYPES])
        for stream in old.sse
    ]
    new_streams = [
        SseStreamView(stream.label, [event for event in stream.events if event.type not in IGNORED_SSE_TYPES])
        for stream in new.sse
    ]
    if len(old_streams) != len(new_streams):
        report.differences.append(
            Difference("sse.length", "protocol", len(old_streams), len(new_streams))
        )
    else:
        for index, (old_stream, new_stream) in enumerate(zip(old_streams, new_streams, strict=True)):
            base = f"sse[{index}]"
            if old_stream.label != new_stream.label:
                report.differences.append(
                    Difference(f"{base}.label", "protocol", old_stream.label, new_stream.label)
                )
            if len(old_stream.events) != len(new_stream.events):
                report.differences.append(
                    Difference(
                        f"{base}.events.length",
                        "protocol",
                        len(old_stream.events),
                        len(new_stream.events),
                    )
                )
                continue
            for event_index, (old_event, new_event) in enumerate(
                zip(old_stream.events, new_stream.events, strict=True)
            ):
                event_path = f"{base}.events[{event_index}]"
                if old_event.type != new_event.type:
                    report.differences.append(
                        Difference(f"{event_path}.type", "protocol", old_event.type, new_event.type)
                    )
                _diff_value(f"{event_path}.payload", None, old_event.payload, new_event.payload, report)

    old_db = {snapshot.table: snapshot for snapshot in old.db}
    new_db = {snapshot.table: snapshot for snapshot in new.db}
    if set(old_db) != set(new_db):
        report.differences.append(
            Difference("db.tables", "protocol", sorted(old_db), sorted(new_db))
        )
    for table in sorted(set(old_db) & set(new_db)):
        old_snapshot, new_snapshot = old_db[table], new_db[table]
        if old_snapshot.keys != new_snapshot.keys:
            report.differences.append(
                Difference(f"db.{table}.keys", "protocol", old_snapshot.keys, new_snapshot.keys)
            )
            continue
        old_rows = sorted(
            old_snapshot.rows, key=lambda row: _expected_identity(old_snapshot.keys, row)
        )
        new_rows = sorted(
            new_snapshot.rows, key=lambda row: _expected_identity(new_snapshot.keys, row)
        )
        if len(old_rows) != len(new_rows):
            report.differences.append(
                Difference(f"db.{table}.rows", "protocol", len(old_rows), len(new_rows))
            )
            continue
        for index, (old_row, new_row) in enumerate(zip(old_rows, new_rows, strict=True)):
            _diff_value(f"db.{table}.rows[{index}]", None, old_row, new_row, report)

    if old.ui is not None or new.ui is not None:
        if old.ui is None or new.ui is None:
            report.differences.append(Difference("ui", "protocol", old.ui is not None, new.ui is not None))
        else:
            _diff_value("ui", None, old.ui, new.ui, report)

    return report


@dataclass(frozen=True)
class SseStreamView:
    """A filtered SSE stream used by ``compare_fixtures`` (transport noise removed)."""

    label: str
    events: list[Any]
