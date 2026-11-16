"""Credential scrubbing for error text exposed or persisted by the server."""

import dataclasses
import enum
import itertools
import re
import types
import typing
from collections.abc import Iterable, Mapping, Set
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from ptc_agent.core.sandbox.providers.ssh_errors import connect_failure


# httpx includes request URLs in some exception messages. Strip basic-auth
# userinfo before those messages reach clients or durable conversation rows.
# Any scheme, not just http(s): the exceptions most likely to carry a password
# here are the connection failures, and those quote a DSN
# (`postgresql://user:pw@host`, `redis://…`, `amqp://…`) rather than a URL.
#
# The scheme is bounded: with an open-ended run every start position of a long
# scheme-looking word scanned to its end before failing at ``://``, which is
# quadratic on text a caller chooses (a dict key in a 422's ``loc``).
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]{0,31}://)[^@/\s]+@")

# Provider exceptions can echo request headers or key parameters. Mask the
# common credential shapes without replacing otherwise useful diagnostics.
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_PARAM_RE = re.compile(
    r"(?i)\b(api[-_]?key|x-api-key|authorization|access[-_]?token|client[-_]?secret)"
    r"(\s*[=:]\s*)([\"']?)[A-Za-z0-9._~+/=-]{8,}"
)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_URL_KEY_QUERY_RE = re.compile(
    r"(?i)([?&](?:key|apikey|token|secret|password|credential)=)[^&\s\"']+"
)
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b")


def sanitize_error_text(text: str) -> str:
    """Scrub credential-shaped values from raw exception text."""
    text = _URL_USERINFO_RE.sub(r"\1", text)
    text = _BEARER_TOKEN_RE.sub(r"\1 [REDACTED]", text)
    text = _KEY_PARAM_RE.sub(r"\1\2\3[REDACTED]", text)
    text = _URL_KEY_QUERY_RE.sub(r"\1[REDACTED]", text)
    text = _SK_TOKEN_RE.sub("[REDACTED]", text)
    return _GOOGLE_KEY_RE.sub("[REDACTED]", text)


def validation_error_text(
    e: ValidationError, *, owned: Set[str], under: int = 0
) -> str:
    """Each refusal as its field and the validator's own sentence.

    Never the input it rejected, which can carry a credential, nor the framing
    and link that a ValidationError's str() adds. The field path and the sentence
    go through the same rules as a 422 body: a path part the schema (``owned``,
    from :func:`schema_names`) does not define reads ``<key>``, and runs of the
    rejected inputs' own text are cut from the sentence.

    ``under`` drops that many leading path parts, for a caller that validated one
    part of a document and names its place itself (with :func:`public_path`). They
    are made public first and dropped after, so which parts are kept never
    depends on where the path was cut.
    """
    errors = e.errors(include_url=False)
    caller = caller_runs(*(err.get("input") for err in errors))
    parts = []
    for err in itertools.islice(errors, _PUBLIC_ERRORS_MAX):
        msg = _public_message(err, caller).removeprefix("Value error, ")
        loc = _public_loc(err["loc"], owned, err.get("input"))[under:]
        field = ".".join(str(p) for p in loc)
        parts.append(f"{field}: {msg}" if field else msg)
    return "; ".join(parts) or "validation error"


# A 422 names the field and the rule; neither needs more than this. The cap is
# on what is returned (after the value is cut out, so a cut can never leave a
# prefix of it behind), because ``loc`` is a key the caller chose and can be
# megabytes.
_PUBLIC_LOC_MAX = 128
_PUBLIC_MSG_MAX = 1000
_PUBLIC_ERRORS_MAX = 100


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _scrub(text: str, rejected: Any) -> str:
    """Text the client chose (a key, a validator's sentence) minus its own value.

    The rejected value is cut out exactly, which no credential shape is needed
    for; the shape scrub is the backstop for a secret that sits in the text
    without being the rejected value (an extra key, a dict key).
    """
    if isinstance(rejected, str) and len(rejected) >= 8:
        text = text.replace(rejected, "[REDACTED]")
    return sanitize_error_text(text)


# A validator's sentence can carry text of the request that is not the rejected
# value: the host cut out of a URL, a ``repr()`` of the value (whose escapes
# stop an exact cut from matching), a lower-cased copy. So after the exact cut,
# no run of this many characters that occurs in the request's own strings may
# survive in a sentence, whatever shape it has. Shorter runs are the sentence's
# own words.
_ECHO_RUN = 8
# Bounds what is indexed per request and per string; the error path of a route
# that validates before it authenticates must stay cheap.
_ECHO_TEXT_MAX = 16 * 1024
_ECHO_STRING_MAX = 2048
_ECHO_NODES_MAX = 2048


def _fold(text: str) -> str:
    """Lower-case without changing the length, so positions still line up."""
    return "".join(
        lower if len(lower := ch.lower()) == 1 else ch for ch in text
    )


def _push(stack: list[Any], items: Iterable[Any], room: int) -> None:
    """Add at most ``room`` items: the bound is on what is queued, not only on
    what is read, so a huge container never has to be copied."""
    if room > 0:
        stack.extend(itertools.islice(items, room))


def caller_runs(*sources: Any) -> frozenset[str]:
    """Every folded ``_ECHO_RUN``-character run of the request's own strings.

    ``sources`` are what the caller sent: the parsed body (or its raw bytes when
    it did not parse), the query parameters, the path parameters. Keys and
    values at any depth are read, up to ``_ECHO_NODES_MAX`` nodes,
    ``_ECHO_STRING_MAX`` characters of one string and ``_ECHO_TEXT_MAX`` in all.
    A set, so a sentence is checked in time linear in its own length.
    """
    parts: list[str] = []
    used = 0
    nodes = 0
    stack: list[Any] = []
    _push(stack, sources, _ECHO_NODES_MAX)
    while stack and used < _ECHO_TEXT_MAX and nodes < _ECHO_NODES_MAX:
        item = stack.pop()
        nodes += 1
        room = _ECHO_NODES_MAX - nodes - len(stack)
        if isinstance(item, (bytes, bytearray)):
            item = bytes(item[:_ECHO_STRING_MAX]).decode("utf-8", "replace")
        if isinstance(item, str):
            if len(item) >= _ECHO_RUN:
                piece = item[:_ECHO_STRING_MAX]
                parts.append(piece)
                used += len(piece)
        elif isinstance(item, Mapping):
            _push(stack, itertools.chain(item.keys(), item.values()), room)
        elif isinstance(item, (list, tuple, set, frozenset)):
            _push(stack, item, room)
    text = _fold("\x00".join(parts)[:_ECHO_TEXT_MAX])
    return frozenset(
        text[start : start + _ECHO_RUN] for start in range(len(text) - _ECHO_RUN + 1)
    )


def _cut_echo(text: str, runs: frozenset[str]) -> str:
    """Replace every stretch of ``text`` that repeats the request's own text."""
    if not runs or len(text) < _ECHO_RUN:
        return text
    folded = _fold(text)
    covered = bytearray(len(text))
    for start in range(len(folded) - _ECHO_RUN + 1):
        if folded[start : start + _ECHO_RUN] in runs:
            covered[start : start + _ECHO_RUN] = b"\x01" * _ECHO_RUN
    if not any(covered):
        return text
    out: list[str] = []
    inside = False
    for ch, mark in zip(text, covered):
        if mark:
            if not inside:
                out.append("[REDACTED]")
            inside = True
        else:
            out.append(ch)
            inside = False
    return "".join(out)


# Error types whose sentence is built from the schema alone (``Input should be
# 'a' or 'b'``), checked against pydantic's own messages.
_SCHEMA_ONLY_SENTENCES = frozenset({"literal_error", "enum"})


def _public_message(
    err: Mapping[str, Any], caller: frozenset[str] = frozenset()
) -> str:
    """Pydantic sentences that quote the rejected value are replaced by the rule.

    A UUID parse error names the offending character and a discriminated-union
    error the whole tag; the error type already says what was wrong, and the
    union's tag list is the schema's own, so it stays. So does the list of a
    ``Literal`` or an enum: it is the schema's and never the input, and the
    cut would otherwise take it out when the caller's typo is one of its
    words (``Detailed`` for ``detailed``), which is when it is wanted.
    """
    kind = err["type"]
    if kind == "uuid_parsing":
        return "Input should be a valid UUID"
    if kind == "union_tag_invalid":
        expected = (err.get("ctx") or {}).get("expected_tags")
        sentence = "Input tag does not match any of the expected tags"
        return f"{sentence}: {expected}" if expected else sentence
    if kind in _SCHEMA_ONLY_SENTENCES:
        return err["msg"][: 2 * _PUBLIC_MSG_MAX]
    text = _scrub(err["msg"], err.get("input"))
    # Cut on a bounded prefix, then clip: a run of the request's text cannot be
    # left at the end by the clip, and the pass stays linear in what is returned.
    return _cut_echo(text[: 2 * _PUBLIC_MSG_MAX], caller)


# Where a request error's ``loc`` starts.
_LOC_SOURCES = frozenset({"body", "query", "path", "header", "cookie"})
# Pydantic's own marker for "the error is in the dict key, not its value".
_LOC_MARKERS = frozenset({"[key]"})
# What stands in a ``loc`` for a name the caller chose.
LOC_KEY_PLACEHOLDER = "<key>"


def _add_alias(out: set[str], alias: Any) -> None:
    """The names a pydantic alias (a string, ``AliasChoices`` or ``AliasPath``) adds."""
    if isinstance(alias, str):
        out.add(alias)
        return
    for choice in getattr(alias, "choices", None) or ():
        _add_alias(out, choice)
    for step in getattr(alias, "path", None) or ():
        if isinstance(step, str):
            out.add(step)


def _collect_schema_names(annotation: Any, out: set[str], seen: set[int]) -> None:
    """Every name a type annotation defines: fields, aliases, literal tags.

    These are the strings the server itself put in the schema, so they are the
    only ones a ``loc`` may repeat; a dict key or an extra field the caller sent
    is not among them.
    """
    if annotation is None or isinstance(annotation, (str, bytes)) or id(annotation) in seen:
        return
    seen.add(id(annotation))
    value = getattr(annotation, "__value__", None)  # ``type X = ...`` aliases
    if value is not None:
        _collect_schema_names(value, out, seen)
        return
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        out.update(arg for arg in typing.get_args(annotation) if isinstance(arg, str))
        out.update(
            arg.value
            for arg in typing.get_args(annotation)
            if isinstance(arg, enum.Enum) and isinstance(arg.value, str)
        )
        return
    if origin is typing.Annotated:
        base, *metadata = typing.get_args(annotation)
        for item in metadata:
            tag = getattr(item, "tag", None)  # pydantic.Tag("cat")
            if isinstance(tag, str):
                out.add(tag)
            _collect_schema_names(getattr(item, "discriminator", None), out, seen)
        _collect_schema_names(base, out, seen)
        return
    if origin is not None or isinstance(annotation, types.UnionType):
        for arg in typing.get_args(annotation):
            _collect_schema_names(arg, out, seen)
        return
    if not isinstance(annotation, type):
        return
    out.add(annotation.__name__)
    if issubclass(annotation, BaseModel):
        for name, field in annotation.model_fields.items():
            out.add(name)
            for alias in (field.alias, field.validation_alias, field.serialization_alias):
                _add_alias(out, alias)
            _collect_schema_names(field.annotation, out, seen)
            for item in field.metadata:
                _collect_schema_names(getattr(item, "discriminator", None), out, seen)
        return
    if dataclasses.is_dataclass(annotation):
        for field in dataclasses.fields(annotation):
            out.add(field.name)
        hints: Mapping[str, Any] = {}
    elif hasattr(annotation, "__annotations__"):  # TypedDict, NamedTuple
        out.update(annotation.__annotations__)
        hints = {}
    else:
        return
    try:
        hints = typing.get_type_hints(annotation, include_extras=True)
    except Exception:  # noqa: BLE001 - an unresolvable forward reference
        hints = {}
    for hint in hints.values():
        _collect_schema_names(hint, out, seen)


def schema_names(*annotations: Any) -> frozenset[str]:
    """The names the given annotations (models, unions, containers) define."""
    out: set[str] = set()
    seen: set[int] = set()
    for annotation in annotations:
        _collect_schema_names(annotation, out, seen)
    return frozenset(out)


def _dependant_names(dependant: Any, out: set[str], seen: set[int]) -> None:
    if dependant is None or id(dependant) in seen:
        return
    seen.add(id(dependant))
    for group in (
        "path_params",
        "query_params",
        "header_params",
        "cookie_params",
        "body_params",
    ):
        for field in getattr(dependant, group, None) or ():
            out.add(field.name)
            alias = getattr(field, "alias", None)
            if isinstance(alias, str):
                out.add(alias)
            info = getattr(field, "field_info", None)
            _collect_schema_names(getattr(info, "annotation", None), out, seen)
    for sub in getattr(dependant, "dependencies", None) or ():
        _dependant_names(sub, out, seen)


# Routes are not hashable, so the cache is keyed by identity; the route is kept
# in the value so its id cannot be reused by another object while it is cached.
_ROUTE_NAMES: dict[int, tuple[Any, frozenset[str]]] = {}


def route_schema_names(route: Any) -> frozenset[str]:
    """Every name the matched route's parameters and body models define.

    Empty when there is no route (nothing is then the server's to repeat).
    """
    if route is None:
        return frozenset()
    cached = _ROUTE_NAMES.get(id(route))
    if cached is not None and cached[0] is route:
        return cached[1]
    out: set[str] = set()
    _dependant_names(getattr(route, "dependant", None), out, set())
    names = frozenset(out)
    _ROUTE_NAMES[id(route)] = (route, names)
    return names


def _public_loc(
    loc: Iterable[Any],
    owned: Set[str],
    rejected: Any,
    *,
    sources: Set[str] = _LOC_SOURCES,
) -> list[Any]:
    """A ``loc`` with every name the caller chose replaced.

    The location of an error in a dict value, an extra field or a union tag
    includes the key the caller sent, and that key can be a credential of any
    shape: pydantic reports it, the rejected value is not the key, so it cannot
    be cut out of the path and no shape scrub is a guarantee. A name is
    therefore kept only if the server's own schema defines it (``owned``); list
    positions and the leading source (``body``, ``query``, ...) are kept.
    """
    out: list[Any] = []
    for position, part in enumerate(loc):
        if not isinstance(part, str):
            out.append(part)
        elif (position == 0 and part in sources) or part in _LOC_MARKERS:
            out.append(part)
        elif part in owned:
            out.append(_clip(_scrub(part, rejected), _PUBLIC_LOC_MAX))
        else:
            out.append(LOC_KEY_PLACEHOLDER)
    return out


def public_path(parts: Iterable[Any], *, owned: Set[str]) -> str:
    """Where in a document the caller supplied a finding is, as ``a.b[0].c``.

    For a finding that is not a pydantic error but names a place by keys the
    caller chose (an unknown key, an entry of a map), so it needs the same rule
    as :func:`validation_error_text`: a key is kept only if ``owned`` defines it
    (the schema's own names, plus any the caller has separately vetted) and
    reads ``<key>`` otherwise. List positions are kept.
    """
    out = ""
    # A document has no request source to lead with, so ``body`` is a key too.
    for part in _public_loc(parts, owned, None, sources=frozenset()):
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def public_validation_errors(
    errors: Iterable[Mapping[str, Any]],
    *,
    owned: Set[str],
    caller: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """A request-validation 422 as its field and rule, not the rejected value.

    FastAPI's default 422 echoes each error's ``input`` (the rejected value, or
    for a length error the whole list) and ``ctx``, which can carry a credential
    or live Worker output and is returned to the caller and to whatever records
    4xx bodies on the way. Same rule as :func:`validation_error_text`.

    ``owned`` is the set of names the server's schema defines for the request
    (:func:`route_schema_names`): a ``loc`` part outside it is a key or field the
    caller chose and is replaced by ``<key>``. ``msg`` is a validator's own
    sentence, kept with the rejected value and credential shapes cut out and then
    every run of ``_ECHO_RUN`` characters that the request itself contains
    (``caller``, from :func:`caller_runs`): that catches a validator that quotes
    a key, the host of a URL, a ``repr()`` or a lower-cased copy of what it was
    given, which the exact cut cannot. A secret a validator transforms (encodes,
    hashes, splits into runs shorter than the cut) still passes. At most
    ``_PUBLIC_ERRORS_MAX`` errors are returned: the cost of a response must not
    grow with how many things a caller got wrong.
    """
    return [
        {
            "type": err["type"],
            "loc": _public_loc(err["loc"], owned, err.get("input")),
            "msg": _clip(_public_message(err, caller), _PUBLIC_MSG_MAX),
        }
        for err in itertools.islice(errors, _PUBLIC_ERRORS_MAX)
    ]


def single_line(text: str) -> str:
    """Escape CR/LF so untrusted text cannot forge log lines.

    Provider exceptions quote response bodies verbatim, and a body the client
    influenced can carry a newline followed by a convincing fake entry.
    """
    return text.replace("\r", "\\r").replace("\n", "\\n")


# The file panel categorizes a 503 by matching this prefix, so every producer
# must spell it identically or the same condition renders a different card.
SANDBOX_UNREACHABLE_PREFIX = "Sandbox is not reachable: "


def sandbox_unreachable_detail(exc: BaseException) -> str:
    """Client-safe 503 detail for an unreachable sandbox.

    Provider exceptions quote request URLs, sandbox ids and SDK response bodies,
    none of which may cross to a client, so the raw text never reaches this
    string and survives only in the server log. Reading "starting" back out of
    the message is a protocol read rather than the message matching this module
    otherwise avoids: the sandbox layer stamps that word deliberately as the
    carrier for the in-flight case, and it is the one distinction the file panel
    renders differently. An SSH computer's connect failure is the exception:
    the machine is its owner's, so the detail says it is theirs that is not
    answering, in the provider's words naming it, rather than suggest the
    service is down. A tunnel that is down is told in its own words, which say
    whose side it is down on.
    """
    failure = connect_failure(exc)
    if failure is not None:
        owner = failure.for_owner
        lead = "" if owner == str(failure) else "your machine is not answering. "
        return f"{SANDBOX_UNREACHABLE_PREFIX}{lead}{owner}"
    if "starting" in str(exc).lower():
        return f"{SANDBOX_UNREACHABLE_PREFIX}the sandbox is still starting"
    return f"{SANDBOX_UNREACHABLE_PREFIX}please retry in a moment"


def sandbox_failure_http(exc: BaseException) -> HTTPException:
    """422 when an owner's SSH machine answered and refused, 503 otherwise.

    A refusal (a key it does not take, a different host key, an address or a
    platform this server does not serve, a root folder the login user cannot
    have) is the owner's to fix, and the provider's sentence says how; a
    machine that did not answer may on retry. One function for every route
    that starts a machine, so a computer and a workspace on it answer alike.
    """
    failure = connect_failure(exc)
    if failure is not None and failure.answered:
        return HTTPException(status_code=422, detail=str(failure))
    return HTTPException(status_code=503, detail=sandbox_unreachable_detail(exc))
