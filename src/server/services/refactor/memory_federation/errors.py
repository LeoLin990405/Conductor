"""Error codes for the MemFW HTTP contract v2.

Contract section 6 fixes one error shape for every non-2xx response::

    {"contract": "memfw-http/2", "error": {"code": "...", "message": "..."}}

and one table that maps a status onto a code. A client must pass that code
through to its own result instead of collapsing the failure into a generic
exception name, so the table below is the single source of truth for both the
server-side vocabulary and the codes a client produces on its own.

The envelope only counts when it is a v2 one. A body that carries no contract,
or a contract of another major version, is not a MemFW v2 error envelope at
all, and its ``error.code`` is not passed through: a service that does not
speak this contract cannot be allowed to name the client's failure.
"""

from __future__ import annotations

from typing import Any

CONTRACT_NAME = "memfw-http/2"

#: The contract version this client implements.
CONTRACT_MAJOR = 2


def contract_major(contract: Any) -> int | None:
    """The major version of a ``memfw-http/<major>`` contract string.

    ``None`` for anything that is not that shape, including a missing value.

    The contract string comes from the service, so it is not trusted: only its
    shape and its leading integer are read, and nothing else about it is ever
    logged or returned.
    """
    if not isinstance(contract, str):
        return None
    name, _, tail = contract.strip().partition("/")
    if name != "memfw-http":
        return None
    try:
        return int(tail)
    except ValueError:
        return None


def is_contract_envelope(body: Any) -> bool:
    """Whether ``body`` is an envelope of the contract this client speaks.

    Contract section 6: the envelope carries the contract marker. A body
    without one, or with another major version, is not read as a contract
    answer.
    """
    if not isinstance(body, dict):
        return False
    return contract_major(body.get("contract")) == CONTRACT_MAJOR


#: Contract section 6: HTTP status -> error code.
ERROR_CODES_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "too_large",
    500: "internal",
}

#: Codes the client produces locally; the server never sends them.
LOCAL_ERROR_CODES: frozenset[str] = frozenset(
    {
        "timeout",
        "unreachable",
        "invalid_response",
        "write_token_missing",
    }
)

#: Every code a client is allowed to report.
ALL_ERROR_CODES: frozenset[str] = frozenset(ERROR_CODES_BY_STATUS.values()) | LOCAL_ERROR_CODES


def error_code_for_status(status_code: int) -> str:
    """The contract's code for a status; anything unmapped is ``internal``."""
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return "internal"
    return ERROR_CODES_BY_STATUS.get(status, "internal")


def parse_error_code(body: Any, status_code: int) -> str:
    """Read ``error.code`` out of a contract error body.

    Only a **valid v2 envelope** may pass its code through: the body has to
    carry the contract marker of this contract (``memfw-http/2``). A body with
    no contract, with a v1 or a v3 contract, or with a code outside the
    contract's vocabulary falls back to the status-code table. A client must
    never adopt a code from a service that does not speak this contract, and
    it must never invent a code the server did not send.
    """
    if is_contract_envelope(body):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and code.strip():
                candidate = code.strip()
                if candidate in ALL_ERROR_CODES:
                    return candidate
    return error_code_for_status(status_code)


class MemfwContractError(Exception):
    """A MemFW call that failed, carrying the contract error code.

    The server's ``message`` is deliberately **not** carried. It is not needed
    to act on the failure, and an error string that reaches a log line or an
    API response must never be able to smuggle a token or a host path out.
    """

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


__all__ = [
    "ALL_ERROR_CODES",
    "CONTRACT_MAJOR",
    "CONTRACT_NAME",
    "ERROR_CODES_BY_STATUS",
    "LOCAL_ERROR_CODES",
    "MemfwContractError",
    "contract_major",
    "error_code_for_status",
    "is_contract_envelope",
    "parse_error_code",
]
