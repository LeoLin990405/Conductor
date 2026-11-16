"""What a usable review verdict means for the drive (N51, N59).

Two checks sit between the reviewer's reply and the Stage-Gate:

* N51 -- a blocker must be reproducible. Each one names ``file:line``, a
  failure scenario and an executable repro. A blocker without all three is a
  *should-fix*: it is shown to the author but does not block.
* N59 -- a reviewer that lacks evidence says so. ``status: "inconclusive"``
  with ``needed_evidence`` (a test run, an output, a log) is a request for
  evidence, not a failed fix round. ``review_unavailable`` (N42, the call
  failed) is ERROR and stays a separate branch: it never reaches this module.

Both change review outcomes, so each ships behind its own mode (N58):
``CONDUCTOR_DISPATCH_REVIEW_REPRO_MODE`` and
``CONDUCTOR_DISPATCH_REVIEW_INCONCLUSIVE_MODE``, ``off|shadow|enforce``,
default ``shadow``. In shadow the verdict is returned unchanged and the
record says what enforce would have done. N51 also writes one
``shadow_decision`` line per blocked verdict it evaluated (never for a pass:
there is no blocker to judge), carrying the line id as ``subject`` and the
matched ``file:line`` as ``location``.

The reply contract lives here too (``REVIEW_RUBRIC``), so the review prompt
and the checks that read the reply cannot drift apart.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from src.utils.shadow_log import shadow_decision

from . import text_normalize
from .check_modes import ENFORCE, OFF, SHADOW, check_mode
from .types import Verdict

logger = logging.getLogger(__name__)

__all__ = [
    "EVIDENCE_REQUEST",
    "INCONCLUSIVE",
    "INCONCLUSIVE_MODE_ENV",
    "REPRO_MODE_ENV",
    "REVIEW_INCONCLUSIVE",
    "REVIEW_RUBRIC",
    "REVIEW_STATUS_HISTORY",
    "REVIEW_TRIAGE",
    "ReviewVerdict",
    "Triage",
    "blocker_missing",
    "inconclusive_exit_ready",
    "triage_verdict",
    "with_status",
]

#: N51: blockers without file:line, scenario and repro become should-fix.
REPRO_MODE_ENV = "CONDUCTOR_DISPATCH_REVIEW_REPRO_MODE"
#: N59: an INCONCLUSIVE verdict is an evidence request, not a fix round.
INCONCLUSIVE_MODE_ENV = "CONDUCTOR_DISPATCH_REVIEW_INCONCLUSIVE_MODE"

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"
STATUSES = (PASS, FAIL, INCONCLUSIVE)

#: The line's failure reason while it waits for the evidence a reviewer asked for.
REVIEW_INCONCLUSIVE = "review_inconclusive"
#: Line-snapshot key of the latest triage record, read by ``dispatch status``.
REVIEW_TRIAGE = "review_triage"
#: Line-snapshot key: every reported status, oldest first (auto-merge reads it).
REVIEW_STATUS_HISTORY = "review_status_history"
#: ``Triage.action`` when enforce parks the line for evidence.
EVIDENCE_REQUEST = "evidence_request"

#: How many items a record keeps, and how long each may be.
_MAX_ITEMS = 20
_MAX_CHARS = 400

# Appended to the review prompt (the hook is in ``execution.py``). It keeps
# the three-key reply shape the parsers read and adds the two N59 keys.
REVIEW_RUBRIC = (
    "Also set \"status\" to \"pass\", \"fail\" or \"inconclusive\", and "
    "\"needed_evidence\" to a list. Write every blocker as "
    "'path/to/file.py:LINE - scenario: <inputs or state> -> <wrong result> - "
    "repro: <command or test that shows it>'; give each blocker a file:line, a "
    "failure scenario and an executable repro (a command or concrete steps; "
    "never 'n/a'). A security or scope finding puts its tag first, before the "
    "path: 'security: path/to/file.py:LINE - scenario: ...' or "
    "'scope: path/to/file.py:LINE - scenario: ...'. "
    "When you cannot decide because evidence is missing (a test run, its "
    "output, a log), set passed false, status \"inconclusive\" and name each "
    "missing item in needed_evidence. Any new state that stops the "
    "drive needs an operator exit (a dispatch CLI command) with a CLI test; "
    "a change that adds one without that exit is a blocker."
)

_FILE_LINE = re.compile(r"(?<![\w/.-])[\w.-]*(?:/[\w.-]+)*\.[A-Za-z0-9]+:\d+")
_SCENARIO = re.compile(r"\b(?:failure\s+)?scenario\s*:\s*\S|\bfails?\s+when\b\s*\S", re.I)
_REPRO_MARKER = re.compile(r"\brepro(?:duce)?\s*:[ \t]*(.*)", re.I | re.S)
# N71 low: a command is spelled as it is typed, in lower case. A capitalized
# word is prose or a quote ("Git reset --hard destroys work"), not a command,
# so the command matchers are case-sensitive.
_REPRO_COMMAND = re.compile(
    r"`[^`]*\b(?:pytest|python\d?(?:\s+-\w+)*|conductor|cond|uv\s+run|make|npm|"
    r"node|bash|sh|curl|git)\b[^`]*`",
)
# What follows ``repro:`` must be a command or concrete steps, not a marker
# filler. A value made only of these words (and punctuation) is no repro.
_REPRO_FILLER = re.compile(
    r"^[\s\W]*(?:(?:n/?a|none|nil|null|tbd|todo|unknown|unclear|not\s+applicable|"
    r"see\s+above|same|nothing|no\s+repro|-+|\?+)[\s\W]*)*$",
    re.I,
)
# A fenced code block is markup, not a command: it used to count because the
# two backticks of ```powershell ... ``` read as an inline span, so any fence
# (a destructive powershell snippet included) passed the repro check whatever
# it held (N71 low). Drop the fence and its language and judge the block's own
# text.
_FENCE = re.compile(r"(?:`{3,}|~{3,})[ \t]*[\w.+#-]*")
_REPRO_BACKTICK = re.compile(r"`([^`]*[A-Za-z0-9][^`]*)`")
#: A backticked span is a repro when it starts like a known command
#: (``pytest -q``, ``uv run ...``, see ``_REPRO_SHELL``) or names a path or a
#: call (``./run.sh``, ``tests/test_app.py``, ``app.parse([])``). A lone
#: lower-case word (``a``) or a phrase (``see the log``) is prose, not a repro:
#: it used to pass because any span that began with a lower-case letter counted
#: (N71 low, R2 review item 7).
_REPRO_BACKTICK_PATH = re.compile(r"^\s*(?:\$\s*)?\S*[/.]")
_REPRO_SHELL = re.compile(
    r"^\s*(?:\$\s*)?(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:pytest|python\d?(?:\.\d+)?|uvx?|"
    r"conductor|cond|make|npm|npx|pnpm|yarn|node|bash|sh|zsh|curl|git|cargo|go|"
    r"ruff|mypy|docker|\./\S+)\b",
)
_REPRO_TEST_ID = re.compile(r"\S+\.py::\w+")
_REPRO_NUMBERED = re.compile(r"(?:^|\s)(?:1[.)]|step\s*1\b).*\s(?:2[.)]|step\s*2\b)", re.I | re.S)
_REPRO_STEP_SEP = re.compile(r";|->|\u2192|\bthen\b|,", re.I)

# The tag matchers live in ``text_normalize`` so that the N41 router
# (``machine.conflict_tag``) and this N51 exemption read one pattern and
# cannot disagree about the same blocker (R2 review item 4: ``out-of-scope:``
# was a tag for the exemption but not for the router, and "not out of scope:"
# the other way around). The names stay here for the callers and tests that
# already import them; every match is on the folded text.
_SECURITY_TAG = text_normalize.SECURITY_TAG
_SCOPE_TAG = text_normalize.SCOPE_TAG
# N51 exemption (owner decision, 2026-11-14): never downgrade a blocker that names
# security anywhere, or carries a scope tag anywhere. "telescope" and
# "module-scoped" are not tags.
_SECURITY_WORD = re.compile(r"\bsecurity\b", re.I)

@dataclass
class ReviewVerdict(Verdict):
    """A ``Verdict`` that also carries the reviewer's N59 status.

    It is a ``Verdict``, so every existing consumer keeps working. ``status``
    is ``""`` for a reply that did not say (the pre-N59 shape).
    """

    status: str = ""
    needed_evidence: list[str] = field(default_factory=list)
    should_fix: list[str] = field(default_factory=list)


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:_MAX_CHARS] for item in value if str(item).strip()][:_MAX_ITEMS]


def with_status(verdict: Verdict, payload: Mapping[str, object] | None) -> Verdict:
    """``verdict`` with the reply's ``status`` and ``needed_evidence``, if any.

    The reply's ``passed`` stays the authority for MERGE/BLOCK. A status that
    is not one of ``pass|fail|inconclusive`` is dropped, never guessed.
    """
    if not isinstance(payload, Mapping):
        return verdict
    raw = payload.get("status")
    status = raw.strip().casefold() if isinstance(raw, str) else ""
    if status not in STATUSES:
        status = ""
    needed = _texts(payload.get("needed_evidence"))
    if not status and not needed:
        return verdict
    if verdict.passed and status in (FAIL, INCONCLUSIVE):
        # Fail closed: a reply that says it passed but reports fail or
        # inconclusive never merges on the boolean alone.
        verdict = Verdict(
            passed=False,
            blockers=list(verdict.blockers)
            or [f"reviewer reported status {status} with passed true"],
            verdict=verdict.verdict,
        )
    return ReviewVerdict(
        passed=verdict.passed,
        blockers=list(verdict.blockers),
        verdict=verdict.verdict,
        status=status,
        needed_evidence=needed,
    )


def blocker_missing(blocker: str) -> list[str]:
    """What an N51 blocker lacks: any of ``file_line``, ``scenario``, ``repro``."""
    text = str(blocker or "")
    missing: list[str] = []
    if not _FILE_LINE.search(text):
        missing.append("file_line")
    if not _SCENARIO.search(text):
        missing.append("scenario")
    probe = _unfenced(text)
    if not (_REPRO_COMMAND.search(probe) or _real_repro(probe)):
        missing.append("repro")
    return missing


def _unfenced(text: str) -> str:
    """``text`` without code-fence markers and languages; the block text stays."""
    return _FENCE.sub("", text)


def _real_repro(text: str) -> bool:
    """True when a ``repro:`` marker is followed by a command or concrete steps.

    ``repro: n/a``, ``repro: none``, ``repro: tbd``, ``repro: -`` and a bare
    ``repro:`` are the marker without a repro.
    """
    for match in _REPRO_MARKER.finditer(_unfenced(text)):
        value = match.group(1).strip()
        if not value or _REPRO_FILLER.match(value):
            continue
        spans = [
            item
            for item in _REPRO_BACKTICK.findall(value)
            if not _REPRO_FILLER.match(item)
        ]
        if any(
            _REPRO_SHELL.match(item) or _REPRO_BACKTICK_PATH.match(item)
            for item in spans
        ):
            return True
        if _REPRO_SHELL.match(value) or _REPRO_TEST_ID.search(value):
            return True
        if _REPRO_NUMBERED.search(value):
            return True
        steps = [part for part in _REPRO_STEP_SEP.split(value) if part.strip()]
        if len(steps) >= 2 and len(value.split()) >= 4:
            return True
    return False


def _exempt(blocker: str) -> bool:
    """Blockers N51 never downgrades.

    A security or scope finding fails closed (N58: a protected action is not
    relaxed by a format rule), wherever its tag sits, and a ruling check's own
    "not confirmed" line is the machine's, not a reviewer's finding. The tag,
    scope and keyword matchers all read the folded text: a full-width,
    zero-width or look-alike spelling of the tag is the same finding. The
    scope tag is the same pattern the N41 router reads, so a blocker is
    exempt exactly when it would be adjudicated.
    """
    from .rulings import is_unconfirmed_blocker

    text = text_normalize.fold(blocker)
    return (
        is_security_tagged(blocker)
        or bool(_SECURITY_WORD.search(text))
        or text_normalize.scope_tag(blocker)
        or text_normalize.mentions_security(blocker)
        or is_unconfirmed_blocker(blocker)
    )


def is_security_tagged(blocker: str) -> bool:
    """True when ``blocker`` carries a ``security:`` / ``[security]`` tag."""
    return text_normalize.security_tag(blocker)


def inconclusive_exit_ready() -> bool:
    """True once ``dispatch unblock`` resumes a ``review_inconclusive`` park.

    N51: a new drive-stopping state needs an operator exit. Until
    ``operator.py`` lists the reason as a resumable review park, enforce
    must not park a line it could not be unparked from, so it acts as shadow.
    """
    from . import operator as operator_module

    parks = getattr(operator_module, "_RESUMABLE_REVIEW_PARKS", ())
    return REVIEW_INCONCLUSIVE in parks


@dataclass(frozen=True)
class Triage:
    """The verdict the drive acts on, and the record of what was checked.

    ``action`` is ``""`` (act on ``verdict``) or ``EVIDENCE_REQUEST`` (park
    the line for the evidence in ``record["needed_evidence"]``).
    """

    verdict: Verdict
    record: dict[str, object]
    action: str = ""


def _shadow_repro(
    *,
    subject: object,
    keep: Sequence[str],
    downgraded: Sequence[Mapping[str, object]],
    status: str,
) -> None:
    """One log line for an N51 shadow judgement.

    ``would_change`` is true only when enforce would flip the review result --
    nothing reproducible is left, so the line would MERGE instead of BLOCK.
    A partial downgrade, and a verdict whose blockers all pass N51, still
    block; they log ``false`` with the counts, which is the denominator T4
    divides by. ``subject`` is the line id the caller passes (a ``file:line``
    is not a durable identity); the matched ``file:line`` rides along as
    ``location``.
    """
    first = downgraded[0] if downgraded else {}
    blocker = str(first.get("blocker") or "")
    match = _FILE_LINE.search(blocker)
    location = match.group(0) if match else None
    missing = first.get("missing")
    flips = not keep and status != INCONCLUSIVE
    if flips:
        reason = "no_reproducible_blockers"
    elif not keep:
        # An INCONCLUSIVE verdict still blocks on the evidence it asked for.
        reason = "inconclusive_keeps_block"
    elif not downgraded:
        reason = "all_blockers_reproducible"
    else:
        reason = "some_blockers_not_reproducible"
    shadow_decision(
        logger,
        "review_repro",
        would_change=flips,
        subject=subject or location,
        current="block",
        enforced="merge" if flips else "block",
        reason=reason,
        location=location,
        missing=",".join(str(item) for item in missing)
        if isinstance(missing, (list, tuple))
        else "",
        blockers_down=len(downgraded),
        blockers_left=len(keep),
        blocker=blocker,
    )


def triage_verdict(
    verdict: Verdict,
    *,
    subject: object = "",
    environ: Mapping[str, str] | None = None,
    exit_ready: bool | None = None,
) -> Triage:
    """Apply N59 then N51 to a usable verdict. Off/shadow never change it.

    ``subject`` is the line id the shadow log names, so a sampled judgement can
    be traced back to the review that produced it. ``exit_ready`` defaults to
    :func:`inconclusive_exit_ready`; tests pass it.
    """
    repro_mode = check_mode(REPRO_MODE_ENV, environ)
    inconclusive_mode = check_mode(INCONCLUSIVE_MODE_ENV, environ)
    status = getattr(verdict, "status", "") or ""
    needed = list(getattr(verdict, "needed_evidence", []) or [])
    record: dict[str, object] = {
        "status": status or (PASS if verdict.passed else FAIL),
        "status_reported": bool(status),
        "repro_mode": repro_mode,
        "inconclusive_mode": inconclusive_mode,
    }
    if verdict.passed:
        # A MERGE is never touched: neither check can make a pass stricter.
        return Triage(verdict, record if status else {})

    if status == INCONCLUSIVE and inconclusive_mode != OFF:
        record["needed_evidence"] = needed or ["reviewer named no evidence"]
        ready = inconclusive_exit_ready() if exit_ready is None else exit_ready
        if inconclusive_mode == ENFORCE and ready:
            record["action"] = EVIDENCE_REQUEST
            return Triage(verdict, record, EVIDENCE_REQUEST)
        record["would"] = EVIDENCE_REQUEST
        if inconclusive_mode == ENFORCE:
            # Enforce asked for, but there is no operator exit yet (N51).
            record["enforce_blocked"] = "no_operator_exit"
        # Shadow: the verdict stays a BLOCK, exactly as before N59.

    if repro_mode == OFF:
        return Triage(verdict, record if _worth_recording(record) else {})
    keep: list[str] = []
    downgraded: list[dict[str, object]] = []
    for blocker in verdict.blockers:
        text = str(blocker)
        missing = [] if _exempt(text) else blocker_missing(text)
        if missing:
            downgraded.append({"blocker": text[:_MAX_CHARS], "missing": missing})
        else:
            keep.append(text)
    if repro_mode == SHADOW:
        # One line per shadow judgement, including a verdict whose blockers
        # all pass N51: "the check looked and would not change this" is the
        # denominator, not a non-event (see ``shadow_log``'s table).
        _shadow_repro(
            subject=subject, keep=keep, downgraded=downgraded, status=status
        )
    if not downgraded:
        return Triage(verdict, record if _worth_recording(record) else {})
    record["downgraded"] = downgraded[:_MAX_ITEMS]
    record["downgraded_count"] = len(downgraded)
    if repro_mode == SHADOW:
        record["would"] = record.get("would") or "downgrade"
        return Triage(verdict, record)
    should_fix = [item["blocker"] for item in downgraded]
    record["action"] = "downgrade"
    if not keep and status != INCONCLUSIVE:
        # Nothing reproducible is left: the review does not block.
        record["passed_after_downgrade"] = True
        return Triage(
            ReviewVerdict(
                passed=True,
                blockers=[],
                verdict=verdict.verdict,
                status=status,
                needed_evidence=needed,
                should_fix=[str(item) for item in should_fix],
            ),
            record,
        )
    if not keep:
        # An INCONCLUSIVE verdict that still blocks (shadow): what blocks is
        # the evidence the reviewer asked for, never an empty fix round.
        keep = evidence_blockers(needed or ["reviewer named no evidence"])
    return Triage(
        ReviewVerdict(
            passed=False,
            blockers=keep,
            verdict=verdict.verdict,
            status=status,
            needed_evidence=needed,
            should_fix=[str(item) for item in should_fix],
        ),
        record,
    )


def _worth_recording(record: Mapping[str, object]) -> bool:
    return bool(record.get("status_reported") or record.get("would") or record.get("action"))


def record_triage(
    snapshot: dict[str, object] | None,
    triage: Triage,
    *,
    reviewer: str,
    head: str = "",
    at: object = None,
) -> None:
    """Keep the latest triage on the line snapshot for ``dispatch status``."""
    if not isinstance(snapshot, dict) or not triage.record:
        return
    entry = dict(triage.record)
    entry["reviewer"] = reviewer
    if head:
        entry["head"] = head
    if at is not None:
        entry["at"] = at
    snapshot[REVIEW_TRIAGE] = entry
    status = str(entry.get("status") or "")
    if entry.get("status_reported") and status:
        history = snapshot.get(REVIEW_STATUS_HISTORY)
        if not isinstance(history, list):
            history = []
            snapshot[REVIEW_STATUS_HISTORY] = history
        history.append(status)
        del history[:-_MAX_ITEMS]


def evidence_blockers(needed: Sequence[str]) -> list[str]:
    """The evidence request as the line's blockers, so the author sees it."""
    return [f"evidence: {item}"[:_MAX_CHARS] for item in needed][:_MAX_ITEMS]
