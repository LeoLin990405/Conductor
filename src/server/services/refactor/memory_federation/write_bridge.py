"""Stage approved Conductor memories into MemFW's candidate queue.

This bridge never writes the MemFW authority. It posts to the read-only
service's ``/stage`` endpoint, which writes ``_candidates/`` only. A human still
has to confirm the candidate with MemFW.

Contract v2 (``memfw-http/2``) shapes this module in three ways:

* ``/stage`` takes the **write** credential only (contract section 2). The read
  credential is a different secret and the server refuses it here, so there is
  no fallback: without a write token nothing is sent at all.
* Sizes are checked **before** the request (contract section 4): ``content``
  may not exceed 64 KiB of UTF-8 bytes and the JSON body may not exceed
  128 KiB. Content is never shortened to fit -- a shortened candidate would be
  a different memory than the one a human approved.
* The proposal id is checked as a whole string (contract section 5): it has to
  match ``[A-Za-z0-9_-]{1,128}`` exactly. It is never trimmed and never
  shortened, because an id that was tidied up would name a different
  candidate than the one that was approved.
* Failures carry the contract's error code (contract section 6) instead of the
  name of an exception, so a caller can tell a conflict from an unreachable
  peer without parsing a message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

import httpx

from .errors import parse_error_code
from .health import CONTRACT_MAJOR, _validated_base, contract_major, safe_endpoint
from .remote import resolve_memfw_token

logger = logging.getLogger(__name__)

#: Contract section 4: the whole ``/stage`` request body may not exceed 128 KiB.
_MAX_BODY_BYTES = 131072
#: Contract section 4: ``content`` may not exceed 64 KiB of UTF-8 bytes.
_MAX_CONTENT_BYTES = 65536
#: Contract section 5: the title may not exceed 200 characters.
_MAX_TITLE = 200
#: Contract section 5: the proposal id shape, ``[A-Za-z0-9_-]{1,128}``. The
#: pattern is matched against the **whole** string, so an id is accepted as it
#: is or refused: it is never trimmed and never cut.
_PROPOSAL_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
#: The one fixed path the stage request targets. The request line is rebuilt
#: from the validated base plus this path, so the configured string -- with
#: whatever path, query or fragment it carried -- never reaches the client or a
#: transport that would log it.
_STAGE_PATH = "/stage"
#: Contract section 4: ``/stage`` gets three seconds.
_STAGE_TIMEOUT_SECONDS = 3.0


def _enabled() -> bool:
    return os.environ.get("MEMFW_WRITE_BRIDGE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _token() -> str:
    """The write credential, and nothing else.

    Contract section 2: ``/stage`` accepts the write token only. The read token
    is a separate secret that the server refuses on this endpoint, so it is
    never used as a stand-in.
    """
    return resolve_memfw_token("WRITE")


def _encode_json(payload: Mapping[str, Any]) -> bytes:
    """The bytes that go on the wire for this payload.

    The body limit is a limit on the request the server receives, so the size
    is measured with the same serialization the HTTP client uses (httpx's
    ``encode_json``: compact separators, no ASCII escaping, UTF-8).
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _body_json(response: httpx.Response) -> Any:
    """The response body as JSON, or ``None`` when it is not JSON."""
    try:
        return response.json()
    except Exception:
        return None


async def stage_approved_proposal(
    proposal: Mapping[str, Any],
    *,
    content: str,
    endpoint: str,
    sensitivity: str | None = None,
    tags: list[str] | None = None,
    tenant: str = "",
    source: str = "conductor",
) -> dict[str, Any]:
    """Stage one approved memory as a MemFW candidate. Never authoritative."""
    if not _enabled():
        return {"enabled": False, "staged": False}
    if isinstance(sensitivity, str) and sensitivity.strip().casefold() == "secret":
        return {"enabled": True, "staged": False, "error_code": "secret_forbidden"}
    # The approval hook supplies the current user's URL binding. A process-wide
    # MEMFW_WRITE_URL is deliberately not consulted: no binding, no egress.
    raw_url = endpoint.strip()
    if not raw_url:
        return {"enabled": True, "staged": False, "error_code": "bad_config"}
    # The configured URL is validated before anything is sent, through the same
    # boundary the health probe and the remote client use. One that is not a
    # root address -- a path of its own, a userinfo, a query or a fragment --
    # cannot be reduced to a bare peer, so it is refused as a bad configuration
    # and nothing is sent at all. Otherwise the request line is rebuilt from the
    # validated base plus the one fixed stage path, so the raw configured string
    # never reaches the client or a transport that would log it.
    base = _validated_base(raw_url)
    if base is None:
        logger.debug(
            "[refactor] MemFW candidate staging refused: the configured URL is "
            "not a root address -- it carries a userinfo, a query, a fragment "
            "or a path (%s)",
            safe_endpoint(raw_url),
        )
        return {"enabled": True, "staged": False, "error_code": "bad_config"}
    token = _token()
    if not token:
        # Without the write credential the request is never sent: the read
        # credential is not a substitute, and asking the server would only
        # produce a rejection.
        return {"enabled": True, "staged": False, "error_code": "write_token_missing"}
    proposal_id = proposal.get("id")
    if proposal_id is None:
        proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID_PATTERN.fullmatch(
        proposal_id
    ):
        # Contract section 5: the id is validated as a whole string, never
        # trimmed and never shortened. An id with a space, a tab or a newline
        # around it names a different candidate, and the server would write a
        # file under that other name. The request is not sent at all.
        return {"enabled": True, "staged": False, "error_code": "bad_request"}
    text = str(content or "")
    content_bytes = len(text.encode("utf-8"))
    if content_bytes > _MAX_CONTENT_BYTES:
        logger.warning(
            "[refactor] MemFW candidate staging refused: content is %d bytes, "
            "the limit is %d bytes",
            content_bytes,
            _MAX_CONTENT_BYTES,
        )
        return {"enabled": True, "staged": False, "error_code": "too_large"}
    title = str(proposal.get("title") or "memory").strip()[:_MAX_TITLE] or "memory"
    payload = {
        "proposal_id": proposal_id,
        "title": title,
        "content": text,
        "tags": [str(tag)[:80] for tag in (tags or [])][:20],
        "tenant": str(tenant)[:120],
        "source": source[:120],
    }
    body_bytes = _encode_json(payload)
    if len(body_bytes) > _MAX_BODY_BYTES:
        logger.warning(
            "[refactor] MemFW candidate staging refused: the request body is %d "
            "bytes, the limit is %d bytes",
            len(body_bytes),
            _MAX_BODY_BYTES,
        )
        return {"enabled": True, "staged": False, "error_code": "too_large"}
    stage_url = base + _STAGE_PATH

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=_STAGE_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            return await client.post(
                stage_url,
                json=payload,
                headers={"X-Memfw-Token": token},
            )

    try:
        response = await asyncio.wait_for(
            request(), timeout=_STAGE_TIMEOUT_SECONDS
        )
    except (httpx.TimeoutException, asyncio.TimeoutError):
        logger.warning(
            "[refactor] MemFW candidate staging timed out after %.1fs against %s",
            _STAGE_TIMEOUT_SECONDS,
            base,
        )
        return {"enabled": True, "staged": False, "error_code": "timeout"}
    except Exception as exc:  # noqa: BLE001 - approval must not be rolled back by bridge
        logger.warning(
            "[refactor] MemFW candidate staging failed against %s: %s",
            base,
            type(exc).__name__,
        )
        return {"enabled": True, "staged": False, "error_code": "unreachable"}
    if not 200 <= response.status_code < 300:
        error_code = parse_error_code(_body_json(response), response.status_code)
        logger.warning(
            "[refactor] MemFW candidate staging rejected with %s", error_code
        )
        return {"enabled": True, "staged": False, "error_code": error_code}
    body_json = _body_json(response)
    if not isinstance(body_json, dict):
        logger.warning("[refactor] MemFW candidate staging returned an unusable body")
        return {"enabled": True, "staged": False, "error_code": "invalid_response"}
    # Contract section 5: the success body carries the contract marker, so a
    # body from another version of the service is not read as a candidate.
    if contract_major(body_json.get("contract")) != CONTRACT_MAJOR:
        logger.warning(
            "[refactor] MemFW candidate staging returned an unexpected contract version"
        )
        return {"enabled": True, "staged": False, "error_code": "invalid_response"}
    if not body_json.get("staged"):
        logger.warning("[refactor] MemFW candidate staging returned an unusable body")
        return {"enabled": True, "staged": False, "error_code": "invalid_response"}
    return {
        "enabled": True,
        "staged": True,
        "duplicate": bool(body_json.get("duplicate")),
        "candidate_id": str(body_json.get("candidate_id") or ""),
        "path": str(body_json.get("path") or ""),
        "authority": False,
    }
