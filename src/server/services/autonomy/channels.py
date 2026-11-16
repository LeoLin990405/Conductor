"""The explicit channel model: what a reply looks like on each channel.

Article §4.2 (Multi-Channel) is the requirement this module encodes:

    People communicate differently in group chats, email, and GitHub
    discussions. The agent needs to adjust both the level of detail and the
    way it responds to suit each channel.

    IM and other instant messaging channels: keep communication light and
    interrupt people as little as possible. Notify people only when a
    decision or sign-off is needed, ask only direct questions, and give short
    conclusions. A verified hard rule is a cap of 100 words per reply.

    Asynchronous channels such as GitHub, email, and documents: deliver
    structured, information-dense output — the full problem background, the
    change description and the verification results — and draft structured
    emails and documents that a person can review and send.

Before this module those rules lived in one ``if kind == ...`` chain inside
``replies.py``: Feishu was capped at 100 words, GitHub got a ``### Summary``
heading, email got ``Subject``/``Summary`` headers, and *every other* kind
was posted to GitHub as a comment. Limits, formatting and delivery rules were
implicit, per-adapter, and there was no email transport at all.

This module makes the model explicit and data-driven:

* :class:`ChannelSpec` names, for one channel, its family, its transport, its
  reply format, its limits, the sections it expects and its approval rules.
* :func:`resolve_channel` maps a Source kind (plus optional Source config) to
  exactly one spec, so every reply path asks the same question the same way.
* :func:`decide_delivery` answers the one question the norms imply: may this
  reply leave the system right now, does it need a human to approve it first,
  or should it not be delivered at all?
* :func:`shape_reply` renders the reply in the channel's own form.

Nothing here talks to the network; the transports live in sibling modules
(``feishu.py``, ``slack.py``, ``email.py``, ``github.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

# --- families ---------------------------------------------------------------
#: Instant messaging: light replies, people are interrupted rarely.
FAMILY_IM = "im"
#: Asynchronous and durable: structured, information-dense replies.
FAMILY_ASYNC = "async"
#: The Conductor inbox: recorded in-product, never delivered externally.
FAMILY_LOCAL = "local"

# --- transports -------------------------------------------------------------
TRANSPORT_FEISHU = "feishu"
TRANSPORT_SLACK = "slack"
TRANSPORT_EMAIL = "email"
TRANSPORT_GITHUB = "github"
TRANSPORT_LOCAL = "local"
#: Generic IM: the Source kind picks the concrete adapter.
TRANSPORT_IM = "im"
#: No adapter at all: the reply waits for a person.
TRANSPORT_NONE = "none"

# --- reply kinds ------------------------------------------------------------
#: The work finished; the reader may have to sign it off.
KIND_RESULT = "result"
#: A person has to answer before the work can continue.
KIND_QUESTION = "question"
#: The work failed or was blocked.
KIND_FAILURE = "failure"
#: A status update nobody asked for.
KIND_PROGRESS = "progress"
REPLY_KINDS = (KIND_RESULT, KIND_QUESTION, KIND_FAILURE, KIND_PROGRESS)

# --- channels ---------------------------------------------------------------
CHANNEL_CHAT = "chat"
CHANNEL_FEISHU = "feishu"
CHANNEL_SLACK = "slack"
CHANNEL_EMAIL = "email"
CHANNEL_GITHUB = "github"
CHANNEL_MANUAL = "manual"
CHANNEL_SCHEDULE = "schedule"
CHANNEL_UNKNOWN = "unknown"

#: The email sections, in the order they are rendered.
EMAIL_SECTION_ORDER = (
    "Summary",
    "Background",
    "Changes",
    "Verification",
    "Details",
    "Next steps",
)

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"
_SECTION_RE = re.compile(r"^([A-Z][A-Za-z ]{1,24}):\s*$")
_SUBJECT_RE = re.compile(r"^Subject:[ \t]*(.*)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ApprovalRules:
    """When a reply may leave the system without a human reading it first.

    ``require_approval`` is the channel default. ``approval_kinds`` are reply
    kinds that always need a human even when the default is "send". A Source
    may override ``require_approval`` in its own config.
    ``suppress_kinds`` are reply kinds that are never delivered on this
    channel: they are recorded, not sent.
    """

    require_approval: bool = False
    approval_kinds: frozenset[str] = frozenset()
    suppress_kinds: frozenset[str] = frozenset()
    reason: str = ""
    suppress_reason: str = ""


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    """Everything the system knows about one channel."""

    channel: str
    family: str
    label: str
    transport: str
    reply_format: str
    max_words: int | None
    max_chars: int
    required_sections: tuple[str, ...]
    recommended_sections: tuple[str, ...]
    heading: str
    default_subject: str
    approval: ApprovalRules
    norm: str
    instructions: str


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    """What to do with one reply on one channel.

    ``action`` is one of ``deliver``, ``hold`` (wait for a human to approve)
    or ``suppress`` (record it, deliver it nowhere).
    """

    action: str
    reason: str = ""


ACTION_DELIVER = "deliver"
ACTION_HOLD = "hold"
ACTION_SUPPRESS = "suppress"

_IM_NORM = "IM replies must be at most 100 words."


def _im_spec(
    *, channel: str, label: str, transport: str, norm: str
) -> ChannelSpec:
    return ChannelSpec(
        channel=channel,
        family=FAMILY_IM,
        label=label,
        transport=transport,
        reply_format="plain",
        max_words=100,
        max_chars=4000,
        required_sections=(),
        recommended_sections=(),
        heading="",
        default_subject=f"{label} update",
        approval=ApprovalRules(
            require_approval=False,
            suppress_kinds=frozenset({KIND_PROGRESS}),
            suppress_reason=(
                "IM channels notify only for a decision, a question or a "
                "failure; a progress update is not worth interrupting anyone for"
            ),
        ),
        norm=norm,
        instructions=(
            f"{norm} Notify people only when a decision or sign-off is "
            "needed, ask only direct questions, and give a short conclusion. "
            "Progress updates are recorded but not delivered on this channel."
        ),
    )


CHANNEL_SPECS: dict[str, ChannelSpec] = {
    CHANNEL_CHAT: _im_spec(
        channel=CHANNEL_CHAT,
        label="Chat",
        transport=TRANSPORT_IM,
        norm=_IM_NORM,
    ),
    CHANNEL_FEISHU: _im_spec(
        channel=CHANNEL_FEISHU,
        label="Feishu",
        transport=TRANSPORT_FEISHU,
        norm="Feishu/IM replies must be at most 100 words.",
    ),
    CHANNEL_SLACK: _im_spec(
        channel=CHANNEL_SLACK,
        label="Slack",
        transport=TRANSPORT_SLACK,
        norm="Slack replies must be at most 100 words.",
    ),
    CHANNEL_EMAIL: ChannelSpec(
        channel=CHANNEL_EMAIL,
        family=FAMILY_ASYNC,
        label="Email",
        transport=TRANSPORT_EMAIL,
        reply_format="rfc822",
        max_words=None,
        max_chars=100_000,
        required_sections=("Summary",),
        recommended_sections=("Background", "Changes", "Verification"),
        heading="",
        default_subject="Ticket update",
        approval=ApprovalRules(
            require_approval=True,
            reason=(
                "email replies are drafted for a person to review and send, "
                "so they wait for an explicit approval"
            ),
        ),
        norm="Email replies must include Subject and Summary.",
        instructions=(
            "Email replies must include Subject and Summary. Deliver "
            "structured, information-dense output: the problem background, "
            "the change description and the verification results, each as its "
            "own section. The draft is held for a person to review before it "
            "is sent."
        ),
    ),
    CHANNEL_GITHUB: ChannelSpec(
        channel=CHANNEL_GITHUB,
        family=FAMILY_ASYNC,
        label="GitHub",
        transport=TRANSPORT_GITHUB,
        reply_format="markdown",
        max_words=None,
        max_chars=60_000,
        required_sections=("Summary",),
        recommended_sections=("Background", "Changes", "Verification"),
        heading="### Summary",
        default_subject="",
        approval=ApprovalRules(require_approval=False),
        norm="GitHub replies must use a concise ### Summary heading.",
        instructions=(
            "GitHub replies must use a concise ### Summary heading. Follow "
            "the repository's conventions: give the problem background, the "
            "change description and the verification results when they apply."
        ),
    ),
    CHANNEL_MANUAL: ChannelSpec(
        channel=CHANNEL_MANUAL,
        family=FAMILY_LOCAL,
        label="Inbox",
        transport=TRANSPORT_LOCAL,
        reply_format="plain",
        max_words=None,
        max_chars=20_000,
        required_sections=(),
        recommended_sections=(),
        heading="",
        default_subject="",
        approval=ApprovalRules(require_approval=False),
        norm="The reply is recorded in the Conductor inbox, not delivered anywhere.",
        instructions=(
            "The reply is recorded in the Conductor inbox and read there; "
            "there is no external channel to write to."
        ),
    ),
    CHANNEL_SCHEDULE: ChannelSpec(
        channel=CHANNEL_SCHEDULE,
        family=FAMILY_LOCAL,
        label="Inbox",
        transport=TRANSPORT_LOCAL,
        reply_format="plain",
        max_words=None,
        max_chars=20_000,
        required_sections=(),
        recommended_sections=(),
        heading="",
        default_subject="",
        approval=ApprovalRules(require_approval=False),
        norm="The reply is recorded in the Conductor inbox, not delivered anywhere.",
        instructions=(
            "The reply is recorded in the Conductor inbox and read there; "
            "there is no external channel to write to."
        ),
    ),
    CHANNEL_UNKNOWN: ChannelSpec(
        channel=CHANNEL_UNKNOWN,
        family=FAMILY_ASYNC,
        label="Unconfigured channel",
        transport=TRANSPORT_NONE,
        reply_format="markdown",
        max_words=None,
        max_chars=20_000,
        required_sections=("Summary",),
        recommended_sections=(),
        heading="### Summary",
        default_subject="Ticket update",
        approval=ApprovalRules(
            require_approval=True,
            reason=(
                "no delivery channel is configured for this Source, so the "
                "reply waits for a person to read and send it"
            ),
        ),
        norm=(
            "No delivery channel is configured for this Source, so the reply "
            "is held for a person."
        ),
        instructions=(
            "No delivery channel is configured for this Source. Draft the "
            "reply for a person to read; it is not delivered anywhere on its "
            "own."
        ),
    ),
}

#: Source kind -> channel. A kind outside this map has no configured channel.
KIND_TO_CHANNEL: dict[str, str] = {
    "github": CHANNEL_GITHUB,
    "feishu": CHANNEL_FEISHU,
    "slack": CHANNEL_SLACK,
    "email": CHANNEL_EMAIL,
    "manual": CHANNEL_MANUAL,
    "schedule": CHANNEL_SCHEDULE,
}


def spec_for(channel: str) -> ChannelSpec:
    """Return the spec of one channel, or the conservative unknown spec."""
    return CHANNEL_SPECS.get(str(channel or "").strip().lower(), CHANNEL_SPECS[CHANNEL_UNKNOWN])


def resolve_channel(
    kind: str, config: Mapping[str, Any] | None = None
) -> ChannelSpec:
    """Map a Source kind (plus optional Source config) to exactly one spec.

    A Source may narrow its channel inside its own family by setting
    ``config["channel"]`` — a Feishu Source may declare ``"chat"`` to use the
    generic IM norms. A config that would move the reply to another family
    is ignored: the Source kind decides the family.
    """
    base = spec_for(KIND_TO_CHANNEL.get(str(kind or "").strip().lower(), CHANNEL_UNKNOWN))
    override = str((config or {}).get("channel") or "").strip().lower()
    if not override or override == base.channel:
        return base
    candidate = CHANNEL_SPECS.get(override)
    if candidate is None or candidate.family != base.family:
        return base
    return candidate


def transport_for(spec: ChannelSpec, kind: str = "") -> str:
    """Return the concrete transport for one reply.

    The generic ``chat`` channel has no adapter of its own: the Source kind
    picks the IM adapter that carries the message.
    """
    if spec.transport != TRANSPORT_IM:
        return spec.transport
    return (
        TRANSPORT_SLACK
        if str(kind or "").strip().lower() == "slack"
        else TRANSPORT_FEISHU
    )


def decide_delivery(
    spec: ChannelSpec,
    *,
    kind: str = KIND_RESULT,
    config: Mapping[str, Any] | None = None,
) -> DeliveryDecision:
    """Decide whether one reply may be delivered, held or suppressed."""
    reply_kind = str(kind or "").strip().lower() or KIND_RESULT
    rules = spec.approval
    if reply_kind in rules.suppress_kinds:
        return DeliveryDecision(ACTION_SUPPRESS, rules.suppress_reason)
    require = rules.require_approval
    if config is not None and "require_approval" in config:
        require = bool(config.get("require_approval"))
    if reply_kind in rules.approval_kinds:
        require = True
    if require:
        return DeliveryDecision(
            ACTION_HOLD,
            rules.reason or "this channel requires a human to review the reply first",
        )
    return DeliveryDecision(ACTION_DELIVER, "")


def count_reply_units(text: str) -> int:
    """Count the units of one reply: words, with one unit per CJK character.

    A CJK character carries about as much meaning as a latin word, so the
    100-word cap has to count it the same way.
    """
    body = text or ""
    parts = re.findall(rf"[{_CJK}]|[^{_CJK}\s]+|\s+", body)
    return sum(1 for part in parts if not part.isspace())


def cap_reply_words(text: str, max_words: int | None) -> str:
    """Cut a reply down to ``max_words`` units, keeping the leading text."""
    body = (text or "").strip()
    if not max_words or max_words <= 0:
        return body
    parts = re.findall(rf"[{_CJK}]|[^{_CJK}\s]+|\s+", body)
    count = 0
    kept: list[str] = []
    for part in parts:
        if part.isspace():
            kept.append(part)
            continue
        count += 1
        if count > max_words:
            break
        kept.append(part)
    return "".join(kept).strip()


def parse_email_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split one reply into its subject line and its named sections.

    A section is a line such as ``Summary:`` on its own; everything that
    follows belongs to it. Text before the first section header is kept: it
    becomes the ``Summary`` section when the reply has none, and is merged
    into it when it has one.
    """
    subject = ""
    sections: list[list[Any]] = []
    preamble: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        subject_match = _SUBJECT_RE.match(stripped)
        if subject_match and not subject:
            subject = subject_match.group(1).strip()
            continue
        section_match = _SECTION_RE.match(stripped)
        if section_match:
            sections.append([section_match.group(1).strip(), []])
            continue
        if sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)

    result = [(str(name), "\n".join(lines).strip()) for name, lines in sections]
    preamble_text = "\n".join(preamble).strip()
    if preamble_text:
        summary_index = next(
            (index for index, (name, _) in enumerate(result) if name.lower() == "summary"),
            None,
        )
        if summary_index is None:
            result.insert(0, ("Summary", preamble_text))
        else:
            name, body = result[summary_index]
            result[summary_index] = (
                name,
                f"{preamble_text}\n\n{body}".strip() if body else preamble_text,
            )
    if not result:
        result = [("Summary", "")]
    return subject, result


def render_email(text: str, *, subject: str = "Ticket update") -> str:
    """Render one reply as a structured email.

    The result is what a person reads in the approval queue and what the
    mail transport sends: a ``Subject:`` line, then one named section per
    block of the reply.
    """
    body = (text or "").strip()
    parsed_subject, sections = parse_email_sections(body)
    ordered = sorted(
        sections,
        key=lambda item: (
            EMAIL_SECTION_ORDER.index(item[0])
            if item[0] in EMAIL_SECTION_ORDER
            else len(EMAIL_SECTION_ORDER)
        ),
    )
    lines = [f"Subject: {parsed_subject or subject}", ""]
    for index, (name, section_body) in enumerate(ordered):
        if index:
            lines.append("")
        lines.append(f"{name}:")
        lines.append(section_body)
    return "\n".join(lines).strip()


def missing_sections(spec: ChannelSpec, text: str) -> tuple[str, ...]:
    """Return the sections one reply leaves out.

    Only meaningful for the channels that expect named sections (email,
    GitHub); the IM channels have no section structure.
    """
    if not spec.required_sections:
        return ()
    _, sections = parse_email_sections(text)
    present = {name.lower() for name, _ in sections}
    return tuple(
        name for name in spec.required_sections if name.lower() not in present
    )


def shape_reply(
    spec: ChannelSpec, text: str, *, subject: str | None = None
) -> str:
    """Render one reply in the channel's own form.

    The result is idempotent: shaping an already shaped reply changes
    nothing, so the same reply may be shaped when it is queued and again
    when it is sent.
    """
    body = (text or "").strip()
    if not body:
        return ""
    if spec.channel == CHANNEL_EMAIL:
        return render_email(body, subject=subject or spec.default_subject)
    if spec.family == FAMILY_IM:
        capped = cap_reply_words(body, spec.max_words)
        return capped[: spec.max_chars].strip()
    if spec.channel == CHANNEL_GITHUB and not any(
        line.lstrip().startswith("#") for line in body.splitlines()
    ):
        body = f"{spec.heading}\n{body}"
    return body[: spec.max_chars].strip()


def reply_instructions(spec: ChannelSpec) -> str:
    """The per-channel instruction handed to the Router."""
    return spec.instructions


def channel_catalog_instruction() -> str:
    """One sentence per channel family, for prompts without a Source.

    A prompt that cannot name one channel — a batch of candidates from
    several Sources, for instance — still has to say what the norms are.
    """
    return " ".join(
        spec_for(channel).norm
        for channel in (CHANNEL_FEISHU, CHANNEL_GITHUB, CHANNEL_EMAIL)
    )
