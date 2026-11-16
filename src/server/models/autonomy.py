"""Wire shapes for the Super-agent autonomy API."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


SourceKind = Literal["github", "feishu", "email", "slack"]
SourceRoutingMode = Literal["respond-only-explicit", "candidate", "ignore"]
ContextScopeKind = Literal["isolated", "shared"]
ContextDataClass = Literal["triggers", "reports", "tickets", "a2a_messages"]
A2AIntent = Literal["inform", "request_review", "handoff_request"]
A2AAllowedAction = Literal["read_context", "review", "comment", "propose_handoff"]


class CredentialAllowlistConfig(BaseModel):
    """The destinations a Source's vault credential may be used for.

    A dimension the caller left out is ``None`` and stays out of
    ``model_dump(exclude_none=True)``, so it keeps the default derived from
    ``api_base_url`` and the repository. A dimension the caller sent as ``null``
    is also ``None`` but must reach the dump, because
    ``CredentialAllowlist.from_mapping`` reads an explicit empty sequence —
    ``null`` included — as "deny this dimension": dropping it would silently
    restore the permissive default. ``model_fields_set`` is what tells the two
    apart, so the serializer below re-adds a null that was sent and leaves an
    unset default out.

    Any other key is passed through untouched — the full rule, ``schemes`` and
    ``ports`` included, is ``CredentialAllowlist.from_mapping``'s, and the
    validated dump is what reaches it, so a key this model does not name must
    not be dropped here.
    """

    model_config = ConfigDict(extra="allow")

    hosts: list[str] | None = None
    methods: list[str] | None = None
    path_prefixes: list[str] | None = None

    @model_serializer(mode="wrap")
    def _dump(self, handler: Any) -> dict[str, Any]:
        # Only what the caller sent survives; a null the dump's own
        # ``exclude_none`` dropped comes back, because an explicit null is a
        # denial and not the absence of a dimension.
        dumped = handler(self)
        kept = {key: value for key, value in dumped.items() if key in self.model_fields_set}
        for key in self.model_fields_set:
            if key not in kept and getattr(self, key, None) is None:
                kept[key] = None
        return kept


class GitHubSourceConfig(BaseModel):
    repository: str = Field(..., min_length=3, max_length=200)
    secret_name: str = Field(..., min_length=1, max_length=100)
    workspace_id: str = Field(..., min_length=1, max_length=100)
    allowed_logins: list[str] = Field(default_factory=list)
    # Bots whose messages may become inferred candidates; other bots are dropped.
    bot_logins: list[str] = Field(default_factory=list)
    poll_seconds: int = Field(60, ge=15, le=3600)
    # Optional native Worker for Tickets from this Source. Empty means PTC.
    worker_provider: str = Field("", max_length=64)
    worker_model: str = Field("", max_length=120)
    worker_daemon_id: str | None = Field(None, max_length=64)
    # Optional GitHub Enterprise / local-test endpoint. Empty means github.com.
    api_base_url: str = ""
    # Optional narrowing of the credential's hosts, methods and API-relative
    # path prefixes. An omitted dimension keeps the default derived from
    # api_base_url and repository; an explicit empty list denies that dimension.
    credential_allowlist: CredentialAllowlistConfig | None = None
    # Which triggers this Source admits, cell by cell. A cell left out keeps
    # the user policy's own; one named here wins over it.
    admission: dict[str, str] = Field(default_factory=dict)
    # Narrows the delivery channel inside this Source's own family: a GitHub
    # Source may declare "email" to have its replies drafted for a person.
    # Anything outside the family is ignored, the way resolve_channel decides.
    channel: str = Field("", max_length=40)
    routing_mode: SourceRoutingMode = "candidate"
    # Run on the operator's own token: a comment holding this phrase (for
    # example "/conductor", never an @-mention) addresses the agent, and the
    # token's own comments are read except the replies this system posted.
    trigger_phrase: str = Field("", max_length=40)
    # Hold every GitHub reply for a person instead of posting it. This is the
    # GitHub channel's own default, so a config that does not say is stored
    # without the flag and the channel it names decides — see
    # ``_github_config``.
    require_approval: bool = False


class FeishuSourceConfig(BaseModel):
    chat_id: str = Field(..., min_length=3, max_length=200)
    workspace_id: str = Field(..., min_length=1, max_length=100)
    allowed_logins: list[str] = Field(default_factory=list)
    bot_logins: list[str] = Field(default_factory=list)
    poll_seconds: int = Field(60, ge=15, le=3600)
    # Optional native Worker for Tickets from this Source. Empty means PTC.
    worker_provider: str = Field("", max_length=64)
    worker_model: str = Field("", max_length=120)
    worker_daemon_id: str | None = Field(None, max_length=64)
    # The bot's own open id; used to tell a mention from a plain message.
    agent_open_id: str = Field("", max_length=100)
    admission: dict[str, str] = Field(default_factory=dict)
    routing_mode: SourceRoutingMode = "candidate"


class EmailSourceConfig(BaseModel):
    """One email channel: where a reply goes and how it is sent."""

    from_address: str = Field(..., min_length=3, max_length=200)
    to_address: str = Field(..., min_length=3, max_length=200)
    secret_name: str = Field(..., min_length=1, max_length=100)
    workspace_id: str = Field(..., min_length=1, max_length=100)
    smtp_host: str = Field(..., min_length=1, max_length=200)
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_username: str = Field("", max_length=200)
    use_tls: bool = True
    timeout: float = Field(30.0, ge=1, le=300)
    allowed_logins: list[str] = Field(default_factory=list)
    bot_logins: list[str] = Field(default_factory=list)
    poll_seconds: int = Field(60, ge=15, le=3600)
    # Optional native Worker for Tickets from this Source. Empty means PTC.
    worker_provider: str = Field("", max_length=64)
    worker_model: str = Field("", max_length=120)
    worker_daemon_id: str | None = Field(None, max_length=64)
    # An email draft waits for a person to read it before it is sent.
    require_approval: bool = True
    routing_mode: SourceRoutingMode = "candidate"


class SlackSourceConfig(BaseModel):
    """One Slack channel: where a reply goes and how it is sent."""

    channel_name: str = Field(..., min_length=1, max_length=200)
    webhook_secret_name: str = Field(..., min_length=1, max_length=100)
    workspace_id: str = Field(..., min_length=1, max_length=100)
    # Extra hosts an Enterprise Grid deployment may serve its webhooks from.
    allowed_webhook_hosts: list[str] = Field(default_factory=list)
    allowed_logins: list[str] = Field(default_factory=list)
    bot_logins: list[str] = Field(default_factory=list)
    poll_seconds: int = Field(60, ge=15, le=3600)
    worker_provider: str = Field("", max_length=64)
    worker_model: str = Field("", max_length=120)
    worker_daemon_id: str | None = Field(None, max_length=64)
    require_approval: bool = False
    routing_mode: SourceRoutingMode = "candidate"


class SourceCreate(BaseModel):
    kind: SourceKind = "github"
    # Validated against GitHubSourceConfig/FeishuSourceConfig/EmailSourceConfig/
    # SlackSourceConfig by the route, so the Source kinds can share one
    # create/update wire shape.
    config: dict[str, Any]
    enabled: bool = True


class SourceUpdate(BaseModel):
    config: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


class SourceState(BaseModel):
    agent_login: str = ""
    repository: str = ""
    cursor: str = ""
    last_poll_at: str = ""
    last_error: str = ""


class SourceView(BaseModel):
    source_id: str
    user_id: str
    kind: str
    config: dict[str, Any]
    state: dict[str, Any]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SourceList(BaseModel):
    sources: list[SourceView]


class SourceCheckRequest(BaseModel):
    pass


class DaemonCreate(BaseModel):
    name: str = Field("local", max_length=120)
    provider: str = Field("", max_length=64)
    max_concurrency: int = Field(1, ge=1, le=32)


class DaemonConcurrencyUpdate(BaseModel):
    max_concurrency: int = Field(..., ge=1, le=32)


class DaemonView(BaseModel):
    daemon_id: str
    name: str
    provider: str
    max_concurrency: int = 1
    last_seen_at: datetime | None = None
    created_at: datetime


class CapabilityAudit(BaseModel):
    """N44 S2: what the grant verifier decided without enforcing it (N58).

    In ``shadow`` the daemon runs a Ticket whose signed grant it would have
    refused under ``enforce``; this records that so the Ticket shows it.
    """

    mode: str = Field("", max_length=16)
    would_refuse: bool
    reason: str = Field("", max_length=2000)


class DaemonReport(BaseModel):
    lease_id: UUID
    state: Literal["ready", "blocked", "failed", "dropped", "waiting"]
    summary: str = Field("", max_length=20000)
    body: str | None = Field(None, max_length=256 * 1024)
    question: str = Field("", max_length=4000)
    attention_reason: (
        Literal[
            "uncommitted_changes",
            "git_status_unavailable",
            "completion_report_missing",
            "completion_no_change",
            "completion_unverified",
        ]
        | None
    ) = None
    # Echoed from the signed grant on a completion. A signed lease rejects a
    # completion whose daemon identity or policy version differs, including
    # when the report omits them. A failed or dropped report may omit both:
    # the daemon token is the identity, which is how a refusal or an early
    # cancel lands.
    policy_version: str = Field("", max_length=64)
    daemon_id: str = Field("", max_length=64)
    # N44 S2: kept on the Ticket's mandate and shown as TicketView.capability_audit.
    capability_audit: CapabilityAudit | None = None

    @field_validator("body", mode="before")
    @classmethod
    def body_is_utf8_bounded(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                size = len(value.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("body must be valid UTF-8 text") from exc
            if size > 256 * 1024:
                raise ValueError("body must be at most 262144 UTF-8 bytes")
        return value


class DaemonProgress(BaseModel):
    lease_id: UUID
    summary: str = Field(..., max_length=4000)


_WORKTREE_BASE_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_WORKTREE_BRANCH_PREFIX = "conductor/ticket/"
# Characters git refuses in a ref name, plus whitespace and control bytes.
_WORKTREE_BRANCH_BAD_CHARS = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def _worktree_path_value(value: str) -> str:
    """An absolute daemon-host path with no control bytes and no ``..`` part."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("worktree_path must not contain control characters")
    if not value.startswith("/"):
        raise ValueError("worktree_path must be an absolute path")
    if ".." in value.split("/"):
        raise ValueError("worktree_path must not contain '..'")
    return value


class DaemonWorktreeBind(BaseModel):
    """A daemon's report that it prepared the Ticket checkout on its own host.

    ``worktree_path`` is the daemon-host path (never a server path); the bind
    is fenced on this ``lease_id`` being the Ticket's live lease and on
    ``base_sha`` matching the server's durable pin. The base branch is the
    server's pin, so the daemon does not send one.
    """

    lease_id: UUID
    worktree_path: str = Field(..., min_length=1, max_length=500)
    branch: str = Field(..., min_length=1, max_length=200)
    base_sha: str = Field(..., min_length=40, max_length=64)

    @field_validator("worktree_path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        return _worktree_path_value(value)

    @field_validator("base_sha")
    @classmethod
    def _full_object_id(cls, value: str) -> str:
        if not _WORKTREE_BASE_SHA_RE.fullmatch(value):
            raise ValueError("base_sha must be a full 40 or 64 character lowercase hex id")
        return value

    @field_validator("branch")
    @classmethod
    def _ticket_branch(cls, value: str) -> str:
        if (
            not value.startswith(_WORKTREE_BRANCH_PREFIX)
            or value == _WORKTREE_BRANCH_PREFIX
            or value.startswith("-")
            or _WORKTREE_BRANCH_BAD_CHARS.search(value)
            or ".." in value
            or "@{" in value
            or "//" in value
            or value.endswith(("/", ".", ".lock"))
            or any(
                part.startswith(".") or part.endswith(".lock")
                for part in value.split("/")
            )
        ):
            raise ValueError(
                f"branch must be a plain ref name under {_WORKTREE_BRANCH_PREFIX!r}"
            )
        return value


class DaemonWorktreeCleanup(BaseModel):
    """The true outcome of a daemon removing one bound checkout attempt.

    ``worktree_path`` + ``worktree_generation`` (and ``lease_id`` when given)
    name the attempt, so a late report for an older attempt cannot clear or
    mark pending a newer binding of the same path.
    """

    lease_id: UUID | None = None
    worktree_path: str = Field(..., min_length=1, max_length=500)
    worktree_generation: int = Field(..., ge=1)
    outcome: Literal["removed", "failed"]
    detail: str = Field("", max_length=4000)

    @field_validator("worktree_path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        return _worktree_path_value(value)


class DaemonOutputEvent(BaseModel):
    """One visible chunk from a running local Worker.

    Only visible model text and tool names are accepted: tool arguments,
    results and the prompt are intentionally absent from this contract.
    """

    # ``stop`` carries no payload: the daemon closed its own text channel (a
    # local refusal or a skipped record) and the server records the taint.
    kind: Literal["text", "reasoning", "tool", "stop"] = "text"
    text: str = Field("", max_length=4000)
    # The final newline of ``text`` is synthetic (added by the daemon so a message
    # ends on a line break); for credential continuity the event runs straight
    # into the next one.
    join: bool = False
    tools: list[Annotated[str, Field(max_length=256)]] = Field(
        default_factory=list, max_length=8
    )


class DaemonOutputBatch(BaseModel):
    lease_id: UUID
    # Identifies the daemon-side generation (its lease id in practice); the
    # activity stream passes it to the CLI so a new run resets local rendering
    # state. Empty is accepted for older daemons and rendered as one stream.
    run_id: str = Field("", max_length=128)
    # Stream fence. The first batch of a run carries no epoch and batch_seq 1;
    # the server allocates the epoch and answers with it, and every later batch
    # echoes that epoch with batch_seq incremented by one. A daemon never
    # invents an epoch: a mismatch is refused as ``stale_output_epoch``.
    epoch: int | None = Field(None, ge=1, le=2**62)
    batch_seq: int = Field(1, ge=1, le=2**62)
    events: list[DaemonOutputEvent] = Field(default_factory=list, max_length=64)


class DaemonClaimRequest(BaseModel):
    claim_id: UUID | None = None
    # The local policy version the daemon is running under. The lease records
    # it, so the device can refuse a lease granted under a policy it replaced.
    policy_version: str = Field("", max_length=64)
    # The completion-report protocol the daemon parses: 1 is the JSON
    # completion contract. An older daemon omits it and keeps the marker
    # lines it parses (an older server ignores the field).
    completion_contract: int = Field(0, ge=0, le=1000)


class DaemonLeaseRef(BaseModel):
    ticket_id: UUID
    lease_id: UUID


class DaemonHeartbeatRequest(BaseModel):
    leases: list[DaemonLeaseRef] = Field(default_factory=list)


class PolicyView(BaseModel):
    accept_mode: str
    version: str
    rules: list[dict[str, Any]] = Field(default_factory=list)
    admission: dict[str, str] = Field(default_factory=dict)


class PolicyUpdate(BaseModel):
    accept_mode: Optional[Literal["explicit", "observe", "auto"]] = None
    admission: Optional[dict[str, str]] = None


class SourceCheckView(BaseModel):
    ok: bool
    error: str = ""
    agent_login: str = ""
    repository: str = ""
    can_push: bool = False
    private: bool = False


class OutboxView(BaseModel):
    """One outbound reply, as a person reads it in the approval queue."""

    outbox_id: str
    ticket_id: str = ""
    source_id: str = ""
    thread_key: str = ""
    body: str = ""
    status: str = ""
    channel: str = ""
    reply_kind: str = ""
    created_at: datetime
    approved_at: Optional[datetime] = None
    approved_by: str = ""


class OutboxList(BaseModel):
    replies: list[OutboxView]


class TriggerView(BaseModel):
    trigger_id: str
    source_id: str
    kind: str
    thread_key: str
    actor_login: str
    addressed: bool
    text: str
    url: str
    received_at: datetime


class TriggerList(BaseModel):
    triggers: list[TriggerView]


def _blank_id(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _id_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


class TicketStopView(BaseModel):
    """The durable stop claim, including whether the runner was verified."""

    stop_id: str
    ticket_id: str
    mode: Literal["daemon", "backend", "not_started"]
    verification: Literal["pending", "verified", "unverified", "not_applicable"]
    verified: bool
    escalated: bool = False
    requested_at: datetime
    deadline_at: datetime
    acknowledged_at: datetime | None = None
    verified_at: datetime | None = None
    escalated_at: datetime | None = None
    reason: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0


class DaemonStopAck(BaseModel):
    lease_id: UUID
    process_gone: bool
    signal: str = Field("", max_length=32)
    pid: int | None = Field(None, ge=1)
    pgid: int | None = Field(None, ge=1)
    checked_at: datetime | None = None
    # Non-secret PID/PGID evidence for the audit row; clients may omit it.
    processes: list[dict[str, Any]] = Field(default_factory=list)


class TicketView(BaseModel):
    ticket_id: str
    user_id: str
    workspace_id: str
    title: str
    scope: str
    status: str
    mandate: dict[str, Any]
    origin_trigger_id: str | None = None
    # Projected from the row. Not ticket columns.
    source_id: str = ""
    trigger_id: str = ""
    origin_thread_key: str
    router_thread_id: str | None = None
    worker_thread_id: str | None = None
    worker_run_id: str | None = None
    worker_provider: str = ""
    worker_model: str = ""
    worker_daemon_id: str | None = None
    switches: list[dict[str, Any]] = []
    # The explicit shared/isolated boundary this Ticket runs inside.
    context_scope_id: str | None = None
    attempts: int
    attention: dict[str, Any] | None = None
    summary: str
    error: str
    # Persisted host checkout for this Ticket ('' when none); a dirty blocked
    # Ticket resumes it. worktree_host says whose host ('server' / 'daemon').
    worktree_path: str = ""
    # Advances on every bind; cleanup fences on (path, generation).
    worktree_generation: int = 0
    worktree_branch: str = ""
    # Durable base pin: written once, kept after cleanup.
    worktree_base_sha: str = ""
    worktree_host: str = ""
    worktree_cleanup_pending: bool = False
    # Recorded outcome of a speculative (observe) run: accepted / discarded /
    # approved, with the Worker's closing evidence. Projected from the mandate.
    speculative_outcome: dict[str, Any] | None = None
    # N44 S2: the daemon's shadow grant-verifier decision, from the mandate.
    capability_audit: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str = ""
    stop: TicketStopView | None = None

    @model_validator(mode="before")
    @classmethod
    def _project_source_and_trigger(cls, data: Any) -> Any:
        # Copy so a caller's row is never written back. A non-empty id wins.
        # Otherwise source_id comes from the mandate, and trigger_id from the
        # origin trigger, then mandate.trigger_id.
        if not isinstance(data, Mapping):
            return data
        projected = dict(data)
        mandate = projected.get("mandate")
        mandate_map = mandate if isinstance(mandate, dict) else {}
        if _blank_id(projected.get("source_id")):
            projected["source_id"] = _id_text(mandate_map.get("source_id"))
        if _blank_id(projected.get("trigger_id")):
            projected["trigger_id"] = _id_text(
                projected.get("origin_trigger_id")
            ) or _id_text(mandate_map.get("trigger_id"))
        outcome = mandate_map.get("speculative_outcome")
        projected["speculative_outcome"] = (
            dict(outcome) if isinstance(outcome, dict) else None
        )
        audit = mandate_map.get("capability_audit")
        projected["capability_audit"] = dict(audit) if isinstance(audit, dict) else None
        return projected


class TicketInputView(BaseModel):
    input_id: str
    ticket_id: str
    trigger_id: str | None = None
    kind: str
    text: str
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    created_at: datetime


class TicketReportView(BaseModel):
    report_id: str
    ticket_id: str
    state: str
    summary: str
    question: str | None = None
    body: str | None = None
    git_check: Literal["unsupported_runtime"] | None = None
    created_at: datetime


class TicketEventView(BaseModel):
    event_id: str
    ticket_id: str
    actor: str
    action: str
    from_owner: str = ""
    to_owner: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TicketDetail(TicketView):
    inputs: list[TicketInputView] = Field(default_factory=list)
    reports: list[TicketReportView] = Field(default_factory=list)
    events: list[TicketEventView] = Field(default_factory=list)


class TicketList(BaseModel):
    tickets: list[TicketView]


class ManualTaskCreate(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20000)
    title: str = Field("", max_length=120)
    workspace_id: str = Field(..., min_length=1, max_length=100)
    worker_provider: str = Field("", max_length=64)
    worker_model: str = Field("", max_length=120)
    # When set, the Ticket runs on this user's local daemon, not the backend.
    worker_daemon_id: str | None = Field(None, max_length=64)
    # A local daemon revalidates this against its operator-configured root.
    cwd: str | None = Field(None, min_length=1, max_length=4096)
    # Run the request as written, skipping the prompt refiner (the TUI's /raw).
    no_refine: bool = False


class ScheduleCreate(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20000)
    title: str = Field("", max_length=120)
    workspace_id: str = Field(..., min_length=1, max_length=100)
    cron_expression: str = Field(..., min_length=1, max_length=120)
    timezone: str = Field("UTC", max_length=80)
    worker_provider: str = Field("", max_length=64)
    worker_model: str = Field("", max_length=120)
    worker_daemon_id: str | None = Field(None, max_length=64)
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    prompt: Optional[str] = Field(None, min_length=1, max_length=20000)
    title: Optional[str] = Field(None, max_length=120)
    workspace_id: Optional[str] = Field(None, min_length=1, max_length=100)
    cron_expression: Optional[str] = Field(None, min_length=1, max_length=120)
    timezone: Optional[str] = Field(None, max_length=80)
    worker_provider: Optional[str] = Field(None, max_length=64)
    worker_model: Optional[str] = Field(None, max_length=120)
    worker_daemon_id: Optional[str] = Field(None, max_length=64)
    enabled: Optional[bool] = None


class ScheduleView(BaseModel):
    schedule_id: str
    user_id: str
    workspace_id: str
    title: str
    prompt: str
    cron_expression: str
    timezone: str
    enabled: bool
    worker_provider: str = ""
    worker_model: str = ""
    worker_daemon_id: str | None = None
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class ScheduleList(BaseModel):
    schedules: list[ScheduleView]


class TicketReplyRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


class TicketReassignRequest(BaseModel):
    """New owner for a Ticket; ``daemon_id`` null means the backend scheduler.

    ``force`` acknowledges an unverified stop when the daemon no longer holds
    a live lease. A runner that is still heartbeating is not overridden.
    """

    daemon_id: UUID | None = None
    force: bool = False


class TicketLineSetRequest(BaseModel):
    """Group a Ticket under a line, or under none when ``line_id`` is null."""

    line_id: UUID | None = None


class LineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    branch: str = Field("", max_length=200)
    worktree_path: str = Field("", max_length=500)


class LineView(BaseModel):
    line_id: str
    user_id: str
    name: str
    branch: str = ""
    worktree_path: str = ""
    status: str
    created_at: datetime
    updated_at: datetime


class LineTicketView(BaseModel):
    ticket_id: str
    title: str
    status: str
    owner: str = ""
    worker_provider: str = ""
    attempts: int = 0
    updated_at: datetime
    last_activity_at: datetime | None = None


class TicketLineGroup(BaseModel):
    line: LineView | None = None
    tickets: list[LineTicketView] = Field(default_factory=list)
    ticket_count: int = 0
    open_count: int = 0
    last_activity_at: datetime | None = None


class TicketLinesResponse(BaseModel):
    lines: list[TicketLineGroup]
    ungrouped: TicketLineGroup | None = None

class TaskCloseRequest(BaseModel):
    reason: str = Field("", max_length=500)


class TaskActivityEvent(BaseModel):
    event_id: str
    kind: str
    at: datetime
    summary: str = ""
    detail: str = ""
    state: str = ""


class TaskActivity(BaseModel):
    ticket_id: str
    events: list[TaskActivityEvent] = Field(default_factory=list)


class TicketActionResponse(BaseModel):
    ticket: TicketView


class RawRunEvent(BaseModel):
    id: str
    data: str


class RawRunResponse(BaseModel):
    thread_id: str | None = None
    run_id: str | None = None
    events: list[RawRunEvent] = Field(default_factory=list)
    note: str | None = None


class OverviewCounts(BaseModel):
    attention: int = 0
    running: int = 0
    queued: int = 0
    completed: int = 0
    failed: int = 0
    blocked: int = 0


class AutonomyOverview(BaseModel):
    counts: OverviewCounts
    sources: int = 0
    enabled_sources: int = 0
    # The Router's configured dispatch model, so an operator CLI can show what
    # will route a new Ticket without reading the server's local config.
    router_model: str = ""


class HandoffFileView(BaseModel):
    path: str
    size: int = Field(..., ge=0)
    sha256: str = Field(..., min_length=64, max_length=64)


class HandoffRegisterRequest(BaseModel):
    producer: str = Field(..., min_length=1, max_length=200)
    consumer: str = Field(..., min_length=1, max_length=200)
    purpose: str = Field("", max_length=500)
    # The daemon route computes size + sha256 from these workspace-relative
    # paths, so a caller cannot plant a checksum it did not produce.
    paths: list[str] = Field(..., min_length=1, max_length=256)
    # Workspace root the paths are confined to (same boundary as native cwd).
    workspace_root: str = Field("", max_length=4096)


class HandoffManifestRequest(BaseModel):
    """Caller-supplied manifest for the user-facing register route."""

    producer: str = Field(..., min_length=1, max_length=200)
    consumer: str = Field(..., min_length=1, max_length=200)
    purpose: str = Field("", max_length=500)
    files: list[HandoffFileView] = Field(..., min_length=1, max_length=256)


class HandoffVerifyRequest(BaseModel):
    workspace_root: str = Field("", max_length=4096)


class HandoffView(BaseModel):
    handoff_id: str
    ticket_id: str
    user_id: str
    producer: str
    consumer: str
    purpose: str
    status: str
    files: list[HandoffFileView] = Field(default_factory=list)
    verify_error: str
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None


class HandoffList(BaseModel):
    handoffs: list[HandoffView]


class HandoffVerifyResponse(BaseModel):
    handoff: HandoffView
    outcome: str
    error: str = ""


class TicketClarificationView(BaseModel):
    clarification_id: str
    ticket_id: str
    status: str
    clarity_score: float
    original_prompt: str
    expanded_request: str
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed_text: str | None = None
    created_at: datetime


class ClarificationConfirmRequest(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=20000)


class RefinementConfirmRequest(BaseModel):
    """Confirmation of a refined request; omitted text adopts the rewrite."""

    text: str | None = Field(None, min_length=1, max_length=20000)


class ContextScopePolicy(BaseModel):
    """Data policy attached to a context scope; closed to unknown keys."""

    model_config = ConfigDict(extra="forbid")

    data_classes: list[ContextDataClass] = Field(
        default_factory=lambda: ["triggers", "reports", "tickets", "a2a_messages"],
        max_length=4,
    )
    allow_a2a: bool = False
    allow_cross_user: bool = False
    max_participants: int = Field(10, ge=1, le=100)
    retention_days: int = Field(90, ge=1, le=3650)

    @model_validator(mode="after")
    def _classes_are_unique(self) -> "ContextScopePolicy":
        if len(set(self.data_classes)) != len(self.data_classes):
            raise ValueError("data_classes must not repeat")
        return self


class ContextScopeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    kind: ContextScopeKind = "shared"
    # An explicit user/account mapping: only these peers may attach a Ticket.
    shared_with_user_ids: list[str] = Field(default_factory=list, max_length=20)
    policy: ContextScopePolicy = Field(default_factory=ContextScopePolicy)
    # Optional first Ticket to bind at creation time.
    ticket_id: str | None = Field(None, max_length=64)


class ContextScopeAttach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., min_length=1, max_length=64)


class ContextScopeView(BaseModel):
    scope_id: str
    user_id: str
    name: str
    kind: str
    shared_with_user_ids: list[str] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)
    ticket_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class A2AProvenance(BaseModel):
    """Who produced a message; trust is always untrusted data, never authority."""

    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["agent", "ticket"] = "agent"
    trust: Literal["untrusted"] = "untrusted"
    source_id: str = Field(..., min_length=1, max_length=128)
    source_thread_key: str = Field("", max_length=512)
    actor: str = Field(..., min_length=1, max_length=128)
    summary: str = Field("", max_length=500)


class A2AEnvelope(BaseModel):
    """Versioned wire contract for one agent-to-agent message."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["conductor.a2a/1"] = "conductor.a2a/1"
    message_id: UUID
    sender_ticket_id: UUID
    recipient_ticket_id: UUID
    intent: A2AIntent
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=8000)
    allowed_actions: list[A2AAllowedAction] = Field(default_factory=list, max_length=8)
    provenance: A2AProvenance
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _time_window_is_bounded(self) -> "A2AEnvelope":
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("issued_at and expires_at must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if (self.expires_at - self.issued_at).total_seconds() > 86400:
            raise ValueError("an A2A message may live at most 24 hours")
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must not repeat")
        return self


class A2AAgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field("", max_length=120)


class A2AAgentView(BaseModel):
    agent_id: str
    ticket_id: str
    user_id: str
    name: str = ""
    revoked_at: datetime | None = None
    created_at: datetime


class A2AMessageView(BaseModel):
    message_id: str
    sender_ticket_id: str
    recipient_ticket_id: str
    agent_id: str = ""
    scope_id: str = ""
    intent: str
    subject: str
    body: str
    allowed_actions: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime
    expires_at: datetime
    status: str
    input_id: str | None = None
    created_at: datetime


class A2AMessageList(BaseModel):
    messages: list[A2AMessageView] = Field(default_factory=list)


class A2AAgentCreated(A2AAgentView):
    # The token is returned exactly once, at registration.
    token: str


class A2AAgentList(BaseModel):
    agents: list[A2AAgentView] = Field(default_factory=list)
