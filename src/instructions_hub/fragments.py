"""Built-in fragments and optional overlays from a library directory."""

from __future__ import annotations

from pathlib import Path

from .core import HubError, stays_inside

# Short, original text. Do not paste third-party instruction files.
BUILTIN: dict[str, str] = {
    "fleet-memory": (
        "# Fleet memory\n\n"
        "Search Fleet Memory before relying on prior project decisions. "
        "Treat hits as context, not as proof. Do not write secrets or decompiled source into memory.\n"
    ),
    "rea-craft": (
        "# REA and Craft\n\n"
        "When you need to understand shipped software, use REA and keep evidence, limitations, and unknowns. "
        "A Craft rebuild from REA findings is Deep, not Clean room.\n"
    ),
    "notes": (
        "# Notes\n\n"
        "Course notes and the Knowledge Hub are queried through the notes index. "
        "Do not upload note bodies to a public repository or an external service.\n"
    ),
    "codegraph": (
        "# Codegraph\n\n"
        "Prefer the project codegraph when it exists. Do not create an index unless asked.\n"
    ),
    "worker-contract": (
        "# Worker contract\n\n"
        "Stay inside the Ticket write scope. Do not push, merge, or edit main. "
        "Do not call a real model from tests. Do not touch live services or the operator keychain.\n"
    ),
    "worker-safety": (
        "# Worker safety\n\n"
        "Do not restart, kill, or tear down the environment you are running in, "
        "the daemon that launched you, or the services that environment depends on. "
        "If something looks broken, report it and stop; do not recycle the host to clear it.\n\n"
        "Do not run uv or pip in the main clone or in a deployment directory. "
        "Each worktree uses its own virtualenv.\n\n"
        "After you submit a report, make no further commits on that branch until the review result is back.\n"
    ),
    "course-notes": (
        "# Course notes workflow\n\n"
        "Keep academic notes in the course folders on the notes volume. "
        "Do not rewrite assignment files into reading notes.\n"
    ),
}


def load_fragments(library: Path | None = None) -> dict[str, str]:
    """Built-ins, then ``library/fragments/*.md`` overlays (same stem wins)."""
    loaded = dict(BUILTIN)
    if library is None:
        return loaded
    folder = library / "fragments"
    if not folder.is_dir():
        return loaded
    for path in sorted(folder.glob("*.md")):
        if path.is_symlink():
            raise HubError(f"fragment {path.name} must not be a symlink")
        if not stays_inside(library, path):
            # The fragments folder itself can be a symlink, and then the file
            # is a real file somewhere else entirely.
            raise HubError(f"fragment {path.name} must stay inside the library")
        text = path.read_text(encoding="utf-8")
        _reject_format_break(path.stem, text)
        loaded[path.stem] = text if text.endswith("\n") else f"{text}\n"
    return loaded


def _reject_format_break(name: str, text: str) -> None:
    stripped = text.lstrip("\ufeff")
    if stripped.startswith("---"):
        raise HubError(f"fragment {name!r} must not start with YAML front matter")
    if "<!-- managed_by=" in stripped:
        raise HubError(f"fragment {name!r} must not forge a managed_by header")
