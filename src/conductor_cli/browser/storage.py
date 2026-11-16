"""Validation and allowlist filtering for imported browser storage state."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .service import BrowserCommandError, clean_domain

MAX_STATE_BYTES = 1_000_000
MAX_COOKIES = 5_000
MAX_ORIGINS = 500
MAX_STORAGE_ITEMS = 500


@dataclass(frozen=True)
class StorageState:
    cookies: list[dict[str, Any]] = field(repr=False)
    local_storage: dict[str, dict[str, str]] = field(repr=False)
    domains: list[str]


def _checked_state_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise BrowserCommandError("Could not safely read the login state file") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > MAX_STATE_BYTES
        ):
            raise BrowserCommandError("Login state must be a private regular file no larger than 1 MB")
        chunks: list[bytes] = []
        remaining = MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_STATE_BYTES:
            raise BrowserCommandError("Login state file is too large")
        return data
    finally:
        os.close(descriptor)


def _origin_host(origin: Any) -> tuple[str, Any]:
    if not isinstance(origin, str) or len(origin) > 2048:
        raise BrowserCommandError("Login state contains a malformed origin")
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        # Force malformed port syntax to fail during parsing.
        _ = parsed.port
    except ValueError:
        raise BrowserCommandError("Login state contains a malformed origin") from None
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise BrowserCommandError("Login state contains a malformed origin")
    try:
        host = clean_domain(hostname)
    except BrowserCommandError:
        raise BrowserCommandError("Login state contains a malformed origin") from None
    return host, parsed


def _normalize_state(data: bytes, domains: list[str]) -> StorageState:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise BrowserCommandError("Login state is not valid JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"cookies", "origins"}:
        raise BrowserCommandError("Login state must contain only cookies and origins")
    if not isinstance(payload["cookies"], list) or not isinstance(payload["origins"], list):
        raise BrowserCommandError("Login state cookies and origins must be lists")
    if len(payload["cookies"]) > MAX_COOKIES or len(payload["origins"]) > MAX_ORIGINS:
        raise BrowserCommandError("Login state contains too many entries")
    try:
        allowed = {clean_domain(domain) for domain in domains}
    except BrowserCommandError:
        raise BrowserCommandError("Allowed domains must be exact DNS names") from None
    if not allowed or len(allowed) != len(domains):
        raise BrowserCommandError("Allowed domains must be non-empty and unique")

    cookies: list[dict[str, Any]] = []
    imported_domains: set[str] = set()
    cookie_fields = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    for cookie in payload["cookies"]:
        if (
            not isinstance(cookie, dict)
            or set(cookie) - cookie_fields
            or not {"name", "value", "domain"} <= set(cookie)
        ):
            raise BrowserCommandError("Login state contains a malformed or unsupported cookie")
        domain_raw = cookie["domain"]
        if not isinstance(domain_raw, str):
            raise BrowserCommandError("Login state contains a malformed cookie domain")
        domain_for_match = domain_raw[1:] if domain_raw.startswith(".") else domain_raw
        if domain_for_match.startswith("."):
            raise BrowserCommandError("Login state cookie domains may have at most one leading dot")
        try:
            domain = clean_domain(domain_for_match)
        except BrowserCommandError:
            raise BrowserCommandError("Login state contains a malformed cookie domain") from None
        # Ignore unrelated sites before examining their cookie names or values.
        if domain not in allowed:
            continue

        name, value = cookie["name"], cookie["value"]
        if not isinstance(name, str) or not name or len(name) > 256 or not isinstance(value, str) or len(value) > 8192:
            raise BrowserCommandError("Login state contains a malformed cookie")
        normalized: dict[str, Any] = {
            "name": name,
            "value": value,
            # Keep the source's leading dot so Chromium retains domain-cookie scope.
            "domain": domain_raw,
            "path": cookie.get("path", "/"),
        }
        if not isinstance(normalized["path"], str) or not normalized["path"].startswith("/"):
            raise BrowserCommandError("Login state contains a malformed cookie path")
        if "expires" in cookie:
            expires = cookie["expires"]
            if type(expires) not in {int, float} or not math.isfinite(expires):
                raise BrowserCommandError("Login state contains a malformed cookie expiry")
            normalized["expires"] = expires
        for flag in ("httpOnly", "secure"):
            if flag in cookie:
                if type(cookie[flag]) is not bool:
                    raise BrowserCommandError("Login state contains a malformed cookie flag")
                normalized[flag] = cookie[flag]
        if "sameSite" in cookie:
            same_site = cookie["sameSite"]
            if not isinstance(same_site, str) or same_site not in {"Strict", "Lax", "None"}:
                raise BrowserCommandError("Login state contains an unsupported SameSite value")
            normalized["sameSite"] = same_site
        cookies.append(normalized)
        imported_domains.add(domain)

    local_storage: dict[str, dict[str, str]] = {}
    for origin_row in payload["origins"]:
        if not isinstance(origin_row, dict) or set(origin_row) != {"origin", "localStorage"}:
            raise BrowserCommandError("Login state contains a malformed origin entry")
        host, parsed = _origin_host(origin_row["origin"])
        # Filter unrelated sites before inspecting their local-storage entries.
        if host not in allowed:
            continue
        try:
            port = parsed.port
        except ValueError:
            raise BrowserCommandError("Login state contains a malformed origin") from None
        if (
            parsed.scheme != "https"
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise BrowserCommandError("Allowed local-storage origins must be HTTPS origins")
        normalized_origin = f"https://{host}"
        entries = origin_row["localStorage"]
        if not isinstance(entries, list) or len(entries) > MAX_STORAGE_ITEMS:
            raise BrowserCommandError("Login state contains malformed local storage")
        values: dict[str, str] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "value"}
                or not isinstance(entry["name"], str)
                or not isinstance(entry["value"], str)
            ):
                raise BrowserCommandError("Login state contains a malformed local-storage entry")
            if not entry["name"] or len(entry["name"]) > 1024 or len(entry["value"]) > 32768:
                raise BrowserCommandError("Login state contains an oversized local-storage entry")
            if entry["name"] in values:
                raise BrowserCommandError("Login state contains duplicate local-storage keys")
            values[entry["name"]] = entry["value"]
        if normalized_origin in local_storage:
            raise BrowserCommandError("Login state contains duplicate origins")
        local_storage[normalized_origin] = values
        if values:
            imported_domains.add(host)

    return StorageState(
        cookies=cookies,
        local_storage=local_storage,
        domains=[domain for domain in domains if clean_domain(domain) in imported_domains],
    )


def read_storage_state(path: Path, domains: list[str]) -> StorageState:
    """Read a private browser state file and retain only exact allowed domains."""
    return _normalize_state(_checked_state_file(path), domains)
