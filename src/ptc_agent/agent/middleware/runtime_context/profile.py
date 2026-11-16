"""The profile tier: the snapshot the epoch freezes, and how a change reads.

The profile moves underneath a thread from the platform side, so it follows the
same rule as a file: the block keeps the epoch's snapshot and a row carries the
delta. The hash is taken over the rendered fields rather than the raw profile,
so a field the block never shows cannot move the block.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ptc_agent.agent.middleware.runtime_context.changes import sha256_text
from ptc_agent.agent.middleware.runtime_context.state import as_dict
from ptc_agent.agent.middleware.runtime_context.templates import render_template
from src.server.models.profile_vocabulary import prompt_preferences

_PROFILE_LABELS = {
    "name": "Name",
    "timezone": "Timezone",
    "locale": "Locale",
}

def line_text(value: Any) -> str | None:
    """A profile value as the block prints it on its line, or None when the
    key holds nothing (null, "", [] or {}) and has no line. A list reads
    joined in words; false and 0 are values, and print."""
    if not value and not isinstance(value, (int, float)):
        return None
    return ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)




@dataclass(frozen=True, slots=True)
class ProfileSwitches:
    """What the build has that the profile block's steering points to: the
    chat-app settings folder (``channels``, None for a build whose block says
    nothing of it)."""

    channels: bool | None = None


def render_user_profile(
    user_profile: dict[str, Any],
    *,
    main_agent_steering: bool,
    switches: ProfileSwitches = ProfileSwitches(),
    role: str = "generalist",
) -> str:
    """The ``components/user_profile.md.j2`` block for one profile.

    The profile is shaped here, at render time, so the template only prints:
    ``agent_preference`` splits into the keys printed as the user wrote them
    and the structured keys whose value selects guidance. A snapshot keeps the
    profile as it was read, so an epoch frozen before any of this renders the
    same way.
    """
    printed, steering = prompt_preferences(as_dict(user_profile.get("agent_preference")))
    return render_template(
        "components/user_profile.md.j2",
        user_profile=user_profile,
        preference_lines=[
            (key, text) for key, value in printed.items() if (text := line_text(value)) is not None
        ],
        agent_steering=steering,
        main_agent_steering=main_agent_steering,
        channels_enabled=bool(switches.channels),
        role=role,
    )


def profile_read_answered(user_profile: dict[str, Any] | None) -> bool:
    """Whether the platform read behind ``user_profile`` answered.

    An empty profile is a read that did not answer rather than a profile the
    user cleared; nothing is added to it after the read.
    """
    return bool(user_profile)


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    """The user's profile and data counts as one turn saw them."""

    user_profile: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_state(cls, value: Any) -> ProfileSnapshot | None:
        data = as_dict(value)
        if not data:
            return None
        return cls(
            user_profile=as_dict(data.get("user_profile")),
        )

    def to_state(self) -> dict[str, Any]:
        return {
            "user_profile": dict(self.user_profile),
        }

    def fields(self) -> dict[str, str]:
        """Label to rendered value, one entry per line the block shows.

        Each agent preference key is its own field so one changed key names
        only itself.
        """
        rendered: dict[str, str] = {}
        profile = self.user_profile
        for key in ("name", "timezone", "locale"):
            if profile.get(key):
                rendered[_PROFILE_LABELS[key]] = str(profile[key])
        for key, value in sorted(as_dict(profile.get("agent_preference")).items()):
            # Every key, the ones that select guidance among them: a move in
            # one moves the block. A key holding nothing has no line there,
            # so it is no field here either.
            text = line_text(value)
            if text is not None:
                rendered[f"Preference {key}"] = text
        return rendered

    def sha(self) -> str:
        """Hash of what the block renders, so a field it never shows cannot move it."""
        return sha256_text(json.dumps(self.fields(), sort_keys=True, default=str))


def profile_diff_lines(
    frozen: ProfileSnapshot, current: ProfileSnapshot
) -> list[str]:
    before = frozen.fields()
    after = current.fields()
    lines: list[str] = []
    for label in sorted(before.keys() | after.keys()):
        was, now = before.get(label), after.get(label)
        if was == now:
            continue
        if was is None:
            lines.append(f"{label}: {now} (not in the frozen block)")
        elif now is None:
            lines.append(f"{label}: no longer set (the frozen block says {was})")
        else:
            lines.append(f"{label}: {now} (the frozen block says {was})")
    return lines

