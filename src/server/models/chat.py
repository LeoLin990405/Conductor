"""
Request and response models for Chat API endpoint.

This module defines Pydantic models for the /api/v1/threads/messages endpoints
that use the ptc-agent library for code execution in Daytona sandboxes.
"""

import copy
import uuid
from typing import Annotated, Any, Dict, List, Literal, Mapping, Optional, Union

from pydantic import AfterValidator, BaseModel, Field, field_validator

from src.server.models.additional_context import AdditionalContext

from src.llms.reasoning import ReasoningLevel


def _uuid_text(value: str) -> str:
    # The key is stored in a UUID column, so anything else failed at the
    # insert as a 500; refused here it is the client's 422.
    try:
        return str(uuid.UUID(value))
    except ValueError:
        # uuid.UUID's own sentence quotes the normalized text it failed to parse
        # (hyphens removed, so an exact cut of the caller's value never matches).
        raise ValueError("request key must be a UUID") from None


RequestKey = Annotated[str, AfterValidator(_uuid_text)]

# Roles the chat path can VALIDLY persist into the ``messages`` channel. The
# chat path writes ``{"role", "content"}`` dicts straight in; under DeltaChannel
# that raw write is stored BEFORE the reducer runs, so a role must clear TWO bars:
#
#   1. ``convert_to_messages`` must rebuild it from ``{"role", "content"}`` alone
#      — it is replayed on every reconstruction. ``tool``/``function`` fail here:
#      they map to ToolMessage/FunctionMessage and need ``tool_call_id``/``name``
#      ``ChatMessage`` can't supply, so they raise on every resume and brick the
#      thread.
#   2. langgraph's ``ensure_message_ids`` must STAMP it at ``put_writes`` (role in
#      langgraph's ``_MESSAGE_ROLES``). An accepted-but-unstamped role persists
#      id-less; the hard-stop checkpoint flush / offload write the full list back
#      via ``aupdate_state``, and the non-minting reducer can't dedup an id-less
#      message against that copy, so it DUPLICATES.
#
# ``developer`` clears bar 1 (``convert_to_messages`` maps it to SystemMessage) but
# FAILS bar 2 (not in langgraph's ``_MESSAGE_ROLES``), so it is excluded — clients
# wanting developer semantics send ``system``, which maps identically and stamps.
# ``role`` is client-controlled; reject the rest with a clean 422 before persisting.
_VALID_MESSAGE_ROLES = frozenset(
    {"user", "assistant", "system", "human", "ai"}
)


# =============================================================================
# HITL (Human-in-the-Loop) Models
# =============================================================================


class HITLDecision(BaseModel):
    """Decision for a single HITL action request."""

    type: Literal["approve", "reject"] = Field(
        description="Whether to approve or reject the action"
    )
    message: Optional[str] = Field(
        None,
        description="Feedback message, typically used when rejecting to explain why or request changes",
    )


class HITLResponse(BaseModel):
    """Response to a HITL interrupt: positional decisions.

    ``decisions`` answers the stock interrupt, where slot i is the answer to
    action request i.
    """

    decisions: List[HITLDecision] = Field(
        default_factory=list,
        description="List of decisions corresponding to each action request in the interrupt",
    )


REJECTED_WITH_FEEDBACK = "User rejected this action with the following feedback: "
REJECTED_WITHOUT_FEEDBACK = "User rejected this action. No specific feedback was provided."


def _format_rejection_message(user_feedback: Optional[str]) -> str:
    """The tool message the agent reads after a rejection.

    Worded for any interrupted action, not only a plan, so the model does not
    go rewriting a plan it never had.
    """
    if user_feedback and user_feedback.strip():
        return REJECTED_WITH_FEEDBACK + user_feedback.strip()
    return REJECTED_WITHOUT_FEEDBACK


def rejection_feedback(message: Optional[str]) -> str:
    """What the user wrote when rejecting, from a message the resume path
    reworded, for a tool that words its own result: quoting the reworded
    message back puts the no-feedback sentence in the user's mouth."""
    message = (message or "").strip()
    if message == REJECTED_WITHOUT_FEEDBACK:
        return ""
    return message.removeprefix(REJECTED_WITH_FEEDBACK).strip()


def serialize_hitl_response_map(hitl_response: Mapping[str, Any]) -> Dict[str, dict]:
    """Convert validated HITLResponse models into plain dicts for LangGraph resume.

    LangChain's HumanInTheLoopMiddleware expects `resume` payloads to be
    subscriptable dicts (e.g. `{"decisions": [...]}`), not Pydantic model
    instances.

    For rejection decisions, this function also formats the message to clearly
    indicate that the user rejected the plan and includes their feedback.
    """

    serialized: Dict[str, dict] = {}
    for interrupt_id, response in hitl_response.items():
        if hasattr(response, "model_dump"):
            response_dict = response.model_dump()  # type: ignore[call-arg]
        elif hasattr(response, "dict"):
            response_dict = response.dict()  # type: ignore[call-arg]
        elif isinstance(response, dict):
            response_dict = copy.deepcopy(
                response
            )  # Deep copy to avoid mutating nested structures
        else:
            raise TypeError(
                "Unsupported HITL response type: "
                f"interrupt_id={interrupt_id} type={type(response)!r}"
            )

        # Format rejection messages to be clear and include feedback
        if "decisions" in response_dict:
            for decision in response_dict["decisions"]:
                if decision.get("type") == "reject":
                    decision["message"] = _format_rejection_message(
                        decision.get("message")
                    )

        serialized[interrupt_id] = response_dict

    return serialized


def summarize_hitl_response_map(hitl_response: Mapping[str, Any]) -> Dict[str, Any]:
    """Summarize a HITL response map for persistence.

    Returns:
        Dict with keys:
            - feedback_action: "APPROVED" if all decisions approve; else "DECLINED"
            - content: concatenated reject messages (may be empty)
            - interrupt_ids: list of interrupt ids present
    """

    interrupt_ids = list(hitl_response.keys())
    reject_messages: List[str] = []
    any_reject = False

    for interrupt_id, response in hitl_response.items():
        if hasattr(response, "decisions"):
            decisions = list(getattr(response, "decisions") or [])
        elif isinstance(response, dict):
            decisions = list(response.get("decisions") or [])
        else:
            raise TypeError(
                "Unsupported HITL response type: "
                f"interrupt_id={interrupt_id} type={type(response)!r}"
            )

        for decision in decisions:
            if hasattr(decision, "type"):
                decision_type = getattr(decision, "type")
                message = getattr(decision, "message", None)
            elif isinstance(decision, dict):
                decision_type = decision.get("type")
                message = decision.get("message")
            else:
                raise TypeError(
                    "Unsupported HITL decision type: "
                    f"interrupt_id={interrupt_id} type={type(decision)!r}"
                )

            if decision_type == "reject":
                any_reject = True
                if message:
                    msg = str(message).strip()
                    if msg and msg not in reject_messages:
                        reject_messages.append(msg)

    feedback_action = "DECLINED" if any_reject else "APPROVED"
    content = "\n".join(reject_messages) if reject_messages else ""

    return {
        "feedback_action": feedback_action,
        "content": content,
        "interrupt_ids": interrupt_ids,
    }


# =============================================================================
# Common Message Types
# =============================================================================


class ContentItem(BaseModel):
    type: str = Field(..., description="The type of content (text, image, etc.)")
    text: Optional[str] = Field(None, description="The text content if type is 'text'")
    image_url: Optional[str] = Field(
        None, description="The image URL if type is 'image'"
    )


class ChatMessage(BaseModel):
    role: str = Field(
        ..., description="The role of the message sender (user or assistant)"
    )
    content: Union[str, List[ContentItem]] = Field(
        ...,
        description="The content of the message, either a string or a list of content items",
    )

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        """Reject roles unsafe under delta replay — see ``_VALID_MESSAGE_ROLES``.

        An unsupported role either raises in ``convert_to_messages`` on every
        reconstruction (a bricked thread) or persists id-less and duplicates on
        the hard-stop flush; both are rejected with a 422 before anything persists.
        """
        if v not in _VALID_MESSAGE_ROLES:
            allowed = ", ".join(sorted(_VALID_MESSAGE_ROLES))
            # The role is not repeated: it is caller text (a pasted credential
            # lands in a field like this), and a repr() of it defeats any
            # attempt to cut the exact value back out of the sentence.
            raise ValueError(f"Unsupported message role. Must be one of: {allowed}.")
        return v


# =============================================================================
# Chat Request/Response Models
# =============================================================================


class ThreadOrigin(BaseModel):
    """Who initiated a thread — stored under the thread's metadata['origin'].

    'agent' = spawned by another agent (id = the dispatching thread's id),
    'automation' = scheduled/triggered run (id = automation id), 'system' =
    set aside for platform-initiated threads. The label is advisory —
    client-supplied, not authenticated, and nothing branches on it.
    User-initiated threads carry no origin at all rather than an explicit
    'user' entry. ``name`` is display only (the history label above a
    message). ``agent`` labels it too, and the turn's run line names it as
    acting for the user; the rest of the model's context reads ``type`` alone.
    """

    type: Literal["agent", "automation", "system"]
    id: Optional[str] = Field(default=None, max_length=100)
    # automation: the automation's name at send time, capped as automations cap it
    name: Optional[str] = Field(default=None, max_length=255)
    # agent: which agent dispatched the thread
    agent: Optional[str] = None


class GeneralistReport(BaseModel):
    """A dispatched generalist's thread reporting back, and its workspace."""

    kind: Literal["generalist"]
    thread_id: str
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None


class TaskReport(BaseModel):
    """A background subagent task in this thread, finished or failed."""

    kind: Literal["task"]
    task_id: str
    display_id: str
    description: Optional[str] = None
    failed: bool = False


# What a report-back turn announces, kept on its query row for display only.
# The turn's own message is model-facing and hidden from the user, so history
# shows a labelled row built from this instead. Only the server's own
# report-back requests keep it: any other request has it dropped once parsed,
# so a malformed one is still refused there.
ReportSource = Annotated[GeneralistReport | TaskReport, Field(discriminator="kind")]


class ChatRequest(BaseModel):
    """Request model for streaming chat endpoint."""

    # Identity fields (user_id comes from Bearer token JWT sub claim)
    workspace_id: Optional[str] = Field(
        default=None,
        description="Workspace the turn runs in; Home's id (POST /workspaces/home) "
        "runs it in the user's Home. Required on a new thread; a follow-up "
        "turn defaults to its thread's.",
    )

    # Messages
    messages: List[ChatMessage] = Field(
        default_factory=list,
        description="History of messages between the user and the assistant",
    )

    # Agent options
    subagents_enabled: Optional[List[str]] = Field(
        default=None,
        description="List of subagent names to enable (default: from config)",
    )
    steer_only: bool = Field(
        default=False,
        description="When True, this POST may only steer an in-flight workflow; "
        "if the thread has none, reject with admission-conflict "
        "code='not_running' (an SSE error event once the stream has started, "
        "HTTP 409 otherwise) instead of starting a new turn. Set by gateway "
        "steer probes, whose SSE readers ignore turn events — without this, a "
        "steer racing the workflow's exit silently spawns a turn whose output "
        "(interrupts included) is dropped.",
    )

    # Idempotency (v4 attempt chain): one logical send = one request_key,
    # reused verbatim across network retransmits. The server dedups on it
    # (globally unique), so a lost-response resubmit reconnects to the
    # existing run instead of starting a duplicate. Server-minted fallback
    # when omitted (no dedup).
    request_key: Optional[RequestKey] = Field(
        default=None,
        description="Client-generated UUID identifying this logical send; "
        "reuse across retransmits for idempotent delivery.",
    )
    # Internal retry provenance: only the /retry route sets it, as a
    # parameter to _handle_send_message — a body-supplied value is
    # overwritten there unconditionally, so clients cannot chain a new
    # attempt onto an arbitrary failed run. The new run reuses the
    # turn_index with attempt_no+1 and no new query row.
    retry_of_run_id: Optional[str] = Field(
        default=None,
        description="Run id this request is an attempt-chain retry of. "
        "Internal use only; ignored when supplied by clients.",
    )

    # Interrupt/resume support (HITL)
    hitl_response: Optional[Dict[str, HITLResponse]] = Field(
        default=None,
        description="Structured HITL response: {interrupt_id: HITLResponse}. "
        "Use this to respond to interrupt events with approve/reject decisions.",
    )
    checkpoint_id: Optional[str] = Field(
        default=None, description="Specific checkpoint ID to resume from"
    )
    fork_from_turn: Optional[int] = Field(
        default=None,
        ge=0,
        description="Marks the request as an edit/regenerate fork at checkpoint_id, "
        "naming the turn it replaces. The server reads that turn from the "
        "checkpoint and deletes queries/responses from it onward.",
    )
    expected_latest_run_id: Optional[str] = Field(
        default=None,
        description="The thread's newest run as this client last saw it: the "
        "replay's latest_run_id, or a run the client started or joined since. A "
        "fork is refused (409 stale_fork) when the thread's latest run is a "
        "different one, so an edit or regenerate from a view that missed a turn "
        "cannot delete it. Omit to skip the check.",
    )
    # Server-stamped: the thread's branch tip when the route read the fork's
    # turn. Read only for a fork, whose validation always overwrites it, so a
    # client value never reaches START, which admits the fork only while the
    # tip still holds this value.
    fork_branch_tip: Optional[str] = Field(
        default=None,
        description="Branch tip the fork was validated against. Server-stamped.",
    )
    # Server-stamped beside fork_branch_tip: the newest run_seq read with it.
    fork_run_horizon: Optional[int] = Field(
        default=None,
        description="Newest run sequence read with fork_branch_tip. Server-stamped.",
    )
    # Server-stamped: the newest run_seq read before the route found a HITL
    # answer's pause on the branch. Read only for an answer, whose route always
    # overwrites it. START refuses the answer if a run started since, so a fork
    # admitted after that check cannot leave it running on the replaced pause.
    resume_run_horizon: Optional[int] = Field(
        default=None,
        description="Newest run sequence read with the pause a HITL answer "
        "resumes. Server-stamped.",
    )

    # Localization and context
    locale: Optional[str] = Field(
        default=None, description="Locale for output language, e.g., 'en-US' or 'zh-CN'"
    )
    timezone: Optional[str] = Field(
        default=None,
        description="IANA timezone identifier (e.g., 'America/New_York', 'Asia/Shanghai')",
    )

    # Skill loading
    additional_context: Optional[List[AdditionalContext]] = Field(
        default=None,
        description="Additional context to be included. Supports: skills (skill instructions)",
    )

    # LLM selection (optional)
    llm_model: Optional[str] = Field(
        default=None,
        description=(
            "Model name from models.json or the user's own models (e.g., "
            "'minimax-m3', 'claude-sonnet-4-5'). Omitted, the turn runs the "
            "thread's llm_model, else the account default, else the deployment "
            "default. A named model is stored on the thread once the turn is "
            "admitted, so later turns keep it."
        ),
    )

    # Native runtime binding for a thread this send creates. A native thread
    # is executed by the vendor CLI, not by the PTC graph; existing threads
    # keep the binding stored on the row and ignore this field.
    native_provider: Optional[str] = Field(
        default=None,
        description=(
            "Start a thread on a vendor-native agent CLI (deepseek-dsh, codex, "
            "claude-code, cursor, kimi, zcode, minimax, mimo, grok, stepfun), "
            "or 'auto' to use capability-aware routing. Gemini and Qwen are "
            "PTC-only by design. Ignored for an existing thread; the binding is "
            "immutable after creation."
        ),
    )
    routing_capabilities: Optional[List[str]] = Field(
        default=None,
        description="Required capabilities when native_provider='auto'.",
    )

    # Unrelated to ``subagents_enabled``, which names the subagents a build
    # compiles.
    subagents_allowed: Optional[bool] = Field(
        default=None,
        description="The subagent switch for a thread this send creates; "
        "omitted, or the side the user's default is on, the thread follows "
        "that default. Ignored for an existing thread, which "
        "PATCH /threads/{id} changes.",
    )

    swarm: bool = Field(
        default=False,
        description="Start Swarm mode for a thread this send creates. "
        "Ignored for an existing thread, which PATCH /threads/{id} changes.",
    )

    # Reasoning effort override (optional - defaults to model's configured level)
    # The full canonical vocabulary, not a subset: a level the chosen model does
    # not declare is clamped in LLM.__init__, which is the only layer that can
    # see the model's enum. Narrowing here instead turns an unsupported level
    # into a 422 the caller cannot act on.
    reasoning_effort: Optional[ReasoningLevel] = Field(
        default=None,
        description="Reasoning effort for this turn only. Omitted, the turn "
        "runs the thread's own (PATCH /threads/{id}), else the user's "
        "Settings. Never stored on the thread.",
    )

    fast_mode: Optional[bool] = Field(
        default=None,
        description="When True, enables priority service tier for faster "
        "responses (Codex OAuth models only), for this turn only. Omitted, the "
        "turn runs the thread's own, else the user's Settings.",
    )

    # Internal: query type override (e.g., "system" for report-back invocations)
    query_type: Optional[str] = Field(
        default=None,
        description="Override query_type for this request. Internal use only.",
    )

    # Internal: PTC thread id whose report-back this run consumes. When a
    # report-back run (dispatched after PTC completion) finishes, its
    # completion hook clears the durable watch-set entry for this PTC thread.
    report_back_ptc_thread_id: Optional[str] = Field(
        default=None,
        description="PTC thread id this report-back run consumes. "
        "Internal use only.",
    )

    # Internal: the thread watching this dispatched PTC run, in Home. Stamped
    # into the run's START metadata so finalize can build report-back hook
    # ordering keys (all completions reporting into one Chief of Staff thread
    # serialize) without I/O. An ordering hint only — executors re-resolve
    # the authoritative origin from Redis at execution time.
    origin_home_thread_id: Optional[str] = Field(
        default=None,
        description="Thread watching this dispatched PTC run. "
        "Internal use only.",
    )

    # Internal: dispatch-incarnation token for the report-back pair, minted
    # per reserve(). Stamped into START metadata; terminal teardowns compare
    # it against the live origin so a stale clear can never destroy a
    # re-dispatched pair's watch state.
    origin_dispatch_gen: Optional[str] = Field(
        default=None,
        description="Report-back dispatch generation token. Internal use only.",
    )

    # Internal: what a report-back turn announces, stamped on its query row so
    # history can label the turn whose own message is hidden. Display only.
    report_source: Optional[ReportSource] = Field(
        default=None,
        description="What this report-back turn reports on. Internal use only.",
    )

    # Internal: build this turn's agent without the subagent machinery
    # (no Task/TaskOutput tools). Task report-back turns deliberately do
    # NOT set it — TaskOutput is their retrieval path; re-announce
    # recursion is prevented by the outbox's ledger arbitration
    # (result_delivered_at), not structurally.
    disable_subagents: Optional[bool] = Field(
        default=None,
        description="Build this turn's agent without subagent tools. "
        "Internal use only.",
    )

    # Internal: the parked steer this turn is posted for. START claims the
    # job in its own transaction, so a steer some running turn already read
    # never also becomes a turn of its own.
    steer_job_id: Optional[str] = Field(
        default=None,
        description="The steer job this turn carries. Internal use only.",
    )

    # Server-stamped: the burst-guard ZSET member held by this request's
    # admission. The route overwrites whatever a client sent (a forged value
    # could only orphan the caller's own slot until reap, but stamping keeps
    # the release chain trustworthy end to end).
    burst_slot_id: Optional[str] = Field(
        default=None,
        description="Burst-guard slot held for this request. Server-stamped.",
    )

    # External thread identity (for channel integrations like Telegram, Slack)
    external_thread_id: Optional[str] = Field(
        default=None,
        description="Stable external thread identifier (e.g. 'chat_id:topic_id'). "
        "When provided with platform, conductor resolves to an existing thread or creates a new one.",
    )
    # Format: lowercase prefix (a-z, underscore), optionally `:<SUFFIX>` where
    # the suffix is an opaque tag the surface owns — it starts with a letter OR
    # digit and accepts A-Z, 0-9, `.` and `-`, so a digit-first tag stays valid
    # and the pattern must not require a leading [A-Z].
    # Current namespace: 'web', 'telegram', 'slack', 'discord', 'feishu'.
    # Reserved prefixes shouldn't be reused for other origins.
    platform: Optional[str] = Field(
        default=None,
        pattern=r"^[a-z_]+(:[A-Z0-9][A-Z0-9.-]*)?$",
        max_length=50,
        description="Surface the chat originated from. Examples: 'web', "
        "'telegram', 'slack', 'discord', 'feishu'. Absent for "
        "system-initiated threads (see origin).",
    )
    # The client that draws a surface owns what that surface accepts, so the
    # rules for it are text on the request rather than a branch in conductor.
    # conductor ships built-in rules only for the surface it renders itself
    # (`web`), and this field replaces them when it is sent.
    surface_rules: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Delivery rules for the surface this turn arrived on, "
        "written by the client that owns the surface (a channel gateway). "
        "Honoured only on a service-token request; a user's own value is "
        "dropped. conductor carries them to the model only when they are news "
        "to the thread: the first turn, a change of surface or of the text, the "
        "first turn after a compaction. The client sends them on every request; "
        "conductor decides whether to restate.",
    )
    # Orthogonal to platform: platform = which user surface, origin = who
    # initiated. Absent origin = user-initiated (the common case is never
    # written). Affects the caller's own thread labeling and the delivery
    # rules of its own turn, so it is accepted from any client without gating.
    origin: Optional["ThreadOrigin"] = Field(
        default=None,
        description="Who sent this turn. Recorded on the thread at creation "
        "and read on every turn for the turn's delivery rules, so an "
        "automation sends it on every request; a turn without it is a "
        "person's, even on a thread an automation started.",
    )

    @field_validator("surface_rules")
    @classmethod
    def _normalize_surface_rules(cls, v: Optional[str]) -> Optional[str]:
        """Blank text is no rules, not empty rules.

        A gateway with nothing to say may send an empty string or drop the key,
        and both have to land in the same state: the built-in behavior, and a
        rules key that carries no digest of nothing.
        """
        if v is None:
            return None
        return v.strip() or None


# =============================================================================
# Utility Request Models
# =============================================================================


class TTSRequest(BaseModel):
    text: str = Field(..., description="The text to convert to speech")
    voice_type: Optional[str] = Field(
        "BV700_V2_streaming", description="The voice type to use"
    )
    encoding: Optional[str] = Field("mp3", description="The audio encoding format")
    speed_ratio: Optional[float] = Field(1.0, description="Speech speed ratio")
    volume_ratio: Optional[float] = Field(1.0, description="Speech volume ratio")
    pitch_ratio: Optional[float] = Field(1.0, description="Speech pitch ratio")
    text_type: Optional[str] = Field("plain", description="Text type (plain or ssml)")
    with_frontend: Optional[int] = Field(
        1, description="Whether to use frontend processing"
    )
    frontend_type: Optional[str] = Field("unitTson", description="Frontend type")


class GeneratePodcastRequest(BaseModel):
    content: str = Field(..., description="The content of the podcast")


class SubagentCancelRequest(BaseModel):
    """Request model for stopping several background tasks at once."""

    task_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Task ids to stop; each is an independent cancel intent.",
    )


class SubagentMessageRequest(BaseModel):
    """Request model for sending a message to a running subagent."""

    content: str = Field(..., description="The instruction/message to send to the subagent")
    input_id: Optional[str] = Field(
        None,
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
        description=(
            "Client-chosen id for this instruction, echoed on the "
            "steering_delivered entry or steering_returned frame that settles "
            "it. The delivery can arrive before this request answers, so the "
            "client names the instruction up front. Generated when omitted."
        ),
    )


