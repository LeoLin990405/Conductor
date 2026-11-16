"""Wire shapes for the Super-agent deliberation API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class DeliberationParticipantIn(BaseModel):
    agent_key: str = Field(..., min_length=1, max_length=64)
    role: Literal["participant", "planner"]
    adapter: Literal["mock", "cli", "llm", "daemon"] = "mock"
    daemon_id: str | None = Field(None, max_length=64)
    display_name: str = Field("", max_length=120)


class DeliberationCreate(BaseModel):
    objective: str = Field(..., min_length=1, max_length=20000)
    max_rounds: int = Field(3, ge=1, le=10)
    participants: list[DeliberationParticipantIn] = Field(..., min_length=3, max_length=8)


class DeliberationProviderGroup(BaseModel):
    group_key: str = Field(..., min_length=1, max_length=64)
    agent_keys: list[str] = Field(..., min_length=1, max_length=8)


class DeliberationStart(BaseModel):
    script: Literal["consensus", "holdout"] = "consensus"
    holdout_key: str = Field("grok", max_length=64)
    provider_groups: list[DeliberationProviderGroup] | None = Field(
        None, min_length=1, max_length=8
    )


class DeliberationContinue(BaseModel):
    objective: str = Field(..., min_length=1, max_length=20000)
    additional_context: str | None = Field(None, max_length=20000)
    max_rounds: int | None = Field(None, ge=1, le=10)
