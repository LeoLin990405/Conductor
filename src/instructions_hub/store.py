"""Status and diff for an isolated HOME, or the operator HOME read-only.

A target loads more than one file, so both commands report every file it
loads. A file that is not on disk is a mismatch, not a silent skip.

The summary of a target names the file that carries the instructions — the
body the Claude stub points at, the shared file for Kimi and DSH — so that
``render``, ``status`` and ``diff`` describe the same file for the same
profile and target. ``files`` holds the complete list either way.
"""

from __future__ import annotations

from pathlib import Path

from .core import HubError, resolve_home, stays_inside
from .profiles import load_profiles
from .render import PlannedFile, render
from .targets import TARGETS, Target, get_target


def library_root(home: Path, *, operator: bool = False) -> Path:
    """The library under ``home``: ``<home>/.conductor/instructions``.

    The path must stay inside the HOME once its ancestors are resolved.
    ``<home>/.conductor`` can be a symlink that points elsewhere, and the
    fragments read from it would then be files outside the HOME.
    """
    isolated = resolve_home(home, operator=operator)
    library = isolated / ".conductor" / "instructions"
    if not stays_inside(isolated, library):
        raise HubError("the default library must stay inside the isolated HOME")
    return library


def resolve_library(
    home: Path, library: Path | None = None, *, operator: bool = False
) -> Path:
    """The library every subcommand reads when ``--library`` is omitted.

    One rule for all of them: the default library is the one under the
    isolated HOME, so ``list``, ``status``, ``render`` and ``diff`` describe
    the same content for one HOME. An explicit library wins.

    A library that does not exist is not an error: the hub falls back to its
    built-in fragments and profiles. The caller passes the result to
    ``load_fragments``/``load_profiles``, which treat a missing directory as
    "no overlays".
    """
    if library is not None:
        return Path(library)
    return library_root(home, operator=operator)


def status(
    home: Path,
    *,
    profile: str = "operator",
    targets: list[str] | None = None,
    library: Path | None = None,
    environ: dict[str, str] | None = None,
    operator: bool = False,
) -> dict[str, object]:
    isolated = resolve_home(home, operator=operator)
    lib = resolve_library(isolated, library, operator=operator)
    _check_profile(profile, lib)
    rows = [
        _row(target, profile, isolated, lib, environ) for target in _select(targets)
    ]
    return {"home": str(isolated), "profile": profile, "targets": rows}


def diff_one(
    home: Path,
    profile: str,
    target: str,
    *,
    library: Path | None = None,
    environ: dict[str, str] | None = None,
    operator: bool = False,
) -> dict[str, object]:
    isolated = resolve_home(home, operator=operator)
    lib = resolve_library(isolated, library, operator=operator)
    chosen = get_target(target)
    rendered = render(
        profile,
        chosen.name,
        library=lib if lib.exists() else None,
        home=isolated,
        environ=environ,
    )
    files = [_diff_row(planned, isolated) for planned in rendered.files]
    # The summary describes the file that carries the instructions. It is one
    # of the planned files by identity, so the row is read once, not twice.
    primary = next(
        (
            row
            for row, planned in zip(files, rendered.files)
            if planned is rendered.content
        ),
        files[0] if files else {},
    )
    return {
        "target": chosen.name,
        "path": primary.get("path", ""),
        "same": all(row["matches"] for row in files),
        "current": primary.get("current"),
        "rendered": primary.get("rendered", ""),
        "digest": primary.get("digest", ""),
        "files": files,
    }


def _check_profile(profile: str, library: Path) -> None:
    """Fail once on an unknown profile instead of per target."""
    if profile not in load_profiles(library if library.exists() else None):
        raise HubError(f"unknown profile {profile!r}")


def _row(
    target: Target,
    profile: str,
    isolated: Path,
    lib: Path,
    environ: dict[str, str] | None,
) -> dict[str, object]:
    """Every file of one target, with what is on disk next to the render."""
    try:
        rendered = render(
            profile,
            target.name,
            library=lib if lib.exists() else None,
            home=isolated,
            environ=environ,
        )
    except (HubError, ValueError) as exc:
        # One target that cannot be rendered must not hide the other nine.
        return {
            "target": target.name,
            "path": "",
            "error": str(exc),
            "matches": False,
            "files": [],
        }
    files = [_status_row(planned, isolated) for planned in rendered.files]
    row: dict[str, object] = {
        "target": target.name,
        "path": str(rendered.content.path),
        "matches": all(row["matches"] for row in files),
        "files": files,
    }
    legacy = _legacy_present(target, isolated)
    if legacy:
        row["legacy"] = legacy
    return row


def _legacy_present(target: Target, isolated: Path) -> list[str]:
    """Old instruction paths for ``target`` that still exist (lstat only)."""
    found: list[str] = []
    for parts in target.legacy:
        path = isolated.joinpath(*parts)
        if not stays_inside(isolated, path):
            continue
        try:
            path.lstat()
        except OSError:
            continue
        found.append(str(path))
    return found


def _status_row(planned: PlannedFile, isolated: Path) -> dict[str, object]:
    current, error = _read(planned.path, isolated)
    row: dict[str, object] = {
        "role": planned.role,
        "path": str(planned.path),
        "exists": current is not None or error is not None,
        "managed": bool(current and "managed_by=conductor" in current),
        "matches": error is None and current == planned.body,
        "digest": planned.digest,
        "bytes": planned.bytes,
    }
    if error is not None:
        row["error"] = error
    return row


def _diff_row(planned: PlannedFile, isolated: Path) -> dict[str, object]:
    current, error = _read(planned.path, isolated)
    row: dict[str, object] = {
        "role": planned.role,
        "path": str(planned.path),
        "exists": current is not None or error is not None,
        "matches": error is None and current == planned.body,
        "current": current,
        "rendered": planned.body,
        "digest": planned.digest,
        "bytes": planned.bytes,
    }
    if error is not None:
        row["error"] = error
    return row


def _read(path: Path, isolated: Path) -> tuple[str | None, str | None]:
    """``(content, error)`` of ``path``; content is None when not read.

    A path that leaves the isolated HOME once its ancestors are resolved
    is not read at all. ``<home>/.agents`` can be a symlink that points
    elsewhere, and reporting that file's content would hand out a file
    the hub was never pointed at.

    A file that is on disk but cannot be read -- not UTF-8, or refused by
    the OS -- is a mismatch with an ``error``, not an abort: one bad agent
    file must not hide what the hub knows about the others.
    """
    if path.is_symlink() or not path.is_file():
        return None, None
    if not stays_inside(isolated, path):
        return None, None
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, "unreadable: not UTF-8"
    except OSError as exc:
        return None, f"unreadable: {exc.strerror or type(exc).__name__}"


def _select(names: list[str] | None) -> list[Target]:
    if not names:
        return list(TARGETS)
    selected = []
    seen: set[str] = set()
    for name in names:
        target = get_target(name)
        if target.name not in seen:
            selected.append(target)
            seen.add(target.name)
    return selected
