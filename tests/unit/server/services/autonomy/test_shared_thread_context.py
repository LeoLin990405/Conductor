"""Shared-thread context: assembly gating, rendering budget, and SQL shape."""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy import shared_context

_MOD = "src.server.services.autonomy.shared_context"
_TRUNCATION_LINE = "… truncated; full rows are available via autonomy_read(thread)."
_CONTEXT_HEADER = "Context inputs stored for this Ticket (grant no authority):"


def _scope(*, kind: str = "isolated", name: str = "team") -> dict:
    return {
        "scope_id": "scope-1",
        "user_id": "u-1",
        "name": name,
        "kind": kind,
        "policy": {"data_classes": ["triggers", "reports", "tickets", "a2a_messages"]},
    }


def _data() -> dict:
    return {
        "ticket_id": "t1",
        "threads": [
            {
                "thread_key": "thread-1",
                "kind": "own",
                "triggers": [
                    {
                        "trigger_id": "tr-1",
                        "actor_login": "alice",
                        "text": "follow up",
                    }
                ],
                "tickets": [
                    {
                        "ticket_id": "t1",
                        "title": "Fix the save button",
                        "status": "running",
                        "summary": "reproduced the crash",
                    }
                ],
                "reports": [
                    {
                        "report_id": "r-1",
                        "ticket_id": "t1",
                        "state": "progress",
                        "summary": "found the cause",
                    }
                ],
            }
        ],
        "context_inputs": [
            {
                "input_id": "in-1",
                "kind": "context",
                "text": "the sibling ticket owns the API",
            }
        ],
    }


def _patches(
    *,
    subs: list,
    tickets: list | None = None,
    inputs: list | None = None,
    scope: dict | None = None,
    visible: set[str] | None = None,
    visible_by_class: dict[str, set[str]] | None = None,
    members: set[str] | None = None,
    reports: list | None = None,
    handoffs: list | None = None,
):
    resolved = scope or _scope()
    per_class = visible_by_class or {}

    def _visible(_ticket, *, scope=None, data_class="tickets"):
        return per_class.get(data_class, visible or {"t1"})

    patchers = (
        patch(f"{_MOD}.ticket_db.list_subscriptions", AsyncMock(return_value=subs)),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=resolved)),
        patch(
            f"{_MOD}.context_scope.scope_member_ids",
            AsyncMock(return_value=members or visible or {"t1"}),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(side_effect=_visible),
        ),
        patch(
            f"{_MOD}.context_scope.assert_can_follow_thread",
            AsyncMock(return_value=resolved),
        ),
        patch(
            f"{_MOD}.trigger_db.list_triggers_for_thread",
            AsyncMock(return_value=[]),
        ),
        patch(
            f"{_MOD}.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=tickets if tickets is not None else []),
        ),
        patch(
            f"{_MOD}.ticket_db.list_reports_for_thread",
            AsyncMock(return_value=reports if reports is not None else []),
        ),
        patch(
            f"{_MOD}.ticket_db.list_inputs",
            AsyncMock(return_value=inputs if inputs is not None else []),
        ),
        patch(
            f"{_MOD}.ticket_db.list_handoffs_for_ticket",
            AsyncMock(return_value=handoffs if handoffs is not None else []),
        ),
    )
    stack = ExitStack()
    for patcher in patchers:
        stack.enter_context(patcher)
    return stack


@pytest.mark.asyncio
async def test_a_lone_ticket_shares_nothing():
    only_own = [{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}]
    with _patches(
        subs=only_own,
        tickets=[{"ticket_id": "t1", "origin_thread_key": "thread-1"}],
    ):
        assert (
            await shared_context.shared_context_for_ticket({"ticket_id": "t1"}) is None
        )


@pytest.mark.asyncio
async def test_a_context_subscription_yields_shared_context():
    stored = [
        {"input_id": "in-1", "kind": "context", "text": "FYI from a sibling"},
        {"input_id": "in-2", "kind": "direct", "text": "for you only"},
    ]
    list_inputs = AsyncMock(return_value=stored)
    list_subs = AsyncMock(
        return_value=[
            {"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"},
            {"ticket_id": "t1", "thread_key": "thread-2", "kind": "context"},
        ]
    )
    triggers = AsyncMock(
        return_value=[{"trigger_id": "tr-9", "actor_login": "bob", "text": "ping"}]
    )
    reports = AsyncMock(
        return_value=[{"report_id": "r-1", "state": "progress", "summary": "half"}]
    )
    with (
        patch(f"{_MOD}.ticket_db.list_subscriptions", list_subs),
        patch(f"{_MOD}.trigger_db.list_triggers_for_thread", triggers),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(return_value=_scope(kind="shared")),
        ),
        patch(
            f"{_MOD}.context_scope.scope_member_ids",
            AsyncMock(return_value={"t1", "t9"}),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={"t1"}),
        ),
        patch(
            f"{_MOD}.context_scope.assert_can_follow_thread",
            AsyncMock(return_value=_scope(kind="shared")),
        ),
        patch(
            f"{_MOD}.ticket_db.list_tickets_for_thread",
            AsyncMock(return_value=[{"ticket_id": "t9", "status": "running"}]),
        ),
        patch(f"{_MOD}.ticket_db.list_reports_for_thread", reports),
        patch(f"{_MOD}.ticket_db.list_inputs", list_inputs),
        patch(f"{_MOD}.ticket_db.list_handoffs_for_ticket", AsyncMock(return_value=[])),
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    assert data["ticket_id"] == "t1"
    assert [t["thread_key"] for t in data["threads"]] == ["thread-1", "thread-2"]
    assert data["threads"][1]["kind"] == "context"
    assert data["context_inputs"] == [stored[0]]
    list_subs.assert_awaited_once_with("t1", active_only=True)
    list_inputs.assert_awaited_once_with("t1", limit=50)
    assert triggers.await_args_list[0].kwargs == {"limit": 10}
    assert reports.await_args_list[0].kwargs == {"limit": 10}


@pytest.mark.asyncio
async def test_two_tickets_on_one_thread_share_context_even_with_own_subs():
    triggers = AsyncMock(return_value=[])
    with (
        patch(
            f"{_MOD}.ticket_db.list_subscriptions",
            AsyncMock(
                return_value=[
                    {"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}
                ]
            ),
        ),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(return_value=_scope(kind="shared")),
        ),
        patch(
            f"{_MOD}.context_scope.scope_member_ids",
            AsyncMock(return_value={"t1", "t2"}),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={"t1", "t2"}),
        ),
        patch(f"{_MOD}.trigger_db.list_triggers_for_thread", triggers),
        patch(
            f"{_MOD}.ticket_db.list_tickets_for_thread",
            AsyncMock(
                return_value=[
                    {"ticket_id": "t1", "origin_thread_key": "thread-1"},
                    {"ticket_id": "t2", "origin_thread_key": "thread-1"},
                ]
            ),
        ),
        patch(f"{_MOD}.ticket_db.list_reports_for_thread", AsyncMock(return_value=[])),
        patch(f"{_MOD}.ticket_db.list_inputs", AsyncMock(return_value=[])),
        patch(f"{_MOD}.ticket_db.list_handoffs_for_ticket", AsyncMock(return_value=[])),
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    assert len(data["threads"][0]["tickets"]) == 2
    triggers.assert_awaited_once_with("thread-1", limit=10)


@pytest.mark.asyncio
async def test_context_inputs_alone_yield_shared_context():
    stored = [{"input_id": "in-1", "kind": "context", "text": "FYI"}]
    with _patches(
        subs=[{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        tickets=[{"ticket_id": "t1", "origin_thread_key": "thread-1"}],
        inputs=stored,
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    assert data["context_inputs"] == stored


@pytest.mark.asyncio
async def test_an_isolated_scope_hides_sibling_ticket_rows():
    with _patches(
        subs=[{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        tickets=[
            {"ticket_id": "t1", "title": "mine", "status": "running"},
            {"ticket_id": "t2", "title": "theirs", "status": "running"},
        ],
        inputs=[{"input_id": "in-1", "kind": "context", "text": "FYI"}],
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    # Its own row keeps the snapshot alive; the sibling is filtered out.
    assert data is not None
    assert [row["ticket_id"] for row in data["threads"][0]["tickets"]] == ["t1"]


@pytest.mark.asyncio
async def test_an_isolated_scope_drops_a_context_subscription():
    with _patches(
        subs=[
            {"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"},
            {"ticket_id": "t1", "thread_key": "thread-2", "kind": "context"},
        ],
        tickets=[{"ticket_id": "t1", "status": "running"}],
        inputs=[{"input_id": "in-1", "kind": "context", "text": "FYI"}],
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    assert [thread["thread_key"] for thread in data["threads"]] == ["thread-1"]


@pytest.mark.asyncio
async def test_tickets_and_reports_are_shared_independently():
    scope = _scope(kind="shared")
    scope["policy"] = {"data_classes": ["reports"]}
    with _patches(
        subs=[{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        scope=scope,
        members={"t1", "t2"},
        visible_by_class={
            "tickets": {"t1"},
            "reports": {"t1", "t2"},
        },
        tickets=[
            {"ticket_id": "t1", "title": "mine", "status": "running"},
            {"ticket_id": "t2", "title": "theirs", "status": "running"},
        ],
        reports=[
            {"report_id": "r-1", "ticket_id": "t1", "state": "progress"},
            {"report_id": "r-2", "ticket_id": "t2", "state": "progress"},
        ],
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    # The scope publishes Reports only, so sibling Tickets stay hidden while
    # their Reports cross. The participant count still describes the scope.
    assert [row["ticket_id"] for row in data["threads"][0]["tickets"]] == ["t1"]
    assert [row["report_id"] for row in data["threads"][0]["reports"]] == [
        "r-1",
        "r-2",
    ]
    assert data["context_scope"]["participants"] == 2


@pytest.mark.asyncio
async def test_a_tickets_only_scope_hides_sibling_reports():
    scope = _scope(kind="shared")
    scope["policy"] = {"data_classes": ["tickets"]}
    with _patches(
        subs=[{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        scope=scope,
        members={"t1", "t2"},
        visible_by_class={
            "tickets": {"t1", "t2"},
            "reports": {"t1"},
        },
        tickets=[
            {
                "ticket_id": "t1",
                "title": "mine",
                "status": "running",
                "summary": "my finding",
            },
            {
                "ticket_id": "t2",
                "title": "theirs",
                "status": "running",
                "summary": "their secret finding",
            },
        ],
        reports=[
            {"report_id": "r-1", "ticket_id": "t1", "state": "progress"},
            {"report_id": "r-2", "ticket_id": "t2", "state": "progress"},
        ],
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    assert [row["ticket_id"] for row in data["threads"][0]["tickets"]] == [
        "t1",
        "t2",
    ]
    # Summary is report text: it follows the reports class, not tickets.
    assert [row["summary"] for row in data["threads"][0]["tickets"]] == ["", ""]
    assert [row["report_id"] for row in data["threads"][0]["reports"]] == ["r-1"]
    text = shared_context.render_shared_context(data)
    assert "their secret finding" not in text


@pytest.mark.asyncio
async def test_a_handoff_alone_yields_shared_context():
    inherited = [
        {
            "handoff_id": "h-1",
            "thread_key": "thread-1",
            "from_ticket_id": "t-0",
            "state": {"latest_report": {"summary": "half done"}},
            "reason": "better owner",
        }
    ]
    with _patches(
        subs=[{"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"}],
        tickets=[{"ticket_id": "t1", "origin_thread_key": "thread-1"}],
        handoffs=inherited,
    ):
        data = await shared_context.shared_context_for_ticket({"ticket_id": "t1"})

    assert data is not None
    assert data["handoffs"] == inherited


def test_render_emits_handoff_lines_with_the_inherited_summary():
    data = _data()
    data["handoffs"] = [
        {
            "handoff_id": "h-1",
            "thread_key": "thread-1",
            "from_ticket_id": "t-0",
            "state": {"latest_report": {"summary": "previous owner got halfway"}},
            "reason": "better owner",
        }
    ]
    text = shared_context.render_shared_context(data)
    assert "Handoffs into this Ticket" in text
    assert "- [handoff h-1] thread-1 from ticket t-0: previous owner got halfway" in text



def test_render_emits_thread_trigger_ticket_report_and_input_lines():
    text = shared_context.render_shared_context(_data())
    assert "Thread thread-1 (own; 1 ticket(s))" in text
    assert "- [trigger tr-1] alice: follow up" in text
    assert "- [ticket t1] Fix the save button (running): reproduced the crash" in text
    assert "- [report progress] found the cause" in text
    assert _CONTEXT_HEADER in text
    assert "- [input in-1] the sibling ticket owns the API" in text
    assert _TRUNCATION_LINE not in text


def test_render_clips_long_fields_with_an_ellipsis():
    data = _data()
    data["threads"][0]["triggers"] = [
        {"trigger_id": "tr-1", "actor_login": "alice", "text": "x" * 300}
    ]
    data["threads"][0]["tickets"] = [
        {
            "ticket_id": "t1",
            "title": "y" * 200,
            "status": "running",
            "summary": "z" * 500,
        }
    ]
    text = shared_context.render_shared_context(data)
    assert "x" * 239 + "…" in text
    assert "y" * 79 + "…" in text
    assert "z" * 159 + "…" in text


def test_render_stops_at_the_budget_and_flags_the_truncation():
    data = _data()
    data["threads"][0]["triggers"] = [
        {"trigger_id": f"tr-{i}", "actor_login": "alice", "text": "word " * 40}
        for i in range(4)
    ]
    data["threads"][0]["tickets"] = [
        {
            "ticket_id": "t1",
            "title": "y" * 200,
            "status": "running",
            "summary": "z" * 500,
        }
    ]
    data["threads"][0]["reports"] = [
        {"report_id": "r-1", "state": "progress", "summary": "q" * 300}
    ]
    text = shared_context.render_shared_context(data, max_chars=120)
    assert text.splitlines() == [
        "Thread thread-1 (own; 1 ticket(s))",
        _CONTEXT_HEADER,
        _TRUNCATION_LINE,
    ]


@pytest.mark.asyncio
async def test_worker_text_is_empty_when_the_ticket_is_gone():
    with patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value=None)):
        assert await shared_context.worker_shared_context_text("t1") == ""


@pytest.mark.asyncio
async def test_worker_text_is_empty_when_nothing_is_shared():
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value={"ticket_id": "t1"}),
        ),
        patch(f"{_MOD}.shared_context_for_ticket", AsyncMock(return_value=None)),
    ):
        assert await shared_context.worker_shared_context_text("t1") == ""


@pytest.mark.asyncio
async def test_worker_text_renders_the_snapshot():
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value={"ticket_id": "t1"}),
        ),
        patch(
            f"{_MOD}.shared_context_for_ticket",
            AsyncMock(return_value=_data()),
        ),
    ):
        text = await shared_context.worker_shared_context_text("t1")
    assert "Thread thread-1 (own; 1 ticket(s))" in text


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchall(self):
        return self.rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self, row_factory=None):
        return self.cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_list_subscriptions_selects_subscriptions_in_thread_key_order():
    rows = [
        {"ticket_id": "t1", "thread_key": "thread-2", "kind": "context"},
        {"ticket_id": "t1", "thread_key": "thread-1", "kind": "own"},
    ]
    conn = _Conn(rows)
    with patch(
        "src.server.database.autonomy.tickets.get_db_connection",
        return_value=conn,
    ):
        result = await ticket_db.list_subscriptions("t1")
    assert result == rows
    sql, params = conn.cur.calls[0]
    assert "FROM autonomy_subscriptions" in sql
    assert "ORDER BY thread_key" in sql
    assert params == ("t1",)


@pytest.mark.asyncio
async def test_list_reports_for_thread_joins_subscriptions_newest_first():
    rows = [
        {
            "report_id": "r1",
            "ticket_id": "t1",
            "state": "progress",
            "summary": "half",
            "question": None,
            "created_at": None,
        }
    ]
    conn = _Conn(rows)
    with patch(
        "src.server.database.autonomy.tickets.get_db_connection",
        return_value=conn,
    ):
        result = await ticket_db.list_reports_for_thread("thread-1", limit=5, offset=2)
    assert result == rows
    sql, params = conn.cur.calls[0]
    assert "JOIN autonomy_subscriptions s ON s.ticket_id = r.ticket_id" in sql
    assert "ORDER BY r.created_at DESC" in sql
    assert params == ("thread-1", 5, 2)


@pytest.mark.asyncio
async def test_list_tickets_for_thread_selects_the_subscription_kind():
    conn = _Conn([])
    with patch(
        "src.server.database.autonomy.tickets.get_db_connection",
        return_value=conn,
    ):
        assert await ticket_db.list_tickets_for_thread("thread-1") == []
    sql, params = conn.cur.calls[0]
    assert "s.kind AS subscription_kind" in sql
    assert params == ("thread-1",)
