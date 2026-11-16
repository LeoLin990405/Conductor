"""The shared error vocabulary of the MemFW HTTP contract v2.

Contract section 6 fixes one error envelope and one status table. The parser
that reads the envelope is the shared one: ``remote.py`` and ``write_bridge.py``
both use it, so a rule that only one of them applied would be a rule that one
of them could break.
"""

from __future__ import annotations

import pytest

from src.server.services.refactor.memory_federation import errors
from src.server.services.refactor.memory_federation import health

#: The contract markers that are *not* this contract's own.
_FOREIGN_ENVELOPES = [
    pytest.param({}, id="contract-missing"),
    pytest.param({"contract": "memfw-http/1"}, id="contract-v1"),
    pytest.param({"contract": "memfw-http/3"}, id="contract-v3"),
]


def _status_codes() -> list:
    return [
        pytest.param(401, "unauthorized", id="401"),
        pytest.param(403, "forbidden", id="403"),
        pytest.param(404, "not_found", id="404"),
        pytest.param(409, "conflict", id="409"),
        pytest.param(500, "internal", id="500"),
    ]


@pytest.mark.parametrize("envelope", _FOREIGN_ENVELOPES)
@pytest.mark.parametrize(("status", "expected"), _status_codes())
def test_a_foreign_envelope_falls_back_to_the_status_table(envelope, status, expected):
    """Only a v2 envelope may pass its code through.

    A body with no contract, or with a v1 or a v3 contract, is not a MemFW v2
    error envelope at all. Its ``error.code`` is not adopted -- the status
    table decides, so a 401 reads as ``unauthorized`` even when the body
    claims something else.
    """
    body = {**envelope, "error": {"code": "conflict", "message": "not a v2 envelope"}}

    assert errors.parse_error_code(body, status) == expected


@pytest.mark.parametrize("envelope", _FOREIGN_ENVELOPES)
def test_a_foreign_envelope_is_not_read_as_a_contract_envelope(envelope):
    assert errors.is_contract_envelope(envelope) is False
    assert errors.is_contract_envelope({"contract": "memfw-http/2"}) is True


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (413, "too_large"),
        (500, "internal"),
    ],
)
def test_a_v2_envelope_passes_its_code_through(status, code):
    body = {"contract": "memfw-http/2", "error": {"code": code, "message": "..."}}

    assert errors.parse_error_code(body, status) == code


def test_a_v2_envelope_with_a_code_outside_the_vocabulary_falls_back():
    body = {"contract": "memfw-http/2", "error": {"code": "made_up"}}

    assert errors.parse_error_code(body, 409) == "conflict"


@pytest.mark.parametrize(
    "body",
    [
        None,
        "<html>proxy error</html>",
        {"error": "not a contract body"},
        {"contract": "memfw-http/2"},
        {"contract": "memfw-http/2", "error": {"code": ""}},
        {"contract": "memfw-http/2", "error": {"code": 7}},
    ],
)
def test_a_body_that_is_not_a_v2_envelope_falls_back_to_the_status_table(body):
    assert errors.parse_error_code(body, 404) == "not_found"


def test_the_contract_major_is_shared_with_the_health_module():
    """The health module re-exports the contract helpers it used to own.

    The version of the contract is one fact; two copies of it would drift.
    """
    assert health.CONTRACT_MAJOR is errors.CONTRACT_MAJOR == 2
    assert health.contract_major is errors.contract_major
    assert health.contract_major("memfw-http/2") == 2
    assert health.contract_major("memfw-http/3") == 3
    assert health.contract_major("memfw-http/2.1") is None
    assert health.contract_major(None) is None
