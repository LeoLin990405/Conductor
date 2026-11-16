"""Central skills repo: discovery and metadata parsing.

The repo is a plain directory tree (default ``~/.conductor/skills``, see
:mod:`.config`) where each immediate subdirectory that carries a
``SKILL.md`` is one skill. Import (see :mod:`.importer`) copies a skill into
the repo and records where it came from so later syncs can update it.

A skill's ``SKILL.md`` follows the Agent Skills convention: YAML front matter
with ``name`` and ``description``, then markdown body. Neither field is
trusted: the directory name is the identity and the front matter is only
reported.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # PyYAML is already a server dependency; keep parsing optional for the
    # CLI-only install path.
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

MANIFEST_FILENAME = "manifest.json"
SKILL_MD = "SKILL.md"

# Directory-name charset. Same shape as the Agent Skills name rule: this is
# the identity used in paths and manifests, so it stays conservative.
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME_LEN = 64

# Names the hub itself owns as subdirectories of the repo. A skill occupying
# one of these would sit inside the snapshot/preset/project stores (and an
# imported "snapshots" skill could plant fake snapshot JSON), so they can
# never be skill names.
RESERVED_NAMES = frozenset({"snapshots", "presets", "projects"})

# The manifest lives in a user-writable directory, so it is size-capped and
# validated before use rather than trusted.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class SkillsError(Exception):
    """A skills operation failed; the message is safe to print."""


class InvalidName(SkillsError):
    """A proposed skill name is outside the allowed charset/length."""


def valid_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_NAME_LEN and bool(NAME_RE.match(name))


def require_valid_name(name: str) -> str:
    if not isinstance(name, str) or not valid_name(name):
        raise InvalidName(
            f"invalid skill name {name!r}: expected lowercase words joined by '-' "
            f"(max {MAX_NAME_LEN} chars)"
        )
    if name in RESERVED_NAMES:
        raise InvalidName(f"invalid skill name {name!r}: name is reserved by the skills hub")
    return name


def _read_json(path: Path, *, expect_dict: bool = False) -> Any:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise SkillsError(f"{path}: file exceeds {MAX_MANIFEST_BYTES} bytes")
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise SkillsError(f"{path}: not valid JSON ({exc.msg})") from exc
    except OSError as exc:
        raise SkillsError(f"{path}: {exc.strerror or exc}") from exc
    if expect_dict and not isinstance(data, dict):
        raise SkillsError(f"{path}: expected a JSON object")
    return data


@dataclass(frozen=True)
class SkillEntry:
    """One skill in the central repo."""

    name: str
    path: Path
    description: str = ""
    installed: tuple[str, ...] = ()
    source: str | None = None
    source_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "installed": list(self.installed),
            "source": self.source,
            "source_ref": self.source_ref,
        }


def discover_skills(repo: Path) -> list[SkillEntry]:
    """List skills currently in ``repo``.

    An unreadable directory surfaces as an error rather than an empty list:
    answering "no skills" for a repo that exists but cannot be read would
    make a later uninstall pass believe there is nothing it owns.
    """
    manifest = load_manifest(repo)
    entries: list[SkillEntry] = []
    if not repo.exists():
        return entries
    if not repo.is_dir():
        raise SkillsError(f"skills repo {repo} is not a directory")
    try:
        children = sorted(repo.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise SkillsError(f"could not list skills repo {repo}: {exc}") from exc
    for child in children:
        if child.is_symlink():
            # A symlinked skill dir is still a skill as long as it resolves.
            if not child.resolve().is_dir():
                continue
        elif not child.is_dir():
            continue
        if not (child / SKILL_MD).is_file():
            continue
        entries.append(_entry_for(child, manifest))
    return entries


def _entry_for(child: Path, manifest: Mapping[str, Mapping[str, Any]]) -> SkillEntry:
    meta = parse_skill_md(child / SKILL_MD)
    record = manifest.get(child.name) or {}
    agents = record.get("agents") or []
    return SkillEntry(
        name=child.name,
        path=child,
        description=meta.get("description", ""),
        installed=tuple(a for a in agents if isinstance(a, str)),
        source=record.get("source") if isinstance(record.get("source"), str) else None,
        source_ref=(
            record.get("source_ref")
            if isinstance(record.get("source_ref"), str)
            else None
        ),
    )


def get_skill(repo: Path, name: str) -> SkillEntry | None:
    require_valid_name(name)
    path = repo / name
    if not (path / SKILL_MD).is_file():
        return None
    return _entry_for(path, load_manifest(repo))


_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def parse_skill_md(path: Path) -> dict[str, str]:
    """Read the YAML front matter of a ``SKILL.md``.

    Never raises for content problems: a malformed file yields an empty dict
    so one broken skill cannot break listing the repo. A missing file is also
    tolerated (callers use presence to decide what is a skill).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}
    raw = match.group("body")
    if yaml is None:  # pragma: no cover - fallback when PyYAML is absent
        parsed: dict[str, Any] = {}
        for line in raw.splitlines():
            if not line or line[:1] in {" ", "\t", "#"} or ":" not in line:
                continue
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip().strip("'\"")
        return {k: v for k, v in parsed.items() if isinstance(v, str)}
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {k: v for k, v in loaded.items() if isinstance(v, str)}


# --- manifest ---------------------------------------------------------------


def manifest_path(repo: Path) -> Path:
    return repo / MANIFEST_FILENAME


def load_manifest(repo: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(manifest_path(repo), expect_dict=True)
    if data is None:
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for name, record in data.items():
        if not isinstance(name, str) or not valid_name(name):
            continue
        if name in RESERVED_NAMES:
            continue
        if not isinstance(record, dict):
            continue
        cleaned[name] = record
    return cleaned


def write_manifest(repo: Path, manifest: Mapping[str, Mapping[str, Any]]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    data = {name: dict(record) for name, record in manifest.items()}
    for name in data:
        require_valid_name(name)
    atomic_write_json(manifest_path(repo), data)


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: tmp file in the same directory, then rename."""
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


# --- import sources ---------------------------------------------------------


# URI schemes that must never be treated as scp-style git hosts.
_SCHEME_HOSTS = frozenset(
    {
        "file",
        "git",
        "http",
        "https",
        "ssh",
        "ftp",
        "ftps",
        "sftp",
        "data",
        "mailto",
    }
)


def is_git_url(source: str) -> bool:
    text = source.strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    if text.startswith(("git@", "ssh://")):
        return True
    if text.startswith(("http://", "https://")):
        try:
            split = urlsplit(text)
        except ValueError:
            return False
        return bool(split.scheme) and bool(split.netloc)
    # Non-http URI schemes (file:///, git://, …) are not git clone sources
    # for this CLI — only http(s)/ssh/git@ forms are.
    if "://" in text:
        return False
    # scp-like 'host:path' shorthand — never a Windows drive or a scheme:
    match = re.match(r"^([A-Za-z0-9._-]+):(.+)$", text)
    if not match:
        return False
    host, path = match.group(1), match.group(2)
    if host.lower() in _SCHEME_HOSTS or len(host) == 1:
        return False
    return bool(re.match(r"^[A-Za-z0-9._/-]+$", path))


def is_zip_path(source: str) -> bool:
    return source.lower().endswith(".zip")


def redact_source(source: str) -> str:
    """Strip userinfo credentials from a source string before it is stored or printed."""
    text = source.strip()
    if "://" not in text or "@" not in text:
        return text
    try:
        split = urlsplit(text)
    except ValueError:
        return text
    if not split.username and not split.password:
        return text
    host = split.hostname or ""
    if split.port:
        host = f"{host}:{split.port}"
    path = split.path or ""
    query = f"?{split.query}" if split.query else ""
    fragment = f"#{split.fragment}" if split.fragment else ""
    return f"{split.scheme}://{host}{path}{query}{fragment}"


def redact_text(text: str) -> str:
    """Best-effort credential scrub for free-form messages (clone stderr, etc.)."""
    # user:pass@host → host inside any URL-looking token
    return re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@",
        r"\1",
        text,
    )


def source_name(source: str) -> str:
    """A short human label for an import source, never a filesystem path."""
    text = redact_source(source).rstrip("/")
    if is_git_url(text):
        tail = re.split(r"[:/]", text)[-1]
        return tail or text
    return Path(text).name or text
