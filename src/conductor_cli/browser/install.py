"""Install the bundled browser skill without replacing user files."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..skills_hub.catalog import AGENT_ADAPTERS

_SKILL_NAME = "agent-browser"


@dataclass(frozen=True)
class InstallSummary:
    installed: tuple[str, ...]
    already_present: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]


def _default_source() -> Path:
    packaged = Path(__file__).with_name("skill")
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "skills" / _SKILL_NAME


def _default_targets() -> tuple[Path, ...]:
    home = Path.home()
    return tuple(adapter.destination(home, _SKILL_NAME) for adapter in AGENT_ADAPTERS)


def _source_files(source: Path) -> tuple[tuple[Path, str], ...]:
    """Return regular files in a source tree, rejecting links and odd entries."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError("skill source is not a regular directory")
    files: list[tuple[Path, str]] = []
    for root, dirs, names in os.walk(source, followlinks=False):
        root_path = Path(root)
        dirs.sort()
        names.sort()
        for name in dirs:
            candidate = root_path / name
            if candidate.is_symlink() or not candidate.is_dir():
                raise ValueError("skill source contains an unsafe directory")
        for name in names:
            candidate = root_path / name
            mode = candidate.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("skill source contains a non-regular file")
            relative = candidate.relative_to(source).as_posix()
            if relative.startswith("../") or "/../" in relative:
                raise ValueError("skill source contains an unsafe path")
            files.append((candidate, relative))
    if not files:
        raise ValueError("skill source is empty")
    if not any(relative == "SKILL.md" for _, relative in files):
        raise ValueError("skill source has no SKILL.md")
    return tuple(files)


def _open_directory_chain(path: Path) -> int:
    """Create/open each absolute directory using no-follow directory handles."""
    if not path.is_absolute():
        raise ValueError("destination path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise ValueError("destination path contains an unsafe component")
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _copy_tree_exclusive(source_files: tuple[tuple[Path, str], ...], dest_fd: int) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for source_file, relative in source_files:
        parts = relative.split("/")
        current_fd = os.dup(dest_fd)
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = child_fd
            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            out_fd = os.open(parts[-1], file_flags, 0o600, dir_fd=current_fd)
            try:
                source_fd = os.open(source_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                        raise ValueError("source file is no longer regular")
                    while chunk := os.read(source_fd, 65536):
                        view = memoryview(chunk)
                        while view:
                            written = os.write(out_fd, view)
                            view = view[written:]
                finally:
                    os.close(source_fd)
            finally:
                os.close(out_fd)
        finally:
            os.close(current_fd)


def _install_one(source_files: tuple[tuple[Path, str], ...], target: Path) -> str:
    if not target.is_absolute():
        return "failed: destination must be an absolute path"
    if target.name != _SKILL_NAME:
        return "failed: destination must end in agent-browser"
    parent = target.parent
    try:
        parent_fd = _open_directory_chain(parent)
    except (OSError, ValueError):
        return "failed: destination path is unsafe or unavailable"
    try:
        leaf = target.name
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return "present"
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            return "present"
        destination_fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            _copy_tree_exclusive(source_files, destination_fd)
        finally:
            os.close(destination_fd)
        return "installed"
    except (OSError, ValueError):
        # Partial copies are retained. Removing them could destroy user data if
        # another process populated the destination after our exclusive mkdir.
        return "failed: copy stopped; any files already written were retained"
    finally:
        os.close(parent_fd)


def install_skills(
    source: Path | None = None,
    targets: Sequence[Path] | None = None,
) -> InstallSummary:
    """Install the bundled skill to explicit destination paths or agent defaults.

    ``targets`` are complete skill destination paths, each ending in
    ``agent-browser``. Existing destinations are left untouched.
    """
    source_path = source or _default_source()
    try:
        files = _source_files(source_path)
    except (OSError, ValueError):
        return InstallSummary((), (), ((str(source_path), "failed: skill source is unsafe or unavailable"),))

    selected_targets = tuple(targets) if targets is not None else _default_targets()
    installed: list[str] = []
    present: list[str] = []
    failed: list[tuple[str, str]] = []
    for raw_target in selected_targets:
        target = Path(raw_target).expanduser()
        result = _install_one(files, target)
        if result == "installed":
            installed.append(str(target))
        elif result == "present":
            present.append(str(target))
        else:
            failed.append((str(target), result.removeprefix("failed: ")))
    return InstallSummary(tuple(installed), tuple(present), tuple(failed))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Conductor agent-browser skill")
    parser.add_argument("--source", type=Path, help="skill source directory (primarily for isolated use)")
    parser.add_argument(
        "--targets",
        nargs="+",
        type=Path,
        help="complete skill destination paths; defaults to the five supported agent directories",
    )
    args = parser.parse_args(argv)
    summary = install_skills(args.source, args.targets)
    for path in summary.installed:
        print(f"installed: {path}")
    for path in summary.already_present:
        print(f"already present: {path}")
    for path, reason in summary.failed:
        print(f"failed: {path} ({reason})", file=sys.stderr)
    print(
        f"summary: {len(summary.installed)} installed, "
        f"{len(summary.already_present)} already present, {len(summary.failed)} failed"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
