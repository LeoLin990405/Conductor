"""``conductor roles ...``: read and edit the role registry (R6 L1, N2).

``roles show`` prints the four roles (router, planner, reviewer, worker) with
their candidates, defaults and constraints plus the hard rules;
``roles set ROLE`` changes one role with a required ``--reason``; it sends the
operator credential (``INTERNAL_SERVICE_TOKEN``, as ``cond apps`` does) and is
audited server side as ``operator:<id>``; ``roles audit`` lists the changes. The server
validates every change; a refused one comes back as an API error.
"""

from __future__ import annotations

import argparse
from typing import Any

ROLES = ("router", "planner", "reviewer", "worker")


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    roles = subparsers.add_parser("roles", help="show and edit the role registry")
    commands = roles.add_subparsers(dest="roles_command", required=True)

    show = commands.add_parser("show", help="show the four roles and the hard rules")
    show.set_defaults(handler=_show)

    setter = commands.add_parser("set", help="change one role (audited)")
    setter.add_argument("role", choices=ROLES)
    setter.add_argument("--candidates", help="comma-separated, owner preference order (name or name@version)")
    setter.add_argument("--default", dest="default_model", help="the default occupant")
    setter.add_argument(
        "--constraint",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a constraint, e.g. requires_session_resume=true (repeatable)",
    )
    setter.add_argument(
        "--extra", action="append", default=[], metavar="KEY=VALUE", help="a role extra (repeatable)"
    )
    setter.add_argument("--reason", required=True, help="why (stored in the audit)")
    setter.add_argument("--expected-revision", type=int, default=None)
    setter.set_defaults(handler=_set)

    audit = commands.add_parser("audit", help="list role registry changes")
    audit.add_argument("--role", choices=ROLES)
    audit.add_argument("--limit", type=int, default=20)
    audit.set_defaults(handler=_audit)


def _value(raw: str) -> Any:
    text = raw.strip()
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null"}:
        return None
    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()]
    try:
        return int(text)
    except ValueError:
        return text


def _pairs(items: list[str], flag: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"{flag} expects KEY=VALUE, got {item!r}")
        out[key.strip()] = _value(value)
    return out


def build_update(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"reason": args.reason}
    if args.candidates:
        body["candidates"] = [n.strip() for n in args.candidates.split(",") if n.strip()]
    if args.default_model:
        body["default"] = args.default_model
    if args.constraint:
        body["constraints"] = _pairs(args.constraint, "--constraint")
    if args.extra:
        body["extra"] = _pairs(args.extra, "--extra")
    if args.expected_revision is not None:
        body["expected_revision"] = args.expected_revision
    return body


def _show(args: argparse.Namespace, client: Any) -> Any:
    return client.get("/api/v1/roles")


def _operator_headers() -> dict[str, str]:
    """The operator credential (``INTERNAL_SERVICE_TOKEN``); never printed."""
    from .apps_cli import _service_credential

    token, user = _service_credential()
    if not token:
        raise SystemExit(
            "roles set needs the operator credential: set INTERNAL_SERVICE_TOKEN "
            "in the environment or the checkout .env"
        )
    return {"X-Service-Token": token, "X-User-Id": user}


def _set(args: argparse.Namespace, client: Any) -> Any:
    body = build_update(args)
    http = getattr(client, "_http", None)
    headers = _operator_headers()
    if http is None:
        return client.request("PUT", f"/api/v1/roles/{args.role}", json=body)
    previous = {key: http.headers.get(key) for key in headers}
    http.headers.update(headers)
    try:
        return client.request("PUT", f"/api/v1/roles/{args.role}", json=body)
    finally:
        for key, value in previous.items():
            if value is None:
                http.headers.pop(key, None)
            else:
                http.headers[key] = value


def _audit(args: argparse.Namespace, client: Any) -> Any:
    params: dict[str, Any] = {"limit": args.limit}
    if args.role:
        params["role"] = args.role
    return client.get("/api/v1/roles/audit", params=params)
