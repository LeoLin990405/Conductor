"""``dispatch status`` lines for N51/N59/N60 records on a line snapshot.

Stdlib only: ``conductor_cli`` may not import ``src.*``. The keys are the
ones ``src.tools.auto_dispatch.review_triage`` and ``completion_evidence``
write: ``review_triage``, ``completion_evidence``, ``worker_claim``.
INCONCLUSIVE (missing evidence) and ERROR (``review_unavailable``) each get
their own line and their own operator hint.
"""

from __future__ import annotations

from typing import Any

REVIEW_INCONCLUSIVE = "review_inconclusive"
REVIEW_UNAVAILABLE = "review_unavailable"


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def review_evidence_lines(record: dict[str, Any]) -> list[str]:
    """What the review checks and the completion evidence recorded."""
    lines: list[str] = []
    triage = record.get("review_triage")
    if isinstance(triage, dict) and triage:
        status = _clip(triage.get("status"), 20) or "—"
        mode = _clip(triage.get("inconclusive_mode"), 10)
        if status == "inconclusive":
            needed = triage.get("needed_evidence")
            items = ", ".join(_clip(item, 60) for item in needed) if isinstance(needed, list) else ""
            acted = "evidence requested" if triage.get("action") == "evidence_request" else (
                f"inconclusive {mode}: acted as BLOCK"
            )
            lines.append(f"    review  INCONCLUSIVE ({acted}); needs: {items or '—'}")
        count = triage.get("downgraded_count")
        if count:
            repro = _clip(triage.get("repro_mode"), 10)
            verb = "downgraded to should-fix" if triage.get("action") == "downgrade" else (
                f"would downgrade (repro {repro})"
            )
            lines.append(f"    review  {count} blocker(s) without file:line/scenario/repro {verb}")
    evidence = record.get("completion_evidence")
    if isinstance(evidence, dict) and evidence:
        head = _clip(evidence.get("head"), 12) or "—"
        if evidence.get("verified"):
            lines.append(f"    evidence  verified on {head}")
        else:
            failed = evidence.get("failed")
            why = ", ".join(_clip(item, 40) for item in failed) if isinstance(failed, list) else ""
            lines.append(f"    evidence  NOT verified on {head}: {why or '—'}")
        mismatch = evidence.get("mismatch")
        if isinstance(mismatch, list) and mismatch:
            lines.append(
                "    evidence  worker claim disagrees: "
                + ", ".join(_clip(item, 50) for item in mismatch)
            )
    return lines


def review_evidence_hints(ident: str, line_id: str, row: dict[str, Any]) -> list[str]:
    """The operator exit for a line parked on INCONCLUSIVE, separate from ERROR."""
    reason = _clip(row.get("failure_reason"), 80)
    status = (_clip(row.get("status"), 40) or "").upper()
    if status != "BLOCKED":
        return []
    if reason == REVIEW_INCONCLUSIVE:
        return [
            f"    → add the evidence named above, then: conductor dispatch unblock {ident} {line_id}"
            "  (review again; not a fix round)"
        ]
    return []
