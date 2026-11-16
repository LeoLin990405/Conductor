"""plugin.json validation — the spec's failure ladder, implemented literally.

Errors are classified by where jsonschema reports them: only a root-level
``additionalProperties`` violation (unknown top-level key) and a non-object
``extensions`` value are non-fatal warnings (spec §5.2); every other schema
violation on plugin.json is fatal and nothing is written.
"""

from json import dumps, loads
from typing import Any

from jsonschema.validators import validator_for
from referencing import Registry

from src.config.runtime.plugins import NAMESPACE
from src.server.models.plugin import Diagnostic
from src.server.services.plugins.errors import PluginFatal
from src.server.services.plugins.schemas import (
    PLUGIN_SCHEMA,
    SCHEMA_URL_RE,
    SUPPORTED_SCHEMA_VERSIONS,
    describe_schema_error,
)
from src.server.utils.error_sanitization import public_path

SPEC_URL = "https://agent-plugins.org/specification"

_NAME_RULE = (
    "is not a usable plugin name: use lowercase letters, digits, dots and "
    "hyphens, start and end with a letter or digit, and never repeat '-' "
    "or '.'"
)

# The names a finding about ``extensions`` may repeat: ``extensions`` itself and
# our own namespace; any other is a key the package chose.
_EXTENSIONS_OWNED = frozenset({"extensions", NAMESPACE})

# Empty registry: no $ref can resolve outside the vendored document, so the
# network-retrieval path is unreachable by construction.
_NO_REMOTE_REFS = Registry()

_validator_cls = validator_for(PLUGIN_SCHEMA)
_VALIDATOR = _validator_cls(PLUGIN_SCHEMA, registry=_NO_REMOTE_REFS)


def check_schema_version(value: Any, *, kind: str) -> str | None:
    """Return the declared spec version when it's a canonical-but-unsupported
    URL — the one case that deserves a better message than a const mismatch."""
    if not isinstance(value, str):
        return None
    match = SCHEMA_URL_RE.match(value)
    if match and match.group(2) == kind:
        version = match.group(1)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            return version
    return None


def validate_manifest(raw: bytes) -> tuple[dict[str, Any], list[Diagnostic]]:
    """Validate plugin.json; return the verbatim document plus warnings.

    Raises PluginFatal on any violation past the two tolerated warns.
    """
    try:
        doc = loads(raw.decode("utf-8"))
        # A ``\ud800`` escape decodes to a lone surrogate, which neither a
        # JSON response nor the stored manifest can encode.
        dumps(doc, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError) as e:
        raise PluginFatal(f"plugin.json is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise PluginFatal("plugin.json must be a JSON object")

    version = check_schema_version(doc.get("$schema"), kind="plugin")
    if version is not None:
        raise PluginFatal(
            f"plugin.json targets Agent Plugins {version}; this deployment "
            f"supports {', '.join(sorted(SUPPORTED_SCHEMA_VERSIONS))}"
        )

    warnings: list[Diagnostic] = []
    fatal: list[Diagnostic] = []
    # Where each namespace is written, found in one pass when the first
    # finding needs it: a package can carry as many namespaces as its size
    # allows, each of them wrong, so searching for one per finding is a
    # quadratic walk.
    placed: dict[str, int] = {}
    for error in _VALIDATOR.iter_errors(doc):
        path = list(error.absolute_path)
        message = describe_schema_error(error)
        if error.validator == "additionalProperties" and not path:
            # In its own words rather than the validator's. Every dialect puts
            # something here that the canonical schema does not model, so this
            # is the warning nearly every real install shows, and "Additional
            # properties are not allowed" reads like a package that failed.
            # The spec's answer to an unmodelled key is to keep it and carry
            # on, which is precisely what happened, so the sentence says so.
            # The keys are not named: each is text the package chose, and the
            # warning is about the spec, not about any one of them.
            unknown = set(error.instance) - set(error.schema.get("properties", ()))
            many = len(unknown) > 1
            warnings.append(
                Diagnostic(
                    scope="plugin",
                    code="unknown_root_key",
                    message=(
                        f"plugin.json carries {len(unknown)} key"
                        f"{'s' if many else ''} at its root that this version of "
                        f"the spec does not define. {'They are' if many else 'It is'} "
                        "kept exactly as written and otherwise ignored, which is "
                        "what the spec asks for."
                    ),
                    spec_ref=SPEC_URL,
                )
            )
        elif path and path[0] == "extensions" and error.validator == "type":
            # The value is not repeated (jsonschema's sentence quotes it), and a
            # namespace is named only if it is ours: any other is a key the
            # package chose, and can be a credential as easily as a vendor's id.
            where = public_path(path, owned=_EXTENSIONS_OWNED)
            # Two namespaces that are not ours read alike, so say which comes
            # first by its place among the package's own.
            place = ""
            if len(path) > 1 and path[1] != NAMESPACE:
                if not placed:
                    placed = {
                        name: at for at, name in enumerate(doc["extensions"], 1)
                    }
                place = f" (namespace {placed[path[1]]} of {len(placed)})"
            warnings.append(
                Diagnostic(
                    scope="plugin",
                    target=where,
                    code="extensions_invalid",
                    message=f"{where}{place} is not of type {error.validator_value!r}",
                    spec_ref=SPEC_URL,
                )
            )
        else:
            loc = ".".join(str(p) for p in path) or "plugin.json"
            if path == ["name"] and error.validator == "pattern":
                # This one is fatal, so it is the first wall a package author
                # hits, and the schema answers it with a lookahead regex.
                message = f"{error.instance!r} {_NAME_RULE}"
            fatal.append(
                Diagnostic(
                    level="error",
                    scope="plugin",
                    target=loc,
                    code="manifest_invalid",
                    message=f"{loc}: {message}",
                    spec_ref=SPEC_URL,
                )
            )
    if fatal:
        raise PluginFatal(
            "; ".join(d.message for d in fatal),
            diagnostics=fatal + warnings,
        )
    return doc, warnings


def manifest_extension(manifest: dict[str, Any], namespace: str) -> Any:
    """The extension payload for one namespace, or None.

    A non-object ``extensions`` (or member) was already downgraded to a warn —
    downstream must treat it as absent, which this accessor guarantees.
    """
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict):
        return None
    payload = extensions.get(namespace)
    return payload if isinstance(payload, dict) else None
