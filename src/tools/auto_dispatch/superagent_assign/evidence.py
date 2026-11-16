"""The review-evidence section N73 adds before the diff (PRD 5.1).

The Reviewer is the last defence once the soft gates are open, so it is shown
facts about how the line was produced -- never another reviewer's conclusion
(R6 L5):

1. the assignment record: the author's model, effort, runtime, source and the
   Super-agent's reason; the selector's counterfactual; re-ask or fallback;
2. the record-only gate findings (N46, N47, N52) with their file names;
3. scope: the declared write scope, the files the diff touches, the files
   outside the scope, and whether a high-risk path is touched (PRD 3.3).

The section is empty -- the prompt stays byte-identical -- when there is no
assignment record, no finding, nothing outside the scope and no high-risk
path. It is bounded (``SECTION_MAX_BYTES``).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .record import assignment_entries

SECTION_MAX_BYTES = 3_000
_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_REVIEW_RULES = (
    "Answer every finding and every out-of-scope file: accept it with a reason, "
    "or BLOCK with file:line, the failure scenario and an executable repro. The "
    "assignment record is not a reason to BLOCK by itself."
)


def diff_files(diff: str) -> list[str]:
    return list(dict.fromkeys(match.group(2) for match in _DIFF_FILE.finditer(diff or "")))


def _in_scope(path: str, scopes: Iterable[str]) -> bool:
    from fnmatch import fnmatch

    for scope in scopes:
        scope = str(scope or "").strip().lstrip("./")
        if not scope:
            continue
        if path == scope or path.startswith(scope.rstrip("/") + "/") or fnmatch(path, scope):
            return True
    return False


def _latest(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    entries = assignment_entries(snapshot.get("assignment"))
    return entries[-1] if entries else None


def review_section(snapshot: Any, line: Any, diff: str, *, environ=None) -> str:
    """The section text (ending in a newline), or ``""`` when there is nothing."""
    from ..auto_merge import high_risk_globs, is_high_risk
    from ..gates import shadow_findings

    snap = snapshot if isinstance(snapshot, Mapping) else {}
    scope = [str(item) for item in getattr(line, "write_scope", ()) or ()]
    files = diff_files(diff)
    outside = [path for path in files if scope and not _in_scope(path, scope)]
    risk = is_high_risk(files, scope, bool(snap.get("risk_bearing")), globs=high_risk_globs(environ=environ))
    findings = shadow_findings(dict(snap))
    entry = _latest(snap)
    if entry is None and not findings and not outside and not risk:
        return ""
    rows: list[str] = ["N73 review evidence (facts, not another reviewer's verdict):"]
    if entry is not None:
        choice = entry.get("choice") or {}
        selector = entry.get("selector_choice") or {}
        rows.append(
            "assignment: "
            f"model={choice.get('model') or selector.get('model') or '?'} "
            f"effort={choice.get('effort') or 'default'} "
            f"runtime={choice.get('runtime') or 'default'} "
            f"source={entry.get('source')} mode={entry.get('mode')} "
            f"selector_would_pick={selector.get('model') or '?'}"
        )
        if choice.get("reason"):
            rows.append(f"assignment reason: {str(choice.get('reason'))[:300]}")
        if entry.get("reask") or entry.get("fallback_reason"):
            rows.append(
                f"reask={bool(entry.get('reask'))} fallback={entry.get('fallback_reason') or '-'}"
            )
    for item in findings:
        names = ", ".join(item.get("files") or []) or "-"
        rows.append(
            f"gate finding {item.get('check')} would_refuse={item.get('would_refuse')}: "
            f"{str(item.get('finding'))[:200]} (files: {names})"
        )
    rows.append("declared write_scope: " + (", ".join(scope) or "(none)"))
    rows.append("diff files: " + (", ".join(files) or "(none)"))
    if outside:
        rows.append("outside write_scope: " + ", ".join(outside))
    rows.append(f"high-risk paths touched: {'yes' if risk else 'no'}")
    rows.append(_REVIEW_RULES)
    text = "\n".join(rows) + "\n"
    raw = text.encode("utf-8")
    if len(raw) > SECTION_MAX_BYTES:
        text = raw[: SECTION_MAX_BYTES - 32].decode("utf-8", "ignore") + "\n[section truncated]\n"
    return text
