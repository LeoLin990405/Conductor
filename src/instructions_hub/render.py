"""Render a profile for one CLI. Never truncate: refuse over the size budget.

A target does not read one file. Claude reads a stub that names a rendered
body; Kimi and DSH read the shared ``.agents/AGENTS.md`` plus their own CLI
file (``.kimi-code/AGENTS.md`` / ``.dsh/AGENTS.md``). Every one of those files
is planned here — including a CLI file with no extra fragments — and the size
budget is checked per final file.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .core import HubError, stays_inside
from .fragments import load_fragments
from .profiles import Profile, fragments_for, load_profiles
from .targets import (
    CLI_EXTRA_FILES,
    SIZE_BUDGET,
    Target,
    cli_extra_path,
    get_target,
    rendered_file,
    rendered_path,
    target_path,
)


@dataclass(frozen=True)
class PlannedFile:
    """One file a target loads, and the content the hub would write into it."""

    role: str
    path: Path
    body: str
    digest: str
    bytes: int
    fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rendered:
    """Every file a target loads, in the order the CLI reads them."""

    target: str
    profile: str
    fragments: tuple[str, ...]
    files: tuple[PlannedFile, ...]
    content: PlannedFile

    @property
    def body(self) -> str:
        """The instruction content the target loads."""
        return self.content.body

    @property
    def digest(self) -> str:
        """The hash of that content."""
        return self.content.digest

    @property
    def bytes(self) -> int:
        """The size of the file that carries it."""
        return self.content.bytes


def render(
    profile: str,
    target: str,
    *,
    library: Path | None = None,
    home: Path,
    environ: dict[str, str] | None = None,
) -> Rendered:
    """Plan every file ``target`` loads under ``home`` for ``profile``."""
    profiles = load_profiles(library)
    if profile not in profiles:
        raise HubError(f"unknown profile {profile!r}")
    chosen = get_target(target)
    selected = profiles[profile]
    catalog = load_fragments(library)
    # One spelling of the HOME for every file of this target, so the stub,
    # the shared file and the CLI file all name the same directory.
    base = Path(home).expanduser().resolve(strict=False)
    names = _body_names(selected, chosen)
    extra_names = _extra_names(selected, chosen.name)
    missing = _missing(catalog, (*names, *extra_names))
    if missing:
        raise HubError(f"missing fragments: {', '.join(missing)}")
    files: list[PlannedFile] = []
    if chosen.kind == "stub":
        # Claude reads a one-line stub. The body it names is a second file, so
        # both are planned and both are checked against the size budget.
        document, digest, _size = _managed(_join(catalog, names), chosen)
        stub = f"@{rendered_path(profile)}\n"
        files.append(
            PlannedFile(
                "stub",
                target_path(chosen, base, environ),
                stub,
                digest,
                len(stub.encode("utf-8")),
                names,
            )
        )
        content = PlannedFile(
            "rendered",
            _inside(base, base.joinpath(*rendered_file(profile))),
            document,
            digest,
            len(document.encode("utf-8")),
            names,
        )
        files.append(content)
    else:
        document, digest, _size = _managed(_join(catalog, names), chosen)
        content = PlannedFile(
            "shared" if chosen.shared else "managed",
            target_path(chosen, base, environ),
            document,
            digest,
            len(document.encode("utf-8")),
            names,
        )
        files.append(content)
        if chosen.name in CLI_EXTRA_FILES:
            # Always plan the CLI file. An empty extra list still produces a
            # managed body, so a missing or stale file is a mismatch.
            extra_document, extra_digest, _size = _managed(
                _join(catalog, extra_names), chosen
            )
            files.append(
                PlannedFile(
                    "cli",
                    cli_extra_path(chosen.name, base, environ),
                    extra_document,
                    extra_digest,
                    len(extra_document.encode("utf-8")),
                    extra_names,
                )
            )
    for planned in files:
        _check_budget(chosen, planned, base, catalog)
    return Rendered(chosen.name, profile, names, tuple(files), content)


def _body_names(profile: Profile, target: Target) -> tuple[str, ...]:
    if target.shared:
        return tuple(profile.fragments)
    return tuple(fragments_for(profile, target.name))


def _extra_names(profile: Profile, target: str) -> tuple[str, ...]:
    if target not in CLI_EXTRA_FILES:
        return ()
    return tuple(profile.extra.get(target, ()))


def _missing(catalog: dict[str, str], names: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    for name in names:
        if name not in catalog and name not in missing:
            missing.append(name)
    return missing


def _join(catalog: dict[str, str], names: tuple[str, ...]) -> str:
    if not names:
        # A CLI file with no extra fragments is still a managed file on disk.
        return ""
    pieces = [catalog[name].rstrip() for name in names]
    return "\n\n".join(pieces).strip() + "\n"


def _managed(payload: str, target: Target) -> tuple[str, str, int]:
    """Managed document, the digest stored in its header, and that file's size."""
    prefix = "<!-- managed_by=conductor source="
    suffix = f" -->\n\n{payload}"
    digest = hashlib.sha256(f"{prefix}{suffix}".encode("utf-8")).hexdigest()[:12]
    document = f"{prefix}{digest}{suffix}"
    if target.kind == "mdc":
        # Cursor reads front matter before the body, so the wrapper is part of
        # the file the budget applies to.
        document = "---\nalwaysApply: true\n---\n\n" + document
    return document, digest, len(document.encode("utf-8"))


def _inside(home: Path, path: Path) -> Path:
    """Refuse a path whose resolved ancestors leave the isolated HOME.

    A symlinked directory under the HOME can point anywhere, so the file
    the CLI would read is not the file the hub names.
    """
    if not stays_inside(home, path):
        raise HubError(f"{path.name} must stay inside the isolated HOME")
    return path


def _check_budget(
    target: Target,
    planned: PlannedFile,
    home: Path,
    catalog: dict[str, str],
) -> None:
    """Refuse a file over the budget, naming the file and its largest fragment."""
    if planned.bytes <= SIZE_BUDGET:
        return
    try:
        shown = planned.path.relative_to(home)
    except ValueError:
        shown = planned.path
    largest = ""
    if planned.fragments:
        largest = max(
            planned.fragments,
            key=lambda name: len(catalog[name].encode("utf-8")),
        )
    detail = f"; largest fragment is {largest}" if largest else ""
    raise HubError(
        f"rendered {target.name} file {shown} is {planned.bytes} bytes "
        f"(budget {SIZE_BUDGET}){detail}"
    )
