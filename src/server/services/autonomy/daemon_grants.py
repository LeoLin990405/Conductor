"""Sign a daemon lease, and refuse a completion that no longer matches it.

The private key stays on the server (`CONDUCTOR_DAEMON_GRANT_SIGNING_FILE`,
or `CONDUCTOR_DAEMON_GRANT_SIGNING_KEY` as `kid:base64url-seed`). The device
holds only the public half. A retried claim returns the grant already
stored, so the signature does not change under a daemon that is checking it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.conductor_cli.daemon_grant import GrantError, SigningKey, sign_grant

_SIGNING_FILE = "CONDUCTOR_DAEMON_GRANT_SIGNING_FILE"
_SIGNING_KEY = "CONDUCTOR_DAEMON_GRANT_SIGNING_KEY"
# A refusal or an early cancel. The daemon token already names the device,
# so the body does not have to echo the signed identity. A completion does.
_REFUSAL_STATES = frozenset({"failed", "dropped"})


class GrantSigningError(GrantError):
    """The server was asked to sign and cannot."""


def configured_signing_key() -> SigningKey | None:
    """The key this process signs with, or None when signing is not configured.

    A configured key that cannot be read is an error. Falling through to an
    unsigned lease would look, to a device that trusts a key, like a grant
    the real server refused to stand behind.
    """
    path = os.environ.get(_SIGNING_FILE, "").strip()
    if path:
        return _key_from_file(Path(path).expanduser())
    inline = os.environ.get(_SIGNING_KEY, "").strip()
    if not inline:
        return None
    return SigningKey.parse(inline)


def _key_from_file(path: Path) -> SigningKey:
    from src.conductor_cli.daemon_grant import read_owned_text

    try:
        text = read_owned_text(path, "signing key", secret=True)
    except GrantError as exc:
        raise GrantSigningError("signing key file cannot be read") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GrantSigningError("signing key file is not JSON") from exc
    if not isinstance(raw, dict):
        raise GrantSigningError("signing key file is not a key")
    kid = raw.get("kid")
    seed = raw.get("seed")
    if not isinstance(kid, str) or not isinstance(seed, str):
        raise GrantSigningError("signing key file is not a key")
    return SigningKey.parse(f"{kid}:{seed}")


def _epoch(value: datetime | int | float) -> int:
    if isinstance(value, bool):
        raise GrantSigningError("lease expiry is not a timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


async def attach_lease_grant(
    *,
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    policy_version: str,
    expires_at: datetime | int | float,
    signing_key: SigningKey | None = None,
    now: int | None = None,
) -> dict[str, Any] | None:
    """The grant stored for this lease, signing one only when none exists.

    None means this server is not configured to sign. A device with trusted
    keys then refuses the lease; a device without them keeps the older path.
    """
    from src.server.database.autonomy import daemons as daemon_db

    key = signing_key if signing_key is not None else configured_signing_key()
    if key is None:
        return None
    existing = await daemon_db.read_lease_grant(daemon_id, lease_id)
    if existing is not None:
        return existing
    issued_at = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    expires = _epoch(expires_at)
    if expires <= issued_at:
        raise GrantSigningError("lease expiry is not after it was issued")
    grant = sign_grant(
        key=key,
        lease_id=str(lease_id),
        ticket_id=str(ticket_id),
        daemon_id=str(daemon_id),
        policy_version=str(policy_version or ""),
        expires_at=expires,
        issued_at=issued_at,
    )
    stored = await daemon_db.store_lease_grant(daemon_id, lease_id, grant)
    if stored is None:
        raise GrantSigningError("lease disappeared before the grant was stored")
    return stored


def report_divergence(
    binding: dict[str, Any] | None,
    *,
    daemon_id: str,
    policy_version: str,
    state: str,
) -> str:
    """Why this report must be refused, or an empty string when it may land.

    A lease that was never signed keeps the previous report contract. A
    signed lease accepts a completion only when the daemon identity and the
    policy version still match the grant and the row. A failed or dropped
    report is the daemon's own refusal or an early cancel: the binding was
    loaded for the daemon token, so the body may omit the signed identity,
    and a policy file that moved during the run does not block it. A
    revoked lease accepts only that refusal. A device with no trusted keys
    still completes on this path by echoing the identity the claim returned.
    """
    if not binding or not binding.get("grant"):
        return ""
    row_daemon = str(binding.get("daemon_id") or "")
    reported_daemon = str(daemon_id or "")
    # The body names some other daemon than the token that holds the lease.
    if reported_daemon and reported_daemon != row_daemon:
        return "Daemon identity diverges from the signed lease"
    if state in _REFUSAL_STATES:
        return ""
    grant = binding["grant"]
    if not isinstance(grant, dict):
        return "Daemon identity diverges from the signed lease"
    signed_daemon = str(grant.get("daemon_id") or "")
    signed_policy = str(grant.get("policy_version") or "")
    row_policy = str(binding.get("policy_version") or "")
    reported_policy = str(policy_version or "")
    if signed_daemon != row_daemon or reported_daemon != row_daemon:
        return "Daemon identity diverges from the signed lease"
    if signed_policy != row_policy or reported_policy != row_policy:
        return "Policy version diverges from the signed lease"
    if binding.get("revoked_at") is not None:
        return "Lease is revoked"
    return ""
