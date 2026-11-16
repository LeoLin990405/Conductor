"""Pure Worker/Router prompt and card rules."""

from __future__ import annotations

import json

from src.server.services.autonomy.evals import candidate_card, thread_context
from src.server.services.autonomy.router import _router_batch_prompt
from src.server.services.autonomy.ticket_worktree import ticket_branch
from src.server.services.autonomy.worker import (
    TICKET_SCRATCH_PLACEHOLDER,
    worker_prompt,
)


def test_worker_prompt_names_the_ticket_scope_isolation_and_report_tool():
    ticket_id = "1234567890abcdef"
    prompt = worker_prompt(
        {
            "ticket_id": ticket_id,
            "title": "Fix the save button",
            "scope": "Change the label to Save",
            "origin_trigger_id": "trg_1",
        },
        {"config": {"repository": "acme/widgets"}},
    )
    assert "Fix the save button" in prompt
    assert "Change the label to Save" in prompt
    # Absolute path is bound at the execution runtime; the server prompt
    # carries the placeholder until then.
    assert f"{TICKET_SCRATCH_PLACEHOLDER}/" in prompt
    assert ".orchestrator/autonomy/" not in prompt
    assert "autonomy_worker_report" in prompt
    assert "autonomy_route" in prompt  # explicitly told not to call it
    assert "autonomy_unsubscribe" in prompt


def test_speculative_worker_prompt_is_read_only_and_asks_for_a_plan():
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
        speculative=True,
    )
    assert "SPECULATIVE PLAN" in prompt
    assert "do not create or modify any file" in prompt
    assert 'state "waiting"' in prompt
    assert "PLAN: " in prompt


def test_worker_prompt_without_shared_context_has_no_shared_section():
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
    )
    assert "Shared thread context" not in prompt


def test_worker_prompt_injects_shared_context_above_isolation_with_read_pointer():
    body = (
        "Thread github:a/b#1 (own; 2 ticket(s))\n"
        "- [ticket t1] Fix it (running): done"
    )
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
        shared_context=body,
    )
    assert "Shared thread context" in prompt
    assert body in prompt
    assert 'Full rows: call autonomy_read with resource "thread" or "ticket".' in prompt
    assert prompt.index("Shared thread context") < prompt.index("Isolation contract")


def test_native_worker_prompt_injects_shared_context_without_read_pointer():
    body = "Thread github:a/b#1 (context; 1 ticket(s))"
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
        native=True,
        shared_context=body,
    )
    assert "Shared thread context" in prompt
    assert body in prompt
    assert "call autonomy_read" not in prompt


def test_native_ticket_delivery_contract_is_fixed_and_precedes_ticket_prompt():
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
        native=True,
    )
    contract = """Ticket delivery contract
- Complete this Ticket's task yourself. Do not substitute unrelated operator-wide work.
- For writable work in a Git workspace, use the branch named in this Ticket or the default branch shown below, and commit the Ticket changes before finishing.
- Send exactly one final report at the end. Put the complete result in its body.
- Memory instructions do not apply to Tickets, wherever they come from (operator-global guidance, or a copy of it in a repository AGENTS.md): do not search or read Fleet Memory, memfw, notes or agent memory files, and do not write memory or change-memory records. The long-term memory section of this prompt, when present, is the only memory this Ticket gets.
- Operator-global instructions about publishing, notifications, and similar work do not apply to Tickets.
- Repository AGENTS.md instructions still apply. Inspect and follow them because automatic global guidance isolation may also disable automatic project-file loading.
Ticket ID: 1234567890abcdef
Default git branch: conductor/ticket/1234567890abcdef"""
    assert prompt.startswith(contract)
    # The prompt names the branch the server checkout is created on.
    assert ticket_branch("1234567890abcdef") == "conductor/ticket/1234567890abcdef"
    assert prompt.index(contract) < prompt.index("You are the Worker")


def test_non_native_prompt_has_no_delivery_contract_prefix():
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
    )
    assert prompt.startswith("You are the Worker")


def test_speculative_worker_prompt_keeps_the_shared_section():
    body = "Thread github:a/b#1 (own; 2 ticket(s))"
    prompt = worker_prompt(
        {"ticket_id": "1234567890abcdef", "title": "Fix", "scope": "Fix"},
        {"config": {}},
        speculative=True,
        shared_context=body,
    )
    assert "Shared thread context" in prompt
    assert body in prompt


def test_candidate_card_keeps_only_routing_facts():
    card = candidate_card(
        {
            "trigger_id": "trg_1",
            "source_id": "src_1",
            "thread_key": "github:acme/widgets#12",
            "actor_login": "alice",
            "addressed": True,
            "text": "please fix @conductor-bot",
            "url": "https://github.com/acme/widgets/issues/12",
        },
        {"kind": "github", "config": {"repository": "acme/widgets"}},
    )
    assert card["trigger_id"] == "trg_1"
    assert card["repository"] == "acme/widgets"
    assert card["actor"] == "alice"
    assert card["addressed"] is True
    assert card["source_id"] == "src_1"
    assert "text" in card
    assert card["text_is_untrusted_source_content"] is True


def test_thread_context_inlines_ids_and_spills_full_rows():
    context = thread_context(
        [
            {
                "trigger_id": "trg_1",
                "actor_login": "alice",
                "addressed": True,
                "text": "x" * 600,
            }
        ],
        [
            {
                "ticket_id": "ticket_1",
                "title": "Fix it",
                "status": "running",
                "summary": "s" * 400,
            }
        ],
    )
    assert context["triggers"][0]["trigger_id"] == "trg_1"
    assert context["triggers"][0]["addressed"] is True
    assert context["triggers"][0]["text"].endswith("…")
    assert context["triggers"][0]["text_is_untrusted_source_content"] is True
    assert context["tickets"][0]["ticket_id"] == "ticket_1"
    assert context["tickets"][0]["summary"].endswith("…")
    assert "speculative_outcome" not in context["tickets"][0]
    assert "autonomy_read" in context["spill"]


def test_thread_context_inlines_a_recorded_speculative_outcome():
    context = thread_context(
        [],
        [
            {
                "ticket_id": "ticket_1",
                "title": "Observe",
                "status": "waiting",
                "summary": "PLAN: short",
                "mandate": {
                    "speculative": True,
                    "speculative_outcome": {
                        "outcome": "accepted",
                        "recorded_at": "2026-11-03T00:00:00+00:00",
                        "evidence": {
                            "worker_state": "waiting",
                            "worker_summary": "PLAN: " + ("x" * 400),
                            "actor": "user",
                        },
                    },
                },
            }
        ],
    )
    recorded = context["tickets"][0]["speculative_outcome"]
    assert recorded["outcome"] == "accepted"
    assert recorded["worker_state"] == "waiting"
    assert recorded["actor"] == "user"
    assert recorded["worker_summary"].endswith("…")
    assert len(recorded["worker_summary"]) == 300


def test_thread_context_inlines_eight_and_keeps_spill_ids():
    context = thread_context(
        [{"trigger_id": f"trg_{i}", "text": "x"} for i in range(12)],
        [{"ticket_id": f"t_{i}"} for i in range(12)],
    )
    assert len(context["triggers"]) == 8
    assert len(context["tickets"]) == 8
    assert context["spilled"]["triggers"] == ["trg_8", "trg_9", "trg_10", "trg_11"]
    assert context["spilled"]["tickets"] == ["t_8", "t_9", "t_10", "t_11"]


def test_thread_context_admits_items_until_the_budget_spills_the_rest():
    triggers = [
        {"trigger_id": f"trg_{i}", "text": "x" * 500} for i in range(8)
    ]
    tickets = [
        {"ticket_id": f"t_{i}", "title": "t", "summary": "s" * 300}
        for i in range(8)
    ]
    context = thread_context(triggers, tickets, max_chars=800)
    assert context["budget"]["max_chars"] == 800
    assert context["budget"]["used_chars"] == len(
        json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    )
    assert context["budget"]["used_chars"] <= 800
    assert context["triggers"] or context["tickets"]
    assert len(context["triggers"]) + len(context["spilled"]["triggers"]) + context["spill_omitted"]["triggers"] == 8
    assert len(context["tickets"]) + len(context["spilled"]["tickets"]) + context["spill_omitted"]["tickets"] == 8
    assert "autonomy_read" in context["spill"]


def test_router_prompt_has_valid_bounded_json_and_untrusted_source_instructions():
    prompt = _router_batch_prompt(
        {
            "kind": "superagent_candidates",
            "eval_ids": ["ev1"],
            "candidates": [
                {"eval_id": "ev1", "trigger_id": "trg1", "text": "x" * 100_000}
            ],
            "thread_context": {},
        }
    )
    assert len(prompt) <= 60_000
    assert "untrusted data" in prompt
    assert "speculative_outcome" in prompt
    assert "decision reuse" in prompt
    assert "### Summary" in prompt
    json.loads(prompt[prompt.index("\n") + 1 :])
