"""Offline demo of the prompt clarification gate.

Shows the gate holding an ambiguous prompt (``awaiting_clarification``), a
confirm resuming the ticket onto the launch path, a clear prompt passing
through untouched, and the deterministic heuristic fast path — with no
Postgres, Redis, or network. The gate service module's database imports are
monkeypatched with in-memory fakes (plain dicts/lists) that mirror the real
``src/server/database/autonomy`` signatures, and the clarifier is a canned
fake behind the ``Clarifier`` protocol, so the LLM is never touched.

Run from the repo root:

    uv run python scripts/clarify_gate_demo.py

Exit codes: 0 on success, 2 when the gate module is not importable yet.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# The gate reads these at import/call time; set them before importing the module.
os.environ["AUTONOMY_CLARIFY_ENABLED"] = "1"
os.environ["AUTONOMY_CLARIFY_THRESHOLD"] = "0.6"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AMBIGUOUS_PROMPT = "do something with the repo asap"
CLEAR_PROMPT = (
    "Fix the off-by-one pagination bug in src/server/database/tickets.py so "
    "list_tickets returns the full requested page, and add a regression test "
    "in tests/unit/server/test_tickets.py covering page=2 with limit=10."
)

# What the user ends up confirming: the draft's expanded request, edited with
# the open questions answered (this is the text the gate stores as the goal).
EDITED_EXPANDED_REQUEST = (
    "Audit the acme/widgets repository and open a PR fixing the single "
    "highest-impact bug: scope is bug fixes only (no new features), and done "
    "means a merged-ready PR with before/after reproduction steps."
)

USER_ID = "user-demo"
AMBIGUOUS_TICKET_ID = "ticket-ambiguous"
CLEAR_TICKET_ID = "ticket-clear"
GITHUB_SOURCE_ID = "source-github-1"


class _FakeStore:
    """In-memory stand-ins for the autonomy tables."""

    def __init__(self) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self.clarifications: dict[str, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        self.sources: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"


def _build_fakes(
    store: _FakeStore, captured: list[dict[str, Any]]
) -> dict[str, Any]:
    """Async fakes mirroring the real database-layer signatures."""

    async def get_ticket(ticket_id: str) -> dict[str, Any] | None:
        row = store.tickets.get(str(ticket_id))
        return dict(row) if row else None

    async def update_ticket(
        ticket_id: str, *, expected_status: str | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        row = store.tickets.get(str(ticket_id))
        if row is None:
            return None
        if expected_status is not None and row.get("status") != expected_status:
            return None  # optimistic-status fence failed: someone else won
        row.update(fields)
        return dict(row)

    async def add_input(
        *, ticket_id: str, trigger_id: str | None, kind: str, text: str
    ) -> dict[str, Any]:
        return {
            "input_id": store.next_id("input"),
            "ticket_id": str(ticket_id),
            "trigger_id": trigger_id,
            "kind": kind,
            "text": text,
        }

    async def create_clarification(**fields: Any) -> dict[str, Any]:
        row = {
            "clarification_id": store.next_id("clr"),
            "status": "awaiting",
            **fields,
        }
        store.clarifications[row["clarification_id"]] = row
        # The real layer returns the row plus its id; expose both flat and
        # nested so either access pattern works.
        return {**row, "row": dict(row)}

    async def get_active_clarification(
        ticket_id: str,
    ) -> dict[str, Any] | None:
        for row in store.clarifications.values():
            if (
                str(row.get("ticket_id")) == str(ticket_id)
                and row.get("status") == "awaiting"
            ):
                return dict(row)
        return None

    async def resolve_clarification(
        clarification_id: str | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        row = (
            store.clarifications.get(str(clarification_id))
            if clarification_id is not None
            else None
        )
        if row is None:
            row = await get_active_clarification(str(fields.get("ticket_id") or ""))
        if row is None:
            return None
        row["status"] = "confirmed"
        row["confirmed_text"] = fields.get("confirmed_text") or fields.get("text") or ""
        return dict(row)

    async def get_source_any_user(source_id: str) -> dict[str, Any] | None:
        row = store.sources.get(str(source_id))
        return dict(row) if row else None

    async def enqueue_outbox(
        *,
        user_id: str,
        ticket_id: str | None,
        source_id: str | None,
        thread_key: str,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        row = {
            "outbox_id": store.next_id("outbox"),
            "user_id": user_id,
            "ticket_id": ticket_id,
            "source_id": source_id,
            "thread_key": thread_key,
            "body": body,
            "idempotency_key": idempotency_key,
            "status": "pending",
        }
        store.outbox.append(row)
        return dict(row)

    async def capture_effect(**kw: Any) -> bool:
        captured.append(kw)
        return True

    return {
        "get_ticket": get_ticket,
        "update_ticket": update_ticket,
        "add_input": add_input,
        "create_clarification": create_clarification,
        "get_active_clarification": get_active_clarification,
        "resolve_clarification": resolve_clarification,
        "get_source_any_user": get_source_any_user,
        "enqueue_outbox": enqueue_outbox,
        "capture_effect": capture_effect,
    }


class _FakeClarifier:
    """Canned ``Clarifier``: rich draft for the ambiguous prompt, near-perfect
    score for the clear one. Never touches an LLM."""

    def __init__(self, assessment_type: Any) -> None:
        self._assessment_type = assessment_type

    async def assess(self, *, user_id: str, prompt: str) -> Any:
        if prompt == AMBIGUOUS_PROMPT:
            return self._assessment_type(
                score=0.3,
                expanded_request=(
                    "Improve the acme/widgets repository by auditing it and "
                    "completing one high-impact change."
                ),
                assumptions=[
                    "'the repo' means the acme/widgets GitHub repository",
                    "an improvement to existing code is wanted, not a new feature",
                ],
                open_questions=[
                    "Which area matters most: bugs, performance, or developer experience?",
                    "What does 'done' look like: a PR, a written summary, or a merged change?",
                ],
            )
        return self._assessment_type(
            score=0.95,
            expanded_request=prompt,
            assumptions=[],
            open_questions=[],
        )


def _seed_store(store: _FakeStore) -> None:
    store.sources[GITHUB_SOURCE_ID] = {
        "source_id": GITHUB_SOURCE_ID,
        "user_id": USER_ID,
        "kind": "github",
        "config": {"repository": "acme/widgets"},
    }
    for ticket_id, prompt in (
        (AMBIGUOUS_TICKET_ID, AMBIGUOUS_PROMPT),
        (CLEAR_TICKET_ID, CLEAR_PROMPT),
    ):
        store.tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "user_id": USER_ID,
            "workspace_id": "workspace-demo",
            "title": prompt.strip().splitlines()[0][:80],
            "scope": prompt,
            "status": "queued",
            "mandate": {
                "strength": "explicit",
                "source": "github",
                "source_id": GITHUB_SOURCE_ID,
                "goal": prompt,
            },
            "policy_version": "demo",
            "origin_trigger_id": f"trigger-{ticket_id}",
            "origin_thread_key": f"github:{ticket_id}",
            "attention": None,
            "summary": "",
            "error": "",
        }


def _print_draft(draft: dict[str, Any]) -> None:
    score = draft.get("clarity_score", draft.get("score"))
    print(f"    score:             {score}")
    print(f"    expanded_request:  {draft.get('expanded_request')}")
    assumptions = draft.get("assumptions") or []
    print(f"    assumptions:       {len(assumptions)} item(s)")
    for item in assumptions:
        print(f"      - {item}")
    questions = draft.get("open_questions") or []
    print(f"    open_questions:    {len(questions)} item(s)")
    for item in questions:
        print(f"      - {item}")


async def _main() -> int:
    try:
        from src.server.services.autonomy import clarify
    except Exception as exc:  # noqa: BLE001 - module may not exist yet
        print(
            "The clarification-gate module is not importable yet, so this demo "
            "cannot run end-to-end."
        )
        print(f"Import failed: {exc!r}")
        print("Expected: src/server/services/autonomy/clarify.py (in progress).")
        return 2

    store = _FakeStore()
    _seed_store(store)
    captured: list[dict[str, Any]] = []
    fakes = _build_fakes(store, captured)

    # Monkeypatch the module's aliased imports (dynamic attribute assignment,
    # exactly how the unit tests patch it).
    clarify.ticket_db = SimpleNamespace(
        get_ticket=fakes["get_ticket"],
        update_ticket=fakes["update_ticket"],
        add_input=fakes["add_input"],
    )
    clarify.clarification_db = SimpleNamespace(
        create_clarification=fakes["create_clarification"],
        get_active_clarification=fakes["get_active_clarification"],
        resolve_clarification=fakes["resolve_clarification"],
    )
    clarify.source_db = SimpleNamespace(
        get_source_any_user=fakes["get_source_any_user"],
    )
    clarify.outbox_db = SimpleNamespace(
        enqueue_outbox=fakes["enqueue_outbox"],
    )
    clarify.enqueue_effect = fakes["capture_effect"]

    clarifier = _FakeClarifier(clarify.ClarityAssessment)

    print("=== 1. Ambiguous prompt hits the gate (github source) ===")
    print(f"prompt: {AMBIGUOUS_PROMPT!r}")
    verdict = await clarify.gate(
        ticket=dict(store.tickets[AMBIGUOUS_TICKET_ID]), clarifier=clarifier
    )
    print(f"gate() -> {verdict!r}")
    held = store.tickets[AMBIGUOUS_TICKET_ID]
    print(f"ticket status:   {held['status']}")
    print(f"ticket attention: {held['attention']}")
    draft = await clarify.clarification_db.get_active_clarification(
        AMBIGUOUS_TICKET_ID
    )
    print("stored clarification draft:")
    _print_draft(draft)
    print("captured channel reply (outbox):")
    for row in store.outbox:
        print(f"  body: {row['body']!r}")
    print("captured effects:")
    for kw in captured:
        print(f"  kind={kw.get('kind')} idempotency_key={kw.get('idempotency_key')}")

    print()
    print("=== 2. Confirm the edited expanded request ===")
    confirmed = await clarify.confirm_clarification(
        ticket_id=AMBIGUOUS_TICKET_ID, text=EDITED_EXPANDED_REQUEST
    )
    resumed = store.tickets[AMBIGUOUS_TICKET_ID]
    print(f"confirm_clarification() -> {sorted(confirmed)}")
    print(f"ticket status:   {resumed['status']}")
    print(f"ticket scope:    {resumed['scope']!r}")
    print(f"mandate.goal:    {resumed['mandate'].get('goal')!r}")
    launches = [kw for kw in captured if kw.get("kind") == "launch_worker"]
    for kw in launches:
        print(f"launch effect:   kind={kw.get('kind')}")
        print(f"  idempotency_key: {kw.get('idempotency_key')}")
        print(f"  payload:         {kw.get('payload')}")

    print()
    print("=== 3. Clear prompt passes the gate untouched ===")
    effects_before = len(captured)
    verdict = await clarify.gate(
        ticket=dict(store.tickets[CLEAR_TICKET_ID]), clarifier=clarifier
    )
    print(f"prompt: {CLEAR_PROMPT!r}")
    print(f"gate() -> {verdict!r}")
    print(
        "active clarification row: "
        f"{await clarify.clarification_db.get_active_clarification(CLEAR_TICKET_ID)}"
    )
    print(f"effects captured during gate: {len(captured) - effects_before}")
    print(f"ticket status:   {store.tickets[CLEAR_TICKET_ID]['status']}")

    print()
    print("=== 4. Deterministic heuristic fast path (no LLM) ===")
    ambiguous_score = clarify.heuristic_clarity_score(AMBIGUOUS_PROMPT)
    clear_score = clarify.heuristic_clarity_score(CLEAR_PROMPT)
    print(f"heuristic_clarity_score(ambiguous) = {ambiguous_score:.3f}")
    print(f"heuristic_clarity_score(clear)     = {clear_score:.3f}")
    print(f"threshold                          = {clarify.clarity_threshold():.2f}")

    print()
    print("Demo complete: gate held, confirm resumed, clear passed, heuristic ran.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
