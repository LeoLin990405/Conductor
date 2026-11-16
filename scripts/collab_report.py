#!/usr/bin/env python3
"""Read-only N80 rollout report over the collab audit table and Ticket events.

The numbers answer the questions N80 section 9 asks before each promotion of
``CONDUCTOR_COLLAB_MODE``:

* shadow: calls per app, refusals by code, rate-limited calls, redactions;
* docs: claims, the share of leases that ended ``completed`` (``done`` and
  passed the server's checks), lease expiries, ``out_of_scope`` vetoes,
  ``self_review`` refusals and how long handoffs waited to be accepted.

Two sources, exactly one required::

    python scripts/collab_report.py --dsn "dbname=conductor host=127.0.0.1" --json
    python scripts/collab_report.py --from-json snapshot.json

``--dsn`` opens one connection, refuses a database named ``postgres`` before
libpq is called (``src/conductor_cli/db_guard.py``), marks the session read
only, and runs SELECT statements only; the transaction is rolled back. A
snapshot is a JSON object with ``audit``, ``ticket_events``, ``leases`` and
``handoffs`` lists, the same columns the queries return.

The script never writes a file, a row or the mode. It prints the promotion
checks; the owner changes the mode. Refusals from a non-loopback address are not a
column of ``collab_audit``, so that number is reported as not recorded.

Ticket events count when ``actor`` starts with ``app:``. The kind is
``detail.collab_event`` (``claimed``, ``released``, ``lease_expired``,
``handed_over``, ``reported``) and falls back to ``action``; a veto reason is
``detail.reason``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

SCHEMA = "conductor.collab_report/v1"
DEFAULT_DAYS = 7

#: Promotion thresholds from N80 section 9.
SHADOW_MIN_DAYS = 7
DOCS_MIN_DAYS = 7
DOCS_MIN_CLAIMS = 10
DOCS_DONE_RATE = 0.70
DOCS_OUT_OF_SCOPE_MAX = 0

APP_ACTOR_PREFIX = "app:"
OUT_OF_SCOPE = "out_of_scope"
SELF_REVIEW = "self_review"
RATE_LIMITED = "rate_limited"

#: The only statements the database store may run. Each is a SELECT with one
#: ``since`` parameter; nothing here can write.
QUERIES: dict[str, str] = {
    "audit": (
        "SELECT app_id, agent, tool, target, code, http_status, rate_limited, "
        "redacted, duration_ms, created_at FROM collab_audit "
        "WHERE created_at >= %s ORDER BY created_at"
    ),
    "ticket_events": (
        "SELECT ticket_id, actor, action, detail, created_at "
        "FROM autonomy_ticket_events "
        "WHERE created_at >= %s AND actor LIKE 'app:%%' ORDER BY created_at"
    ),
    "leases": (
        "SELECT lease_id, app_id, target_ref, target_kind, state, end_reason, "
        "created_at, ended_at FROM collab_leases "
        "WHERE created_at >= %s ORDER BY created_at"
    ),
    "handoffs": (
        "SELECT handoff_id, lease_id, from_app_id, to_target, reason, state, "
        "accepted_by, created_at, accepted_at FROM collab_handoffs "
        "WHERE created_at >= %s ORDER BY created_at"
    ),
    # Not windowed: how long the rollout has produced data at all, so a
    # seven-day window can still show seven days of shadow history.
    "first_audit": "SELECT MIN(created_at) AS first_at FROM collab_audit",
}


READ_ONLY_SQL = "SET TRANSACTION READ ONLY"
_ALLOWED_SQL = frozenset(QUERIES.values()) | {READ_ONLY_SQL}


class ReadOnlyStore(Protocol):
    """Everything the report may ask of a source: four reads, no writes."""

    def audit(self, since: datetime) -> list[dict[str, Any]]: ...

    def ticket_events(self, since: datetime) -> list[dict[str, Any]]: ...

    def leases(self, since: datetime) -> list[dict[str, Any]]: ...

    def handoffs(self, since: datetime) -> list[dict[str, Any]]: ...

    def first_audit_at(self) -> datetime | None: ...


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def parse_time(value: object) -> datetime | None:
    """A stored timestamp (datetime, ISO string or epoch seconds) in UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _since_filter(rows: object, since: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        created = parse_time(row.get("created_at"))
        if created is not None and created < since:
            continue
        out.append(dict(row))
    return out


@dataclass(frozen=True)
class SnapshotStore:
    """Rows already exported to a JSON object; reading it changes nothing."""

    data: Mapping[str, Any]

    @classmethod
    def from_file(cls, path: Path) -> "SnapshotStore":
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValueError("snapshot must be a JSON object")
        return cls(loaded)

    def audit(self, since: datetime) -> list[dict[str, Any]]:
        return _since_filter(self.data.get("audit"), since)

    def ticket_events(self, since: datetime) -> list[dict[str, Any]]:
        return _since_filter(self.data.get("ticket_events"), since)

    def leases(self, since: datetime) -> list[dict[str, Any]]:
        return _since_filter(self.data.get("leases"), since)

    def handoffs(self, since: datetime) -> list[dict[str, Any]]:
        return _since_filter(self.data.get("handoffs"), since)

    def first_audit_at(self) -> datetime | None:
        rows = self.data.get("audit")
        times = [
            parse_time(row.get("created_at"))
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, Mapping)
        ]
        known = [moment for moment in times if moment is not None]
        return min(known) if known else None


class ReadOnlyViolation(RuntimeError):
    """A statement other than one of :data:`QUERIES` reached the store."""


class DatabaseStore:
    """One read-only psycopg connection; only :data:`QUERIES` are executed.

    ``connector`` is ``psycopg.connect`` by default and is called with the
    string ``db_guard`` checked, so a ``postgres`` database is refused before
    anything is opened.
    """

    def __init__(self, dsn: str, *, connector: Any = None) -> None:
        from src.conductor_cli.db_guard import guarded_connect

        self._conn = guarded_connect(dsn, connector=connector, autocommit=False)
        # psycopg starts every transaction with BEGIN READ ONLY from here on;
        # the SET makes the server refuse a write even if that were bypassed.
        try:
            self._conn.read_only = True
            self._run(READ_ONLY_SQL, ())
        except BaseException:
            self.close()
            raise

    def _run(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        if sql not in _ALLOWED_SQL:
            raise ReadOnlyViolation("collab_report only runs its fixed SELECT statements")
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            if cur.description is None:
                return []
            names = [col[0] for col in cur.description]
            return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]

    def _select(self, name: str, since: datetime) -> list[dict[str, Any]]:
        return self._run(QUERIES[name], (since,))

    def audit(self, since: datetime) -> list[dict[str, Any]]:
        return self._select("audit", since)

    def ticket_events(self, since: datetime) -> list[dict[str, Any]]:
        return self._select("ticket_events", since)

    def leases(self, since: datetime) -> list[dict[str, Any]]:
        return self._select("leases", since)

    def handoffs(self, since: datetime) -> list[dict[str, Any]]:
        return self._select("handoffs", since)

    def first_audit_at(self) -> datetime | None:
        rows = self._run(QUERIES["first_audit"], ())
        return parse_time(rows[0].get("first_at")) if rows else None

    def close(self) -> None:
        try:
            self._conn.rollback()
        finally:
            self._conn.close()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _text(value: object) -> str:
    return str(value or "").strip()


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _detail(value: object) -> dict[str, Any]:
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _app_of_actor(actor: str) -> str:
    """``app:codex/gpt-5`` -> ``codex``."""
    return actor[len(APP_ACTOR_PREFIX) :].split("/", 1)[0]


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile; ``None`` for no samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


@dataclass
class _AppStats:
    calls: int = 0
    refusals: Counter[str] = field(default_factory=Counter)
    rate_limited: int = 0
    redacted: int = 0
    claims: int = 0
    completed: int = 0
    expired: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "refusals": dict(sorted(self.refusals.items())),
            "rate_limited": self.rate_limited,
            "redacted": self.redacted,
            "claims": self.claims,
            "completed": self.completed,
            "lease_expired": self.expired,
        }


def build_report(
    store: ReadOnlyStore,
    *,
    now: datetime,
    days: int = DEFAULT_DAYS,
    mode: str = "off",
) -> dict[str, Any]:
    """Every metric and promotion check for the window ending at ``now``."""
    since = now - timedelta(days=days)
    audit = store.audit(since)
    events = [
        row
        for row in store.ticket_events(since)
        if _text(row.get("actor")).startswith(APP_ACTOR_PREFIX)
    ]
    leases = store.leases(since)
    handoffs = store.handoffs(since)

    apps: dict[str, _AppStats] = {}

    def stats(app_id: object) -> _AppStats:
        key = _text(app_id) or "(unauthenticated)"
        return apps.setdefault(key, _AppStats())

    refusals: Counter[str] = Counter()
    rate_limited = redacted = 0
    for row in audit:
        app = stats(row.get("app_id"))
        app.calls += 1
        code = _text(row.get("code")) or "ok"
        limited = bool(row.get("rate_limited")) or code == RATE_LIMITED
        if code != "ok":
            refusals[code] += 1
            app.refusals[code] += 1
        if limited:
            rate_limited += 1
            app.rate_limited += 1
        count = _int(row.get("redacted"))
        redacted += count
        app.redacted += count

    lease_states: Counter[str] = Counter()
    for row in leases:
        app = stats(row.get("app_id"))
        app.claims += 1
        state = _text(row.get("state"))
        lease_states[state] += 1
        if state == "completed":
            app.completed += 1
        if state == "expired" or _text(row.get("end_reason")) == "lease_expired":
            app.expired += 1

    event_kinds: Counter[str] = Counter()
    out_of_scope = 0
    vetoed_tickets: set[str] = set()
    for row in events:
        detail = _detail(row.get("detail"))
        kind = _text(detail.get("collab_event")) or _text(row.get("action"))
        event_kinds[kind] += 1
        if _text(detail.get("reason")) == OUT_OF_SCOPE:
            out_of_scope += 1
            vetoed_tickets.add(f"t:{_text(row.get('ticket_id')).lower()}")
    # A lease that ended out_of_scope counts once: the Ticket event for the
    # same Ticket already counted it when the server wrote both.
    out_of_scope += sum(
        1
        for row in leases
        if _text(row.get("end_reason")) == OUT_OF_SCOPE
        and _text(row.get("target_ref")).lower() not in vetoed_tickets
    )

    claims = len(leases)
    completed = lease_states.get("completed", 0)
    expired = sum(
        1
        for row in leases
        if _text(row.get("state")) == "expired"
        or _text(row.get("end_reason")) == "lease_expired"
    )
    done_rate = (completed / claims) if claims else None

    waits: list[float] = []
    handoff_states: Counter[str] = Counter()
    for row in handoffs:
        handoff_states[_text(row.get("state"))] += 1
        start = parse_time(row.get("created_at"))
        end = parse_time(row.get("accepted_at"))
        if start is not None and end is not None:
            waits.append(max(0.0, (end - start).total_seconds()))

    days_of_data = 0.0
    first_seen = store.first_audit_at()
    if first_seen is not None:
        days_of_data = max(0.0, (now - first_seen).total_seconds() / 86400)

    shadow_checks = {
        "days_of_data": {
            "value": round(days_of_data, 2),
            "need": SHADOW_MIN_DAYS,
            "pass": days_of_data >= SHADOW_MIN_DAYS,
        },
    }
    docs_checks = {
        "duration_or_claims": {
            "value": {"days": round(days_of_data, 2), "claims": claims},
            "need": {"days": DOCS_MIN_DAYS, "claims": DOCS_MIN_CLAIMS},
            "pass": days_of_data >= DOCS_MIN_DAYS or claims >= DOCS_MIN_CLAIMS,
        },
        "done_rate": {
            "value": None if done_rate is None else round(done_rate, 4),
            "need": DOCS_DONE_RATE,
            "pass": done_rate is not None and done_rate >= DOCS_DONE_RATE,
        },
        "out_of_scope": {
            "value": out_of_scope,
            "need": DOCS_OUT_OF_SCOPE_MAX,
            "pass": out_of_scope <= DOCS_OUT_OF_SCOPE_MAX,
        },
    }

    return {
        "schema": SCHEMA,
        "mode": mode,
        "window": {
            "since": since.replace(microsecond=0).isoformat(),
            "until": now.replace(microsecond=0).isoformat(),
            "days": days,
        },
        "totals": {
            "calls": len(audit),
            "refusals": dict(sorted(refusals.items())),
            "rate_limited": rate_limited,
            "redacted": redacted,
            "self_review_refusals": refusals.get(SELF_REVIEW, 0),
            "non_loopback_refusals": "not recorded",
            "claims": claims,
            "completed": completed,
            "done_rate": None if done_rate is None else round(done_rate, 4),
            "lease_expired": expired,
            "lease_states": dict(sorted(lease_states.items())),
            "out_of_scope": out_of_scope,
            "ticket_events": dict(sorted(event_kinds.items())),
            "handoffs": dict(sorted(handoff_states.items())),
            "handoff_accept_seconds": {
                "samples": len(waits),
                "median": _median(waits),
                "p95": _percentile(waits, 0.95),
            },
        },
        "apps": {name: apps[name].to_dict() for name in sorted(apps)},
        "promotion": {
            "shadow_to_docs": {
                "ready": all(check["pass"] for check in shadow_checks.values()),
                "checks": shadow_checks,
            },
            "docs_to_full": {
                "ready": all(check["pass"] for check in docs_checks.values()),
                "checks": docs_checks,
            },
            "note": "The report never changes CONDUCTOR_COLLAB_MODE.",
        },
    }


def render_text(report: Mapping[str, Any]) -> str:
    totals = report["totals"]
    window = report["window"]
    lines = [
        f"collab report  mode={report['mode']}  {window['since']} .. {window['until']}",
        f"calls {totals['calls']}  refusals {sum(totals['refusals'].values())}"
        f"  rate_limited {totals['rate_limited']}  redacted {totals['redacted']}",
        f"claims {totals['claims']}  completed {totals['completed']}"
        f"  done_rate {totals['done_rate']}  lease_expired {totals['lease_expired']}"
        f"  out_of_scope {totals['out_of_scope']}"
        f"  self_review {totals['self_review_refusals']}",
        f"non-loopback refusals: {totals['non_loopback_refusals']}",
    ]
    wait = totals["handoff_accept_seconds"]
    lines.append(
        f"handoff accept seconds: n={wait['samples']} median={wait['median']} p95={wait['p95']}"
    )
    if totals["refusals"]:
        lines.append(
            "refusals by code: "
            + ", ".join(f"{code}={n}" for code, n in totals["refusals"].items())
        )
    for name, app in report["apps"].items():
        lines.append(
            f"  {name}: calls={app['calls']} claims={app['claims']}"
            f" completed={app['completed']} expired={app['lease_expired']}"
            f" rate_limited={app['rate_limited']} redacted={app['redacted']}"
        )
    for gate, result in report["promotion"].items():
        if not isinstance(result, Mapping):
            continue
        failing = [name for name, check in result["checks"].items() if not check["pass"]]
        verdict = "ready" if result["ready"] else "not ready: " + ", ".join(failing)
        lines.append(f"{gate}: {verdict}")
    lines.append(report["promotion"]["note"])
    return "\n".join(lines)


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = parse_time(value)
    if parsed is None:
        raise SystemExit(f"--now is not a timestamp: {value}")
    return parsed


def main(argv: Sequence[str] | None = None, *, connector: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only N80 collab rollout report")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dsn", help="libpq connection string (never the postgres database)")
    source.add_argument("--from-json", type=Path, help="snapshot JSON exported earlier")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--now", help="end of the window (ISO 8601); default is now")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    from src.collab.modes import current_mode

    now = _now(args.now)
    mode = current_mode()
    if args.from_json is not None:
        report = build_report(
            SnapshotStore.from_file(args.from_json), now=now, days=args.days, mode=mode
        )
    else:
        from src.conductor_cli.db_guard import DatabaseRefused

        try:
            store = DatabaseStore(args.dsn, connector=connector)
        except DatabaseRefused as exc:
            print(f"collab_report: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - a connect failure is an exit code
            # The class name only: a driver message can echo connection
            # parameters, and the DSN may carry a password.
            print(f"collab_report: cannot open the database ({type(exc).__name__})", file=sys.stderr)
            return 2
        try:
            report = build_report(store, now=now, days=args.days, mode=mode)
        finally:
            store.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    # Run as ``python scripts/collab_report.py`` from the repository root.
    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    raise SystemExit(main())
