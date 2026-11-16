"""Proof that a daemon lease was signed by the real server.

A compromised cloud worker can hand the daemon any Ticket the claim API
will return. The device therefore keeps the server's public keys itself,
more than one so a key can rotate, and runs a lease only when the grant
verifies, is still inside its expiry, and has not been presented before.
The private key never leaves the server.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import secrets
import stat
import threading
import time

try:  # POSIX only; the daemon targets macOS/Linux.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

DEFAULT_TRUSTED_KEYS = Path.home() / ".config" / "conductor" / "daemon_trusted_keys.json"
DEFAULT_NONCE_FILE = (
    Path.home() / ".local" / "state" / "conductor" / "daemon-grant-nonces.json"
)
GRANT_FORMAT = 1
_KID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SIGNED_FIELDS = (
    "v",
    "kid",
    "lease_id",
    "ticket_id",
    "daemon_id",
    "policy_version",
    "nonce",
    "iat",
    "exp",
)


class GrantError(ValueError):
    """The grant must not run on this device."""


class GrantUnsigned(GrantError):
    """The lease arrived without a signature."""


class GrantMalformed(GrantError):
    """The grant is not a well-formed signed payload."""


class GrantSignatureError(GrantError):
    """The signature does not match a key this device trusts."""


class GrantExpired(GrantError):
    """The signature is valid and its expiry has passed."""


class GrantMismatch(GrantError):
    """The signed payload names a different lease than the one just claimed."""


class GrantReplayed(GrantError):
    """This nonce was already accepted."""


class NonceStoreError(GrantError):
    """The replay record cannot be trusted, so the grant must not run."""


def refusal_reason(exc: GrantError) -> str:
    """The daemon report for a grant that must not reach the native CLI."""
    if isinstance(exc, GrantExpired):
        return "Local daemon refused the lease: the signed grant has expired."
    if isinstance(exc, GrantReplayed):
        return "Local daemon refused the lease: the signed grant was already used."
    if isinstance(exc, GrantUnsigned):
        return "Local daemon refused the lease: it carries no signed grant."
    if isinstance(exc, GrantMismatch):
        return (
            "Local daemon refused the lease: the signed grant does not match "
            "the Ticket."
        )
    if isinstance(exc, NonceStoreError):
        return "Local daemon refused the lease: replay state could not be confirmed."
    if isinstance(exc, GrantMalformed):
        return "Local daemon refused the lease: the signed grant is not valid."
    return (
        "Local daemon refused the lease: the signed grant is not from a "
        "trusted server key."
    )


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    if not isinstance(text, str) or not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise GrantMalformed("grant encoding is not base64url")
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError) as exc:
        raise GrantMalformed("grant encoding is not base64url") from exc


def _unix(value: Any, name: str) -> int:
    # bool is an int. A JSON `true` must not become a timestamp.
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrantMalformed(f"grant {name} is not an integer timestamp")
    if value < 0 or value > 4_000_000_000:
        raise GrantMalformed(f"grant {name} is outside the accepted range")
    return value


def _text_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise GrantMalformed(f"grant {name} is missing")
    if any(char in value for char in ("\n", "\r", "=")):
        raise GrantMalformed(f"grant {name} is not a single token")
    return value


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes the signature covers.

    The order is fixed, so a field moved or a space added is a different
    grant. `sig` is not part of the message.
    """
    lines = ["daemon-grant-v1"]
    for field in _SIGNED_FIELDS:
        value = payload[field]
        lines.append(f"{field}={value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SigningKey:
    """The server's private half of one grant key. The seed is not shown."""

    kid: str
    _seed: bytes

    def __repr__(self) -> str:
        return f"SigningKey(kid={self.kid!r})"

    @classmethod
    def generate(cls, kid: str) -> SigningKey:
        if not _KID.fullmatch(kid or ""):
            raise GrantError("key id must be 1-64 letters, digits, '.', '_' or '-'")
        return cls(kid, secrets.token_bytes(32))

    @classmethod
    def parse(cls, text: str) -> SigningKey:
        """`kid:` plus the url-safe base64 of the 32-byte seed, with no padding."""
        kid, sep, seed_b64 = (text or "").strip().partition(":")
        if not sep or not _KID.fullmatch(kid):
            raise GrantError("signing key must look like kid:base64url-seed")
        try:
            seed = _b64d(seed_b64)
        except GrantMalformed as exc:
            raise GrantError("signing key seed is not base64url") from exc
        if len(seed) != 32:
            raise GrantError("signing key seed must be 32 bytes")
        return cls(kid, seed)

    def public_bytes(self) -> bytes:
        return self._private().public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def public_b64(self) -> str:
        return _b64e(self.public_bytes())

    def _private(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self._seed)


@dataclass(frozen=True)
class VerifiedGrant:
    """A signature that matched a trusted key and has not expired."""

    kid: str
    lease_id: str
    ticket_id: str
    daemon_id: str
    policy_version: str
    nonce: str
    issued_at: int
    expires_at: int


@dataclass(frozen=True)
class TrustedKeys:
    """The public keys this device accepts, keyed by the id in the grant."""

    keys: Mapping[str, bytes]
    path: Path | None = None
    # The resolved location the keys were loaded from. Authorization must check
    # this immutable location, not a re-resolution of the configured pathname.
    resolved_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.keys:
            raise GrantError("trusted keys must name at least one key")
        if self.path is not None and self.resolved_path is None:
            object.__setattr__(
                self, "resolved_path", Path(self.path).expanduser().resolve(strict=False)
            )

    def public_key(self, kid: str) -> bytes | None:
        raw = self.keys.get(kid)
        return bytes(raw) if raw is not None else None

    @classmethod
    def trust(cls, *signing_keys: SigningKey) -> TrustedKeys:
        """The public halves of these signing keys, for a device or a test."""
        found: dict[str, bytes] = {}
        for key in signing_keys:
            if key.kid in found:
                raise GrantError(f"duplicate key id {key.kid}")
            found[key.kid] = key.public_bytes()
        return cls(keys=found)

    @classmethod
    def from_mapping(cls, raw: Any, *, path: Path | str = "<trusted-keys>") -> TrustedKeys:
        if not isinstance(raw, Mapping):
            raise GrantError(f"{path}: trusted keys must be a JSON object")
        if raw.get("format", GRANT_FORMAT) != GRANT_FORMAT:
            raise GrantError(f"{path}: unsupported trusted-key format {raw.get('format')!r}")
        entries = raw.get("keys")
        if not isinstance(entries, list) or not entries:
            raise GrantError(f"{path}: trusted keys need a non-empty 'keys' list")
        found: dict[str, bytes] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise GrantError(f"{path}: each trusted key must be an object")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not _KID.fullmatch(kid):
                raise GrantError(f"{path}: a trusted key id is missing or invalid")
            if kid in found:
                raise GrantError(f"{path}: key id {kid} is listed twice")
            public = entry.get("public_key")
            if not isinstance(public, str):
                raise GrantError(f"{path}: key {kid} has no public_key")
            try:
                raw_key = _b64d(public)
            except GrantMalformed as exc:
                raise GrantError(f"{path}: key {kid} public_key is not base64url") from exc
            if len(raw_key) != 32:
                raise GrantError(f"{path}: key {kid} public_key must be 32 bytes")
            try:
                Ed25519PublicKey.from_public_bytes(raw_key)
            except (ValueError, TypeError) as exc:
                raise GrantError(f"{path}: key {kid} public_key is not Ed25519") from exc
            found[kid] = raw_key
        return cls(keys=found, path=Path(path))

    @classmethod
    def load(cls, path: Path | str) -> TrustedKeys:
        """Read the device's trusted-key file.

        The file is the device's trust anchor, so a file this user does not
        own, or one other users can write, is refused rather than trusted.
        """
        target = Path(path).expanduser()
        # Resolve and pin before opening: the bytes are read, and later
        # authorized, from this same immutable location. Resolving after the
        # read would let an ancestor alias change in between associate keys
        # loaded from one location with another, permitted one.
        pinned = target.resolve(strict=False)
        text = read_owned_text(pinned, "trusted keys")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GrantError(f"{pinned}: trusted keys are not JSON") from exc
        return cls(
            keys=cls.from_mapping(parsed, path=pinned).keys,
            path=target,
            resolved_path=pinned,
        )


def read_owned_text(path: Path, label: str, *, secret: bool = False) -> str:
    """Read a file this user owns. A secret file must not be group-readable."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GrantError(f"{path}: {label} cannot be read") from exc
    try:
        metadata = os.fstat(descriptor)
        exposed = metadata.st_mode & (0o077 if secret else 0o022)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or exposed
        ):
            detail = "and private to this user" if secret else "and not writable by others"
            raise GrantError(
                f"{path}: {label} must be a regular file owned by this user {detail}"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def sign_grant(
    *,
    key: SigningKey,
    lease_id: str,
    ticket_id: str,
    daemon_id: str,
    policy_version: str,
    expires_at: int,
    issued_at: int | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Sign one lease. The returned object is what the daemon verifies."""
    issued = int(time.time()) if issued_at is None else issued_at
    payload: dict[str, Any] = {
        "v": GRANT_FORMAT,
        "kid": key.kid,
        "lease_id": _text_id(lease_id, "lease_id"),
        "ticket_id": _text_id(ticket_id, "ticket_id"),
        "daemon_id": _text_id(daemon_id, "daemon_id"),
        "policy_version": _policy_version(policy_version),
        "nonce": nonce if nonce is not None else secrets.token_urlsafe(24),
        "iat": _unix(issued, "iat"),
        "exp": _unix(expires_at, "exp"),
    }
    if not isinstance(payload["nonce"], str) or not _NONCE.fullmatch(payload["nonce"]):
        raise GrantMalformed("grant nonce is missing")
    if payload["exp"] <= payload["iat"]:
        raise GrantMalformed("grant expiry is not after it was issued")
    signature = key._private().sign(canonical_bytes(payload))
    return {**payload, "sig": _b64e(signature)}


def _policy_version(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise GrantMalformed("grant policy_version is not valid")
    if any(char in value for char in ("\n", "\r", "=")):
        raise GrantMalformed("grant policy_version is not valid")
    return value


def _payload_from_grant(grant: Mapping[str, Any]) -> dict[str, Any]:
    version = grant.get("v")
    if isinstance(version, bool) or version != GRANT_FORMAT:
        raise GrantMalformed("grant version is not supported")
    kid = grant.get("kid")
    if not isinstance(kid, str) or not _KID.fullmatch(kid):
        raise GrantMalformed("grant key id is missing")
    nonce = grant.get("nonce")
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise GrantMalformed("grant nonce is missing")
    policy_version = grant.get("policy_version")
    return {
        "v": GRANT_FORMAT,
        "kid": kid,
        "lease_id": _text_id(grant.get("lease_id"), "lease_id"),
        "ticket_id": _text_id(grant.get("ticket_id"), "ticket_id"),
        "daemon_id": _text_id(grant.get("daemon_id"), "daemon_id"),
        "policy_version": _policy_version(policy_version),
        "nonce": nonce,
        "iat": _unix(grant.get("iat"), "iat"),
        "exp": _unix(grant.get("exp"), "exp"),
    }


def verify_grant(
    grant: Any, keys: TrustedKeys, *, now: int
) -> VerifiedGrant:
    """The verified payload, or a `GrantError` the daemon turns into a refusal.

    The signature is checked before the expiry, so a grant that was not
    signed by a trusted key is refused even when its expiry is also wrong.
    """
    if not isinstance(grant, Mapping):
        raise GrantUnsigned("grant is missing")
    signature = grant.get("sig")
    if not isinstance(signature, str) or not signature:
        raise GrantUnsigned("grant is unsigned")
    payload = _payload_from_grant(grant)
    if payload["exp"] <= payload["iat"]:
        raise GrantMalformed("grant expiry is not after it was issued")
    public = keys.public_key(payload["kid"])
    if public is None:
        raise GrantSignatureError("grant key is not trusted")
    try:
        raw_signature = _b64d(signature)
    except GrantMalformed as exc:
        raise GrantSignatureError("grant signature is not valid") from exc
    if len(raw_signature) != 64:
        raise GrantSignatureError("grant signature is not valid")
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(
            raw_signature, canonical_bytes(payload)
        )
    except InvalidSignature as exc:
        raise GrantSignatureError("grant signature does not match") from exc
    current = _unix(now, "now")
    if current >= payload["exp"]:
        raise GrantExpired("grant has expired")
    return VerifiedGrant(
        kid=payload["kid"],
        lease_id=payload["lease_id"],
        ticket_id=payload["ticket_id"],
        daemon_id=payload["daemon_id"],
        policy_version=payload["policy_version"],
        nonce=payload["nonce"],
        issued_at=payload["iat"],
        expires_at=payload["exp"],
    )


def bind_grant(
    grant: VerifiedGrant,
    *,
    ticket_id: str,
    lease_id: str,
    policy_version: str,
) -> None:
    """Refuse a signature that was minted for a different lease or policy."""
    if (
        grant.ticket_id != str(ticket_id or "")
        or grant.lease_id != str(lease_id or "")
        or grant.policy_version != str(policy_version or "")
    ):
        raise GrantMismatch("signed grant does not match the ticket")


class NonceStore:
    """Nonces this device has already accepted, so a captured grant cannot run twice.

    An unreadable record is a refusal. Resetting it on a parse error would
    open the replay window the file exists to close.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        # Pin the resolved record location once: a symlinked component that is
        # re-pointed later (for example by a Worker that can write an ancestor)
        # cannot redirect the replay ledger between consent checks.
        self._resolved = self.path.resolve(strict=False)
        self._lock = threading.Lock()

    @property
    def resolved_path(self) -> Path:
        """The immutable replay-record location this store reads and writes."""
        return self._resolved

    def _open_parent(self, *, create: bool) -> tuple[int, str]:
        """Open the replay record's parent directory without following symlinks.

        The path is resolved component by component with ``O_NOFOLLOW``: a
        symlinked (or group/world-writable) ancestor could redirect the replay
        ledger to a path the Worker controls, letting a consumed grant be
        presented again against an empty record. Returns the directory fd and
        the record's file name for dir_fd-relative access.
        """
        if ".." in self.path.parts:
            raise NonceStoreError("replay state path is not allowed")
        parent = self._resolved.parent
        directory_flag = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(str(parent.anchor or "/"), directory_flag)
            for part in parent.parts[1:]:
                child = self._open_child(part, descriptor, directory_flag, create)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise NonceStoreError(
                    "replay state directory must be private to the daemon user"
                )
            return descriptor, self._resolved.name
        except NonceStoreError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if not create and isinstance(exc, FileNotFoundError):
                # The parent directory does not exist yet: there is no record.
                raise
            raise NonceStoreError("replay state path cannot be opened safely") from exc

    @staticmethod
    def _open_child(part: str, parent: int, flags: int, create: bool) -> int:
        try:
            return os.open(part, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(part, 0o700, dir_fd=parent)
            return os.open(part, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)

    @contextlib.contextmanager
    def _process_lock(self):
        """An interprocess lock covering one read-check-write of the record.

        Two daemon processes can share the same replay file. The thread lock
        only serialises this instance, so a separate lock file carries an
        advisory ``flock`` across processes; losing an update here would open
        the replay window the file exists to close. The lock file is opened
        relative to the pinned parent directory, never through a symlink.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        directory = None
        descriptor = None
        try:
            directory, name = self._open_parent(create=True)
            lock_name = name + ".lock"
            descriptor = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except NonceStoreError:
            raise
        except OSError as exc:
            raise NonceStoreError("replay state cannot be locked") from exc
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def seen(self, nonce: str, *, now: int) -> bool:
        with self._lock, self._process_lock():
            return nonce in self._load(now)

    def consume(self, nonce: str, expires_at: int, *, now: int) -> None:
        """Remember `nonce` until `expires_at`, or raise `GrantReplayed`."""
        if not _NONCE.fullmatch(nonce or ""):
            raise GrantMalformed("grant nonce is missing")
        if int(expires_at) <= int(now):
            # A grant that expired while the revocation poll was in flight is
            # not allowed to run, and must not be silently dropped.
            raise GrantExpired("signed grant has expired")
        with self._lock, self._process_lock():
            current = self._load(now)
            if nonce in current:
                raise GrantReplayed("signed grant was already used")
            current[nonce] = int(expires_at)
            self._write({item: exp for item, exp in current.items() if exp > now})

    def _load(self, now: int) -> dict[str, int]:
        try:
            directory, name = self._open_parent(create=False)
        except FileNotFoundError:
            return {}
        try:
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory
                )
            except FileNotFoundError:
                return {}
            except OSError as exc:
                raise NonceStoreError("replay state cannot be read") from exc
            try:
                info = os.fstat(descriptor)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise NonceStoreError("replay state must be private")
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = None
                    text = stream.read()
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        finally:
            os.close(directory)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise NonceStoreError("replay state is unreadable") from exc
        if not isinstance(parsed, Mapping) or parsed.get("format") != GRANT_FORMAT:
            raise NonceStoreError("replay state is unreadable")
        nonces = parsed.get("nonces")
        if not isinstance(nonces, Mapping):
            raise NonceStoreError("replay state is unreadable")
        live: dict[str, int] = {}
        for nonce, exp in nonces.items():
            if not isinstance(nonce, str) or isinstance(exp, bool) or not isinstance(exp, int):
                raise NonceStoreError("replay state is unreadable")
            if exp > now:
                live[nonce] = exp
        return live

    def _write(self, nonces: dict[str, int]) -> None:
        if ".." in self.path.parts:
            raise NonceStoreError("replay state path is not allowed")
        directory = None
        try:
            directory, name = self._open_parent(create=True)
            temporary = f".{name}.{os.getpid()}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
            except OSError as exc:
                raise NonceStoreError("replay state cannot be written") from exc
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(
                        {"format": GRANT_FORMAT, "nonces": nonces},
                        stream,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(
                    temporary, name, src_dir_fd=directory, dst_dir_fd=directory
                )
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
        except NonceStoreError:
            raise
        except OSError as exc:
            raise NonceStoreError("replay state cannot be written") from exc
        finally:
            if directory is not None:
                os.close(directory)


@dataclass(frozen=True)
class OriginCheck:
    """The device's trusted keys and the nonces it has already accepted."""

    keys: TrustedKeys
    nonces: NonceStore
