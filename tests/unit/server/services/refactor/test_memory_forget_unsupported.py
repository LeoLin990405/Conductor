"""The forget notice may say that a provider does not support the operation.

The remote MemFW of contract v2 has no forget endpoint at all. That is not an
outage and not a read-only provider: the capability is missing, and the forget
result has to say so. Before this, every status outside the four the old
vocabulary knew was rewritten to ``unavailable``, and the note that explains
the status was counted as a provider of its own -- so the result said
``unavailable`` twice and never said why.

The three layers a forget passes through are pinned here:

* the **result** the API returns -- the carriers of the forget;
* the **notice** the service hands to the persistence layer;
* the **persistence** itself -- the row the table holds afterwards.

The ruling that explains the status has to survive all three: the forget
result carries it one key deeper than the status it explains, the notice
carries it beside the status, and the row keeps it in ``memfw_reason`` so a
read-back says why as well as what. Only a *code* is persisted -- one of the
three forget reasons -- never the remote's own wording. A ruling that maps to
nothing is stored as NULL and logged under a stable code, never as itself.

No database is touched: the persistence layer is driven through a mock
connection whose cursor keeps the rows it is given and answers the notice
SELECT from them, so the read-back below really fetches what was written. The
column the real table holds -- and the CHECK that guards what may go in it --
are pinned by the migration test of revision 142.

The last section drives the whole chain -- the route, the service, the real
notice write and the real notice read over that one fake connection -- and asks
the question the row exists to answer: does the API report what the federation
said while it was still talking, or what the table holds afterwards?
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.server.database.refactor import memory as db_memory
from src.server.services.refactor import memory_forget as forget
from tests.unit.server.services.refactor.test_memory_forget_review import (
    BODY,
    ENTRY,
    NS,
    OTHER_OWNER,
    OWNER,
    FakeStore,
    _install_db,
    _row,
)

pytestmark = pytest.mark.asyncio

#: What the federation answers for a URL-bound MemFW of contract v2.
REMOTE_FORGET = {
    "memfw": "unsupported",
    "memfw_reason": "remote memfw has no forget endpoint",
}

#: The same notice with a ruling the forget vocabulary has -- spelled as the
#: code itself, the way a caller that already knows the vocabulary hands one
#: over. The column of revision 142 is guarded by that vocabulary, so this is
#: the one that reaches the row unchanged.
REMOTE_FORGET_IN_VOCAB = {
    "memfw": "unsupported",
    "memfw_reason": "remote_rejected",
}

#: A ruling outside the three forget codes. ``lexical`` is one of the
#: contract's ``/related`` reasons: it says why one memory sits next to
#: another, which is not a reason a forget ever gives.
REMOTE_FORGET_OUT_OF_VOCAB = {
    "memfw": "unsupported",
    "memfw_reason": "lexical",
}


def _install_federation(monkeypatch, answer):
    monkeypatch.setattr(forget, "_notify_federation", AsyncMock(return_value=answer))


async def test_the_result_carries_the_unsupported_status_and_its_reason(monkeypatch):
    """The API result says ``unsupported`` and keeps the note that explains it."""
    _install_db(monkeypatch, [_row()])
    _install_federation(monkeypatch, dict(REMOTE_FORGET))
    store = FakeStore({(NS, "fact.md"): {"content": BODY}})

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.carriers[f"federation:memfw:{ENTRY}"] == "unsupported"
    assert (
        result.carriers[f"federation:memfw:{ENTRY}:reason"]
        == "remote memfw has no forget endpoint"
    )
    # The note is not a provider of its own: it is neither counted nor shown
    # as one.
    assert f"federation:memfw_reason:{ENTRY}" not in result.carriers
    assert not [
        key for key in result.carriers if key.startswith("federation:memfw_reason")
    ]


async def test_the_notice_keeps_the_unsupported_status(monkeypatch):
    """The notice the service writes names the provider and the new status."""
    _install_db(monkeypatch, [_row()])
    _install_federation(monkeypatch, dict(REMOTE_FORGET))
    notices = AsyncMock()
    monkeypatch.setattr(forget.db_memory, "record_forget_provider_notices", notices)

    carriers = await forget._notify_rows(OWNER, [_row()])

    assert notices.await_args.kwargs["notices"] == [
        {
            "entry_id": ENTRY,
            "provider": "memfw",
            "status": "unsupported",
            # The ruling is handed over in the federation's own words: which
            # of the three forget codes it maps to is the persistence layer's
            # business, not the service's.
            "reason": "remote memfw has no forget endpoint",
        },
    ]
    assert carriers[f"federation:memfw:{ENTRY}"] == "unsupported"
    assert f"federation:memfw_reason:{ENTRY}" not in carriers


class _FakeTable:
    """The rows the mock connection holds."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.statements: list[str] = []


@pytest.fixture
def fake_table(monkeypatch):
    """A mock connection whose cursor keeps the rows it is given.

    The cursor answers the notice SELECT out of those same rows, so a
    read-back really fetches what the write path stored instead of being
    asserted off the SQL text. It is the minimum a real driver would do:
    ``executemany`` records -- as the UPSERT the write spells, so a retry
    updates the row it already holds instead of adding a second one --
    ``execute`` filters on the owner and the entry, ``fetchall`` returns what
    matched.
    """
    table = _FakeTable()

    class _Cursor:
        def __init__(self) -> None:
            self.fetched: list[dict] = []

        async def executemany(self, sql, values):
            statement = " ".join(str(sql).split())
            table.statements.append(statement)
            for value in values:
                row = tuple(value)
                if "ON CONFLICT" in statement:
                    table.rows[:] = [
                        held for held in table.rows if held[:3] != row[:3]
                    ]
                table.rows.append(row)

        async def execute(self, sql, values=None):
            statement = " ".join(str(sql).split())
            table.statements.append(statement)
            if "SELECT provider, status, memfw_reason" not in statement:
                return
            owner, entry_id = values
            self.fetched = sorted(
                (
                    {
                        "provider": provider,
                        "status": status,
                        "memfw_reason": reason,
                    }
                    for row_owner, row_entry, provider, status, reason in table.rows
                    if row_owner == owner and row_entry == entry_id
                ),
                key=lambda row: row["provider"],
            )

        async def fetchall(self):
            return [dict(row) for row in self.fetched]

    class _Connection:
        @asynccontextmanager
        async def cursor(self, *args, **kwargs):
            yield _Cursor()

        @asynccontextmanager
        async def transaction(self):
            yield None

    @asynccontextmanager
    async def _connection(*args, **kwargs):
        yield _Connection()

    monkeypatch.setattr(db_memory.pool, "get_db_connection", _connection)
    return table


async def test_the_persistence_layer_holds_the_unsupported_status(fake_table):
    """The row the table holds afterwards names the provider and the status.

    The old allowlist refused ``unsupported``, so the write raised and the
    notice was lost; the status of a provider that cannot forget has to
    survive the write.
    """
    await db_memory.record_forget_provider_notices(
        user_id=OWNER,
        notices=[{"entry_id": ENTRY, "provider": "memfw", "status": "unsupported"}],
    )

    # The row the table holds, read back out of it. No ruling was handed
    # over, so the reason column is NULL -- the vocabulary's own "none".
    assert fake_table.rows == [(OWNER, ENTRY, "memfw", "unsupported", None)]
    assert any(
        statement.startswith("INSERT INTO memory_forget_provider_notices")
        for statement in fake_table.statements
    )


async def test_the_persistence_layer_still_refuses_an_unknown_status(fake_table):
    """The allowlist is widened by one status, not opened."""
    with pytest.raises(ValueError):
        await db_memory.record_forget_provider_notices(
            user_id=OWNER,
            notices=[{"entry_id": ENTRY, "provider": "memfw", "status": "made_up"}],
        )

    assert fake_table.rows == []


async def test_a_forget_of_an_unsupported_provider_reports_a_partial_result(
    monkeypatch,
):
    """The status is not ``unavailable``, so the forget is not ``completed``.

    ``completed`` means every copy of the memory is gone. A provider that
    cannot forget is not a provider whose copy is gone.
    """
    _install_db(monkeypatch, [_row()])
    _install_federation(monkeypatch, dict(REMOTE_FORGET))
    store = FakeStore({(NS, "fact.md"): {"content": BODY}})

    result = await forget.forget_memory_entry(
        user_id=OWNER,
        entry_id=ENTRY,
        store=store,
        reason=forget.ForgetReason.USER_REQUEST,
    )

    assert result.status == "partial"
    assert result.carriers["store"] == "deleted"
    assert result.carriers[f"federation:memfw:{ENTRY}"] == "unsupported"


async def test_the_reason_round_trips_through_the_notice_and_the_row(
    monkeypatch, fake_table
):
    """The ruling reaches the row, and a read-back gives it back unchanged.

    The status says *that* the provider could not forget; the ruling says
    *why*. A read-back of the row has both, so it does not have to guess --
    and the ruling that comes back is the one that went in, character for
    character.
    """
    _install_federation(monkeypatch, dict(REMOTE_FORGET_IN_VOCAB))
    handed_over: list[dict] = []
    real_record = db_memory.record_forget_provider_notices

    async def spy(*, user_id, notices):
        handed_over.extend(dict(notice) for notice in notices)
        await real_record(user_id=user_id, notices=notices)

    monkeypatch.setattr(forget.db_memory, "record_forget_provider_notices", spy)

    carriers = await forget._notify_rows(OWNER, [_row()])

    # The API level keeps the note that explains the status ...
    assert carriers[f"federation:memfw:{ENTRY}"] == "unsupported"
    assert carriers[f"federation:memfw:{ENTRY}:reason"] == "remote_rejected"
    # ... the notice the persistence layer was handed carries it ...
    assert handed_over == [
        {
            "entry_id": ENTRY,
            "provider": "memfw",
            "status": "unsupported",
            "reason": "remote_rejected",
        }
    ]
    # ... and the row the table holds gives it back.
    assert fake_table.rows == [(OWNER, ENTRY, "memfw", "unsupported", "remote_rejected")]
    insert = next(
        statement
        for statement in fake_table.statements
        if statement.startswith("INSERT INTO memory_forget_provider_notices")
    )
    assert "memfw_reason" in insert
    assert "memfw_reason = EXCLUDED.memfw_reason" in insert


async def test_a_read_back_of_the_notice_returns_the_reason_it_stored(
    fake_table,
):
    """A read-back of the notice returns the reason, fetched from the row.

    Asserting the SQL string would only prove the column was named. This
    writes a notice the way the forget does, fetches it back through the
    notice read path, and compares what came back: the code the row kept,
    which is the one the write path mapped the remote's wording onto -- and
    not that wording, which never reaches the row.
    """
    await db_memory.record_forget_provider_notices(
        user_id=OWNER,
        notices=[
            {
                "entry_id": ENTRY,
                "provider": "memfw",
                "status": "unsupported",
                "reason": "remote memfw has no forget endpoint",
            }
        ],
    )

    notices = await forget.forget_provider_notices(user_id=OWNER, entry_id=ENTRY)

    assert notices == [
        {
            "provider": "memfw",
            "status": "unsupported",
            "memfw_reason": "no_forget_endpoint",
        }
    ]
    # Only the code is persisted: the remote's own wording is not in the row,
    # so it is not in the read-back either.
    assert "remote memfw has no forget endpoint" not in notices[0]["memfw_reason"]
    assert "remote memfw has no forget endpoint" not in fake_table.rows[0]


async def test_a_read_back_of_a_notice_without_a_reason_returns_none(fake_table):
    """A notice whose ruling was outside the vocabulary reads back as NULL.

    The row keeps the notice either way -- the status is the evidence -- and
    the read-back says so plainly rather than inventing a reason.
    """
    await db_memory.record_forget_provider_notices(
        user_id=OWNER,
        notices=[
            {
                "entry_id": ENTRY,
                "provider": "memfw",
                "status": "unsupported",
                "reason": "lexical",
            }
        ],
    )

    notices = await forget.forget_provider_notices(user_id=OWNER, entry_id=ENTRY)

    assert notices == [
        {"provider": "memfw", "status": "unsupported", "memfw_reason": None}
    ]


async def test_a_ruling_outside_the_vocabulary_is_null_and_only_its_code_is_logged(
    fake_table, caplog
):
    """The notice survives a ruling the forget vocabulary does not have.

    The CHECK of revision 142 is the three forget codes, so a ruling outside
    them cannot be stored. The write keeps the notice and stores NULL rather
    than failing the forget over a note: the status is the evidence, the
    ruling is the explanation. What is logged is the stable code and nothing
    else -- the ruling is untrusted text, and a log line that carried it would
    put it somewhere no vocabulary governs. ``lexical`` is one of the
    contract's ``/related`` reasons: it is exactly the sort of ruling that
    looks like a vocabulary and belongs to a different question.
    """
    with caplog.at_level(logging.WARNING, logger="src.server.database.refactor.memory"):
        await db_memory.record_forget_provider_notices(
            user_id=OWNER,
            notices=[
                {
                    "entry_id": ENTRY,
                    "provider": "memfw",
                    "status": "unsupported",
                    "reason": REMOTE_FORGET_OUT_OF_VOCAB["memfw_reason"],
                }
            ],
        )

    assert fake_table.rows == [(OWNER, ENTRY, "memfw", "unsupported", None)]
    assert "reason_not_in_vocab" in caplog.text
    assert "lexical" not in caplog.text


# --- the whole chain: route -> service -> the row --------------------------


#: The code the persistence layer maps the remote's wording onto. It is what
#: the row keeps and what the read-back hands back -- and it is deliberately
#: *not* the wording the carriers carry while the federation is still talking.
#: A response that holds it can therefore only have read it out of the row:
#: nothing above the SQL ever spells it.
STORED_REASON = "no_forget_endpoint"

#: What the federation answers when the forget is retried: the provider has
#: changed its mind, so the notice is written a second time for the same
#: provider and the UPSERT has to fold it into the row already there.
REMOTE_FORGET_RETRY = {
    "memfw": "read_only",
    "memfw_reason": "remote memfw has no forget endpoint",
}


def _install_federation_per_owner(monkeypatch, answers):
    """The federation answers each owner with its own notice.

    Two owners may be told different things about the same provider, which is
    what makes an owner-scoped read-back observable at all: if the notices
    were ever mixed, the second owner's answer would carry the first owner's
    marker.
    """
    async def answer(*, user_id, entry_id, content_hash):
        return dict(answers[user_id])

    monkeypatch.setattr(forget, "_notify_federation", AsyncMock(side_effect=answer))


def _install_forget_chain(monkeypatch, rows, answers, *, closure=None):
    """Route -> service -> the fake table, with the notice write left real.

    ``_install_db`` is the service-level fake the tests above use: it stubs
    the closure, the tombstones, the projection erase and the policy purge --
    and it stubs the notice write out along with them. That last stub is undone
    here, because the row the write leaves behind is the whole point of a
    read-back: it is what the notice SELECT fetches and what the API answers
    with. Nothing else is faked, so the chain under test is the route, the
    service, the real notice write and the real notice read over the one fake
    connection.
    """
    real_record = db_memory.record_forget_provider_notices
    _install_db(monkeypatch, rows)
    monkeypatch.setattr(
        forget.db_memory, "record_forget_provider_notices", real_record
    )
    _install_federation_per_owner(monkeypatch, answers)
    if closure is not None:
        monkeypatch.setattr(forget.db_memory, "get_forget_closure", closure)
    return real_record


def _install_route(monkeypatch, store):
    """The forget route, with the store it resolves and nothing else faked.

    The route coroutine is called directly rather than through a test client:
    it is the same function FastAPI awaits, and calling it keeps the
    authenticated owner an argument of the test instead of something a
    dependency override supplies.
    """
    from src.server.app import memory as app_memory

    monkeypatch.setattr(app_memory, "_long_term_store", lambda: store)
    return app_memory.forget_memory_entry_route


def _spy_on_result(monkeypatch):
    """Every result the service built, carriers and read-back alike.

    The carriers are the immediate answer -- what the federation said while it
    was still talking -- and they are what the stored marker is contrasted
    against.
    """
    real_forget = forget.forget_memory_entry
    seen: list[forget.ForgetResult] = []

    async def spy(*, user_id, entry_id, store, reason):
        result = await real_forget(
            user_id=user_id, entry_id=entry_id, store=store, reason=reason
        )
        seen.append(result)
        return result

    monkeypatch.setattr(forget, "forget_memory_entry", spy)
    return seen


def _spy_on_read_back(monkeypatch):
    """Every answer the notice SELECT handed back.

    The read-back is the one step that talks to the table once the write is
    behind us, so recording what it returned is what lets a test say the API
    answered with *that* rather than with something it already held.
    """
    real_get = db_memory.get_forget_provider_notices
    fetched: list[list[dict]] = []

    async def spy(*, user_id, entry_id):
        notices = await real_get(user_id=user_id, entry_id=entry_id)
        fetched.append([dict(notice) for notice in notices])
        return notices

    monkeypatch.setattr(forget.db_memory, "get_forget_provider_notices", spy)
    return fetched


def _strings(node) -> set[str]:
    """Every string a payload carries, keys and values alike.

    What the read-back looks like on the wire is the route's business -- one
    key, a list, a nested mapping. That a marker reached it is not, so the
    marker is looked for across the whole payload instead of at one address.
    """
    found: set[str] = set()
    if isinstance(node, str):
        found.add(node)
    elif isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str):
                found.add(key)
            found |= _strings(value)
    elif is_dataclass(node) and not isinstance(node, type):
        found |= _strings(asdict(node))
    elif isinstance(node, (list, tuple, set, frozenset)):
        for item in node:
            found |= _strings(item)
    return found


def _notices(payload) -> list[Mapping]:
    """Every mapping in the payload that names a provider and a status."""
    found: list[Mapping] = []
    if isinstance(payload, Mapping):
        if "provider" in payload and "status" in payload:
            found.append(payload)
        for value in payload.values():
            found.extend(_notices(value))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            found.extend(_notices(item))
    return found


def _reason_of(notice: Mapping):
    """The ruling a notice carries, under whatever name the route gave it."""
    for key in ("memfw_reason", "reason"):
        if key in notice:
            return notice[key]
    return None


async def test_the_route_answers_with_the_notice_the_row_holds(
    monkeypatch, fake_table
):
    """The API reports what the table holds, not what the federation said.

    The forget result is built while the federation is still talking, so its
    carriers carry the remote's own wording. The row keeps only the code that
    wording maps to. The chain is driven end to end -- the route, the service,
    the real notice write and the real notice read over the fake table -- and
    the response has to carry the stored code, which the carriers never spell:
    the answer came out of the row, not out of the result.
    """
    _install_forget_chain(monkeypatch, [_row()], {OWNER: dict(REMOTE_FORGET)})
    seen = _spy_on_result(monkeypatch)
    fetched = _spy_on_read_back(monkeypatch)
    route = _install_route(monkeypatch, FakeStore({(NS, "fact.md"): {"content": BODY}}))

    payload = await route(entry_id=UUID(ENTRY), user_id=OWNER, body=None)

    # The immediate answer -- the carriers -- says *why* in the remote's own
    # words, and never spells the code the row keeps.
    assert seen[0].carriers[f"federation:memfw:{ENTRY}"] == "unsupported"
    assert REMOTE_FORGET["memfw_reason"] in _strings(seen[0].carriers)
    assert STORED_REASON not in _strings(seen[0].carriers)
    # The write put the code in the row ...
    assert fake_table.rows == [(OWNER, ENTRY, "memfw", "unsupported", STORED_REASON)]
    # ... the notice SELECT handed the code back ...
    assert fetched == [
        [{"provider": "memfw", "status": "unsupported", "memfw_reason": STORED_REASON}]
    ]
    # ... and the API answered with exactly that, code and all.
    assert STORED_REASON in _strings(payload)
    assert _notices(payload) == fetched[0]
    assert [(n["provider"], n["status"]) for n in _notices(payload)] == [
        ("memfw", "unsupported")
    ]
    assert [_reason_of(n) for n in _notices(payload)] == [STORED_REASON]


async def test_a_retried_forget_reads_back_the_row_it_updated(
    monkeypatch, fake_table
):
    """A retry updates the notice instead of leaving a second one behind.

    The first forget wrote the row. The retry -- the entry is gone, the
    tombstone is the evidence -- writes the notice again for the same provider,
    which the UPSERT folds into the row already there. The read-back therefore
    answers with one notice, and it is the *updated* one: the status the retry
    produced, with the code the row kept for the reason. The code is the marker
    again -- the retry's own carriers carried the remote's wording, so only the
    read-back can produce it.
    """
    _install_forget_chain(monkeypatch, [_row()], {OWNER: dict(REMOTE_FORGET)})
    route = _install_route(monkeypatch, FakeStore({(NS, "fact.md"): {"content": BODY}}))

    first = await route(entry_id=UUID(ENTRY), user_id=OWNER, body=None)

    _install_federation_per_owner(monkeypatch, {OWNER: dict(REMOTE_FORGET_RETRY)})
    seen = _spy_on_result(monkeypatch)
    fetched = _spy_on_read_back(monkeypatch)
    second = await route(entry_id=UUID(ENTRY), user_id=OWNER, body=None)

    # One row, holding the retry's status and the code for its ruling: the
    # second write updated it instead of adding a row.
    assert fake_table.rows == [(OWNER, ENTRY, "memfw", "read_only", STORED_REASON)]
    # The first answer was the first forget's, the second the retry's: the
    # read-back is taken again, not carried over.
    assert [(n["provider"], n["status"]) for n in _notices(first)] == [
        ("memfw", "unsupported")
    ]
    assert fetched == [
        [{"provider": "memfw", "status": "read_only", "memfw_reason": STORED_REASON}]
    ]
    assert _notices(second) == fetched[0]
    # The retry's carriers carried the remote's wording; the API carries the
    # code the row kept.
    assert REMOTE_FORGET_RETRY["memfw_reason"] in _strings(seen[0].carriers)
    assert STORED_REASON not in _strings(seen[0].carriers)
    assert STORED_REASON in _strings(second)
    assert [_reason_of(n) for n in _notices(second)] == [STORED_REASON]


async def test_another_owner_reads_none_of_these_notices(monkeypatch, fake_table):
    """The notice SELECT is owner-scoped, so the route that reads it is too.

    Both owners forget the *same* entry id, and the table holds one notice
    each. Each caller's answer carries only its own: the marker the first
    owner's row kept is not in the second owner's answer, and the status the
    second owner was told is not in the first's. The closure here is
    owner-scoped the way the SQL is -- the service-level fake would otherwise
    hand the owner the other owner's row as well.
    """
    other_row = {
        **_row(namespace=(OTHER_OWNER, "memory")),
        "user_id": OTHER_OWNER,
    }
    rows = [_row(), other_row]

    async def closure(*, user_id, entry_id):
        return [
            row
            for row in rows
            if row.get("user_id") == user_id and str(row.get("entry_id")) == entry_id
        ]

    _install_forget_chain(
        monkeypatch,
        rows,
        {OWNER: dict(REMOTE_FORGET), OTHER_OWNER: {"memfw": "read_only"}},
        closure=closure,
    )
    route = _install_route(monkeypatch, FakeStore({(NS, "fact.md"): {"content": BODY}}))

    owned = await route(entry_id=UUID(ENTRY), user_id=OWNER, body=None)
    other = await route(entry_id=UUID(ENTRY), user_id=OTHER_OWNER, body=None)

    # One row per owner, for the same entry and the same provider.
    assert sorted(fake_table.rows) == sorted(
        [
            (OWNER, ENTRY, "memfw", "unsupported", STORED_REASON),
            (OTHER_OWNER, ENTRY, "memfw", "read_only", None),
        ]
    )
    # The owner reads its own notice, with the marker its row kept ...
    assert [(n["provider"], n["status"]) for n in _notices(owned)] == [
        ("memfw", "unsupported")
    ]
    assert [_reason_of(n) for n in _notices(owned)] == [STORED_REASON]
    # ... and the other owner reads its own, and nothing of the first's.
    assert [(n["provider"], n["status"]) for n in _notices(other)] == [
        ("memfw", "read_only")
    ]
    assert [_reason_of(n) for n in _notices(other)] == [None]
    assert STORED_REASON not in _strings(other)
    assert "unsupported" not in _strings(other)
