"""Review isolation (R6 L5, N5): a secondary reviewer sees facts, not conclusions.

The primary reviewer's verdict, reasons and blockers must not reach the
secondary reviewer's prompt, or the second opinion anchors on the first one
(the "orchestration collapse" failure mode). Facts -- the diff, G2 exit codes,
acceptance text, the author's handoff -- are shared; conclusions are not.

Checked channels (2026-11-14):

* ``CrossReviewer.review_line`` builds the prompt from ``branch_evidence`` and
  the machine's ``notes``; neither reads ``primary_verdict`` or the primary
  review cell.
* ``line.task`` is never overwritten with a fix brief (``_fix_task`` output is
  passed to the launch only), so blockers do not ride in ``Task:``.
* The handoff view's ``blocker_count`` is the count of the blocking
  reviewer's blockers for the last fix round. It is a conclusion, so the
  secondary stage leaves it out (:func:`evidence_hides_blocker_count`).
* Unconfirmed fix-ruling notes are built from a blocking reviewer's blockers.
  A secondary reviewer gets them only when it issued that BLOCK itself
  (:func:`notes_visible`).

A human adjudicating a primary/secondary conflict still sees both verdicts;
that path does not go through these prompts.
"""

from __future__ import annotations

from typing import Any, Mapping

SECONDARY_STAGE = "secondary_review"


def review_stage(snapshot: Any) -> str:
    """The line's Stage-Gate stage name, lower case, or ``""``."""
    if not isinstance(snapshot, Mapping):
        return ""
    machine = snapshot.get("machine")
    if isinstance(machine, Mapping):
        stage = machine.get("stage")
        if isinstance(stage, str):
            return stage.strip().lower()
    phase = snapshot.get("phase")
    return phase.strip().lower() if isinstance(phase, str) else ""


def _blocking_reviewer(snapshot: Mapping[str, Any]) -> str:
    machine = snapshot.get("machine")
    if isinstance(machine, Mapping):
        return str(machine.get("blocking_reviewer") or "").strip().lower()
    return ""


def is_secondary(snapshot: Any) -> bool:
    return review_stage(snapshot) == SECONDARY_STAGE


def evidence_hides_blocker_count(snapshot: Any) -> bool:
    """True when the review in progress is the secondary one."""
    return is_secondary(snapshot)


def notes_visible(snapshot: Any, reviewer: str | None) -> bool:
    """Whether ruling notes (another reviewer's conclusions) may reach ``reviewer``.

    Outside the secondary stage nothing changes. In it, the notes are shown
    only to the reviewer that issued the BLOCK the ruling answers.
    """
    if not is_secondary(snapshot):
        return True
    if not isinstance(snapshot, Mapping):
        return False
    blocker = _blocking_reviewer(snapshot)
    return bool(blocker) and blocker == str(reviewer or "").strip().lower()
