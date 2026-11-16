"""``conductor apps ...``: register and manage collab apps (N80 section 3.2).

These are operator verbs. They call the operator routes under
``/api/v1/collab/admin/apps`` with the operator credential those routes
require: the internal service token, sent as ``X-Service-Token`` with
``X-User-Id`` the way every privileged local caller of Conductor sends it. It
is read from ``INTERNAL_SERVICE_TOKEN`` in the environment or the checkout
``.env`` (the file the server reads) and is never printed. An app token is
never used here. ``register`` and ``rotate`` receive the new plaintext app
token exactly once; it goes straight into the keychain entry
``conductor-collab/<app_id>`` and is never printed, logged or returned --
the output names the keychain reference instead, plus an MCP hub snippet
that is printed only (nothing is written to the hub or any agent config).

``cond apps ...`` forwards here.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from .client import CLIError, read_token
from .collab_cli import (
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    exit_code_for,
    local_failure,
    render_envelope,
    resolve_base_url,
)

#: Mirrors ``src.collab.scopes.SCOPES`` (``conductor_cli`` may not import
#: ``src``); a test keeps the two equal.
SCOPES = ("read", "propose", "claim", "report", "ask", "review")


#: The server's refusal when ``BYOK_ENCRYPTION_KEY`` is unset (mirrors
#: ``src.server.database.encryption.ENCRYPTION_KEY_MISSING_CODE``).
BYOK_KEY_MISSING_CODE = "byok_key_missing"


def byok_hint(verb: str) -> str:
    """How to fix a missing ``BYOK_ENCRYPTION_KEY``; holds no value."""
    return (
        "hint: set BYOK_ENCRYPTION_KEY to a long random value (for example the "
        "output of `openssl rand -hex 32`) in the checkout .env the server reads, "
        "or in the server's environment, restart the server, then rerun "
        f"`conductor apps {verb}`. Keep the value: data encrypted with it cannot "
        "be read with another key."
    )


class UsageProblem(Exception):
    """Bad local input; the message never contains a secret."""


# -- seams (tests replace these) ---------------------------------------------


def _keychain() -> Any:
    from collab_mcp.client import Keychain

    return Keychain()


def _transport() -> Any:
    return None


def _operator_token() -> str | None:
    # Resolved at call time, so a HOME set by the caller is the one used.
    return read_token(Path.home() / ".config" / "conductor" / "token")


#: The server's default local user (``AUTH_USER_ID`` unset).
DEFAULT_USER_ID = "local-dev-user"


def _checkout_env() -> dict[str, str]:
    """``KEY=value`` pairs of the checkout ``.env``; empty when there is none.

    The checkout is ``CONDUCTOR_REPO_ROOT`` (``cond`` sets it), else the one
    this module runs from when it is a server checkout.
    """
    named = os.environ.get("CONDUCTOR_REPO_ROOT", "").strip()
    if named:
        root = Path(named).expanduser()
    else:
        root = Path(__file__).resolve().parents[2]
        if not (root / "server.py").is_file():
            return {}
    path = root / ".env"
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    try:
        values = dotenv_values(path)
    except (OSError, UnicodeError):
        return {}
    return {str(key): str(value) for key, value in values.items() if value is not None}


def _service_credential() -> tuple[str | None, str]:
    """``(service_token, user_id)`` for the operator routes; token may be None."""
    token = os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
    user = os.environ.get("AUTH_USER_ID", "").strip()
    if not token or not user:
        stored = _checkout_env()
        token = token or stored.get("INTERNAL_SERVICE_TOKEN", "").strip()
        user = user or stored.get("AUTH_USER_ID", "").strip()
    return (token or None), (user or DEFAULT_USER_ID)


# -- parser -------------------------------------------------------------------


def _scope_list(value: str) -> list[str]:
    parts = [part.strip().lower() for part in value.replace(",", " ").split() if part.strip()]
    unknown = sorted(set(parts) - set(SCOPES))
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown scope: {', '.join(unknown)} (choose from {', '.join(SCOPES)})"
        )
    return parts


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    apps = subparsers.add_parser("apps", help="register and manage collab apps (N80)")
    apps.set_defaults(handler=handle)
    verbs = apps.add_subparsers(dest="apps_command", required=True)

    def verb(name: str, help_text: str, *, app: bool = True) -> argparse.ArgumentParser:
        parser = verbs.add_parser(name, help=help_text)
        if app:
            parser.add_argument("app_id", help="lowercase app id, e.g. claude-code")
        parser.add_argument("--json", action="store_true", help="print the v1 JSON envelope")
        return parser

    register = verb("register", "register an app; its token goes to the keychain only")
    register.add_argument(
        "--scopes",
        type=_scope_list,
        default=None,
        help="comma separated scopes (default: read)",
    )
    register.add_argument("--can-write", action="store_true", help="the app may take tasks with a write scope")
    register.add_argument("--max-leases", type=int, default=None, help="leases held at once (1-3, default 1)")
    register.add_argument("--mcp-target", default=None, help="MCP hub agent id for the printed snippet")

    verb("list", "list apps (token digest prefix only, never a token)", app=False)

    scopes = verb("scopes", "add or remove scopes")
    scopes.add_argument("--add", action="append", default=None, choices=SCOPES)
    scopes.add_argument("--remove", action="append", default=None, choices=SCOPES)

    verb("rotate", "issue a new token (the old one stops working); keychain only")
    verb("revoke", "revoke the token and release every lease the app holds")

    audit = verb("audit", "show the app's audit rows")
    audit.add_argument("--since", default=None, help="ISO timestamp")
    audit.add_argument("--limit", type=int, default=None)

    snippet = verb("snippet", "print the MCP hub entry for an app (printed only, never applied)")
    snippet.add_argument("--mcp-target", default=None, help="MCP hub agent id")


# -- handler --------------------------------------------------------------------


def _request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    verb = args.apps_command
    if verb == "list":
        return "apps_list", {}
    app_id = args.app_id
    if verb == "register":
        payload: dict[str, Any] = {"app_id": app_id, "scopes": sorted(set(args.scopes or ["read"]))}
        if args.can_write:
            payload["can_write"] = True
        if args.max_leases is not None:
            if not 1 <= args.max_leases <= 3:
                raise UsageProblem("--max-leases must be between 1 and 3")
            payload["max_leases"] = args.max_leases
        return "apps_register", payload
    if verb == "scopes":
        add = sorted(set(args.add or []))
        remove = sorted(set(args.remove or []))
        if not add and not remove:
            raise UsageProblem("name at least one --add or --remove scope")
        if set(add) & set(remove):
            raise UsageProblem("a scope cannot be added and removed at once")
        return "apps_scopes", {"app_id": app_id, "add": add, "remove": remove}
    if verb == "rotate":
        return "apps_rotate", {"app_id": app_id}
    if verb == "revoke":
        return "apps_revoke", {"app_id": app_id}
    if verb == "audit":
        query: dict[str, Any] = {"app_id": app_id}
        if args.since:
            query["since"] = args.since
        if args.limit is not None:
            query["limit"] = args.limit
        return "apps_audit", query
    raise UsageProblem(f"unknown apps verb: {verb}")


def _store_token(
    envelope: dict[str, Any], app_id: str, keychain: Any
) -> tuple[dict[str, Any], str]:
    """Move ``data.token`` into the keychain; the returned envelope has none."""
    from collab_mcp.client import KeychainRef

    data = dict(envelope.get("data") or {})
    token = data.pop("token", None)
    if not isinstance(token, str) or not token.strip():
        raise CLIError("Conductor did not return a token for the app")
    ref = KeychainRef.for_app(app_id)
    try:
        keychain.write(ref, token)
    except Exception:
        raise CLIError(
            f"the app is registered but its token could not be stored at {ref}; "
            f"run `conductor apps rotate {app_id}` to issue a new one"
        ) from None
    data["token_stored_at"] = str(ref)
    stored = dict(envelope)
    stored["data"] = data
    return stored, token


def handle(args: argparse.Namespace) -> int:
    from collab_mcp.client import (
        CollabClient,
        CollabClientError,
        CollabUnavailable,
        mcp_hub_snippet,
        scrub_secret,
        validate_app_id,
    )

    verb = args.apps_command
    command = f"apps {verb}"
    json_mode = bool(getattr(args, "json", False))
    try:
        if verb != "list":
            args.app_id = validate_app_id(args.app_id)
        if verb == "snippet":
            snippet = mcp_hub_snippet(args.app_id, target=args.mcp_target)
            envelope = {"ok": True, "code": "ok", "data": {"mcp_hub": snippet}, "untrusted_fields": [], "redacted": 0}
            render_envelope(command, envelope, json_mode=json_mode, exit_code=EXIT_OK)
            return EXIT_OK
        action, payload = _request(args)
        snippet = (
            mcp_hub_snippet(args.app_id, target=args.mcp_target) if verb == "register" else None
        )
    except (UsageProblem, CollabClientError) as exc:
        return local_failure(command, "invalid_input", str(exc), json_mode=json_mode, exit_code=EXIT_USAGE)
    service_token, user_id = _service_credential()
    if not service_token:
        return local_failure(
            command,
            "unauthorized",
            "no operator credential: set INTERNAL_SERVICE_TOKEN in the environment "
            "or the checkout .env (the value the server runs with)",
            json_mode=json_mode,
            exit_code=EXIT_REFUSED,
        )
    try:
        client = CollabClient(
            _operator_token(),
            base_url=resolve_base_url(args),
            transport=_transport(),
            require_token=False,
            service_token=service_token,
            user_id=user_id,
        )
    except (CollabClientError, CLIError) as exc:
        return local_failure(command, "invalid_input", str(exc), json_mode=json_mode, exit_code=EXIT_USAGE)
    issued: str | None = None
    try:
        try:
            response = client.admin(action, payload)
        except CollabUnavailable as exc:
            return local_failure(command, "unreachable", str(exc), json_mode=json_mode, exit_code=EXIT_UNREACHABLE)
        envelope = response.envelope
        if response.ok and verb in {"register", "rotate"}:
            try:
                envelope, issued = _store_token(envelope, args.app_id, _keychain())
            except CLIError as exc:
                # Whatever came back, the token itself is not in this message.
                return local_failure(command, "keychain_failed", str(exc), json_mode=json_mode, exit_code=EXIT_REFUSED)
            if snippet is not None:
                envelope["data"]["mcp_hub"] = snippet
        elif isinstance(envelope.get("data"), dict) and "token" in envelope["data"]:
            # Never print a token, even one a refusal carried by mistake.
            envelope = dict(envelope)
            envelope["data"] = {key: value for key, value in envelope["data"].items() if key != "token"}
        if envelope.get("code") == BYOK_KEY_MISSING_CODE:
            envelope = dict(envelope)
            message = str(envelope.get("message") or "BYOK_ENCRYPTION_KEY is not set on the server")
            envelope["message"] = f"{message}; {byok_hint(verb)}"
        safe = scrub_secret(scrub_secret(client.scrub(envelope), issued), service_token)
        code = exit_code_for(response)
        render_envelope(command, safe, json_mode=json_mode, exit_code=code)
        return code
    finally:
        client.close()


__all__ = ["BYOK_KEY_MISSING_CODE", "SCOPES", "byok_hint", "handle", "register_parser"]
