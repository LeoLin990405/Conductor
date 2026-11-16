"""Deliver a fix round as a structured Input and verify the Worker's report.

A free-text blocker list is easy to skim: a Worker can answer "fixed" without
touching the one item the reviewer cared about. The N6 collaboration lesson is
that a ruling has to be code-level and itemised, and the Worker's report has to
confirm each item individually. This module builds that Input from the
reviewer's blockers and checks the report against it.

The rendered Input is plain text so every provider can receive it; the
structured form is kept beside it in the line snapshot for the machine and for
operators. Item ids (``M1`` for must, ``N1`` for must-not) make the report
checkable without rewriting the sentence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.server.services.autonomy.completion import completion_report_from_text

#: Snapshot keys owned by this module.
FIX_RULING = "fix_ruling"
RULING_CHECK = "ruling_check"

FIX_ROUND_INPUT = "fix_round"
USER_ROLE = "user"

#: The defaults are deliberately concrete: "be careful" is not a ruling.
DEFAULT_MUST_NOT = (
    "Do not patch only the reported edge case; fix the cause the blocker names.",
    "Do not weaken, skip, delete or rewrite the check that found the blocker.",
    "Do not change tests or acceptance commands to make them pass.",
)
ESCALATION_MUST = (
    "State the new approach or the narrower specification you will use, and why it "
    "removes the whole class of blockers rather than the one reported edge case.",
)
ESCALATION_MUST_NOT = (
    "Do not continue the same approach by patching another edge case.",
)

MAX_ITEMS = 20
MAX_ITEM_CHARS = 1_000
MAX_EXAMPLES = 10
MAX_REPORT_CHARS = 200_000
MAX_RULING_BYTES = 12_000

MUST = "must"
MUST_NOT = "must_not"

_NEGATIVE = re.compile(
    r"\b(?:not|no|never|missing|unconfirmed|pending|todo|to\s+do|skipped|failed|"
    r"unmet|did\s+not|didn't|don't|not\s+done|not\s+confirmed|n/a)\b",
    re.IGNORECASE,
)
_POSITIVE = re.compile(
    r"(\b(?:confirmed|verified|done|complete(?:d)?|met|yes|covered|satisfied|"
    r"addressed|fixed|implemented|passed)\b|✅|✓|☑)",
    re.IGNORECASE,
)
_ITEM_LINE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:RULING\s+)?\[?([MN]\s*\d{1,3})\]?"
    r"(?:\s*[-:–—=]\s*|\s+)(.*)$",
    re.IGNORECASE,
)
#: A ``RULING M1: ...`` mention anywhere in a line. A stream-json log keeps the
#: final message inside a JSON string, so the item is not at a line start.
_RULING_MENTION = re.compile(
    r"\bRULING\s+\[?([MN]\s*\d{1,3})\]?(?:\s*[-:–—=]\s*|\s+)([^\n\"]*)",
    re.IGNORECASE,
)
#: The verification ``ran`` of a completion report that names one ruling item.
_COMPLETION_ITEM = re.compile(
    r"^\s*(?:RULING\s+)?\[?([MN])\s*(\d{1,3})\]?\s*(?:[-:–—=].*)?$",
    re.IGNORECASE,
)
#: A status word that opens the item body decides it. Must-not items are
#: confirmed with negations ("confirmed - I did not touch other files"), so a
#: negation later in the body must not undo an explicit leading "confirmed".
_LEAD = r"^[\s\-*:=\[\]()–—]*"
_LEADING_NO = re.compile(
    _LEAD + r"(?:not|no|never|missing|unconfirmed|pending|todo|to\s+do|skipped|"
    r"failed|unmet|n/a|did\s+not|didn't|partial(?:ly)?)\b",
    re.IGNORECASE,
)
#: "confirmed"/"verified" are the format's own status words and decide alone;
#: a looser opener ("done", "ok", ✅) still needs a body free of negation.
_LEADING_CONFIRMED = re.compile(
    _LEAD + r"(?:confirmed|verified)\b(?!\s*(?:not\b|n't|\?))",
    re.IGNORECASE,
)
_LEADING_YES = re.compile(
    _LEAD + r"(?:(?:done|complete(?:d)?|met|yes|satisfied|addressed|"
    r"fixed|implemented|passed|ok)\b(?!\s*(?:not\b|n't|\?))|✅|✓|☑)",
    re.IGNORECASE,
)
_CAVEAT = re.compile(
    r"\b(?:but|except|however|pending|todo|to\s+do|failed|failing|missing|"
    r"unconfirmed|skipped|unmet|not\s+(?:yet|done|confirmed|fixed|met|complete))\b",
    re.IGNORECASE,
)
#: One verification row of a completion report, as JSON text in a log.
_COMPLETION_ROW = re.compile(
    r'"ran"\s*:\s*"\s*(?:RULING\s+)?\[?([MN])\s*(\d{1,3})\]?\s*"\s*,\s*'
    r'"result"\s*:\s*"([^"\n]*)"',
    re.IGNORECASE,
)


def _body_confirms(body: object) -> bool:
    """True when an item's status text affirms it.

    A leading negative status refutes, a leading affirmative confirms; with
    neither, the body confirms only with an affirmative word and no negation.
    """
    text = str(body or "").strip()
    if not text:
        return False
    if _LEADING_NO.match(text):
        return False
    if _LEADING_CONFIRMED.match(text):
        return True
    if _LEADING_YES.match(text):
        # A must-not is confirmed with a plain negation ("done, no tests were
        # changed"), so only a caveat about the item itself undoes the opener.
        return not _CAVEAT.search(_LEADING_YES.sub("", text, count=1))
    return bool(_POSITIVE.search(text)) and not _NEGATIVE.search(text)


#: The format example the ruling itself prints; echoing it confirms nothing.
_PLACEHOLDER = re.compile(r"<(?:what you changed|how you avoided it)>", re.IGNORECASE)


def _one_line(value: object, limit: int = MAX_ITEM_CHARS) -> str:
    """A bounded item text on one logical line, without control characters."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _clean_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence):
        return ()
    out: list[str] = []
    for item in value:
        text = _one_line(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_ITEMS:
            break
    return tuple(out)


@dataclass(frozen=True, slots=True, eq=False)
class RulingItem:
    """One item the Worker must confirm, with the id the report cites.

    Equality with a plain string compares the item's text, so a caller holding
    a tuple of items can still ask ``"missing test" in ruling.must``.
    """

    id: str
    text: str
    kind: str = MUST

    def __str__(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RulingItem):
            return (self.id, self.text, self.kind) == (other.id, other.text, other.kind)
        if isinstance(other, str):
            return self.text == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.id, self.text, self.kind))

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text, "kind": self.kind}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RulingItem":
        return cls(
            id=_one_line(raw.get("id"), 16).upper(),
            text=_one_line(raw.get("text")),
            kind=_one_line(raw.get("kind") or MUST, 16).lower(),
        )


@dataclass(frozen=True, slots=True)
class Ruling:
    """A fix round's must / must-not contract, and its examples."""

    line_id: str = ""
    fix_round: int = 0
    must: tuple[RulingItem, ...] = ()
    must_not: tuple[RulingItem, ...] = ()
    examples: tuple[str, ...] = ()
    summary: str = ""

    @property
    def items(self) -> tuple[RulingItem, ...]:
        return self.must + self.must_not

    @property
    def must_texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.must)

    @property
    def must_not_texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.must_not)

    def to_dict(self) -> dict[str, object]:
        return {
            "line_id": self.line_id,
            "fix_round": self.fix_round,
            "must": [item.to_dict() for item in self.must],
            "must_not": [item.to_dict() for item in self.must_not],
            "examples": list(self.examples),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Ruling | None":
        if not isinstance(raw, Mapping):
            return None
        must = _items_from_raw(raw.get("must"), MUST)
        must_not = _items_from_raw(raw.get("must_not") or raw.get("must-not"), MUST_NOT)
        if not must and not must_not:
            return None
        try:
            fix_round = int(raw.get("fix_round") or 0)
        except (TypeError, ValueError):
            fix_round = 0
        return cls(
            line_id=_one_line(raw.get("line_id"), 200),
            fix_round=max(fix_round, 0),
            must=must,
            must_not=must_not,
            examples=_clean_items(raw.get("examples"))[:MAX_EXAMPLES],
            summary=_one_line(raw.get("summary"), 4_000),
        )

    def render(self) -> str:
        """The fix-round Input body: ids, both lists, examples, and the format."""
        heading = "Ruling for the fix round"
        if self.line_id:
            heading += f" on {self.line_id}"
        if self.fix_round:
            heading += f" (round {self.fix_round})"
        lines = [
            heading + ".",
            "Confirm every item individually in your final report; a general "
            '"done" is not a confirmation.',
        ]
        if self.summary:
            lines.extend(["", self.summary])
        lines.extend(["", "Must:"])
        lines.extend(f"- [{item.id}] {item.text}" for item in self.must)
        if not self.must:
            lines.append("- (none)")
        lines.extend(["", "Must not:"])
        lines.extend(f"- [{item.id}] {item.text}" for item in self.must_not)
        if not self.must_not:
            lines.append("- (none)")
        if self.examples:
            lines.extend(["", "Examples:"])
            lines.extend(f"- {example}" for example in self.examples)
        lines.extend(
            [
                "",
                "Report each item once, in this form:",
                "RULING M1: confirmed - <what you changed>",
                "RULING N1: confirmed - <how you avoided it>",
                "If your final report is a JSON completion report, add one "
                'verification entry per item instead: {"ran": "RULING M1", '
                '"result": "confirmed - <what you changed>"}.',
            ]
        )
        text = "\n".join(lines)
        raw = text.encode("utf-8")
        if len(raw) <= MAX_RULING_BYTES:
            return text
        note = "\n[ruling truncated]"
        room = max(MAX_RULING_BYTES - len(note.encode("utf-8")), 0)
        return raw[:room].decode("utf-8", "ignore") + note

    def input_text(self) -> str:
        return self.render()

    def to_input(self) -> "RulingInput":
        return RulingInput(
            line_id=self.line_id,
            fix_round=self.fix_round,
            text=self.render(),
            must=self.must,
            must_not=self.must_not,
            examples=self.examples,
            summary=self.summary,
        )


@dataclass(frozen=True, slots=True)
class RulingInput:
    """A fix round delivered as one Input, with its structured lists intact."""

    line_id: str = ""
    fix_round: int = 0
    text: str = ""
    must: tuple[RulingItem, ...] = ()
    must_not: tuple[RulingItem, ...] = ()
    examples: tuple[str, ...] = ()
    summary: str = ""
    kind: str = FIX_ROUND_INPUT
    role: str = USER_ROLE

    @property
    def items(self) -> tuple[RulingItem, ...]:
        return self.must + self.must_not

    @property
    def must_texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.must)

    @property
    def must_not_texts(self) -> tuple[str, ...]:
        return tuple(item.text for item in self.must_not)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "role": self.role,
            "line_id": self.line_id,
            "fix_round": self.fix_round,
            "text": self.text,
            "must": [item.to_dict() for item in self.must],
            "must_not": [item.to_dict() for item in self.must_not],
            "examples": list(self.examples),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RulingInput | None":
        ruling = Ruling.from_dict(raw)
        if ruling is None:
            return None
        text = str(raw.get("text") or "") or ruling.render()
        return cls(
            line_id=ruling.line_id,
            fix_round=ruling.fix_round,
            text=text,
            must=ruling.must,
            must_not=ruling.must_not,
            examples=ruling.examples,
            summary=ruling.summary,
            kind=_one_line(raw.get("kind") or FIX_ROUND_INPUT, 32),
            role=_one_line(raw.get("role") or USER_ROLE, 16),
        )


@dataclass(frozen=True, slots=True)
class RulingCheck:
    """Which ruling items the Worker's report confirmed, and which it missed."""

    confirmed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()
    #: ``False`` from a persisted check is authoritative even when the writer
    #: did not repeat the missing ids. ``None`` means derive from the lists.
    declared_complete: bool | None = None

    @property
    def complete(self) -> bool:
        if self.declared_complete is False:
            return False
        return not self.missing and not self.unconfirmed

    @property
    def ok(self) -> bool:
        return self.complete

    @property
    def passed(self) -> bool:
        return self.complete

    @property
    def missing_items(self) -> tuple[str, ...]:
        return self.missing

    @property
    def missing_ids(self) -> tuple[str, ...]:
        return self.missing

    def to_dict(self) -> dict[str, object]:
        return {
            "confirmed": list(self.confirmed),
            "missing": list(self.missing),
            "unconfirmed": list(self.unconfirmed),
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "RulingCheck | None":
        """Read the persisted shape, preserving an explicit ``complete: false``."""
        if not isinstance(raw, Mapping):
            return None

        def ids(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return ()
            return tuple(str(item) for item in value if str(item))

        confirmed = ids("confirmed")
        missing = ids("missing") or ids("unconfirmed")
        complete = raw.get("complete")
        declared = complete if isinstance(complete, bool) else None
        # A snapshot written before ``complete`` existed is complete only when
        # it lists no missing item. Never infer completeness from a count.
        if declared is None:
            declared = not missing and bool(confirmed)
        return cls(
            confirmed=confirmed,
            missing=missing,
            unconfirmed=missing,
            declared_complete=declared,
        )

    def __bool__(self) -> bool:
        return self.complete

    def __eq__(self, other: object) -> bool:
        """Compare usefully with another check, a bool, or an id sequence."""
        if isinstance(other, RulingCheck):
            return (
                self.confirmed,
                self.missing,
                self.unconfirmed,
            ) == (other.confirmed, other.missing, other.unconfirmed)
        if isinstance(other, bool):
            return self.complete is other
        if isinstance(other, (tuple, list, set, frozenset)):
            wanted = [str(item) for item in other]
            return wanted in (list(self.missing), list(self.confirmed))
        return NotImplemented


def _items_from_raw(value: object, kind: str) -> tuple[RulingItem, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    found: list[RulingItem] = []
    for raw in value[:MAX_ITEMS]:
        if isinstance(raw, Mapping):
            item = RulingItem.from_dict(raw)
            if item.text:
                found.append(
                    RulingItem(
                        id=item.id or _next_id(kind, len(found)),
                        text=item.text,
                        kind=kind,
                    )
                )
        else:
            text = _one_line(raw)
            if text:
                found.append(RulingItem(_next_id(kind, len(found)), text, kind))
    return tuple(found)


def _next_id(kind: str, index: int) -> str:
    prefix = "M" if kind == MUST else "N"
    return f"{prefix}{index + 1}"


def _line_id(line: object) -> str:
    if isinstance(line, str):
        return line
    return str(getattr(line, "id", "") or "")


def build_ruling(
    line: object = "",
    blockers: Sequence[str] | str = (),
    *,
    fix_round: int = 0,
    must: Sequence[str] | str = (),
    must_not: Sequence[str] | str = (),
    examples: Sequence[str] | str = (),
    escalation: object = None,
    summary: str = "",
    line_id: str = "",
) -> Ruling:
    """Build the fix-round ruling from the reviewer's blockers.

    Each blocker becomes one ``must`` item. ``must_not`` carries the concrete
    prohibitions every fix round needs; an escalation adds one more must and one
    more must-not that force a change of approach instead of another edge case.
    """
    if isinstance(line, (list, tuple)) and all(isinstance(item, str) for item in line):
        if not blockers:
            blockers = line
        line = ""
    line_id = line_id or _line_id(line)
    must_texts = list(_clean_items(must))
    for blocker in _clean_items(blockers):
        if blocker not in must_texts:
            must_texts.append(blocker)
    must_not_texts = list(_clean_items(must_not))
    for default in DEFAULT_MUST_NOT:
        if default not in must_not_texts:
            must_not_texts.append(default)

    escalation_data = escalation
    if not isinstance(escalation_data, Mapping):
        raw = getattr(escalation, "to_dict", None)
        escalation_data = raw() if callable(raw) else None
    if isinstance(escalation_data, Mapping) and escalation_data:
        item = _one_line(
            escalation_data.get("instruction") or ESCALATION_MUST,
            MAX_ITEM_CHARS,
        )
        if item and item not in must_texts:
            must_texts.append(item)
        for cannot in ESCALATION_MUST_NOT:
            if cannot not in must_not_texts:
                must_not_texts.append(cannot)
    if not summary and isinstance(escalation_data, Mapping):
        summary = _one_line(escalation_data.get("summary"), 4_000)

    built_must = tuple(
        RulingItem(_next_id(MUST, index), text, MUST)
        for index, text in enumerate(must_texts[:MAX_ITEMS])
    )
    built_must_not = tuple(
        RulingItem(_next_id(MUST_NOT, index), text, MUST_NOT)
        for index, text in enumerate(must_not_texts[:MAX_ITEMS])
    )
    return Ruling(
        line_id=line_id,
        fix_round=max(int(fix_round or 0), 0),
        must=built_must,
        must_not=built_must_not,
        examples=_clean_items(examples)[:MAX_EXAMPLES],
        summary=_one_line(summary, 4_000),
    )


#: A name that reads naturally from the machine.
ruling_for_fix = build_ruling
fix_round_ruling = build_ruling


def deliver_ruling(
    line: object = "",
    blockers: Sequence[str] | str = (),
    *,
    fix_round: int = 0,
    line_id: str = "",
    **kwargs: Any,
) -> RulingInput:
    """Build the ruling and return it as the Input the Worker's turn receives."""
    if isinstance(line, (list, tuple)) and all(isinstance(item, str) for item in line):
        if not blockers:
            blockers = line
        line = ""
    return build_ruling(
        line, blockers, fix_round=fix_round, line_id=line_id, **kwargs
    ).to_input()


#: Explicit spellings used by callers that treat the Input as the deliverable.
deliver_fix_input = deliver_ruling
build_fix_input = deliver_ruling
build_fix_round_input = deliver_ruling
FixRoundInput = RulingInput
FixRoundRuling = Ruling


def render_ruling(ruling: Ruling | RulingInput | Mapping[str, Any]) -> str:
    """Render a ruling object, Input, or persisted dict as prompt text."""
    if isinstance(ruling, RulingInput):
        return ruling.text or Ruling(
            line_id=ruling.line_id,
            fix_round=ruling.fix_round,
            must=ruling.must,
            must_not=ruling.must_not,
            examples=ruling.examples,
            summary=ruling.summary,
        ).render()
    if isinstance(ruling, Ruling):
        return ruling.render()
    parsed = Ruling.from_dict(ruling)
    if parsed is not None:
        return parsed.render()
    if isinstance(ruling, Mapping):
        return str(ruling.get("text") or "")
    return ""


#: The machine stores the ruling as a dict; this reads it back.
def ruling_from_snapshot(snapshot: Mapping[str, Any] | None) -> Ruling | None:
    if not isinstance(snapshot, Mapping):
        return None
    return Ruling.from_dict(snapshot.get(FIX_RULING) or {})


def ruling_from_dict(raw: Mapping[str, Any] | None) -> Ruling | None:
    """Read a persisted ruling dict (the shape ``Ruling.to_dict`` writes)."""
    return Ruling.from_dict(raw or {})


def ruling_input_from_snapshot(
    snapshot: Mapping[str, Any] | None,
) -> RulingInput | None:
    if not isinstance(snapshot, Mapping):
        return None
    return RulingInput.from_dict(snapshot.get(FIX_RULING) or {})


def _status_text(value: object) -> str:
    """One structured status as text. A bool and a nested row are both common."""
    if isinstance(value, bool):
        return "confirmed" if value else "missing"
    if isinstance(value, Mapping):
        for key in ("confirmed", "status", "result", "done", "met"):
            if key in value:
                return _status_text(value[key])
        # A row that names work but carries no verdict does not confirm it.
        return "missing"
    return str(value)


def _report_items(report: object) -> dict[str, str]:
    """Every item id the report mentions, mapped to what it said about it.

    A structured report wins over prose. The accepted shapes are the ones a
    provider adapter or a test can produce without guessing our text format:
    ``{"confirmed": [...]}``, ``{"must": {"M1": true}}``, ``{"items": [...]}``
    and a list of ``{"id": "M1", "confirmed": true}`` rows.
    """
    found: dict[str, str] = {}
    if isinstance(report, Mapping):
        for key in ("ruling", "confirmation", "confirmations", "items", "report"):
            nested = report.get(key)
            if isinstance(nested, (Mapping, Sequence)) and not isinstance(
                nested, (str, bytes)
            ):
                found.update(_report_items(nested))
        for key in ("must", "must_not", "must-not"):
            nested = report.get(key)
            if isinstance(nested, Mapping):
                for item_id, status in nested.items():
                    found[str(item_id).upper()] = _status_text(status)
            elif isinstance(nested, Sequence) and not isinstance(
                nested, (str, bytes)
            ):
                found.update(_report_items(nested))
        for key in ("confirmed", "done", "met"):
            nested = report.get(key)
            if isinstance(nested, Sequence) and not isinstance(
                nested, (str, bytes)
            ):
                for item in nested:
                    if isinstance(item, Mapping):
                        found.update(_report_items(item))
                    else:
                        found[_item_id(item)] = "confirmed"
        for key in ("unconfirmed", "missing", "pending"):
            nested = report.get(key)
            if isinstance(nested, Sequence) and not isinstance(
                nested, (str, bytes)
            ):
                for item in nested:
                    if isinstance(item, Mapping):
                        found.update(_report_items(item))
                    else:
                        found[_item_id(item)] = "missing"
        for item_id_key in ("id", "item", "key"):
            if item_id_key not in report or len(report) > 4:
                continue
            item_id = _item_id(report[item_id_key])
            if not item_id:
                continue
            status = None
            for status_key in ("status", "result", "confirmed", "done", "met"):
                if status_key in report:
                    status = report[status_key]
                    break
            if status is None:
                # Naming the item is not confirming it.
                found[item_id] = "missing"
            else:
                found[item_id] = _status_text(status)
        # A plain ``{"M1": "confirmed", "N1": "missing"}`` is the shortest
        # structured report an adapter or a test can produce.
        for key, status in report.items():
            item_id = _item_id(key)
            if item_id:
                found[item_id] = _status_text(status)
        return found
    if isinstance(report, Sequence) and not isinstance(report, (str, bytes)):
        for item in report:
            if isinstance(item, Mapping):
                found.update(_report_items(item))
            else:
                found[_item_id(item)] = "confirmed"
        return found
    return found


def _item_id(value: object) -> str:
    match = re.search(r"([MN])\s*(\d{1,3})", str(value or ""), re.IGNORECASE)
    if match is None:
        return ""
    return f"{match.group(1).upper()}{int(match.group(2))}"


def _scan_text(report: str, ignore: frozenset[str] = frozenset()) -> dict[str, str]:
    """Item ids the prose mentions, the last mention of an item winning.

    The final report comes last, so the tail is read. Escaped newlines are
    decoded first: a stream-json log keeps the final message in one JSON
    string. ``ignore`` holds the ruling's own item texts, so an echoed prompt
    line (``- [M1] <item text>``) neither confirms nor refutes the item.
    """
    found: dict[str, str] = {}
    text = report[-MAX_REPORT_CHARS:]
    text = (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
    # A completion report's verification rows inside the log (R5 L1 JSON that
    # is not the whole text: a tail with other output, a stream-json event).
    rows = [
        (m.start(), f"{m.group(1).upper()}{int(m.group(2))}", m.group(3))
        for m in _COMPLETION_ROW.finditer(text)
    ]
    for _pos, item_id, status in rows:
        if _PLACEHOLDER.search(status):
            continue
        found[item_id] = "confirmed" if _body_confirms(status) else "missing"
    for raw in text.splitlines():
        match = _ITEM_LINE.match(raw)
        hits = (
            [(match.group(1), match.group(2))]
            if match is not None
            else [(m.group(1), m.group(2)) for m in _RULING_MENTION.finditer(raw)]
        )
        for raw_id, raw_body in hits:
            item_id = raw_id.replace(" ", "").upper()
            body = raw_body.strip()
            normal = " ".join(body.split())
            if _PLACEHOLDER.search(body) or normal in ignore:
                continue
            if len(normal) >= 16 and any(item.startswith(normal) for item in ignore):
                # A truncated echo of an item's own text.
                continue
            # An item line is a confirmation only when it says so. "M1: added
            # the test" names work but never affirms the item, so it stays missing.
            found[item_id] = "confirmed" if _body_confirms(body) else "missing"
    return found


def _completion_items(report: str) -> dict[str, str] | None:
    """Item statuses from an R5 L1 completion report, or None when it is not one.

    A native Worker's final message may be exactly one completion-report JSON
    object (``src.server.services.autonomy.completion``), which has no room for
    free prose lines. Its ``verification`` rows name the item in ``ran`` and the
    status in ``result``; its ``summary``, ``next`` and ``handbacks`` are read
    as prose for ``RULING M1: ...`` lines.
    """
    parsed = completion_report_from_text(report.strip())
    if parsed is None:
        return None
    found: dict[str, str] = {}
    for text in (parsed.summary, parsed.next, *parsed.handbacks):
        found.update(_scan_text(text))
    for row in parsed.verification:
        match = _COMPLETION_ITEM.match(row.ran)
        if match is None:
            continue
        found[f"{match.group(1).upper()}{int(match.group(2))}"] = row.result
    return found


def check_confirmation(
    report: object,
    ruling: Ruling | RulingInput | Mapping[str, Any],
) -> RulingCheck:
    """Check that the Worker's report confirms each ruling item individually.

    A structured report is read first; prose is read for ``M1``/``N1`` lines.
    An item whose line is present but negative is missing, and an item that is
    never mentioned is missing. The check is deliberately strict: a generic
    "done" is not a confirmation of any item.
    """
    # ``check_confirmation(ruling, report)`` is easy to type; accept it too.
    if isinstance(report, (Ruling, RulingInput)) and not isinstance(
        ruling, (Ruling, RulingInput)
    ):
        report, ruling = ruling, report
    parsed = (
        ruling
        if isinstance(ruling, Ruling)
        else (
            Ruling.from_dict(ruling)
            if isinstance(ruling, Mapping)
            else Ruling(
                line_id=ruling.line_id,
                fix_round=ruling.fix_round,
                must=ruling.must,
                must_not=ruling.must_not,
                examples=ruling.examples,
                summary=ruling.summary,
            )
        )
    )
    if parsed is None:
        return RulingCheck()

    echoes = frozenset(" ".join(item.text.split()) for item in parsed.items)
    found = _report_items(report)
    if isinstance(report, str):
        found.update(_completion_items(report) or {})
        prose = _scan_text(report, echoes)
        found.update({key: value for key, value in prose.items() if key not in found})
    elif not found:
        found.update(_scan_text(str(report or ""), echoes))
    # A prose report that merely repeats an item's text is not a confirmation.
    # Only the item id plus an affirmative status counts.

    confirmed: list[str] = []
    missing: list[str] = []
    for item in parsed.items:
        status = found.get(item.id, "")
        normalized = str(status).strip().lower()
        if normalized in {"confirmed", "true", "1", "yes", "done", "pass", "passed"}:
            confirmed.append(item.id)
        elif _body_confirms(status):
            confirmed.append(item.id)
        else:
            missing.append(item.id)
    return RulingCheck(
        confirmed=tuple(confirmed),
        missing=tuple(missing),
        unconfirmed=tuple(missing),
    )


#: Common spellings at call sites.
check_report = check_confirmation
confirm_ruling = check_confirmation
validate_confirmation = check_confirmation
ruling_confirmed = check_confirmation
missing_confirmations = check_confirmation


def unconfirmed_blockers(
    ruling: Ruling | RulingInput | Mapping[str, Any],
    check: RulingCheck | Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """The blocker lines an incomplete check leaves for the next fix round.

    ``check`` may be persisted: a missing or malformed check is treated as
    "nothing confirmed", because an unverified contract must not be accepted.
    """
    parsed = (
        ruling
        if isinstance(ruling, Ruling)
        else (
            Ruling.from_dict(ruling)
            if isinstance(ruling, Mapping)
            else Ruling(
                line_id=ruling.line_id,
                fix_round=ruling.fix_round,
                must=ruling.must,
                must_not=ruling.must_not,
                examples=ruling.examples,
                summary=ruling.summary,
            )
        )
    )
    if parsed is None:
        return ()
    if isinstance(check, Mapping):
        check = RulingCheck.from_dict(check)
    remaining = list(check.missing or check.unconfirmed) if check is not None else []
    if not remaining:
        remaining = [item.id for item in parsed.items]
    by_id = {item.id: item for item in parsed.items}
    lines: list[str] = []
    for item_id in remaining:
        item = by_id.get(item_id)
        if item is None:
            lines.append(f"RULING {item_id}: not confirmed")
        else:
            lines.append(f"RULING {item.id}: not confirmed - {item.text}")
    return tuple(lines)


_UNCONFIRMED_LINE = re.compile(r"^RULING [MN]\d{1,3}: not confirmed(?: - |$)")


def is_unconfirmed_blocker(text: object) -> bool:
    """True for a blocker line :func:`unconfirmed_blockers` wrote.

    Such a BLOCK comes from the ruling check, not from a reviewer: the next
    round re-issues the same ruling instead of wrapping these lines into a new
    one, and it is not a reviewer's failed round for escalation.
    """
    return bool(_UNCONFIRMED_LINE.match(str(text or "")))


def only_unconfirmed(blockers: Sequence[str] | None) -> bool:
    items = [item for item in blockers or () if str(item).strip()]
    return bool(items) and all(is_unconfirmed_blocker(item) for item in items)


def reissue_ruling(
    previous: Ruling,
    *,
    fix_round: int,
    escalation: object = None,
) -> Ruling:
    """The previous ruling's items again, for a round whose report missed them."""
    return build_ruling(
        previous.line_id,
        previous.must_texts,
        fix_round=fix_round,
        must_not=tuple(item.text for item in previous.must_not),
        examples=previous.examples,
        escalation=escalation,
        summary=previous.summary,
    )


def check_ruling(
    report: object,
    ruling: Ruling | RulingInput | Mapping[str, Any],
) -> RulingCheck:
    """A typed alias for :func:`check_confirmation`."""
    return check_confirmation(report, ruling)


__all__ = [
    "DEFAULT_MUST_NOT",
    "ESCALATION_MUST",
    "ESCALATION_MUST_NOT",
    "FIX_ROUND_INPUT",
    "FIX_RULING",
    "FixRoundInput",
    "FixRoundRuling",
    "MUST",
    "MUST_NOT",
    "RULING_CHECK",
    "USER_ROLE",
    "Ruling",
    "RulingCheck",
    "RulingInput",
    "RulingItem",
    "build_fix_input",
    "build_fix_round_input",
    "build_ruling",
    "check_confirmation",
    "check_report",
    "check_ruling",
    "confirm_ruling",
    "deliver_fix_input",
    "deliver_ruling",
    "fix_round_ruling",
    "is_unconfirmed_blocker",
    "only_unconfirmed",
    "reissue_ruling",
    "missing_confirmations",
    "render_ruling",
    "ruling_confirmed",
    "ruling_for_fix",
    "ruling_from_dict",
    "ruling_from_snapshot",
    "ruling_input_from_snapshot",
    "unconfirmed_blockers",
    "validate_confirmation",
]
