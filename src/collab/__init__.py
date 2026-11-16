"""Common collaboration protocol: schemas, scopes, envelope, limits, secrets.

This package is deliberately transport-neutral. The HTTP routes, the
``conductor collab`` CLI and the MCP server all validate with ``schemas``,
authorize with ``scopes``, wrap responses with ``envelope``, rate-limit with
``ratelimit`` and refuse credential-shaped payloads with ``payload``.
"""

from src.collab.envelope import (
    UNTRUSTED_FIELDS,
    error_envelope,
    from_error,
    ok_envelope,
    untrusted_fields,
)
from src.collab.payload import (
    SecretFinding,
    assert_no_secrets,
    contains_secret,
    find_secrets,
)
from src.collab.ratelimit import (
    RateLimitDecision,
    RateLimitPolicy,
    RateLimiter,
    TokenBucket,
)
from src.collab.schemas import (
    SCHEMAS,
    TOOL_INPUT_SCHEMAS,
    TOOL_OUTPUT_SCHEMAS,
    check_schemas,
    validate_tool_input,
    validate_tool_output,
)
from src.collab.scopes import (
    DEFAULT_SCOPES,
    SCOPES,
    TOOL_SCOPES,
    normalize_scopes,
    require_scope,
    scope_allows,
)
from src.collab.types import (
    ERROR_CODES,
    AppIdentity,
    CollabError,
    InvalidSchemaError,
    RateLimitedError,
    ScopeDeniedError,
    SecretInPayloadError,
    TaskRef,
    error_for_code,
    line_ref,
    ticket_ref,
)

__all__ = [
    "DEFAULT_SCOPES",
    "ERROR_CODES",
    "SCHEMAS",
    "SCOPES",
    "TOOL_INPUT_SCHEMAS",
    "TOOL_OUTPUT_SCHEMAS",
    "TOOL_SCOPES",
    "UNTRUSTED_FIELDS",
    "AppIdentity",
    "CollabError",
    "InvalidSchemaError",
    "RateLimitDecision",
    "RateLimitPolicy",
    "RateLimitedError",
    "RateLimiter",
    "ScopeDeniedError",
    "SecretFinding",
    "SecretInPayloadError",
    "TaskRef",
    "TokenBucket",
    "assert_no_secrets",
    "check_schemas",
    "contains_secret",
    "error_envelope",
    "error_for_code",
    "find_secrets",
    "from_error",
    "line_ref",
    "normalize_scopes",
    "ok_envelope",
    "require_scope",
    "scope_allows",
    "ticket_ref",
    "untrusted_fields",
    "validate_tool_input",
    "validate_tool_output",
]
