"""Shapes of deliberation rows. Documentation only; the store keeps dicts."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class PlanStep(TypedDict):
    step_key: str
    title: str
    prompt: str
    assigned_agent: str
    depends_on: list[str]


class Plan(TypedDict):
    summary: str
    steps: list[PlanStep]


class ParticipantRow(TypedDict):
    participant_id: str
    deliberation_id: str
    agent_key: str
    role: str
    adapter: str
    display_name: str
    daemon_id: str | None


class MessageRow(TypedDict):
    message_id: str
    deliberation_id: str
    round_id: str
    round_index: int
    participant_id: str
    agent_key: str
    kind: str
    body: str
    plan: Plan | None
    created_at: str


class VoteRow(TypedDict):
    vote_id: str
    deliberation_id: str
    round_id: str
    round_index: int
    participant_id: str
    agent_key: str
    ballot: str
    comment: str


class DissentRow(TypedDict):
    dissent_id: str
    deliberation_id: str
    participant_id: str
    round_id: str
    round_index: int
    agent_key: str
    reason: str


class RoundRow(TypedDict):
    round_id: str
    deliberation_id: str
    round_index: int
    kind: str
    provider_groups: list[dict]
    status: str
    created_at: str
    closed_at: str | None


class StepRow(TypedDict):
    step_id: str
    deliberation_id: str
    step_key: str
    title: str
    prompt: str
    assigned_agent: str
    depends_on: list[str]
    status: str
    daemon_id: str | None
    child_ticket_id: str | None


class TranscriptEntry(TypedDict):
    round_index: int
    agent_key: str
    kind: str
    body: str
    plan: Plan | None


class Aggregate(TypedDict):
    deliberation_id: str
    ticket_id: str
    user_id: str
    objective: str
    status: str
    round_index: int
    max_rounds: int
    planner_agent_key: str
    thread_key: str
    consensus_plan: Plan | None
    summary: str
    parent_deliberation_id: str | None
    decision_reason: str
    created_at: str
    updated_at: str
    participants: list[ParticipantRow]
    rounds: list[RoundRow]
    messages: list[MessageRow]
    votes: list[VoteRow]
    dissents: list[DissentRow]
    steps: list[StepRow]
    transcript: NotRequired[list[TranscriptEntry]]
    handoff: NotRequired[dict | None]
