"""Pydantic models for User and UserPreferences."""

from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from src.server.models.profile_vocabulary import STRUCTURED_PREFERENCES


# =============================================================================
# JSONB Schema Models (for validation)
# =============================================================================


def _doc(column: str, key: str) -> str:
    """A structured key's description, the one its vocabulary carries."""
    return STRUCTURED_PREFERENCES[column][key].doc


class AgentPreference(BaseModel):
    """AI agent behavior preferences stored in JSONB."""

    model_config = ConfigDict(extra="allow")

    # The structured keys are Any and checked in the endpoint instead
    # (``check_profile`` in ``profile_vocabulary``). Every response model
    # parses the stored row, so a type here would fail the read of a value
    # saved before the key had one; and a vocabulary refusal and a type
    # refusal then share one 422 shape.
    # A null survives the parse too, which the JSONB merge reads as deleting
    # the key: Settings' switch sends one to go back to markdown.
    research_depth: Optional[Any] = Field(None, description=_doc("agent_preference", "research_depth"))
    deliverable: Optional[Any] = Field(None, description=_doc("agent_preference", "deliverable"))
    proactivity: Optional[Any] = Field(None, description=_doc("agent_preference", "proactivity"))
    output_style: Optional[str] = Field(
        None, description="Preferred output style"
    )
    output_format: Optional[Any] = Field(None, description=_doc("agent_preference", "output_format"))


class OtherPreference(BaseModel):
    """Miscellaneous preferences stored in JSONB."""

    model_config = ConfigDict(extra="allow")

    # Strict: a setting that skips approval cards is refused unless it is a
    # real boolean, rather than guessed from "yes" or 1.
    auto_approve_handoffs: Optional[StrictBool] = Field(
        None, description="Hand work to an analyst without asking; absent means ask."
    )
    auto_approve_workspace_creation: Optional[StrictBool] = Field(
        None, description="Create workspaces without asking; absent means ask."
    )


class ModelPreference(BaseModel):
    """Model configuration stored in JSONB: catalog, routing, and tuning.

    Open like its sibling because the key set is owned by the resolver, not by
    this schema. ``profiles`` is the one key with structure the DB layer knows
    about — it merges per model name rather than wholesale.
    """

    model_config = ConfigDict(extra="allow")


# =============================================================================
# User Models
# =============================================================================


class UserBase(BaseModel):
    """Base user fields."""

    email: Optional[str] = Field(None, max_length=255, description="User email")
    name: Optional[str] = Field(None, max_length=255, description="User display name")
    avatar_url: Optional[str] = Field(None, description="URL to user avatar")
    timezone: Optional[str] = Field(
        None, max_length=100, description="User timezone (e.g., 'America/New_York')"
    )
    locale: Optional[str] = Field(
        None, max_length=20, description="User locale (e.g., 'en-US')"
    )


class UserCreate(UserBase):
    """Request model for creating a new user."""

    user_id: str = Field(
        ..., max_length=255, description="External auth ID (e.g., Clerk, Auth0)"
    )


class UserUpdate(UserBase):
    """Request model for updating user profile."""

    onboarding_completed: Optional[bool] = Field(
        None, description="Whether onboarding is completed"
    )
    personalization_completed: Optional[bool] = Field(
        None, description="Whether personalization wizard is completed"
    )


class UserResponse(UserBase):
    """Response model for user details."""

    user_id: str = Field(description="User ID")
    onboarding_completed: bool = Field(
        default=False, description="Whether onboarding is completed"
    )
    personalization_completed: bool = Field(
        default=False, description="Whether personalization wizard is completed"
    )
    has_api_key: bool = Field(
        default=False, description="Whether user has at least one API key configured"
    )
    has_oauth_token: bool = Field(
        default=False, description="Whether user has at least one OAuth token connected"
    )
    access_tier: int = Field(
        default=-1, description="Platform access tier. -1 = no access, 0+ = tier level."
    )
    plan_display_name: Optional[str] = Field(
        default=None,
        description="Display name of the user's active plan (e.g. 'Pro'). None when no subscription.",
    )
    auth_provider: Optional[str] = Field(
        None, description="Authentication provider (e.g. google, github, email)"
    )
    created_at: datetime = Field(description="Creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")
    last_login_at: Optional[datetime] = Field(None, description="Last login timestamp")

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# User Preferences Models
# =============================================================================


class UserPreferencesBase(BaseModel):
    """Base preferences fields."""

    # ``model_preference`` collides with Pydantic's reserved ``model_`` prefix;
    # the column name predates the schema and is the one users see in the API.
    model_config = ConfigDict(protected_namespaces=())

    agent_preference: Optional[AgentPreference] = Field(
        default_factory=AgentPreference, description="AI agent behavior settings"
    )
    other_preference: Optional[OtherPreference] = Field(
        default_factory=OtherPreference, description="Miscellaneous preferences"
    )
    model_preference: Optional[ModelPreference] = Field(
        default_factory=ModelPreference, description="Model catalog, routing and tuning settings"
    )


class UserPreferencesCreate(UserPreferencesBase):
    """Request model for creating user preferences."""

    pass


class UserPreferencesUpdate(UserPreferencesBase):
    """Request model for updating user preferences."""

    apply_default_to: Optional[Literal["new_threads", "existing_threads"]] = Field(
        None,
        description="This write's answer to what a changed default model does "
        "to existing threads: 'existing_threads' moves the threads still on the "
        "old default to the new one, 'new_threads' leaves them. Never stored; "
        "absent, model_preference.default_model_scope decides.",
    )


class UserPreferencesResponse(UserPreferencesBase):
    """Response model for user preferences."""

    user_preference_id: UUID = Field(description="Preference record ID")
    user_id: str = Field(description="User ID")
    created_at: datetime = Field(description="Creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")

    model_config = ConfigDict(from_attributes=True)


class UserPreferencesUpdateResponse(UserPreferencesResponse):
    """Response model for a preferences write."""

    threads_reassigned: int = Field(
        0,
        description="Threads this write moved from the old default model to "
        "the new one (see apply_default_to).",
    )


class UserWithPreferencesResponse(BaseModel):
    """Combined response for GET /users/me endpoint."""

    user: UserResponse = Field(description="User profile")
    preferences: Optional[UserPreferencesResponse] = Field(
        None, description="User preferences (may be null if not set)"
    )
