"""Shared prompt contract for one dispatch line."""

from __future__ import annotations


_LINE_DELIVERY_CONTRACT = """Ticket delivery contract
- Complete this Ticket's task yourself. Do not substitute unrelated operator-wide work.
- For writable work in a Git workspace, use the branch named in this Ticket or the default branch shown below, and commit the Ticket changes before finishing.
- Send exactly one final report at the end. Put the complete result in its body.
- Memory instructions do not apply to Tickets, wherever they come from (operator-global guidance, or a copy of it in a repository AGENTS.md): do not search or read Fleet Memory, memfw, notes or agent memory files, and do not write memory or change-memory records. The long-term memory section of this prompt, when present, is the only memory this Ticket gets.
- Operator-global instructions about publishing, notifications, and similar work do not apply to Tickets.
- Repository AGENTS.md instructions still apply. Inspect and follow them because automatic global guidance isolation may also disable automatic project-file loading."""


def build_line_delivery_task(
    *, dispatch_id: str, line_id: str, branch: str, task: str
) -> str:
    """Put the Task 2 contract and fixed dispatch identity before the line task."""
    return (
        f"{_LINE_DELIVERY_CONTRACT}\n"
        f"Ticket ID: {dispatch_id}:{line_id}\n"
        f"Default git branch: {branch}\n\n"
        f"{task}"
    )
