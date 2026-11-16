"""The human gate: nothing merges or pushes until a person says so.

The pending dispatch is written under ``<state>/proposed/auto-dispatch/<id>/``
(see :func:`default_proposed_dir`) — the
same approval-first lifecycle the memory work uses for ``proposed/*`` rows —
so the decision survives the process that asked for it and can be made from
another machine, the CLI or the API.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from .gates_schema import is_gates_snapshot
from .protocols import ApprovalDecision
from .types import Dispatch, SplitOffOutcome

logger = logging.getLogger(__name__)

PROPOSED_DIR_ENV = "CONDUCTOR_DISPATCH_PROPOSED_DIR"
TokenFactory = Callable[[], str]


def conductor_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """``CONDUCTOR_HOME`` if set, else ``${XDG_STATE_HOME:-~/.local/state}/conductor``.

    Read at call time so a changed environment (tests, ``cond``) is honoured.
    """
    source = os.environ if env is None else env
    home = (source.get("CONDUCTOR_HOME") or "").strip()
    if home:
        return Path(home).expanduser()
    state = (source.get("XDG_STATE_HOME") or "").strip()
    if state:
        return Path(state).expanduser() / "conductor"
    user_home = (source.get("HOME") or "").strip()
    base = Path(user_home).expanduser() if user_home else Path.home()
    return base / ".local" / "state" / "conductor"


def default_proposed_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where pending dispatch summaries live: never the process's working directory.

    ``CONDUCTOR_DISPATCH_PROPOSED_DIR`` wins; otherwise
    ``conductor_state_dir()/proposed/auto-dispatch``.
    """
    source = os.environ if env is None else env
    override = (source.get(PROPOSED_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return conductor_state_dir(source) / "proposed" / "auto-dispatch"


class FileApprovalGate:
    """A file-backed approval gate rooted at a directory of pending dispatches."""

    def __init__(self, root: Path | None = None, *, token_factory: TokenFactory | None = None) -> None:
        self._root = Path(root) if root else default_proposed_dir()
        self._token_factory = token_factory or (lambda: uuid.uuid4().hex[:12])

    @property
    def root(self) -> Path:
        return self._root

    def dispatch_dir(self, dispatch_id: str) -> Path:
        return self._root / dispatch_id

    def summary_path(self, dispatch_id: str) -> Path:
        return self.dispatch_dir(dispatch_id) / "summary.md"

    async def request_approval(self, dispatch: Dispatch) -> str:
        token = self._token_factory()
        directory = self.dispatch_dir(dispatch.id)
        directory.mkdir(parents=True, exist_ok=True)
        self._write(directory / "summary.md", _summary(dispatch))
        self._write(
            directory / "gate.json",
            json.dumps({"token": token, "state": "PENDING", "note": ""}, indent=2),
        )
        index = self._read_index()
        index[token] = dispatch.id
        self._write(self._root / "_index.json", json.dumps(index, indent=2))
        return token

    async def decision(self, token: str) -> ApprovalDecision:
        directory = self.dispatch_dir(self._dispatch_for(token))
        return self._state(directory)

    async def approver(self, token: str) -> str:
        """The note recorded with the decision (who approved), or ``""``."""
        directory = self.dispatch_dir(self._dispatch_for(token))
        return str(self._read_gate(directory).get("note") or "")

    async def resolve(self, token: str, approved: bool, note: str = "") -> ApprovalDecision:
        if approved and str(note or "").strip().lower().startswith("auto:"):
            # Review fix 1: an automatic approver never opens the gate; push
            # and PR need a human (``auto:n73`` only marks ``auto_ready``).
            raise ValueError("an automatic approver cannot approve the human gate")
        dispatch_id = self._dispatch_for(token)
        return await self._resolve(self.dispatch_dir(dispatch_id), token, approved, note)

    async def resolve_dispatch(self, dispatch_id: str, approved: bool, note: str = "") -> ApprovalDecision:
        """Resolve from the dispatch id alone, which is what the API and CLI hold."""
        directory = self.dispatch_dir(dispatch_id)
        state = self._state(directory)
        if state is ApprovalDecision.PENDING:
            # A dispatch that never reached ``request_approval`` has no token
            # yet; mint one into its own directory rather than resolving the
            # empty token, which would land the gate file in the root.
            gate = self._read_gate(directory)
            token = str(gate.get("token") or self._token_factory())
            state = await self._resolve(directory, token, approved, note)
        return state

    async def pending_dispatch_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self._root.iterdir()
            if entry.is_dir() and self._state(entry) is ApprovalDecision.PENDING
        )

    def _dispatch_for(self, token: str) -> str:
        if not token:
            return ""
        dispatch_id = self._read_index().get(token)
        if dispatch_id is None:
            raise KeyError("Unknown approval token")
        return dispatch_id

    def _state(self, directory: Path) -> ApprovalDecision:
        return ApprovalDecision(self._read_gate(directory).get("state", "PENDING"))

    async def _resolve(
        self, directory: Path, token: str, approved: bool, note: str
    ) -> ApprovalDecision:
        dispatch_id = directory.name
        state = "APPROVED" if approved else "REJECTED"
        self._write(
            directory / "gate.json",
            json.dumps({"token": token, "state": state, "note": note}, indent=2),
        )
        index = self._read_index()
        if token:
            index[token] = dispatch_id
            self._write(self._root / "_index.json", json.dumps(index, indent=2))
        logger.info("Dispatch %s %s by a human", dispatch_id, state.lower())
        return ApprovalDecision(state)

    def _read_gate(self, directory: Path) -> dict:
        try:
            payload = json.loads((directory / "gate.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"state": "PENDING"}
        return payload if isinstance(payload, dict) else {"state": "PENDING"}

    def _read_index(self) -> dict[str, str]:
        try:
            payload = json.loads((self._root / "_index.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(k): str(v) for k, v in payload.items()} if isinstance(payload, dict) else {}

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)


SPLIT_OFF = "split_off"


def split_off_lines(dispatch: Dispatch) -> dict[str, dict]:
    """The lines R-SPLIT removed from this round, keyed by line id.

    The state machine writes them under ``outcomes["split_off"]`` (a
    :class:`SplitOffOutcome`). A dispatch reloaded from an older row may only
    carry the line's ``failure_reason``, so both shapes are read here.
    """
    found: dict[str, dict] = {}
    raw = dispatch.outcomes.get(SPLIT_OFF)
    if isinstance(raw, SplitOffOutcome):
        found.update(raw.to_dict())
    elif isinstance(raw, dict):
        found.update(
            {
                str(line_id): dict(details)
                for line_id, details in raw.items()
                if isinstance(details, dict)
            }
        )
    for line in dispatch.lines:
        if line.failure_reason == SPLIT_OFF:
            found.setdefault(line.id, {"line_id": line.id, "reason": SPLIT_OFF})
    return found


def has_split_off(dispatch: Dispatch) -> bool:
    """True when this round left at least one line out of integration."""
    return bool(split_off_lines(dispatch))


def auto_approve_allowed(dispatch: Dispatch) -> bool:
    """A dispatch that split lines off is never approved without a person.

    N73's low-risk auto-approval may skip the human gate for an ordinary
    round. A partial integration is exactly the case a person must see: the
    follow-up drafts are the work this round did not do.
    """
    return not has_split_off(dispatch)


#: Older spellings a caller is likely to reach for.
can_auto_approve = auto_approve_allowed
requires_human = has_split_off


def _summary(dispatch: Dispatch) -> str:
    lines = [
        f"# Auto-dispatch {dispatch.id}",
        "",
        f"**Goal:** {dispatch.goal}",
        "",
        f"**Integrate branch:** `{dispatch.integrate_branch}`",
        "",
        "## Lines",
        "",
        "| Line | Task | Model | Attempts |",
        "|---|---|---|---|",
    ]
    for line in dispatch.lines:
        lines.append(f"| {line.id} | {line.task} | {line.assigned_model or '—'} | {line.attempts} |")
    split = split_off_lines(dispatch)
    if split:
        lines.extend(
            [
                "",
                "## Split off",
                "",
                f"This round left {len(split)} line(s) out of integration. A person "
                "must decide whether to start the follow-up drafts; the remaining "
                "lines are reviewed and integrated normally.",
                "",
                "| Line | Follow-up |",
                "|---|---|",
            ]
        )
        for line_id, details in sorted(split.items()):
            followup = details.get("followup") or "—"
            lines.append(f"| {line_id} | `{followup}` |")
    if _is_gated(dispatch):
        lines.extend(
            [
                "",
                "## Gates",
                "",
                "| Line | G2 | Primary | Secondary | G5 |",
                "|---|---|---|---|---|",
            ]
        )
        for line in dispatch.lines:
            lines.append(
                "| {line_id} | {g2} | {primary} | {secondary} | {g5} |".format(
                    line_id=line.id,
                    g2=_gate_cell(dispatch, line.id, "G2"),
                    primary=_gate_cell(dispatch, line.id, "primary_review"),
                    secondary=_gate_cell(dispatch, line.id, "secondary_review"),
                    g5=_gate_cell(dispatch, line.id, "G5"),
                )
            )
        decision = (
            "approve — the lines are already merged into the integrate branch and G5 passed"
        )
    else:
        decision = "approve — merge every line into the integrate branch"
    if split:
        decision += (
            f" (human only: {len(split)} line(s) split off; auto-approval is disabled)"
        )
    lines.extend(["", "## Decisions", "", f"- [ ] {decision}", ""])
    return "\n".join(lines)


def _is_gated(dispatch: Dispatch) -> bool:
    return is_gates_snapshot(dispatch.gates)


def _gate_cell(dispatch: Dispatch, line_id: str, name: str) -> str:
    lines = dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
    snap = lines.get(line_id) if isinstance(lines, dict) else None
    gates = snap.get("gates") if isinstance(snap, dict) else None
    cell = gates.get(name) if isinstance(gates, dict) else None
    if not isinstance(cell, dict):
        return "—"
    result = cell.get("result") or "—"
    reason = cell.get("reason")
    if reason:
        return f"{result} ({reason})"
    return str(result)


# --- N73 S7: low-risk auto-approval facts ---------------------------------------

_PRIMARY = "primary_review"
_SECONDARY = "secondary_review"
AUTO_APPROVE_AUDIT = "auto_approve"


def _snap(dispatch: Dispatch, line_id: str) -> dict:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    lines = gates.get("lines") if isinstance(gates.get("lines"), dict) else {}
    snap = lines.get(line_id)
    return snap if isinstance(snap, dict) else {}


def _final(history: list, gate: str) -> str | None:
    results = [
        str(entry.get("result") or "")
        for entry in history
        if isinstance(entry, dict) and entry.get("gate") == gate
    ]
    return results[-1] if results else None


def line_approval_facts(dispatch: Dispatch) -> list:
    """``auto_merge.LineApprovalFacts`` for each live line, from its gate snapshot."""
    from .auto_merge import LineApprovalFacts
    from .types import LineStatus

    facts = []
    for line in dispatch.lines:
        if line.status is LineStatus.FAILED:
            continue
        snap = _snap(dispatch, line.id)
        cells = snap.get("gates") if isinstance(snap.get("gates"), dict) else {}
        g2 = cells.get("G2") if isinstance(cells.get("G2"), dict) else {}
        handoff = cells.get("handoff") if isinstance(cells.get("handoff"), dict) else {}
        history = snap.get("history") if isinstance(snap.get("history"), list) else []
        machine = snap.get("machine") if isinstance(snap.get("machine"), dict) else {}
        risk = bool(snap.get("risk_bearing") or machine.get("risk_bearing"))
        reviews = [
            verdict
            for verdict in (_final(history, _PRIMARY), _final(history, _SECONDARY))
            if verdict is not None
        ]
        files = handoff.get("files") if isinstance(handoff.get("files"), list) else []
        facts.append(
            LineApprovalFacts(
                line_id=line.id,
                g2_passed=str(g2.get("result") or "") == "pass",
                reviews=tuple(reviews),
                second_review_required=risk,
                review_history=tuple(
                    str(entry.get("result") or "")
                    for entry in history
                    if isinstance(entry, dict)
                ),
                parked=bool(snap.get("parked_reason") or line.failure_reason),
                diff_files=tuple(str(item) for item in files),
                write_scope=tuple(line.write_scope),
                risk_bearing=risk,
            )
        )
    return facts


def g5_passed(dispatch: Dispatch) -> bool:
    """G5 passed on every live line (the integrate branch is one unit)."""
    from .types import LineStatus

    live = [line for line in dispatch.lines if line.status is not LineStatus.FAILED]
    if not live:
        return False
    for line in live:
        cells = _snap(dispatch, line.id).get("gates")
        g5 = cells.get("G5") if isinstance(cells, dict) else None
        if not isinstance(g5, dict) or g5.get("result") != "pass":
            return False
    return True


def review_unavailable_unblocked(dispatch: Dispatch) -> bool:
    """A human unblocked a ``review_unavailable`` park on this dispatch (PRD 5.3)."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    scheduler = gates.get("scheduler") if isinstance(gates.get("scheduler"), dict) else {}
    audit = scheduler.get("audit") if isinstance(scheduler.get("audit"), list) else []
    for entry in audit:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "")
        reason = str(entry.get("reason") or entry.get("from_reason") or "")
        if "unblock" in kind and "review_unavailable" in reason:
            return True
    return False


REASON_EVIDENCE_MISSING = "evidence_missing"
REASON_GATE_FINDING = "gate_finding"
AUTO_READY = "auto_ready"
AUTO_APPROVAL_KEY = "auto_approval"


def evidence_refusal(dispatch: Dispatch) -> str:
    """Why the evidence cannot carry an auto-approval, or ``""`` (review fix 3).

    Missing changed-file evidence (``handoff.files`` absent or empty, or the
    handoff says Git evidence was unavailable) and any scope, dirty-worktree
    or record-only gate finding keep the dispatch human: L3 made those checks
    record-only, so they must not also be waved through by auto-approval.
    """
    from .gates import shadow_findings
    from .types import LineStatus

    for line in dispatch.lines:
        if line.status is LineStatus.FAILED:
            continue
        snap = _snap(dispatch, line.id)
        cells = snap.get("gates") if isinstance(snap.get("gates"), dict) else {}
        handoff = cells.get("handoff") if isinstance(cells.get("handoff"), dict) else {}
        files = handoff.get("files")
        limits = str(handoff.get("known_limitations") or "")
        if not isinstance(files, list) or not files or "evidence is unavailable" in limits:
            return REASON_EVIDENCE_MISSING
        g2 = cells.get("G2") if isinstance(cells.get("G2"), dict) else {}
        if any(g2.get(key) for key in ("scope_conflict", "dirty_worktree")):
            return REASON_GATE_FINDING
        if shadow_findings(snap):
            return REASON_GATE_FINDING
    return ""


def is_auto_ready(dispatch: Dispatch) -> bool:
    """True when the integrate branch was marked ready by ``auto:n73`` only."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    record = gates.get(AUTO_APPROVAL_KEY)
    return isinstance(record, dict) and record.get("state") == AUTO_READY


def auto_approval(dispatch: Dispatch, *, environ=None):
    """The S7 decision for this dispatch (``auto_merge.AutoApproval``)."""
    from .auto_merge import AutoApproval, auto_approve_decision, auto_approve_enabled

    if auto_approve_enabled(environ=environ):
        refusal = evidence_refusal(dispatch)
        if refusal:
            return AutoApproval(False, refusal)
    return auto_approve_decision(
        line_approval_facts(dispatch),
        g5_passed=g5_passed(dispatch),
        review_unavailable_unblocked=review_unavailable_unblocked(dispatch),
        environ=environ,
    )
