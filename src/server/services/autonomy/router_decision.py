"""The Router's deterministic next-action table for a settled Worker report.

The completion contract gives a Worker three independent signals: the status it
claims (``done``/``partial``/``blocked``/``needs_review``/``needs_split``), the
system's delivery verdict, and a separate attention reason. The Router must not
re-argue those signals with a model. :func:`decide_next_action` reduces them to
one :class:`RouterDecision` through one ordered table; the first matching row
wins.

Attention is deliberately separate from status: a Worker may still be working
and ask a person a question, and a ``needs_split`` request still opens child
work even when another row would ask a person. A *settled ready* report is the
one combination that cannot coexist with an open question, so that
contradiction raises instead of being silently repaired.

``decide_legacy_action`` adapts the older ``(state, attention_reason,
speculative)`` tuple used by ``host.worker_report_transition`` to this table's
input, then delegates to :func:`decide_next_action` so the action still comes
from one ordered table.

Pure and synchronous: no I/O, no database, no model call, no environment
lookup. Host code owns the Ticket write; this module owns only the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from src.server.services.autonomy.completion import (
    COMMIT_DELIVERY_VERDICTS,
    COMPLETION_NO_CHANGE,
    COMPLETION_REPORT_MISSING,
    COMPLETION_REPORT_STATUSES,
    COMPLETION_STATUS_TO_TICKET_STATE,
    COMPLETION_UNVERIFIED,
    SPECULATIVE_STATUS_TO_TICKET_STATE,
)

#: ``uncommitted_changes`` and ``git_status_unavailable`` are facts about the
#: workspace: they veto a delivery and need a person to clean up or restore
#: access before the Ticket can finish.
GIT_ATTENTION_REASONS = frozenset({"uncommitted_changes", "git_status_unavailable"})
#: These mean the run has no valid completion report, made no change, or could
#: not have delivery verified. None of them is success; a person reviews the run.
COMPLETION_ATTENTION_REASONS = frozenset(
    {
        COMPLETION_REPORT_MISSING,
        COMPLETION_NO_CHANGE,
        COMPLETION_UNVERIFIED,
    }
)
ATTENTION_REASONS = GIT_ATTENTION_REASONS | COMPLETION_ATTENTION_REASONS

#: A caller may pass a free-form question through ``attention``. It is not an
#: attention reason, so it stays a distinct marker: only an open question makes
#: a ready report contradictory.
OPEN_QUESTION = "open_question"

COMPLETION_STATUSES = frozenset(COMPLETION_REPORT_STATUSES)
SYSTEM_CHECK_VERDICTS = COMMIT_DELIVERY_VERDICTS
#: The states ``settle_native_completion`` can settle a Ticket to, plus the
#: legacy report states that reach the same table.
SETTLED_STATES = frozenset({"ready", "blocked", "waiting", "failed", "dropped"})


class Action(StrEnum):
    """What the Router does after a Worker report settles."""

    DELIVER_TO_USER = "deliver_to_user"
    SEND_TO_REVIEW = "send_to_review"
    OPEN_CHILD_TICKET = "open_child_ticket"
    REDISPATCH = "redispatch"
    ASK_HUMAN = "ask_human"

    # Lower-case aliases keep both spellings of the wire vocabulary available
    # on the enum itself; iterating the enum still yields five unique actions.
    deliver_to_user = DELIVER_TO_USER
    send_to_review = SEND_TO_REVIEW
    open_child_ticket = OPEN_CHILD_TICKET
    redispatch = REDISPATCH
    ask_human = ASK_HUMAN


#: Module-level spellings for callers that do not want to reach through the
#: enum. Both the wire values and upper-case names are safe to import.
DELIVER_TO_USER = Action.DELIVER_TO_USER
SEND_TO_REVIEW = Action.SEND_TO_REVIEW
OPEN_CHILD_TICKET = Action.OPEN_CHILD_TICKET
REDISPATCH = Action.REDISPATCH
ASK_HUMAN = Action.ASK_HUMAN

deliver_to_user = Action.DELIVER_TO_USER
send_to_review = Action.SEND_TO_REVIEW
open_child_ticket = Action.OPEN_CHILD_TICKET
redispatch = Action.REDISPATCH
ask_human = Action.ASK_HUMAN

#: The earlier table named its action enum ``NextAction``; keep the alias so
#: callers can migrate without a second vocabulary.
NextAction = Action
ACTIONS = frozenset(Action)


@dataclass(frozen=True, slots=True, eq=False)
class RouterDecision:
    """One table result: the action plus the inputs and row that produced it."""

    action: Action
    rule: str = ""
    state: str | None = None
    status: str | None = None
    system_check: str | None = None
    attention: str | None = None
    speculative: bool = False
    open_question: bool = False

    @property
    def next_action(self) -> Action:
        """Backwards-friendly alias for the chosen action."""
        return self.action

    @property
    def reason(self) -> str:
        """The name of the table row that matched."""
        return self.rule

    @property
    def value(self) -> str:
        """The wire spelling of the action."""
        return self.action.value

    def __str__(self) -> str:
        return self.action.value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, RouterDecision):
            return (
                self.action == other.action
                and self.rule == other.rule
                and self.state == other.state
                and self.status == other.status
                and self.system_check == other.system_check
                and self.attention == other.attention
                and self.speculative == other.speculative
                and self.open_question == other.open_question
            )
        if isinstance(other, (Action, str)):
            return self.action == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(
            (
                self.action,
                self.rule,
                self.state,
                self.status,
                self.system_check,
                self.attention,
                self.speculative,
                self.open_question,
            )
        )


@dataclass(frozen=True, slots=True)
class DecisionRule:
    """One ordered table row; a field left ``None`` does not constrain it."""

    name: str
    action: Action
    states: frozenset[str | None] | None = None
    statuses: frozenset[str | None] | None = None
    system_checks: frozenset[str | None] | None = None
    attentions: frozenset[str | None] | None = None
    speculative: bool | None = None
    open_question: bool | None = None

    def matches(
        self,
        *,
        status: str | None = None,
        system_check: str | None = None,
        attention: str | None = None,
        speculative: bool = False,
        open_question: bool = False,
        state: str | None = None,
        completion_status: str | None = None,
        reconciled_state: str | None = None,
    ) -> bool:
        # The completion-contract spellings are accepted here as well so the
        # table remains inspectable by callers written against the earlier
        # ``(completion_status, reconciled_state)`` shape.
        if status is None:
            status = completion_status
        if state is None:
            state = reconciled_state
        if state is None:
            state = _decision_state(
                status=status,
                system_check=system_check,
                attention=attention,
                speculative=speculative,
            )
        return (
            (self.states is None or state in self.states)
            and (self.statuses is None or status in self.statuses)
            and (self.system_checks is None or system_check in self.system_checks)
            and (self.attentions is None or attention in self.attentions)
            and (self.speculative is None or self.speculative is speculative)
            and (self.open_question is None or self.open_question is open_question)
        )


#: Read top to bottom; the first matching row wins. Split requests are first so
#: an explicit request for child work is never swallowed by an attention or
#: question row. The remaining rows are keyed by the settled state, which the
#: system-check verdict and the separate attention reason have already adjusted.
DECISION_TABLE: tuple[DecisionRule, ...] = (
    DecisionRule(
        "needs_split",
        Action.OPEN_CHILD_TICKET,
        statuses=frozenset({"needs_split"}),
    ),
    DecisionRule(
        "open_question",
        Action.ASK_HUMAN,
        open_question=True,
    ),
    DecisionRule(
        "failed",
        Action.REDISPATCH,
        states=frozenset({"failed"}),
    ),
    DecisionRule(
        "blocked",
        Action.ASK_HUMAN,
        states=frozenset({"blocked"}),
    ),
    DecisionRule(
        "waiting",
        Action.SEND_TO_REVIEW,
        states=frozenset({"waiting"}),
    ),
    DecisionRule(
        "ready",
        Action.DELIVER_TO_USER,
        states=frozenset({"ready"}),
    ),
    DecisionRule(
        "dropped",
        Action.DELIVER_TO_USER,
        states=frozenset({"dropped"}),
    ),
    DecisionRule(
        "git_attention_unsettled",
        Action.ASK_HUMAN,
        states=frozenset({None}),
        attentions=GIT_ATTENTION_REASONS,
    ),
    DecisionRule(
        "completion_attention_unsettled",
        Action.SEND_TO_REVIEW,
        states=frozenset({None}),
        attentions=COMPLETION_ATTENTION_REASONS,
    ),
    DecisionRule(
        "fallback",
        Action.ASK_HUMAN,
    ),
)

#: Stable alias for callers that prefer the longer name.
ROUTER_DECISION_TABLE = DECISION_TABLE
TABLE = DECISION_TABLE


def _normalise_status(status: str | None) -> str | None:
    if status is None:
        return None
    if not isinstance(status, str):
        raise ValueError(f"unknown completion status: {status!r}")
    value = status.strip().lower()
    if value in COMPLETION_STATUSES or value in SETTLED_STATES:
        return value
    raise ValueError(f"unknown completion status: {status!r}")


def _normalise_attention(attention: object) -> str | None:
    """Turn a reason, question object, or question text into one marker."""
    if attention is None or attention is False:
        return None
    if attention is True:
        return OPEN_QUESTION
    if isinstance(attention, str):
        value = attention.strip()
        if not value:
            return None
        return value
    if isinstance(attention, dict):
        question = attention.get("question") or attention.get("reason")
        return _normalise_attention(question)
    question = getattr(attention, "question", None)
    if question:
        return _normalise_attention(question)
    raise ValueError(f"unknown attention reason: {attention!r}")


def _normalise_system_check(system_check: object) -> tuple[str | None, str | None]:
    """Return ``(verdict, state_override)`` for the system check."""
    if system_check is None:
        return "not_checked", None
    if isinstance(system_check, bool):
        return ("delivered" if system_check else "missing"), None
    if isinstance(system_check, str):
        value = system_check.strip().lower()
        if value in SYSTEM_CHECK_VERDICTS:
            return value, None
        if value in SETTLED_STATES:
            return None, value
        raise ValueError(f"unknown system-check verdict: {system_check!r}")
    if isinstance(system_check, dict):
        state = system_check.get("state")
        verdict = system_check.get("verdict") or system_check.get("commit_delivery")
        if verdict is not None:
            normalised_verdict, _ = _normalise_system_check(verdict)
            return normalised_verdict, state if state in SETTLED_STATES else None
        if state in SETTLED_STATES:
            return None, state
        raise ValueError(f"unknown system-check verdict: {system_check!r}")
    state = getattr(system_check, "state", None)
    if state in SETTLED_STATES:
        return None, state
    raise ValueError(f"unknown system-check verdict: {system_check!r}")


def _is_open_question(attention: str | None, explicit: bool) -> bool:
    if explicit:
        return True
    if attention is None:
        return False
    return attention == OPEN_QUESTION or attention not in ATTENTION_REASONS


def _speculative_ticket_state(state: str | None) -> str | None:
    """Apply the one speculative-plan rule: never settle ``ready`` or ``blocked``.

    A plan waits for a person to Retry. ``dropped`` is kept, because
    :data:`SPECULATIVE_STATUS_TO_TICKET_STATE` sends ``done`` there ("nothing
    needs doing") and the dropped row delivers that quiet outcome. ``failed``
    is kept so the run can be redispatched. Rewriting ``dropped`` to
    ``waiting`` would contradict that map; leaving a speculative ``ready`` in
    place would deliver work a plan is not allowed to finish.
    """
    if state is None or state in {"failed", "dropped"}:
        return state
    return "waiting"


def _decision_state(
    *,
    status: str | None,
    system_check: str | None,
    attention: str | None,
    speculative: bool,
) -> str | None:
    """Settle the raw inputs into the state whose row decides the action."""
    normalised_status = _normalise_status(status)
    verdict, override = _normalise_system_check(system_check)
    normalised_attention = _normalise_attention(attention)

    if normalised_status is None:
        state = override
    elif normalised_status in COMPLETION_STATUSES:
        if speculative:
            state = SPECULATIVE_STATUS_TO_TICKET_STATE[normalised_status]
        else:
            state = COMPLETION_STATUS_TO_TICKET_STATE[normalised_status]
    elif normalised_status in SETTLED_STATES:
        state = normalised_status
    else:
        state = None

    if override is not None:
        state = override
    elif not speculative and verdict is not None:
        if verdict in {"missing", "unknown"} and state == "ready":
            # A completion-check attention is the same check saying "review the
            # run": its missing commit is the fact being reported, not a
            # contradicted delivery, so it must not escalate the claim to
            # blocked. Git/workspace attentions still veto the ready claim.
            state = (
                "waiting"
                if normalised_attention in COMPLETION_ATTENTION_REASONS
                else "blocked"
            )
        # ``unverified`` is an absence of proof, not a refutation: a done report
        # that lists commits still settles ``ready`` with no attention reason.
        # The bare-done case is already settled as ``waiting`` with the
        # ``completion_unverified`` attention, and the completion-attention
        # branch below routes it to review. Downgrading every ready claim here
        # discarded the report's commits and contradicted the settlement.

    if normalised_attention in GIT_ATTENTION_REASONS:
        if state in {"ready", "dropped"}:
            state = "waiting" if speculative else "blocked"
    elif normalised_attention in COMPLETION_ATTENTION_REASONS:
        if state == "ready":
            state = "waiting"
        elif state == "dropped" and not speculative:
            # A completion-check flag describes a waiting run, not a quiet
            # discard: a non-speculative drop still delivers.
            state = "dropped"
    if speculative:
        # After attention, so a git veto can still move a quiet drop to
        # waiting, and so an override cannot put a plan back on ``ready``.
        state = _speculative_ticket_state(state)
    return state


def decide_next_action(
    *,
    status: str | None = None,
    system_check: str | None = None,
    attention: str | None = None,
    speculative: bool = False,
    open_question: bool = False,
    completion_status: str | None = None,
    reconciled_state: str | None = None,
) -> RouterDecision:
    """Return the one next action for a Worker report.

    ``status`` is the Worker's claimed completion status. ``system_check`` is
    the delivery verdict from ``completion.COMMIT_DELIVERY_VERDICTS`` (a settled
    state or ``ReconciledCompletion`` is also accepted for legacy callers).
    ``attention`` is the separate attention reason. ``speculative`` marks a
    plan run. A plan never settles ``ready`` or ``blocked``; those wait for
    a person to Retry. Speculative ``done`` is not that case: the completion
    map settles it as ``dropped`` ("nothing needs doing"), and the dropped
    row delivers. Delivery here does not require, and must not produce, a
    ready settlement.

    A settled ``ready`` report is the one state that cannot carry an open
    question: ``ready`` means nothing is left for a person to answer, so that
    contradiction raises ``ValueError`` instead of choosing an action for an
    impossible report. Unknown status and system-check vocabulary also raise.
    """
    if status is None:
        status = completion_status if completion_status is not None else reconciled_state
    if reconciled_state is not None and system_check is None:
        system_check = reconciled_state
    normalised_status = _normalise_status(status)
    verdict, _override = _normalise_system_check(system_check)
    normalised_attention = _normalise_attention(attention)
    is_question = _is_open_question(normalised_attention, open_question)
    state = _decision_state(
        status=normalised_status,
        system_check=system_check,
        attention=normalised_attention,
        speculative=speculative,
    )

    # A settled ready report and an open question are the one contradiction:
    # ``ready`` means nothing is left for a person to answer. An attention
    # reason is not a question, so e.g. ``uncommitted_changes`` blocks a ready
    # claim instead of raising. The guard reads the settled state, so every
    # alias (``status``, ``completion_status``, ``reconciled_state``) takes it.
    if state == "ready" and is_question:
        raise ValueError("a ready settlement cannot carry an open question")

    for rule in DECISION_TABLE:
        if rule.matches(
            status=normalised_status,
            system_check=verdict,
            attention=normalised_attention,
            speculative=speculative,
            open_question=is_question,
            state=state,
        ):
            return RouterDecision(
                action=rule.action,
                rule=rule.name,
                state=state,
                status=normalised_status,
                system_check=verdict,
                attention=normalised_attention,
                speculative=speculative,
                open_question=is_question,
            )
    # DECISION_TABLE ends with an unconstrained fallback row.
    raise AssertionError("router decision table has no matching row")


#: The legacy states whose completion-check attention describes the run, not
#: the outcome: the report already settles ``ready``/``dropped``, so the flag
#: must not move the table row. Git facts still veto these states.
_LEGACY_READY_STATES = frozenset({"ready", "dropped"})


def _legacy_table_input(
    state: str,
    reason: str | None,
    speculative: bool,
) -> tuple[str, str | None]:
    """Translate the settled legacy tuple into the table's input.

    The caller already carries the settled state, so the value is passed
    through. A non-speculative ``ready``/``dropped`` report ignores a
    completion-check attention because the flag describes the check, not the
    outcome; Git attentions still veto those states inside
    :func:`_decision_state`.

    Speculative ``ready``/``blocked`` is not rewritten here. That rule lives
    only in :func:`_speculative_ticket_state`, which :func:`_decision_state`
    applies for every caller. A second copy that turned speculative ``ready``
    into ``waiting`` before the table ran made a speculative ``done`` claim
    look as if it had to settle ``ready`` in order to be delivered. ``done``
    settles ``dropped`` and the dropped row delivers; this adapter must not
    invent a different state for it.
    """
    table_attention = reason
    if (
        not speculative
        and state in _LEGACY_READY_STATES
        and reason not in GIT_ATTENTION_REASONS
    ):
        table_attention = None
    return state, table_attention


def decide_legacy_action(
    state: str,
    attention_reason: str | None = None,
    speculative: bool = False,
) -> RouterDecision:
    """Map the legacy ``(state, attention_reason, speculative)`` tuple.

    ``host.worker_report_transition`` predates the completion contract and
    carries the settled state directly. The adapter normalises that tuple into
    the table's input and delegates to :func:`decide_next_action`, so the
    action is chosen by :data:`DECISION_TABLE` rather than a second copy of the
    rules.
    """
    if (
        isinstance(state, tuple)
        and len(state) == 3
        and attention_reason is None
        and speculative is False
    ):
        state, attention_reason, speculative = state
    if state not in SETTLED_STATES:
        raise ValueError(f"unknown reconciled state: {state!r}")
    reason = _normalise_attention(attention_reason)
    if reason is not None and reason not in ATTENTION_REASONS:
        raise ValueError(f"unknown attention reason: {attention_reason!r}")

    table_state, table_attention = _legacy_table_input(state, reason, speculative)
    decision = decide_next_action(
        status=table_state,
        attention=table_attention,
        speculative=speculative,
    )
    # The adapter keeps the legacy tuple's descriptive fields; only the action,
    # rule and settled state come back from the table.
    return replace(
        decision,
        state=state,
        attention=reason,
        speculative=speculative,
    )


#: Short aliases kept for host call sites that read the legacy tuple directly.
legacy_decision = decide_legacy_action
decide_legacy = decide_legacy_action


def legacy_next_action(
    state: str,
    attention_reason: str | None = None,
    speculative: bool = False,
) -> Action:
    """Return only the legacy tuple's action (the common host call)."""
    return decide_legacy_action(state, attention_reason, speculative).action


action_for_legacy_report = legacy_next_action
legacy_action = legacy_next_action
legacy_report_action = legacy_next_action
