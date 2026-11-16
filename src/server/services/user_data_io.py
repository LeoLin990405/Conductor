"""User-data IO layer: the preference and account profile files.

Called by ``profile_files`` to serve ``.agents/user/profile/{preference,user}.json``
and validate agent writes.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import available_timezones

from psycopg.types.json import Json

from src.server.services.store_backends.db_json_route import UnreadableJsonError, UserDataValidationError, load_json
from src.server.database import user as user_db
from src.server.models.profile_vocabulary import check_profile
from src.server.utils.db import UpdateQueryBuilder

logger = logging.getLogger(__name__)

# Sentinel hash returned for empty/cold-user payloads. Stable across replicas
# so two readers of an empty profile agree on the version without hitting DB
# timestamps that don't exist yet.
EMPTY_VERSION = "sha256:0"


# =============================================================================
# JSON encoding / decoding helpers
# =============================================================================


def _json_default(obj: Any) -> Any:
    """JSON serialization hook for Decimal / datetime / UUID."""
    if isinstance(obj, Decimal):
        # Emit as string — stdlib json has no native Decimal-as-number support.
        return format(obj.normalize(), "f") if obj == obj.normalize() else str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Not JSON serializable: {type(obj).__name__}")


def serialize_json(payload: dict[str, Any]) -> str:
    """Render the read payload as pretty-printed JSON the agent can edit."""
    return json.dumps(payload, default=_json_default, indent=2, ensure_ascii=False)


def parse_json(content: str, file: str) -> dict[str, Any]:
    """Parse agent-written JSON. Raises UserDataValidationError on parse failure."""
    try:
        return load_json(content)
    except json.JSONDecodeError as e:
        raise UserDataValidationError(
            error_type="parse_error",
            file=file,
            field_path=f"line {e.lineno} col {e.colno}",
            hint=f"invalid JSON: {e.msg}. Re-read the file and write a syntactically valid JSON object.",
        ) from None
    except UnreadableJsonError as e:
        raise UserDataValidationError(error_type="parse_error", file=file, field_path="", hint=f"invalid JSON: {e}") from None


def _coerce_decimal(value: Any, file: str, field_path: str) -> Decimal:
    """Convert a JSON value to Decimal. Accepts strings, ints, floats."""
    if value is None:
        raise UserDataValidationError(
            error_type="schema_error",
            file=file,
            field_path=field_path,
            hint="required numeric field is missing or null",
        )
    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, str)):
            return Decimal(value)
        if isinstance(value, float):
            # Round-trip through str to avoid float repr noise (e.g. 0.1 → "0.1").
            return Decimal(str(value))
    except (InvalidOperation, ValueError):
        pass
    raise UserDataValidationError(
        error_type="schema_error",
        file=file,
        field_path=field_path,
        hint=f"expected a numeric value (string or number), got {value!r}",
    )


def _coerce_str(value: Any, file: str, field_path: str, *, required: bool = True) -> str | None:
    """Convert to str. Returns None for null when not required."""
    if value is None:
        if required:
            raise UserDataValidationError(
                error_type="schema_error",
                file=file,
                field_path=field_path,
                hint="required string field is missing or null",
            )
        return None
    if not isinstance(value, str):
        raise UserDataValidationError(
            error_type="schema_error",
            file=file,
            field_path=field_path,
            hint=f"expected string, got {type(value).__name__}",
        )
    return value


# ---------------------------------------------------------------------------
# Strict-validation helpers
#
# The agent writes JSON; the DB enforces uniqueness and length constraints.
# Catching typos, duplicates, and overlong values at parse time gives the
# agent a clear hint ("unknown field 'symbo' — did you mean 'symbol'?")
# instead of an opaque ``constraint_error`` after a rollback.
# ---------------------------------------------------------------------------

def _reject_unknown_keys(
    row: dict[str, Any],
    allowed: frozenset[str],
    *,
    file: str,
    path: str,
    tolerate: frozenset[str] = frozenset({"id"}),
) -> None:
    """Reject unknown fields with a helpful suggestion. Tolerated keys are silently ignored."""
    unknown = set(row) - allowed - tolerate
    if not unknown:
        return
    sample = next(iter(unknown))
    suggestion = _suggest_field(sample, allowed)
    suggest_part = f" — did you mean {suggestion!r}?" if suggestion else ""
    raise UserDataValidationError(
        "schema_error", file, path,
        f"unknown field(s) {sorted(unknown)!r}. allowed: {sorted(allowed)!r}.{suggest_part}",
    )


def _suggest_field(unknown: str, allowed: frozenset[str]) -> str | None:
    """Cheap typo suggestion: substring containment or shared 3-char prefix."""
    lower = unknown.lower()
    for name in allowed:
        nl = name.lower()
        if lower == nl or lower in nl or nl in lower:
            return name
        if len(lower) >= 3 and lower[:3] == nl[:3]:
            return name
    return None


def _check_max_len(
    value: str | None, max_len: int, *, file: str, path: str,
) -> None:
    if value is not None and len(value) > max_len:
        raise UserDataValidationError(
            "schema_error", file, path,
            f"value too long ({len(value)} chars); max is {max_len}.",
        )


def _require_nonempty_str(value: Any, *, file: str, path: str) -> str:
    """Required string that must be non-empty after stripping whitespace."""
    coerced = _coerce_str(value, file, path, required=True)
    stripped = coerced.strip() if coerced is not None else ""
    if not stripped:
        raise UserDataValidationError(
            "schema_error", file, path,
            "value is empty or whitespace-only.",
        )
    return stripped


def _reject_negative(
    value: Decimal | None, *, file: str, path: str,
) -> None:
    if value is not None and value < 0:
        raise UserDataValidationError(
            "schema_error", file, path,
            f"must be >= 0, got {value}.",
        )


def _content_hash(payload: dict[str, Any]) -> str:
    """Hash of the agent-visible content for use as ``__version__``.

    Excludes ``__version__`` itself so the hash is stable across the serialize →
    parse round-trip. Hashing content (not ``updated_at``) means an unrelated
    writer that bumps timestamps without changing agent-visible state leaves
    the version unchanged — no spurious version_conflict on the next write.

    Canonicalization recursively sorts list elements (by JSON string form) so
    the hash is invariant to row-order differences between the pre-check fetch
    and the in-transaction recheck. Without this, an ``ORDER BY`` drift between
    fetch helpers would surface as a phantom version_conflict.
    """
    canonical = _canonicalize({k: v for k, v in payload.items() if k != "__version__"})
    encoded = json.dumps(canonical, default=_json_default, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _canonicalize(obj: Any) -> Any:
    """Return a representation of ``obj`` with all nested lists in stable order."""
    if isinstance(obj, dict):
        return {k: _canonicalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return sorted(
            (_canonicalize(item) for item in obj),
            key=lambda x: json.dumps(x, default=_json_default, sort_keys=True, ensure_ascii=False),
        )
    return obj


def _stamp_version(payload: dict[str, Any]) -> dict[str, Any]:
    """Set ``__version__`` to the content hash of ``payload`` in place."""
    payload["__version__"] = _content_hash(payload)
    return payload


# =============================================================================
# =============================================================================


_PREFERENCE_KEYS = ("agent_preference", "other_preference")
# Keys exposed to the agent in preference.json. `other_preference` and
# `model_preference` are server-managed JSONB columns (onboarding state,
# platform flags, model routing) — keeping them out of the agent's view means
# the agent can't accidentally clobber them via replace-mode writes.
_AGENT_PREFERENCE_KEYS = ("agent_preference",)
# Both server-managed keys are tolerated on input (silently dropped — see
# test_parse_silently_drops_other_preference) but treated as "known".
_PREFERENCE_ROOT_KEYS: frozenset[str] = frozenset(
    ("__version__", "other_preference", "model_preference", *_AGENT_PREFERENCE_KEYS)
)


async def fetch_preferences_for_user(user_id: str) -> dict[str, Any] | None:
    """Single row from `user_preferences`. None if the user has never set any."""
    return await user_db.get_user_preferences(user_id)


def serialize_preferences(prefs: dict[str, Any] | None) -> dict[str, Any]:
    """Serialize agent-visible preference columns. Excludes `other_preference`."""
    payload: dict[str, Any] = {"__version__": EMPTY_VERSION}
    src = prefs or {}
    for key in _AGENT_PREFERENCE_KEYS:
        payload[key] = src.get(key) or {}
    return _stamp_version(payload)


def parse_preferences(content: str) -> dict[str, Any]:
    """Parse + validate. Returns the preference values dict."""
    file = "preference.json"
    data = parse_json(content, file)
    if not isinstance(data, dict):
        raise UserDataValidationError("schema_error", file, "", "root must be a JSON object")

    _reject_unknown_keys(
        data, _PREFERENCE_ROOT_KEYS, file=file, path="", tolerate=frozenset(),
    )

    result: dict[str, Any] = {}
    # Only parse agent-visible keys; ignore any `other_preference` the agent
    # might have copied through — that column is server-managed and the
    # applier preserves whatever's already in the DB.
    for key in _AGENT_PREFERENCE_KEYS:
        value = data.get(key)
        if value is None:
            result[key] = {}
            continue
        if not isinstance(value, dict):
            raise UserDataValidationError(
                "schema_error", file, key,
                f"expected an object, got {type(value).__name__}",
            )
        result[key] = value
    return result


def check_preferences(values: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    """``parse_preferences``' values with each structured key checked and in
    its stored spelling, against ``current`` as the save's transaction read
    it: a value the row already holds passes, so a save that leaves one from
    before the key had a vocabulary is not refused for it. Every problem is
    named in one refusal, so one retry can fix them all."""
    checked, problems = check_profile(values, current)
    if problems:
        path, hint = problems[0]
        raise UserDataValidationError(
            "schema_error", "preference.json", path, hint, problems=problems
        )
    return checked


def preferences_equal(current: dict[str, Any] | None, values: dict[str, Any]) -> bool:
    """True when the parsed payload is byte-equal to the current DB row.

    Callers use this to skip a no-op write transaction on identical edits.
    Treats missing/None values and empty dicts as equivalent (the schema
    normalizes both to ``{}``).
    """
    if current is None:
        return all(not (values.get(k) or {}) for k in _AGENT_PREFERENCE_KEYS)
    return all(
        (current.get(k) or {}) == (values.get(k) or {})
        for k in _AGENT_PREFERENCE_KEYS
    )


async def write_preferences(
    cur: Any, values: dict[str, Any], current: dict[str, Any] | None, user_id: str,
) -> None:
    """Replace-mode upsert of the agent-visible preference columns, over
    ``current`` as the version check read it.

    Writes only the agent-visible ``agent_preference`` column.
    ``other_preference`` and ``model_preference`` are left untouched: both are
    server-managed (onboarding state, platform flags, model routing) and not
    exposed to the agent. New users get empty objects on first insert, from
    the explicit value and the column default respectively; existing rows
    keep theirs. The caller drops the preferences cache once it commits.
    """
    if current is not None:
        await cur.execute(
            """
            UPDATE user_preferences SET
                agent_preference = %s::jsonb,
                updated_at = NOW()
            WHERE user_id = %s
            """,
            (
                Json(values.get("agent_preference") or {}),
                user_id,
            ),
        )
    else:
        await cur.execute(
            """
            INSERT INTO user_preferences (
                user_preference_id, user_id,
                agent_preference, other_preference,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            """,
            (
                str(uuid4()), user_id,
                Json(values.get("agent_preference") or {}),
                Json({}),
            ),
        )
    logger.info("[user_data_io] applied preferences user_id=%s", user_id)


# =============================================================================
# User (the account row)
# =============================================================================


_USER_KEYS: frozenset[str] = frozenset({"name", "timezone", "locale", "onboarding_completed"})
# Column lengths from migrations/versions/001_initial_schema.py.
_USER_MAX_LEN: dict[str, int] = {"name": 255, "timezone": 100, "locale": 20}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

# A language, then an optional script and region: `en`, `en-US`, `zh-Hans-CN`,
# `es-419`. At most 12 characters, inside the column. Variants and extensions
# are left out, since no reader of the column (the app's language switch, the
# prompt) has a use for them.
_LOCALE_TAG = re.compile(r"([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?")


@functools.cache
def _iana_zones() -> dict[str, str]:
    """Every IANA zone name, keyed by its lowercase spelling. Not ``ZoneInfo``
    alone, which also opens files under the zone directory that name no zone
    a browser formats in (``posix/...``, ``right/...``). ``Factory`` and
    ``localtime`` are left out for the same reason: browsers refuse both."""
    return {
        zone.lower(): zone
        for zone in available_timezones()
        if zone not in ("Factory", "localtime")
    }


async def fetch_user_for_user(user_id: str) -> dict[str, Any] | None:
    """The user's row, None when there is none."""
    return await user_db.get_user(user_id)


def serialize_user(row: dict[str, Any] | None) -> dict[str, Any]:
    """The columns the agent may set, from the user's row."""
    src = row or {}
    return _stamp_version({
        "__version__": EMPTY_VERSION,
        "name": src.get("name"),
        "timezone": src.get("timezone"),
        "locale": src.get("locale"),
        "onboarding_completed": bool(src.get("onboarding_completed")),
    })


def parse_user(content: str) -> dict[str, Any]:
    """The columns ``content`` sets. A key left out keeps its value, so only
    the keys written are returned; a null clears a string column. Zone and
    locale are checked only by ``diff_user``, against the row: a value saved
    before these checks existed must survive a save that leaves it as it is."""
    file = "user.json"
    data = parse_json(content, file)
    if not isinstance(data, dict):
        raise UserDataValidationError("schema_error", file, "", "root must be a JSON object")

    _reject_unknown_keys(data, _USER_KEYS, file=file, path="", tolerate=frozenset())

    values: dict[str, Any] = {}
    for key, max_len in _USER_MAX_LEN.items():
        if key in data:
            value = _coerce_str(data[key], file, key, required=False)
            # Blank says no more than null does.
            value = (value or "").strip() or None
            _check_max_len(value, max_len, file=file, path=key)
            # Each value is printed as one line of the prompt's profile block.
            if value is not None and _CONTROL_CHARS.search(value):
                raise UserDataValidationError(
                    "schema_error", file, key, "must be one line, without control characters",
                )
            values[key] = value
    if "onboarding_completed" in data:
        flag = data["onboarding_completed"]
        if not isinstance(flag, bool):
            raise UserDataValidationError(
                "schema_error", file, "onboarding_completed",
                f"expected true or false, got {type(flag).__name__}",
            )
        values["onboarding_completed"] = flag
    return values


# POSIX signs: Etc/GMT+8 is eight hours behind UTC, the reverse of what a user
# means by "GMT+8", so taking one for the other runs every turn and new
# automation sixteen hours off.
_ETC_OFFSET = re.compile(r"etc/gmt[+-][1-9][0-9]*")


def _zone(value: str) -> str:
    if _ETC_OFFSET.fullmatch(value.lower()):
        raise UserDataValidationError(
            "schema_error", "user.json", "timezone",
            f"{value!r} counts its offset backwards ('Etc/GMT+8' is UTC-8). Use the zone of the "
            "user's city, like 'Asia/Shanghai'.",
        )
    zone = _iana_zones().get(value.lower())
    if zone is None:
        raise UserDataValidationError(
            "schema_error", "user.json", "timezone",
            f"{value!r} is not an IANA time zone. Use a name like 'America/New_York' or 'Asia/Shanghai', "
            "or null to clear it.",
        )
    return zone


def _locale(value: str) -> str:
    match = _LOCALE_TAG.fullmatch(value)
    if match is None:
        raise UserDataValidationError(
            "schema_error", "user.json", "locale",
            f"{value!r} is not a language tag. Use a language and region joined by a hyphen, "
            "like 'en-US' or 'zh-CN', or null to clear it.",
        )
    # Tags compare without regard to case; the app matches the usual
    # spelling, so `zh-cn` is stored as `zh-CN`.
    language, script, region = match.groups()
    return "-".join(
        part for part in (language.lower(), script and script.title(), region and region.upper()) if part
    )


def diff_user(values: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    """The columns writing ``parse_user``'s values over ``row`` changes, by
    column, each value checked and in its stored spelling."""
    current = row or {}
    changes: dict[str, Any] = {}
    for key, value in values.items():
        stored = current.get(key)
        # parse_user strips what it reads, so compare a stored value the same
        # way, or one saved with stray spaces is checked as if it were new.
        if isinstance(stored, str):
            stored = stored.strip()
        if value is not None and value != stored:
            if key == "timezone":
                value = _zone(value)
            elif key == "locale":
                value = _locale(value)
        if value != current.get(key):
            changes[key] = value
    return changes


async def write_user(cur: Any, changes: dict[str, Any], user_id: str) -> None:
    """One UPDATE of the columns ``changes`` names, as the account's other
    writers make it. Raises when the user has no row, since the save would
    otherwise report a change it never made."""
    builder = UpdateQueryBuilder()
    for column, value in changes.items():
        builder.add_field(column, value, nullable=True)
    query, params = builder.build(table="users", where_clause="user_id = %s", where_params=[user_id])
    await cur.execute(query, params)
    if cur.rowcount == 0:
        raise LookupError(f"no users row for user_id={user_id}")
    logger.info("[user_data_io] applied user changes user_id=%s columns=%s", user_id, sorted(changes))
