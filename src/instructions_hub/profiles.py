"""Named fragment lists for the operator and for workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tomllib

from .core import HubError, stays_inside

BUILTIN_PROFILES: dict[str, dict[str, object]] = {
    "operator": {
        "fragments": ["fleet-memory", "rea-craft", "notes", "codegraph"],
        "extra": {},
    },
    "worker": {
        "fragments": [
            "worker-contract",
            "worker-safety",
            "fleet-memory",
            "notes",
        ],
        "extra": {"dsh": ["course-notes"]},
    },
}


@dataclass(frozen=True)
class Profile:
    name: str
    fragments: tuple[str, ...]
    extra: dict[str, tuple[str, ...]]


def load_profiles(library: Path | None = None) -> dict[str, Profile]:
    profiles = {
        name: Profile(
            name=name,
            fragments=tuple(str(item) for item in raw["fragments"]),  # type: ignore[index]
            extra={
                key: tuple(str(item) for item in values)
                for key, values in dict(raw.get("extra") or {}).items()
            },
        )
        for name, raw in BUILTIN_PROFILES.items()
    }
    if library is None:
        return profiles
    folder = library / "profiles"
    if not folder.is_dir():
        return profiles
    for path in sorted(folder.glob("*.toml")):
        if path.is_symlink():
            raise HubError(f"profile {path.name} must not be a symlink")
        if not stays_inside(library, path):
            # The profiles folder itself can be a symlink, and then the file
            # is a real file somewhere else entirely.
            raise HubError(f"profile {path.name} must stay inside the library")
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        fragments = data.get("fragments") or []
        extra_raw = data.get("extra") or {}
        if not isinstance(fragments, list) or not isinstance(extra_raw, dict):
            raise HubError(f"profile {path.name} is malformed")
        extra = {
            str(key): tuple(str(item) for item in values)
            for key, values in extra_raw.items()
            if isinstance(values, list)
        }
        profiles[path.stem] = Profile(
            name=path.stem,
            fragments=tuple(str(item) for item in fragments),
            extra=extra,
        )
    return profiles


def fragments_for(profile: Profile, target: str) -> list[str]:
    names = list(profile.fragments)
    names.extend(profile.extra.get(target, ()))
    return names
