"""What the user reads about a run that did not complete."""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.server.contracts.status import INTERRUPT_REASON_CREDIT_PAUSE
from src.server.utils.error_sanitization import sanitize_error_text

_ERROR_MAX_CHARS = 500


def clean_error_text(text: str) -> str:
    """Error text fit to persist and send: credential-shaped values scrubbed
    first, so a cut can never leave half a secret behind, then cut."""
    return sanitize_error_text(text)[:_ERROR_MAX_CHARS]


def run_failure_message(run: Dict[str, Any]) -> Optional[str]:
    """The run's own last error, a credit pause's stamped denial, or a line
    naming the status. Relayed verbatim or not at all."""
    if run.get("interrupt_reason") == INTERRUPT_REASON_CREDIT_PAUSE:
        stamped = (run.get("metadata") or {}).get("credit_pause_message")
        return stamped if isinstance(stamped, str) else None
    errors = run.get("errors")
    if errors:
        return clean_error_text(str(errors[-1]))
    return (
        f"workflow run finished as '{run['status']}' "
        f"(run_id={run['conversation_response_id']})"
    )
