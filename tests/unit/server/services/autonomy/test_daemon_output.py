"""Daemon live output: stream fence, taint, atomic persistence, watcher reads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.server.services.autonomy import daemon_output as store
from src.server.utils import output_guard as guard_mod
from tests.unit.server.fake_output_db import FakeOutputDb, FakeStoreError

DAEMON = "daemon-1"
TICKET = "11111111-1111-4111-8111-111111111111"
LEASE = "22222222-2222-4222-8222-222222222222"
LEASE_2 = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def db(monkeypatch):
    fake = FakeOutputDb()
    fake.allow(DAEMON, TICKET, LEASE)
    monkeypatch.setattr(store, "output_db", fake)
    return fake


def text(value: str, kind: str = "text") -> dict:
    return {"kind": kind, "text": value}


def tool(*names: str) -> dict:
    return {"kind": "tool", "tools": list(names)}


async def post(events, *, epoch=None, seq=1, lease=LEASE, run="run-1"):
    return await store.append_output(DAEMON, TICKET, lease, run, epoch, seq, events)


async def open_stream(events=None):
    result = await post(events or [text("hello there\n")])
    assert result.stored and result.epoch >= 1
    return result.epoch


# -- the fence ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_batch_allocates_the_epoch_and_later_batches_echo_it(db):
    first = await post([text("hello there\n")])
    assert (first.stored, first.epoch, first.head) == (True, 1, "1.1")
    second = await post([text("second line\n")], epoch=1, seq=2)
    assert (second.stored, second.head) == (True, "1.2")
    assert db.stored_text(TICKET) == "hello there\nsecond line\n"
    assert db.sessions[TICKET]["last_batch"] == 2


@pytest.mark.asyncio
async def test_a_daemon_cannot_invent_an_epoch(db):
    result = await post([text("hello\n")], epoch=5, seq=1)
    assert (result.stored, result.code) == (False, store.CODE_STALE_EPOCH)
    assert not db.events.get((TICKET, 1))
    assert db.sessions[TICKET]["lease_id"] is None  # nothing was allocated


@pytest.mark.asyncio
async def test_epoch_mismatch_after_allocation_is_refused_and_changes_nothing(db):
    epoch = await open_stream()
    before = dict(db.sessions[TICKET])
    result = await post([text("x\n")], epoch=epoch + 1, seq=2)
    assert result.code == store.CODE_STALE_EPOCH and result.epoch == epoch
    assert db.sessions[TICKET] == before


@pytest.mark.asyncio
async def test_a_different_lease_cannot_write_into_an_allocated_epoch(db):
    db.allow(DAEMON, TICKET, LEASE_2)
    epoch = await open_stream()
    result = await post([text("hi\n")], epoch=epoch, seq=2, lease=LEASE_2)
    assert result.code == store.CODE_STALE_EPOCH


@pytest.mark.asyncio
async def test_reallocation_bumps_the_epoch_resets_the_guard_and_purges_old_rows(db):
    await open_stream([text("prefix Bearer \n")])
    assert db.stored_text(TICKET) == "prefix Bearer \n"
    again = await post([text("fresh start\n")], epoch=None, seq=1)
    assert again.stored and again.epoch == 2
    assert db.stored_text(TICKET, 2) == "fresh start\n"
    assert (TICKET, 1) not in db.events or not db.events[(TICKET, 1)]
    assert db.sessions[TICKET]["tainted"] is False


@pytest.mark.asyncio
async def test_a_deleted_and_recreated_session_never_reissues_an_epoch(db):
    """The watcher holds `prefix Bearer ` at cursor 1.1; the session row is
    deleted and the run allocates again. The epoch must differ, so the watcher
    is reset before it can be handed the suffix."""
    first = await post([text("prefix Bearer \n")])
    watcher = await store.read_output(TICKET, None)
    assert watcher.cursor == f"{first.epoch}.1"
    db.delete_session(TICKET)
    again = await post([tool("bash"), text("abcdef0123456789xyz tail\n")])
    assert again.epoch != first.epoch
    page = await store.read_output(TICKET, watcher.cursor)
    assert page.reset == store.RESET_NEW_EPOCH
    assert page.epoch == again.epoch


@pytest.mark.asyncio
async def test_a_daemon_with_the_old_epoch_cannot_write_into_a_recreated_session(db):
    first = await post([text("hello there\n")])
    db.delete_session(TICKET)
    late = await post([text("late write\n")], epoch=first.epoch, seq=2)
    assert late.code == store.CODE_STALE_EPOCH


def test_allocation_requires_a_sequence_value_that_does_not_go_backwards():
    row = {"lease_id": LEASE, "epoch": 5, "next_seq": 0, "last_batch": 0}
    for bad in (None, 4):
        with pytest.raises(RuntimeError):
            store.plan_batch(
                row,
                lease_id=LEASE,
                run_id="r",
                epoch=None,
                batch_seq=1,
                events=[text("hi\n")],
                allocated_epoch=bad,
            )


@pytest.mark.asyncio
async def test_a_stop_event_taints_the_epoch_and_resets_watchers(db):
    epoch = await open_stream([text("fine so far\n")])
    stopped = await post([{"kind": "stop"}, tool("bash")], epoch=epoch, seq=2)
    # The tool after the stop is dropped, and the ack says so.
    assert stopped.stored is False and stopped.tainted is True
    assert db.kinds(TICKET) == ["text", "output_reset"]
    after = await post([text("more text\n")], epoch=epoch, seq=3)
    assert after.stored is False and after.tainted is True
    again = await post([{"kind": "stop"}], epoch=epoch, seq=4)
    assert db.kinds(TICKET).count("output_reset") == 1 and again.tainted is True


@pytest.mark.asyncio
async def test_allocation_must_start_at_batch_one(db):
    result = await post([text("hello\n")], epoch=None, seq=4)
    assert result.code == store.CODE_BAD_BATCH and result.stored is False


@pytest.mark.asyncio
async def test_duplicate_batch_seq_is_refused_without_a_second_append(db):
    epoch = await open_stream()
    await post([text("once\n")], epoch=epoch, seq=2)
    replay = await post([text("once\n")], epoch=epoch, seq=2)
    assert replay.code == store.CODE_DUPLICATE
    older = await post([text("old\n")], epoch=epoch, seq=1)
    assert older.code == store.CODE_DUPLICATE
    assert db.stored_text(TICKET).count("once") == 1


@pytest.mark.asyncio
async def test_a_skipped_batch_taints_the_stream_instead_of_trusting_the_gap(db):
    epoch = await open_stream()
    gap = await post([text("looks fine\n"), tool("bash")], epoch=epoch, seq=4)
    assert gap.stored is False and gap.tainted is True
    assert gap.reason == "batch_gap"
    assert "looks fine" not in db.stored_text(TICKET)
    assert db.kinds(TICKET) == ["text", "output_reset"]  # the batch's tool is dropped too
    after = await post([text("still fine?\n")], epoch=epoch, seq=5)
    assert after.stored is False and after.tainted is True


@pytest.mark.asyncio
async def test_unleased_or_revoked_writers_store_nothing(db):
    db.revoke(DAEMON, TICKET, LEASE)
    result = await post([text("hello\n")])
    assert (result.stored, result.code) == (False, store.CODE_STALE_LEASE)
    assert TICKET not in db.sessions


# -- taint ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_event_taints_resets_watchers_and_stops_all_later_text(db):
    epoch = await open_stream([text("intro line\n")])
    bad = await post([text("password"), text("=hunter2\n")], epoch=epoch, seq=2)
    assert bad.stored is False and bad.tainted is True
    # "password" had no trailing whitespace: refused, nothing of it stored.
    assert db.stored_text(TICKET) == "intro line\n"
    assert db.kinds(TICKET) == ["text", "output_reset"]
    more = await post([text("clean text\n"), tool("bash")], epoch=epoch, seq=3)
    assert more.stored is False and more.tainted is True
    assert db.stored_text(TICKET) == "intro line\n"
    assert db.kinds(TICKET) == ["text", "output_reset"]
    assert db.sessions[TICKET]["tainted"] is True


@pytest.mark.asyncio
async def test_tool_only_batches_are_not_stored_after_a_taint(db):
    epoch = await open_stream()
    await post([text("Authorization: x\n")], epoch=epoch, seq=2)
    result = await post([tool("bash")], epoch=epoch, seq=3)
    # Nothing follows a taint, and the ack says so.
    assert result.stored is False and result.tainted is True
    assert db.kinds(TICKET).count("tool") == 0


@pytest.mark.asyncio
async def test_batch_stops_storing_text_at_the_first_refusal_but_keeps_what_was_safe(db):
    epoch = await open_stream()
    result = await post(
        [text("safe one\n"), text("Cookie: a=b\n"), text("safe two\n"), tool("t")],
        epoch=epoch,
        seq=2,
    )
    assert result.stored is False
    assert db.stored_text(TICKET).endswith("safe one\n")
    assert "safe two" not in db.stored_text(TICKET)
    assert db.kinds(TICKET)[-2:] == ["text", "output_reset"]


@pytest.mark.asyncio
async def test_taint_survives_a_different_worker_process(db):
    """Every call reads the persisted row: there is no per-process state."""
    epoch = await open_stream()
    await post([text("secret: x\n")], epoch=epoch, seq=2)
    other_worker = await post([text("harmless\n")], epoch=epoch, seq=3)
    assert other_worker.tainted is True and other_worker.stored is False


@pytest.mark.asyncio
async def test_a_corrupted_persisted_guard_fails_closed(db):
    epoch = await open_stream()
    db.sessions[TICKET]["guard_state"] = {"v": 99}
    result = await post([text("harmless\n")], epoch=epoch, seq=2)
    assert result.stored is False and result.tainted is True
    # Watchers are told, once, instead of the stream just going quiet.
    assert db.kinds(TICKET) == ["text", "output_reset"]
    again = await post([text("more\n")], epoch=epoch, seq=3)
    assert again.tainted is True and db.kinds(TICKET).count("output_reset") == 1
    db.sessions[TICKET]["guard_state"] = {**guard_mod.GuardState().to_json(), "v": 1}
    db.sessions[TICKET]["guard_tail"] = "x" * 100_000
    db.sessions[TICKET]["tainted"] = False
    again = await post([text("harmless\n")], epoch=epoch, seq=3)
    assert again.stored is False


# -- the blocker scenarios, end to end through the persisted state ------------


@pytest.mark.asyncio
async def test_oversized_pending_key_crossing_the_window_cannot_bypass(db):
    epoch = await open_stream([text("password\n")])
    seq = 2
    for _ in range(5):
        result = await post([text(" " * 4000)], epoch=epoch, seq=seq)
        assert result.stored is True
        seq += 1
    final = await post([text("=reviewsecret\n")], epoch=epoch, seq=seq)
    assert final.stored is False and final.tainted is True
    assert "reviewsecret" not in db.stored_text(TICKET)


@pytest.mark.asyncio
async def test_separator_glued_context_keeps_the_key_through_persistence(db):
    """The persisted `guard_tail` must still hold `password` after a stretch of
    glued words longer than the window, so a later `=value` is refused."""
    epoch = await open_stream([text(("abcd : " * 520) + "password\n")])
    assert "password" in db.sessions[TICKET]["guard_tail"]
    assert len(db.sessions[TICKET]["guard_tail"]) <= 2560
    await post([text(" " * 4000)], epoch=epoch, seq=2)
    final = await post([text("=synthetic_value\n")], epoch=epoch, seq=3)
    assert final.stored is False and final.tainted is True
    assert "synthetic_value" not in db.stored_text(TICKET)


@pytest.mark.asyncio
async def test_a_tool_name_is_part_of_the_continuity_window(db):
    """`tool: Bearer` then text `abcdefgh` renders as a bearer credential, so
    the tool row counts in the same window as text."""
    epoch = await open_stream([tool("Bearer")])
    after = await post([text("abcdefgh\n")], epoch=epoch, seq=2)
    assert after.stored is False and after.tainted is True
    assert "abcdefgh" not in db.stored_text(TICKET)
    assert db.kinds(TICKET) == ["tool", "output_reset"]


@pytest.mark.asyncio
async def test_text_then_a_tool_name_is_refused_the_same_way(db):
    epoch = await open_stream([text("prefix Bearer \n")])
    named = await post([tool("abcdefgh12345678")], epoch=epoch, seq=2)
    assert named.stored is False and named.tainted is True
    assert db.kinds(TICKET) == ["text", "output_reset"]


@pytest.mark.asyncio
async def test_ordinary_tool_names_do_not_disturb_text(db):
    epoch = await open_stream([text("starting\n"), tool("exec_command"), text("done\n")])
    again = await post([tool("apply_patch", "read_file"), text("more\n")], epoch=epoch, seq=2)
    assert again.stored is True and again.tainted is False
    assert db.kinds(TICKET) == ["text", "tool", "text", "tool", "text"]


@pytest.mark.asyncio
async def test_after_a_taint_no_tool_name_is_stored(db):
    """Names that outlive the text would form a projection of their own
    (`Basic` in one event, its token in the next) that no window tracks."""
    refused = await post([text("Authorization: x\n")])
    assert refused.tainted is True
    rows_before = len(db.events[(TICKET, refused.epoch)])
    named = await post(
        [tool("Basic"), tool("dGVzdDp1c2VyOnBhc3N3b3Jk"), tool("pass\u200bword: synthetic_value")],
        epoch=refused.epoch,
        seq=2,
    )
    assert named.stored is False and named.tainted is True
    assert len(db.events[(TICKET, refused.epoch)]) == rows_before
    assert db.kinds(TICKET).count("tool") == 0


@pytest.mark.asyncio
async def test_hiding_reasoning_does_not_join_a_key_to_its_value_in_the_store(db):
    epoch = await open_stream([text("password\n")])
    middle = await post(
        [text("the worker pauses here\n", "reasoning")], epoch=epoch, seq=2
    )
    assert middle.stored is True
    last = await post([text("=ordinary-value\n")], epoch=epoch, seq=3)
    assert last.stored is False and last.tainted is True
    assert "ordinary-value" not in db.stored_text(TICKET)


@pytest.mark.asyncio
async def test_a_token_split_over_two_joined_messages_is_not_stored_whole(db):
    """The daemon's synthetic newline must not turn a message boundary into a
    word boundary: `eyJ` then `hbGci...` is one JWT."""
    first = await post([{"kind": "text", "text": "eyJ\n", "join": True}])
    assert first.stored is True
    second = await post(
        [{"kind": "text", "text": "hbGciOiJIUzI1NiJ9\n", "join": True}],
        epoch=first.epoch,
        seq=2,
    )
    assert second.stored is False and second.tainted is True
    assert "hbGci" not in db.stored_text(TICKET)
    # The same words sent as plain aligned events are a different statement:
    # the first event really did end the word.
    other = await post([text("eyJ \n")], epoch=None, seq=1)
    more = await post([text("hbGciOiJIUzI1NiJ9 \n")], epoch=other.epoch, seq=2)
    assert more.stored is True


@pytest.mark.asyncio
async def test_a_scheme_word_that_is_only_a_word_in_the_displayed_text_is_still_judged(db):
    """Reviewer repro: `word`, `Bearer`, ` abcdef...` as joined messages. The
    model wrote `wordBearer abcdef...` (no word boundary before the scheme);
    watchers see `word / Bearer /  abcdef...`. The displayed view redacts, so
    it is refused even though the written view alone would not match."""
    outcomes = []
    seq = 1
    epoch = None
    for message in ("word", "Bearer", " abcdef0123456789", " tail"):
        result = await post(
            [{"kind": "text", "text": message + "\n", "join": True}],
            epoch=epoch,
            seq=seq,
        )
        epoch, seq = result.epoch, seq + 1
        outcomes.append(result.stored)
    assert outcomes[:2] == [True, True] and outcomes[2:] == [False, False]
    assert "abcdef0123456789" not in db.stored_text(TICKET)
    assert db.sessions[TICKET]["tainted"] is True


@pytest.mark.asyncio
async def test_bearer_prefix_token_and_tail_are_never_stored_contiguously(db):
    epoch = await open_stream([text("prefix Bearer \n")])
    token = await post([text("abcdef0123456789xyz\n")], epoch=epoch, seq=2)
    tail = await post([text(" tail\n")], epoch=epoch, seq=3)
    assert token.stored is False and tail.stored is False
    stored = db.stored_text(TICKET)
    assert "abcdef0123456789xyz" not in stored and "tail" not in stored


@pytest.mark.asyncio
async def test_long_jwt_split_across_events_is_not_stored(db):
    jwt = "eyJhbGciOiJIUzI1NiJ9." + "A" * 60 + "." + "B" * 60
    epoch = await open_stream()
    pieces = [jwt[i : i + 30] + " " for i in range(0, len(jwt), 30)]
    for index, piece in enumerate(pieces, start=2):
        await post([text(piece)], epoch=epoch, seq=index)
    assert jwt[:30] not in db.stored_text(TICKET)
    assert "AAAAAAAAAA" not in db.stored_text(TICKET)


# -- atomic persistence ------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_append_produces_no_event_and_leaves_the_guard_untouched(db):
    epoch = await open_stream([text("hello there\n")])
    snapshot = dict(db.sessions[TICKET])
    db.fail_before_commit = FakeStoreError("connection lost")
    result = await post([text("password"), text("more\n")], epoch=epoch, seq=2)
    assert (result.stored, result.code) == (False, store.CODE_UNAVAILABLE)
    assert db.sessions[TICKET] == snapshot
    assert db.stored_text(TICKET) == "hello there\n"
    # The store comes back: the retried batch is judged against the committed
    # state (seq 2 is still the expected one), not against a phantom taint.
    db.fail_before_commit = None
    retried = await post([text("a clean line\n")], epoch=epoch, seq=2)
    assert retried.stored is True
    assert db.stored_text(TICKET) == "hello there\na clean line\n"


@pytest.mark.asyncio
async def test_a_client_that_ignores_a_failed_append_is_caught_by_the_fence(db):
    epoch = await open_stream()
    db.fail_before_commit = FakeStoreError("down")
    await post([text("lost batch ending in Bearer \n")], epoch=epoch, seq=2)
    db.fail_before_commit = None
    # The daemon (ignoring stored:false) sends the next batch, as if seq 2 landed.
    result = await post([text("abcdef0123456789xyz\n")], epoch=epoch, seq=3)
    assert result.stored is False and result.tainted is True
    assert "abcdef0123456789xyz" not in db.stored_text(TICKET)


@pytest.mark.asyncio
async def test_duplicate_primary_key_rolls_the_whole_batch_back(db):
    epoch = await open_stream()
    # Corrupt the counter so the next sequence collides with a stored row.
    db.sessions[TICKET]["next_seq"] = 0
    snapshot = dict(db.sessions[TICKET])
    result = await post([text("one\n"), text("two\n")], epoch=epoch, seq=2)
    assert (result.stored, result.code) == (False, store.CODE_UNAVAILABLE)
    assert db.sessions[TICKET] == snapshot
    assert db.stored_text(TICKET) == "hello there\n"


@pytest.mark.asyncio
async def test_concurrent_workers_serialize_on_the_session_row(db):
    epoch = await open_stream()
    results = await asyncio.gather(
        *[post([text(f"worker {n}\n")], epoch=epoch, seq=2) for n in range(8)]
    )
    assert sum(1 for r in results if r.stored) == 1
    assert sum(1 for r in results if r.code == store.CODE_DUPLICATE) == 7
    assert db.commits == 2
    seqs = sorted(db.events[(TICKET, epoch)])
    assert seqs == list(range(1, len(seqs) + 1))


@pytest.mark.asyncio
async def test_batches_that_arrive_out_of_order_taint_instead_of_reordering(db):
    epoch = await open_stream()
    results = await asyncio.gather(
        *[post([text(f"line {n}\n")], epoch=epoch, seq=n) for n in (5, 4, 3, 2)]
    )
    # Seq 5 reaches the row lock first: batch 2 is missing, so continuity is
    # unprovable. The late batches are then replays of an already-passed
    # position. Nothing is stored out of order.
    assert results[0].tainted is True and results[0].stored is False
    assert all(r.code == store.CODE_DUPLICATE for r in results[1:])
    assert not any(f"line {n}" in db.stored_text(TICKET) for n in (2, 3, 4, 5))


@pytest.mark.asyncio
async def test_retention_trims_old_rows_and_a_slow_reader_gets_a_gap_reset(db, monkeypatch):
    monkeypatch.setattr(store, "OUTPUT_MAX_EVENTS", 5)
    epoch = await open_stream([text("line 1\n")])
    for seq in range(2, 12):
        await post([text(f"line {seq}\n")], epoch=epoch, seq=seq)
    assert sorted(db.events[(TICKET, epoch)]) == list(range(7, 12))
    page = await store.read_output(TICKET, store.format_cursor(epoch, 2))
    assert page.reset == store.RESET_GAP
    assert [e["seq"] for e in page.events] == [7, 8, 9, 10, 11]
    assert page.start == store.format_cursor(epoch, 6)
    fresh = await store.read_output(TICKET, None)
    assert fresh.reset is None and fresh.events[0]["seq"] == 7


# -- watchers --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hole_inside_the_retained_rows_is_never_read_across(db):
    """`password `, `ordinary `, `=reviewsecret`: with row 2 removed, a reader
    must not be handed rows 1 and 3 as if they were adjacent."""
    epoch = await open_stream([text("password \n")])
    await post([text("ordinary \n")], epoch=epoch, seq=2)
    await post([text("=reviewsecret\n")], epoch=epoch, seq=3)
    del db.events[(TICKET, epoch)][2]
    for cursor in (None, store.format_cursor(epoch, 0), store.format_cursor(epoch, 1)):
        page = await store.read_output(TICKET, cursor)
        shown = "".join(e["data"]["text"] for e in page.events)
        assert "password" not in shown or "reviewsecret" not in shown
        if page.events and page.events[0]["seq"] == 1:
            assert [e["seq"] for e in page.events] == [1]
        # The next read from where the page stopped is told to reset first.
        if page.reset is None and page.cursor == store.format_cursor(epoch, 1):
            follow = await store.read_output(TICKET, page.cursor)
            assert follow.reset == store.RESET_GAP
            assert [e["seq"] for e in follow.events] == [3]
        if page.reset == store.RESET_GAP:
            # Standing at the hole, the watcher was reset before row 3.
            assert [e["seq"] for e in page.events] == [3]


@pytest.mark.asyncio
async def test_a_gap_over_a_fully_expired_log_resets_once_and_moves_to_the_head(db):
    epoch = await open_stream([text("one\n")])
    await post([text("two\n")], epoch=epoch, seq=2)
    behind = await store.read_output(TICKET, store.format_cursor(epoch, 1))
    assert [e["seq"] for e in behind.events] == [2]
    db.events[(TICKET, epoch)].clear()  # retention/expiry removed every row
    page = await store.read_output(TICKET, store.format_cursor(epoch, 1))
    assert page.reset == store.RESET_GAP
    assert page.cursor == store.format_cursor(epoch, 2) == page.start
    again = await store.read_output(TICKET, page.cursor)
    assert again.reset is None and again.events == []
    await post([text("three\n")], epoch=epoch, seq=3)
    nxt = await store.read_output(TICKET, page.cursor)
    assert nxt.reset is None and [e["data"]["text"] for e in nxt.events] == ["three\n"]


@pytest.mark.asyncio
async def test_a_fresh_watcher_over_a_fully_expired_log_is_not_reset_for_a_gap(db):
    """It holds nothing, so nothing is lost; the cursor it is handed must not
    read as a hole on its very next read."""
    epoch = await open_stream([text("one\n")])
    await post([text("two\n")], epoch=epoch, seq=2)
    db.events[(TICKET, epoch)].clear()  # expiry removed every row, not the session
    page = await store.read_output(TICKET, None)
    assert page.reset is None and page.events == []
    assert page.cursor == page.start == store.format_cursor(epoch, 2)
    again = await store.read_output(TICKET, page.cursor)
    assert again.reset is None and again.events == []
    await post([text("three\n")], epoch=epoch, seq=3)
    nxt = await store.read_output(TICKET, page.cursor)
    assert nxt.reset is None and [e["data"]["text"] for e in nxt.events] == ["three\n"]


@pytest.mark.asyncio
async def test_a_watcher_of_an_old_epoch_over_an_expired_new_log_is_reset_once(db):
    epoch = await open_stream([text("one\n")])
    watcher = await store.read_output(TICKET, None)
    await post([text("fresh start\n")], epoch=None, seq=1)  # a new epoch
    db.events[(TICKET, epoch + 1)].clear()
    page = await store.read_output(TICKET, watcher.cursor)
    assert page.reset == store.RESET_NEW_EPOCH and page.epoch == epoch + 1
    assert page.cursor == page.start == store.format_cursor(epoch + 1, 1)
    again = await store.read_output(TICKET, page.cursor)
    assert again.reset is None


@pytest.mark.asyncio
async def test_reader_resumes_by_epoch_and_sequence(db):
    epoch = await open_stream([text("one\n"), text("two\n")])
    first = await store.read_output(TICKET, None)
    assert first.reset is None and first.cursor == "1.2"
    assert [e["data"]["text"] for e in first.events] == ["one\n", "two\n"]
    await post([text("three\n")], epoch=epoch, seq=2)
    nxt = await store.read_output(TICKET, first.cursor)
    assert [e["data"]["text"] for e in nxt.events] == ["three\n"] and nxt.reset is None
    idle = await store.read_output(TICKET, nxt.cursor)
    assert idle.events == [] and idle.cursor == nxt.cursor and idle.reset is None


@pytest.mark.asyncio
async def test_a_watcher_holding_a_prefix_is_reset_before_a_new_epoch_suffix(db):
    epoch = await open_stream([text("prefix Bearer \n")])
    watcher = await store.read_output(TICKET, None)
    held = "".join(e["data"]["text"] for e in watcher.events)
    assert held.endswith("Bearer \n")
    await post([text("abcdef0123456789xyz tail\n")], epoch=None, seq=1)  # new epoch 2
    page = await store.read_output(TICKET, watcher.cursor)
    assert page.reset == store.RESET_NEW_EPOCH and page.epoch == epoch + 1
    assert page.start == store.format_cursor(epoch + 1, 0)
    assert all(e["cursor"].startswith(f"{epoch + 1}.") for e in page.events)


@pytest.mark.asyncio
async def test_taint_is_delivered_to_watchers_as_a_reset_marker_in_order(db):
    epoch = await open_stream([text("fine\n")])
    await post([text("Cookie: a=b\n")], epoch=epoch, seq=2)
    page = await store.read_output(TICKET, None)
    kinds = [e["data"]["kind"] for e in page.events]
    assert kinds == ["text", store.RESET_KIND]
    marker = page.events[-1]["data"]
    assert marker["reason"] == store.RESET_CONTINUITY
    assert set(marker) == {"kind", "reason", "run_id"}  # no text, nothing to leak


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["garbage", "1", "1.x", "-1.2", "0.0", "1.99", "9" * 30 + ".1"])
async def test_forged_or_stale_cursors_reset_instead_of_stitching(db, cursor):
    await open_stream([text("one\n")])
    page = await store.read_output(TICKET, cursor)
    assert page.reset in {store.RESET_CURSOR, store.RESET_NEW_EPOCH}


@pytest.mark.asyncio
async def test_unavailable_store_reads_as_empty(db):
    await open_stream()
    db.fail_reads = FakeStoreError("down")
    page = await store.read_output(TICKET, "1.1")
    assert page.events == [] and page.reset is None and page.cursor == "1.1"


@pytest.mark.asyncio
async def test_no_session_reads_as_empty(db):
    page = await store.read_output(TICKET, None)
    assert page.events == [] and page.epoch == 0


# -- shape ------------------------------------------------------------------


def test_normalize_keeps_text_raw_and_sanitizes_tool_names():
    events = [
        SimpleNamespace(kind="text", text="a\x1b[2Jb\n", tools=[]),
        SimpleNamespace(kind="tool", text="", tools=["bash", "", "x" * 200, "\x1b[31mred"]),
        SimpleNamespace(kind="text", text="", tools=[]),
        SimpleNamespace(kind="bogus", text="x", tools=[]),
    ]
    normalized = store.normalize_output_events(events)
    assert normalized[0] == {"kind": "text", "text": "a\x1b[2Jb\n"}
    names = normalized[1]["tools"]
    assert names[0] == "bash" and len(names[1]) <= store.OUTPUT_TEXT_MAX
    assert all("\x1b" not in name for name in names)
    assert len(normalized) == 2


def test_normalize_bounds_a_tool_name_before_scanning_it():
    huge = SimpleNamespace(kind="tool", text="", tools=["a" * 1_000_000])
    names = store.normalize_output_events([huge])[0]["tools"]
    assert len(names) == 1 and len(names[0]) <= store.OUTPUT_TOOL_NAME_MAX


def test_normalize_caps_batch_and_tool_count():
    many = [SimpleNamespace(kind="text", text="x\n", tools=[]) for _ in range(200)]
    assert len(store.normalize_output_events(many)) == store.OUTPUT_BATCH_MAX
    wide = SimpleNamespace(kind="tool", text="", tools=[f"t{i}" for i in range(50)])
    assert len(store.normalize_output_events([wide])[0]["tools"]) == store.OUTPUT_TOOL_MAX


def test_the_log_has_no_field_for_arguments_results_or_prompts():
    plan = store.plan_batch(
        {"lease_id": None, "epoch": 1},
        lease_id=LEASE,
        run_id="r",
        epoch=None,
        batch_seq=1,
        events=[text("hi\n"), tool("bash")],
        allocated_epoch=1,
    )
    allowed = {"kind", "text", "tools", "reason", "run_id"}
    assert all(set(data) <= allowed for _, data in plan.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["\u00b2.1", "1.\u0663", "\u0661.1", "1.1\n", " 1.1"])
async def test_non_ascii_digit_cursors_reset_instead_of_failing_silently(db, cursor):
    await open_stream([text("one\n")])
    assert store.parse_cursor(cursor) is None
    page = await store.read_output(TICKET, cursor)
    assert page.reset == store.RESET_CURSOR and page.events


def test_a_reallocation_purges_the_older_epochs_but_the_first_run_does_not():
    first = store.plan_batch(
        {"lease_id": None, "epoch": 1}, lease_id=LEASE, run_id="r", epoch=None,
        batch_seq=1, events=[text("hi\n")], allocated_epoch=1,
    )
    assert first.purge_older_epochs is False
    again = store.plan_batch(
        {"lease_id": LEASE, "epoch": 1, "next_seq": 1, "last_batch": 1,
         "guard_tail": "", "tainted": False,
         "guard_state": guard_mod.GuardState().to_json()},
        lease_id=LEASE, run_id="r", epoch=None, batch_seq=1,
        events=[text("hi\n")], allocated_epoch=2,
    )
    assert again.purge_older_epochs is True


def test_cursor_codec():
    assert store.parse_cursor("3.17") == (3, 17)
    assert store.parse_cursor("") is None and store.parse_cursor("3") is None
    assert store.format_cursor(3, 17) == "3.17"


@pytest.mark.asyncio
async def test_joined_fragment_without_synthetic_newline_is_a_delta(db):
    first = await post([{"kind": "text", "text": "TOK", "join": True}])
    assert first.stored
    second = await post(
        [{"kind": "text", "text": "EN", "join": True}],
        epoch=first.epoch,
        seq=2,
    )
    assert second.stored
    assert db.stored_text(TICKET) == "TOKEN"
