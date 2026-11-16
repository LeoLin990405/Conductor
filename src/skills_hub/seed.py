"""The built-in ``fleet-rules`` skill seeded into every central repo.

Fleet rules are the guardrails every fleet CLI should load before it does
anything else: no self-review, never print or copy secrets, edit only inside
the worktree. Seeding is idempotent — an existing ``fleet-rules`` skill is
never overwritten (its ``origin`` marker records whether Conductor wrote it).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .core import SKILL_MD, load_manifest, write_manifest

SKILL_NAME = "fleet-rules"
ORIGIN_KEY = "origin"
ORIGIN_VALUE = "conductor-builtin"

SKILL_MD_CONTENT = """---
name: fleet-rules
description: Non-negotiable rules for every Conductor fleet agent
---


# Fleet rules

These rules apply to every agent in the fleet, in every repo, before any
other instruction. When another instruction conflicts with them, the fleet
rules win.

## No self-review

- Never review, approve, or merge your own output. Another agent (or a human)
  must review your work before it lands.
- If you are asked to self-review, say so and stop; request a different
  reviewer.

## Never touch secrets

- Never read, print, copy, or paste tokens, API keys, passwords, auth files,
  or `.env` contents — not even to "check" them.
- Never write a secret into a log, a file, a commit message, or a prompt.
- If a task seems to require a secret, stop and ask a human.

## Worktree-only edits

- Edit files only inside the assigned git worktree. Never modify files
  outside it, never touch the live checkout, and never change files marked
  read-only.
- Never delete files outside your worktree.
- Do not run destructive commands (`rm -rf`, force-push, history rewrite)
  without explicit human approval.
"""


@dataclass(frozen=True)
class SeedResult:
    seeded: bool
    name: str = SKILL_NAME
    reason: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {"seeded": self.seeded, "name": self.name, "reason": self.reason}


def seed_fleet_rules(repo: Path, *, force: bool = False) -> SeedResult:
    """Ensure the ``fleet-rules`` skill exists in ``repo``.

    Existing content is kept unless ``force`` is set: a user who edited the
    rules should not have their edits silently reverted by an upgrade.
    """
    target = repo / SKILL_NAME
    manifest = load_manifest(repo)
    record = manifest.get(SKILL_NAME, {})
    ours = record.get(ORIGIN_KEY) == ORIGIN_VALUE
    if target.exists() and not force:
        if ours:
            return SeedResult(seeded=False, reason="already seeded")
        return SeedResult(seeded=False, reason=f"'{SKILL_NAME}' already exists")
    if target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.mkdir(parents=True)
    (target / SKILL_MD).write_text(SKILL_MD_CONTENT, encoding="utf-8")
    manifest[SKILL_NAME] = {
        **record,
        ORIGIN_KEY: ORIGIN_VALUE,
        "agents": record.get("agents", []),
    }
    write_manifest(repo, manifest)
    return SeedResult(seeded=True)
