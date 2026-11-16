from __future__ import annotations

from src.tools.auto_dispatch.delivery import build_line_delivery_task


def test_line_delivery_task_pins_task_2_contract_before_line_task() -> None:
    task = build_line_delivery_task(
        dispatch_id="dispatch-1234",
        line_id="line-5678",
        branch="feature/auto-dispatch-dispatch-1234/line-line-5678",
        task="Add the requested feature.",
    )

    assert task == (
        "Ticket delivery contract\n"
        "- Complete this Ticket's task yourself. Do not substitute unrelated operator-wide work.\n"
        "- For writable work in a Git workspace, use the branch named in this Ticket or the default branch shown below, and commit the Ticket changes before finishing.\n"
        "- Send exactly one final report at the end. Put the complete result in its body.\n"
        "- Memory instructions do not apply to Tickets, wherever they come from (operator-global guidance, or a copy of it in a repository AGENTS.md): do not search or read Fleet Memory, memfw, notes or agent memory files, and do not write memory or change-memory records. The long-term memory section of this prompt, when present, is the only memory this Ticket gets.\n"
        "- Operator-global instructions about publishing, notifications, and similar work do not apply to Tickets.\n"
        "- Repository AGENTS.md instructions still apply. Inspect and follow them because automatic global guidance isolation may also disable automatic project-file loading.\n"
        "Ticket ID: dispatch-1234:line-5678\n"
        "Default git branch: feature/auto-dispatch-dispatch-1234/line-line-5678\n\n"
        "Add the requested feature."
    )


def test_line_delivery_task_does_not_infer_identity_from_run_id() -> None:
    task = build_line_delivery_task(
        dispatch_id="dispatch-stable",
        line_id="line-stable",
        branch="feature/stable/line-stable",
        task="Only the requested line.",
    )

    assert "Ticket ID: dispatch-stable:line-stable" in task
    assert "run_id" not in task
    assert task.endswith("\n\nOnly the requested line.")
