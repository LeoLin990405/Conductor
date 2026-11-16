"""A line is submitted on verified evidence, not on the worker's word (N60).

R8c sub-lines reported "98 passed" and exited with no commit (N49). What the
worker says about its run is advisory. What counts is evidence the system
produced for the exact HEAD under review and the line's write scope:

* the G2 run recorded for that HEAD (``gates.G2.revision``), its result and
  every command's exit code;
* the diff's file names (base...HEAD), all inside ``write_scope``.

``CONDUCTOR_DISPATCH_VERIFIED_COMPLETION_MODE`` is ``off|shadow|enforce``,
default ``shadow``. Shadow records the evidence and every mismatch between
the worker's claim and that evidence on the N38 reviewed-heads ledger of the
line's branch (``evidence`` keyed by HEAD), and on the line snapshot
(``completion_evidence``) when it is unverified or disagrees with the claim. It never changes the outcome. Enforce
is not wired into the drive yet: it records the same and, in addition, says
``would_refuse`` so the drive can promote it once the shadow log is clean.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .check_modes import ENFORCE, OFF, check_mode

__all__ = [
    "COMPLETION_EVIDENCE",
    "COMPLETION_MODE_ENV",
    "WORKER_CLAIM",
    "changed_files",
    "enabled",
    "mode",
    "claim_from_output",
    "note_claim",
    "record_evidence",
    "verify_completion",
]

COMPLETION_MODE_ENV = "CONDUCTOR_DISPATCH_VERIFIED_COMPLETION_MODE"
#: Line-snapshot key: the latest verification, read by ``dispatch status``.
COMPLETION_EVIDENCE = "completion_evidence"
#: Line-snapshot key: the worker's advisory claim about its last run.
WORKER_CLAIM = "worker_claim"

_MAX_FILES = 50
#: Gaps where the system could not say, as opposed to evidence that refutes.
_UNKNOWNS = frozenset({"head_unknown", "diff_unknown"})
_MAX_LEDGER_ENTRIES = 20
_TAIL_CHARS = 8000

_PASSED = re.compile(r"\b(\d+)\s+passed\b", re.I)
_FAILED = re.compile(r"\b(\d+)\s+(?:failed|errors?)\b", re.I)
_ALL_PASS = re.compile(r"\ball\s+(?:the\s+)?tests?\s+pass(?:ed|es)?\b", re.I)
_COMMITTED = re.compile(r"\bcommitted\b|\bcreated\s+commit\b|\bcommit\s+[0-9a-f]{7,40}\b", re.I)


def mode(environ: Mapping[str, str] | None = None) -> str:
    return check_mode(COMPLETION_MODE_ENV, environ)


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    return mode(environ) != OFF


def changed_files(gates: object, line: object, head: str) -> tuple[str, ...] | None:
    """The diff's file names base...``head``, read like G2's scope check, or None.

    A gate runner without a repository (a test double) gives None: the
    evidence then says ``diff_unknown`` rather than guessing.
    """
    settings = getattr(gates, "_settings", None)
    repo = getattr(settings, "repo_root", None)
    if not head or repo is None:
        return None
    from .gates import git_changed_paths

    return git_changed_paths(repo, head, getattr(line, "base_commit", None) or "HEAD")


def claim_from_output(output: str | None) -> dict[str, object]:
    """What the worker's final text claims. Advisory, never trusted.

    A schema-valid completion report (``completion_report_from_text``) wins;
    otherwise the tail of the text is read for "N passed" / "N failed" and
    a commit claim. Nothing found is ``source: none``.
    """
    text = (output or "")[-_TAIL_CHARS:]
    claim: dict[str, object] = {"source": "none", "tests_passed": None, "says_committed": False}
    if not text.strip():
        return claim
    report = _completion_report(text)
    if report is not None:
        results = [item.result.strip().casefold() for item in report.verification]
        passing = {"pass", "passed", "ok", "success", "succeeded"}
        claim.update(
            source="completion_report",
            status=report.status,
            tests_passed=(all(item in passing for item in results) if results else None),
            says_committed=bool(report.artifacts.commits) or report.status == "done",
            commits=list(report.artifacts.commits)[:10],
        )
        return claim
    passed = [int(match) for match in _PASSED.findall(text)]
    failed = [int(match) for match in _FAILED.findall(text)]
    if passed or failed or _ALL_PASS.search(text):
        claim["source"] = "text"
        claim["tests_passed"] = not any(failed)
        if passed:
            claim["passed_count"] = passed[-1]
    if _COMMITTED.search(text):
        claim["source"] = "text"
        claim["says_committed"] = True
    return claim


def _completion_report(text: str):
    from src.server.services.autonomy.completion import completion_report_from_text

    stripped = text.strip()
    start = stripped.rfind("\n{")
    candidates = [stripped] + ([stripped[start + 1 :]] if start != -1 else [])
    for candidate in candidates:
        report = completion_report_from_text(candidate)
        if report is not None:
            return report
    return None


def note_claim(
    snapshot: dict[str, object] | None,
    output: str | None,
    *,
    head: str | None,
    start_head: str | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object] | None:
    """Keep the worker's claim for its run, bound to the HEAD it left.

    A run that claims tests passed or a commit but left the branch where it
    started (N49) is recorded as a mismatch right away.
    """
    if not isinstance(snapshot, dict) or mode(environ) == OFF:
        return None
    claim = claim_from_output(output)
    claim["head"] = head or ""
    claim["start_head"] = start_head or ""
    mismatch: list[str] = []
    unchanged = bool(head) and head == start_head
    if unchanged and claim.get("says_committed"):
        mismatch.append("claims_commit_but_head_unchanged")
    if unchanged and claim.get("tests_passed") is True:
        mismatch.append("claims_tests_passed_without_new_commit")
    if mismatch:
        claim["mismatch"] = mismatch
    if claim["source"] == "none" and not mismatch:
        # Nothing claimed: no record, and no stale claim from an earlier run.
        snapshot.pop(WORKER_CLAIM, None)
        return claim
    snapshot[WORKER_CLAIM] = claim
    return claim


def verify_completion(
    snapshot: Mapping[str, object] | None,
    *,
    head: str,
    write_scope: Sequence[str],
    changed: Sequence[str] | None,
    claim: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object] | None:
    """The system's evidence for ``head`` and how it compares with the claim.

    ``changed`` is the diff's file names (None when git could not say).
    Returns None in ``off`` mode.
    """
    current = mode(environ)
    if current == OFF:
        return None
    from .gates import scope_violations

    gates = snapshot.get("gates") if isinstance(snapshot, Mapping) else None
    g2 = gates.get("G2") if isinstance(gates, Mapping) else None
    g2 = g2 if isinstance(g2, Mapping) else {}
    commands = g2.get("commands") if isinstance(g2.get("commands"), list) else []
    exit_codes = [
        item.get("exit_code") for item in commands if isinstance(item, Mapping)
    ]
    failed: list[str] = []
    revision = g2.get("revision") if isinstance(g2.get("revision"), str) else ""
    if not head:
        failed.append("head_unknown")
    if g2.get("result") != "pass":
        failed.append(f"g2_{g2.get('result') or 'missing'}")
    if head and revision != head:
        failed.append("g2_not_on_head")
    if not exit_codes:
        failed.append("g2_no_commands")
    elif any(code != 0 for code in exit_codes):
        failed.append("g2_nonzero_exit")
    files = list(changed) if changed is not None else None
    outside: list[str] = []
    if files is None:
        failed.append("diff_unknown")
    else:
        if not files:
            failed.append("diff_empty")
        outside = list(scope_violations(files, list(write_scope)))
        if outside:
            failed.append("diff_outside_write_scope")
    entry: dict[str, object] = {
        "mode": current,
        "head": head,
        "verified": not failed,
        "failed": failed,
        "g2": {
            "result": g2.get("result") or "",
            "revision": revision,
            "exit_codes": exit_codes[:_MAX_FILES],
        },
        "diff_files": (files or [])[:_MAX_FILES],
        "diff_count": len(files or []),
        "outside_scope": outside[:_MAX_FILES],
    }
    if isinstance(claim, Mapping) and claim.get("source") not in (None, "none"):
        mismatch = list(claim.get("mismatch") or [])
        claim_head = claim.get("head")
        if claim_head and head and claim_head != head:
            mismatch.append("claim_for_other_head")
        if claim.get("tests_passed") is True and any(
            item.startswith("g2_") and item != "g2_no_commands" for item in failed
        ):
            mismatch.append("claims_tests_passed_g2_disagrees")
        if claim.get("says_committed") and "diff_empty" in failed:
            mismatch.append("claims_commit_diff_empty")
        entry["claim"] = {
            "source": claim.get("source"),
            "tests_passed": claim.get("tests_passed"),
            "says_committed": bool(claim.get("says_committed")),
        }
        if mismatch:
            entry["mismatch"] = list(dict.fromkeys(mismatch))
    if current == ENFORCE and failed:
        entry["would_refuse"] = "completion_unverified"
    return entry


def record_evidence(
    dispatch_gates: dict[str, object] | None,
    snapshot: dict[str, object] | None,
    entry: Mapping[str, object] | None,
    *,
    line_id: str,
    branch: str,
    at: object = None,
) -> None:
    """Write ``entry`` on the branch's N38 ledger, and on the snapshot when it flags.

    The ledger gets an ``evidence`` map keyed by HEAD (newest kept); its
    ``complete``/``heads``/``doubt`` keys are never touched.
    """
    if not entry:
        return
    stored = dict(entry)
    if at is not None:
        stored["at"] = at
    if isinstance(snapshot, dict):
        # The snapshot shows what ``dispatch status`` should flag: evidence
        # that refutes the line, or a claim that disagrees. A clean record, or
        # one whose only gaps are what git could not say (HEAD, diff), lives only on the
        # ledger, so a passing line's snapshot keeps its old shape.
        refuted = [item for item in stored.get("failed") or [] if item not in _UNKNOWNS]
        if not refuted and not stored.get("mismatch"):
            snapshot.pop(COMPLETION_EVIDENCE, None)
        else:
            snapshot[COMPLETION_EVIDENCE] = stored
    if not isinstance(dispatch_gates, dict):
        return
    from .machine import REVIEWED_HEADS

    ledgers = dispatch_gates.get(REVIEWED_HEADS)
    branches = ledgers.get(line_id) if isinstance(ledgers, dict) else None
    ledger = branches.get(branch) if isinstance(branches, dict) else None
    if not isinstance(ledger, dict):
        return
    head = str(stored.get("head") or "")
    if not head:
        return  # nothing to bind; the ledger keeps its old shape
    evidence = ledger.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
        ledger["evidence"] = evidence
    evidence.pop(head, None)
    evidence[head] = {
        "verified": bool(stored.get("verified")),
        "failed": list(stored.get("failed") or []),
        "mismatch": list(stored.get("mismatch") or []),
        "at": stored.get("at"),
    }
    while len(evidence) > _MAX_LEDGER_ENTRIES:
        evidence.pop(next(iter(evidence)))
