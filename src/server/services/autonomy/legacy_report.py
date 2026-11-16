"""The closing instructions a native Worker got before the completion contract.

A local daemon that does not announce the completion contract on its claim
still parses these marker lines, so the server keeps sending it this wording
during a mixed deploy. Every other path uses ``completion.py``.
"""

from __future__ import annotations

LEGACY_NATIVE_FINISH = """Because this Worker runs a vendor CLI with no autonomy report tool, end your final message with exactly these lines:
AUTONOMY_STATE: ready | blocked | failed | dropped
AUTONOMY_QUESTION: <one concrete question, only when blocked>
Use ready when the work is done and verified, blocked when a person must answer, failed when it cannot be completed, and dropped when no work is needed."""

LEGACY_SPECULATIVE_NATIVE_FINISH = """This is a SPECULATIVE PLAN run. Do not modify files, run mutating commands, or push. Investigate with read-only evidence. If you confirm the issue and find a simple, actionable fix, report a concrete plan for a person to approve; otherwise report dropped and stay quiet. End your final message with exactly these lines:
AUTONOMY_STATE: waiting | dropped
AUTONOMY_QUESTION: <one concrete question, only when blocked>"""

#: The follow-up wording that matches the marker prompt above.
LEGACY_FOLLOWUP_TAIL = "AUTONOMY_STATE report lines as instructed above."
