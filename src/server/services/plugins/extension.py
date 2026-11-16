"""The ``ai.conductor`` extension namespace.

``extensions`` is the only extension point Agent Plugins defines — mcp.json is
``additionalProperties: false`` at every level — so this is where a package
says the things the portable document has nowhere to put.

``secrets[]`` declares vault credentials the plugin's servers need: the
standard blueprint fields plus ``bind`` targets that materialize
``${vault:NAME}`` references (behind an optional literal ``prefix``, the
``Bearer `` most vendors want) into the declared servers' env/header maps at
install time. The portable mcp.json is never rewritten — binds land only on
the internal rows we create. ``resolve_binds`` decides where each bind would
land; ``materialize_binds`` writes it.

``servers{}`` describes each entry: how it reads in the UI and the prompt, and
how much of its tool surface the agent sees. ``apply_server_metadata`` copies
it onto the plans. The same key is read from the bundles that ship with the
app (``src.config.runtime.plugins``), which is why the model lives there.

Nothing in this namespace is fatal. The block is our own invention, optional
by construction, and every defect in it is an authoring slip in a field the
spec does not even define — while the cost of refusing was the entire package,
every other server and every skill in it, over one typo. So a bind that cannot
land is dropped with a diagnostic and the install continues, which is the same
isolation §7.1 already gives a defective entry. The block's own shape degrades
the same way: an unknown key, or an entry that does not validate, is dropped
alone (``parse_extension``).

Every diagnostic this namespace produces is read by the person who installed
the package and stored with the install, and what it names comes from a
package nobody vetted: a map key or a rejected value can be a credential of any
shape. So a diagnostic names a place by the namespace's own field names, the
place the package wrote a list entry at, and the servers the package's own
``mcp.json`` declares under a legal name (the install lists those entries
anyway); any other key reads ``<key>`` and a validator's sentence is cut of the
package's own text. The same rules answer a request's 422
(``error_sanitization``).

A place is the one in the package, counted before the entries that do not
validate are dropped, in every diagnostic: the parse says where it dropped an
entry, and ``resolve_binds``, which runs on what is left, says where it could
not place one (``_Written``). What a report costs is meant to grow with the
findings in it and no faster: a package can be large and every part of it wrong,
so what a finding needs is found once for the package (where its namespaces are,
the headers an entry ships, the names a report may repeat) rather than searched
for or rebuilt per finding.
"""

from collections.abc import (
    Callable,
    Collection,
    Container,
    Iterable,
    Iterator,
    Sequence,
)
from dataclasses import dataclass
from typing import Any, TypeVar, get_args, get_origin

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from src.config.runtime.core import VaultBlueprint
from src.config.runtime.plugins import NAMESPACE, ServerMeta
from src.server.models.mcp_server import DESCRIPTION_MAX, INSTRUCTION_MAX
from src.server.models.plugin import Diagnostic
from src.server.services.plugins.mcp import McpEntryPlan
from src.server.utils.error_sanitization import (
    public_path,
    schema_names,
    validation_error_text,
)

__all__ = [
    "NAMESPACE",
    "ConductorExtension",
    "PluginSecret",
    "SecretBind",
    "ServerMeta",
    "apply_server_metadata",
    "declared_server_names",
    "materialize_binds",
    "namespace_path",
    "namespace_path_for",
    "parse_extension",
    "prefix_problem",
    "resolve_binds",
]

PREFIX_MAX = 64


def prefix_problem(prefix: str) -> str | None:
    """Why ``prefix`` cannot sit in front of a secret, or None when it can.

    The prefix is written verbatim ahead of a resolved credential, usually in
    an HTTP header, so a control character in it is header injection rather
    than a typo, and printable ASCII is all a header value may carry. ``$`` is
    refused outright: the value must hold one reference and nothing shaped
    like another, and a ``$VAR`` in front of it fails the row's own validation,
    losing the whole entry over its bind.
    """
    if not 1 <= len(prefix) <= PREFIX_MAX:
        return f"prefix must be 1-{PREFIX_MAX} characters"
    if any(not " " <= c <= "~" for c in prefix):
        return "prefix must be printable ASCII"
    if "$" in prefix:
        return "prefix must not contain '$'"
    return None


class _Written(BaseModel):
    """A list entry that remembers where in its list the package wrote it.

    Parsing drops an entry that does not validate, so the list that is left
    says nothing about the places in the package: the entry that is now first
    may have been written third, and a finding about it later (``resolve_binds``
    runs on the parsed block) must still say third. Set when the block is
    parsed; an entry built any other way has no source, and its place is its
    place in the list it is in.
    """

    _source_index: int | None = PrivateAttr(default=None)

    @property
    def source_index(self) -> int | None:
        return self._source_index


_W = TypeVar("_W", bound=_Written)


def _written_at(entry: _W, index: int) -> _W:
    entry._source_index = index
    return entry


def _source_places(entries: Sequence[_W]) -> Iterator[tuple[int, _W]]:
    """``entries`` with the place the package wrote each one at."""
    for position, entry in enumerate(entries):
        source = entry.source_index
        yield position if source is None else source, entry


class SecretBind(_Written):
    """One place a declared secret is injected: a server's env or header."""

    server: str  # the mcp.json entry key, not the coerced row name
    env: str | None = None
    header: str | None = None
    # Literal text ahead of the secret (``"Bearer "``). Without it a package
    # had to inline ``Bearer ${vault:X}`` in its portable mcp.json, which no
    # other host resolves and which declares nothing for the wizard to ask for.
    prefix: str | None = None

    model_config = {"extra": "forbid"}

    @field_validator("prefix")
    @classmethod
    def _safe_prefix(cls, value: str | None) -> str | None:
        if value is not None and (problem := prefix_problem(value)):
            raise ValueError(problem)
        return value

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "SecretBind":
        if bool(self.env) == bool(self.header):
            raise ValueError("bind must set exactly one of env or header")
        return self


class PluginSecret(VaultBlueprint, _Written):
    """A blueprint plus where it binds. Bindless declarations are legal —
    they surface in the vault UI without touching any server."""

    bind: list[SecretBind] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class ConductorExtension(BaseModel):
    """One model for the whole namespace, whichever kind of package carries it.

    Every key is optional and some are read on only one path — ``secrets`` is
    an upload's. They are still declared here, so a package borrowing our own
    bundles' manifest shape is not warned about keys it was right to carry.
    """

    secrets: list[PluginSecret] = Field(default_factory=list)
    servers: dict[str, ServerMeta] = Field(default_factory=dict)
    #: Accepted and ignored, in the older list form or the dict form that
    #: carried per-skill icons: a package's skills are the directories it
    #: carries, never a claim its manifest makes.
    skills: list[str] | dict[str, Any] = Field(default_factory=dict)
    #: The plugin's mark when it wears someone else's brand: a site, or the
    #: https address of the image itself.
    icon: str | None = None

    model_config = {"extra": "forbid"}


# What a diagnostic may call a part of the namespace: the names the model
# defines. Any other key a package puts in it is the package's own text.
_OWNED = schema_names(ConductorExtension)

# A place in the namespace: field names, list positions and map keys.
_At = Sequence[str | int]


def declared_server_names(plans: Iterable[McpEntryPlan]) -> frozenset[str]:
    """The ``mcp.json`` keys a diagnostic may repeat: the legal server names.

    A key is the package's own text, but one that is a legal name as written
    (``NAME_RE``, 64 characters at most, not renamed to fit) is an identifier
    the install keeps and lists, so it is the user's own. Any other, which
    ``coerce_mcp_name`` rewrote or refused, can be any text of any length.
    """
    return frozenset(p.key for p in plans if p.name == p.key)


def namespace_path(
    parts: _At = (), *, declared_servers: Collection[str] = ()
) -> str:
    """``extensions.ai.conductor.<parts>``, safe to put in a report.

    A key the package chose reads ``<key>`` unless it is one of the namespace's
    own field names or a server ``mcp.json`` declares under a legal name
    (``declared_servers``, from :func:`declared_server_names`).
    """
    return namespace_path_for(declared_servers)(parts)


def namespace_path_for(
    declared_servers: Collection[str] = (),
) -> Callable[[_At], str]:
    """:func:`namespace_path` for one package, with the names it may repeat
    settled once rather than for each of the places it names."""
    owned = _OWNED | frozenset(declared_servers)

    def path(parts: _At = ()) -> str:
        tail = public_path(parts, owned=owned)
        return f"extensions.{NAMESPACE}{'.' + tail if tail else ''}"

    return path


def _diag(code: str, target: str, message: str, *, at: str = "") -> Diagnostic:
    return Diagnostic(
        level="warning",
        scope="entry" if target else "plugin",
        target=target,
        code=code,
        message=f"extensions.{NAMESPACE}{'.' + at if at else ''}: {message}",
    )


@dataclass(frozen=True, slots=True)
class _Reader:
    """One parse of the namespace: the names it may repeat and where it reports."""

    owned: frozenset[str]
    diagnostics: list[Diagnostic] | None

    def report(self, code: str, at: _At, message: str) -> None:
        if self.diagnostics is not None:
            self.diagnostics.append(
                _diag(code, "", message, at=public_path(at, owned=self.owned))
            )

    def invalid(self, at: _At, error: ValidationError, *, under: int = 0) -> None:
        """Report an entry that did not validate: pydantic's findings on one
        line, each after the field it is about, less the ``under`` leading
        parts ``at`` already names.

        The sentence is built only when something reads it: indexing the
        rejected text to cut it costs more than the finding, and a stored
        manifest is parsed again, with no report, on requests the package's
        owner can repeat.
        """
        if self.diagnostics is not None:
            reasons = validation_error_text(error, owned=self.owned, under=under)
            self.report("extension_invalid", at, f"{reasons}; ignored")


def parse_extension(
    payload: Any,
    diagnostics: list[Diagnostic] | None = None,
    *,
    declared_servers: Collection[str] = (),
) -> ConductorExtension:
    """Parse the namespace payload (None = absent), keeping every part that parses.

    A key this build does not read is dropped, and so is a list or map entry
    that does not validate (one secret, one bind, one server block), each with
    a warning naming it, while the rest of the block parses. A newer package's
    next contract field then costs an older build that field, not every
    secret, bind and server instruction beside it. Only a payload that is not
    an object is lost whole.

    ``declared_servers`` are the ``mcp.json`` entry keys of the same package.
    A warning names a ``servers`` entry only when it is one of them
    (``declared_server_names``): the key is otherwise text the package chose
    and reads ``<key>``.
    """
    if payload is None:
        return ConductorExtension()

    reader = _Reader(_OWNED | frozenset(declared_servers), diagnostics)
    if not isinstance(payload, dict):
        reader.report("extension_invalid", (), "is not an object; ignored")
        return ConductorExtension()
    fields: dict[str, Any] = {}
    for key, value in _known_keys(ConductorExtension, payload, (), reader).items():
        # Every top-level key has a default and none depends on another, so a
        # bad one is checked, and dropped, on its own.
        try:
            ConductorExtension.model_validate({key: value})
        except ValidationError as e:
            reader.invalid((key,), e, under=1)
            continue
        fields[key] = value
    return ConductorExtension.model_validate(fields)


def _known_keys(
    model: type[BaseModel], data: dict[str, Any], at: _At, reader: _Reader
) -> dict[str, Any]:
    """``data`` without the keys ``model`` does not declare, and with each
    nested list or map holding only the entries that validate."""
    kept: dict[str, Any] = {}
    for key, value in data.items():
        where = (*at, key)
        field = model.model_fields.get(key)
        if field is None:
            reader.report("extension_key_unknown", where, "unknown key; ignored")
            continue
        nested = _entry_model(field.annotation)
        if nested is not None and isinstance(value, list):
            value = [
                _written_at(parsed, i) if isinstance(parsed, _Written) else parsed
                for i, item in enumerate(value)
                if (parsed := _entry(nested, item, (*where, i), reader)) is not None
            ]
        elif nested is not None and isinstance(value, dict):
            value = {
                name: parsed
                for name, item in value.items()
                if (parsed := _entry(nested, item, (*where, name), reader))
                is not None
            }
        kept[key] = value
    return kept


def _entry_model(annotation: Any) -> type[BaseModel] | None:
    """``M`` for a ``list[M]`` or ``dict[str, M]`` field of models, else None."""
    if get_origin(annotation) not in (list, dict):
        return None
    entry = get_args(annotation)[-1]
    return entry if isinstance(entry, type) and issubclass(entry, BaseModel) else None


def _entry(
    model: type[BaseModel], item: Any, at: _At, reader: _Reader
) -> BaseModel | None:
    """One list or map entry, or None (reported) when it does not validate."""
    if not isinstance(item, dict):
        reader.report("extension_invalid", at, "is not an object; ignored")
        return None
    try:
        return model.model_validate(_known_keys(model, item, at, reader))
    except ValidationError as e:
        reader.invalid(at, e)
        return None


@dataclass(frozen=True, slots=True)
class ResolvedBind:
    """One validated bind: which secret lands in which entry's which field."""

    secret_name: str
    plan: McpEntryPlan
    section: str  # "env" or "headers"
    field: str
    prefix: str = ""


def resolve_binds(
    extension: ConductorExtension,
    plans: list[McpEntryPlan],
    *,
    document_dropped: bool = False,
    diagnostics: list[Diagnostic] | None = None,
) -> list[ResolvedBind]:
    """Say where each usable bind would land. Writes nothing.

    A bind must name an entry in this plugin, match its transport (env for
    stdio, header for remotes), and not overwrite a value the package ships.
    One that fails any of those is dropped, and the entry is checked without
    it, so a server that needed the key fails its check rather than landing
    without it. sse entries are legal targets — their
    binds materialize when the upgrade probe installs them, from the same
    stored manifests.

    ``document_dropped`` says mcp.json failed at document level, so there are
    no plans to name and every bind is unresolvable for a reason that is not
    the bind's — reporting each one would bury the diagnostic that explains
    the drop.

    ``diagnostics`` is written only by the validation pass. ``materialize_binds``
    re-runs this against the same manifests after the row exists, and a second
    copy of each finding in the install report would say nothing new.
    """
    by_key = {p.key: p for p in plans}
    resolved: list[ResolvedBind] = []
    # What each entry ships, in lower case, read when its first bind needs it:
    # every bind is checked against all of them, so reading them again for each
    # bind makes the cost the product of the two counts.
    shipped_headers: dict[str, frozenset[str]] = {}

    # A secret's name was validated when the block parses, so it may be named.
    # Anything else a bind says (the server, the env variable or header it
    # would land on) is the package's own text and is not repeated: the bind's
    # position says which one it was, and the entry it points at, when there is
    # one, is the finding's target like any other finding about that entry.
    def _drop(
        secret_at: int, bind_at: int, secret_name: str,
        server: str | None, message: str,
    ) -> None:
        if diagnostics is not None:
            diagnostics.append(
                _diag(
                    "bind_unusable",
                    server or "",
                    f"secret {secret_name!r} {message}; it was not bound",
                    at=public_path(
                        ("secrets", secret_at, "bind", bind_at), owned=_OWNED
                    ),
                )
            )

    # Places are the package's (``_Written``): the lists are what is left once
    # the entries that did not validate were dropped.
    for i, secret in _source_places(extension.secrets):
        for j, bind in _source_places(secret.bind):
            plan = by_key.get(bind.server)
            if plan is None:
                if not document_dropped:
                    _drop(
                        i, j, secret.name, None,
                        "binds to an unknown server (none of the package's "
                        "mcp.json entries has that key)",
                    )
                continue
            if plan.skip_code is not None:
                # The entry was already dropped (schema/policy) and said so;
                # its bind has nothing to land on.
                continue
            if bind.env is not None:
                if plan.transport != "stdio":
                    _drop(
                        i, j, secret.name, bind.server,
                        "env-binds to a remote server (use header)",
                    )
                    continue
                if bind.env in (plan.config.get("env") or {}):
                    _drop(
                        i, j, secret.name, bind.server,
                        "binds over an env variable the package ships",
                    )
                    continue
                resolved.append(
                    ResolvedBind(
                        secret.name, plan, "env", bind.env, bind.prefix or ""
                    )
                )
            else:
                if plan.transport not in ("http", "sse"):
                    _drop(
                        i, j, secret.name, bind.server,
                        "header-binds to a stdio server (use env)",
                    )
                    continue
                shipped = shipped_headers.get(plan.key)
                if shipped is None:
                    shipped = shipped_headers[plan.key] = frozenset(
                        k.lower() for k in plan.config.get("headers") or {}
                    )
                if bind.header.lower() in shipped:
                    _drop(
                        i, j, secret.name, bind.server,
                        "binds over a header the package ships",
                    )
                    continue
                resolved.append(
                    ResolvedBind(
                        secret.name, plan, "headers", bind.header,
                        bind.prefix or "",
                    )
                )
    return resolved


def materialize_binds(
    extension: ConductorExtension,
    plans: list[McpEntryPlan],
    granted: Container[str],
    *,
    document_dropped: bool = False,
) -> None:
    """Write ``${vault:NAME}``, behind its prefix, into each granted bind's
    target field.

    A name outside ``granted`` is simply not written, so the entry is checked
    and saved without that credential.

    ``document_dropped`` is passed through rather than assumed from an empty
    ``plans``, because an empty plan list is also what a plugin with no MCP
    component at all looks like.
    """
    for bind in resolve_binds(
        extension, plans, document_dropped=document_dropped
    ):
        if bind.secret_name not in granted:
            continue
        target = bind.plan.config.setdefault(bind.section, {})
        target[bind.field] = f"{bind.prefix}${{vault:{bind.secret_name}}}"


def apply_server_metadata(
    extension: ConductorExtension,
    plans: list[McpEntryPlan],
    *,
    document_dropped: bool = False,
    diagnostics: list[Diagnostic] | None = None,
) -> None:
    """Copy each entry's declared description, instruction and exposure mode
    onto its plan, so the installed row reads the way the package intended
    rather than falling to our defaults.

    Text past the row's limit is clipped rather than refused: these fields
    decide how a server introduces itself, and losing the server over the
    length of its own description would be the wrong trade. Blueprints are not
    copied — the request model rejects them on a user server, and ``secrets[]``
    is how a plugin declares a credential it wants the user to hold.
    """
    by_key = {p.key: p for p in plans}
    unknown = 0
    for key, meta in extension.servers.items():
        plan = by_key.get(key)
        if plan is None:
            unknown += 1
            continue
        if not plan.installable:
            continue
        for field, cap in (
            ("description", DESCRIPTION_MAX),
            ("instruction", INSTRUCTION_MAX),
        ):
            value = getattr(meta, field)
            if not value:
                continue
            if len(value) > cap:
                value = value[:cap]
                if diagnostics is not None:
                    diagnostics.append(
                        _diag(
                            "server_meta_clipped", key,
                            f"{field} is longer than {cap} characters and was "
                            "clipped",
                        )
                    )
            plan.config[field] = value
        if meta.tool_exposure_mode is not None:
            plan.config["tool_exposure_mode"] = meta.tool_exposure_mode
    if unknown and diagnostics is not None and not document_dropped:
        # One finding with a count: the keys are the package's own text and are
        # not repeated, so a line per block would read the same each time.
        which = (
            "an unknown server (none of the package's mcp.json entries has "
            "that key)"
            if unknown == 1
            else f"{unknown} unknown servers (none of the package's mcp.json "
            "entries has those keys)"
        )
        diagnostics.append(_diag("server_meta_unknown", "", f"describes {which}"))
