"""RepoBar list paging, fix-CI idempotency and per-source isolation.

Fakes honour ``page`` / ``per_page`` the way GitHub does, so a test fails
when the poller reads one page and moves the cursor past the rest.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.autonomy.wakers import repobar
from src.server.services.autonomy.wakers.repobar import (
    ensure_fix_ci_ticket,
    poll_configured_repositories,
)

T0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
_TOKEN = "ghp_" + ("d" * 24)


def _z(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _source(state, source_id="s1", repository="acme/app"):
    return {
        "source_id": source_id,
        "user_id": "u1",
        "kind": "github",
        "enabled": True,
        "config": {"repository": repository, "workspace_id": "ws1", "poll_seconds": 60},
        "state": state,
    }


async def _yes(*_args):
    return True


def _page(items, params):
    size = int(params.get("per_page") or 30)
    page = int(params.get("page") or 1)
    return items[(page - 1) * size : page * size]


class _Repo:
    """One fake repository; list endpoints page newest-first."""

    def __init__(self, *, issues=(), runs=(), releases=(), pulls=(), reviews=None):
        self.issues = list(issues)
        self.runs = list(runs)
        self.releases = list(releases)
        self.pulls = list(pulls)
        self.reviews = dict(reviews or {})
        self.reads = []

    async def fetch(self, row, path, params):
        self.reads.append((path, dict(params)))
        if path.endswith("/issues"):
            since = _t(params["since"])
            due = [i for i in self.issues if _t(i["updated_at"]) >= since]
            due.sort(key=lambda i: i["updated_at"], reverse=True)
            return _page(due, params)
        if path.endswith("/actions/runs"):
            ordered = sorted(self.runs, key=lambda r: r["created_at"], reverse=True)
            return {"workflow_runs": _page(ordered, params)}
        if path.endswith("/releases"):
            ordered = sorted(self.releases, key=lambda r: r["created_at"], reverse=True)
            return _page(ordered, params)
        if path.endswith("/pulls"):
            ordered = sorted(self.pulls, key=lambda p: p["updated_at"], reverse=True)
            return _page(ordered, params)
        if path.endswith("/reviews"):
            number = int(path.split("/")[-2])
            return _page(self.reviews.get(number, []), params)  # oldest first
        if path.endswith("/requested_reviewers"):
            return {"users": []}
        return []


async def _poll(repo, state, moment, seen):
    async def list_sources(*, limit=100):
        return [_source(dict(state))]

    async def save(user_id, source_id, new_state):
        state.update(new_state)

    async def enqueue(events):
        seen.extend(events)

    await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save,
        enqueue_events=enqueue,
        fetch=repo.fetch,
        workspace_owner_check=_yes,
        feature_enabled=_yes,
        now=lambda: moment,
    )


def _issue(n, at):
    return {"number": n, "title": f"i{n}", "created_at": _z(at), "updated_at": _z(at)}


@pytest.mark.asyncio
async def test_issues_past_the_first_page_are_read_before_the_cursor_moves():
    repo = _Repo(issues=[_issue(n, T0 + timedelta(minutes=n)) for n in range(1, 26)])
    state = {"waker_cursor": _z(T0)}
    seen = []
    moment = T0 + timedelta(hours=1)
    await _poll(repo, state, moment, seen)
    opened = sorted(
        int(e.idempotency_key.rsplit(":", 1)[1]) for e in seen if e.kind == "issue_opened"
    )
    assert opened == list(range(1, 26))
    assert _t(state["waker_cursor"]) == moment - timedelta(seconds=10)
    assert state["waker_list_scan"] == {}


@pytest.mark.asyncio
async def test_a_page_budget_keeps_the_cursor_and_resumes_the_scan():
    # 75 issues: more than one poll's page budget (3 x 20).
    repo = _Repo(issues=[_issue(n, T0 + timedelta(seconds=30 * n)) for n in range(1, 76)])
    state = {"waker_cursor": _z(T0)}
    seen = []
    first = T0 + timedelta(hours=1)
    await _poll(repo, state, first, seen)
    assert state["waker_cursor"] == _z(T0)
    assert state["waker_list_scan"]["issues"]["page"] == 4
    assert len({e.idempotency_key for e in seen}) == 60
    second = first + timedelta(minutes=2)
    await _poll(repo, state, second, seen)
    opened = {
        int(e.idempotency_key.rsplit(":", 1)[1]) for e in seen if e.kind == "issue_opened"
    }
    assert opened == set(range(1, 76))
    assert state["waker_list_scan"] == {}
    # Not past the moment the resumed scan read page 1.
    assert _t(state["waker_cursor"]) <= first - timedelta(seconds=10)
    assert _t(state["waker_cursor"]) > T0


@pytest.mark.asyncio
async def test_a_run_failure_on_the_second_page_is_not_skipped():
    runs = [
        {
            "id": n,
            "name": "ci",
            "conclusion": "failure" if n == 3 else "success",
            "created_at": _z(T0 + timedelta(minutes=n)),
            "updated_at": _z(T0 + timedelta(minutes=n)),
        }
        for n in range(1, 26)
    ]
    repo = _Repo(runs=runs)
    state = {"waker_cursor": _z(T0)}
    seen = []
    await _poll(repo, state, T0 + timedelta(hours=1), seen)
    assert [e for e in seen if e.kind == "ci_failure"]
    assert len([e for e in seen if e.kind == "ci_success"]) == 24


@pytest.mark.asyncio
async def test_a_full_page_of_old_releases_does_not_freeze_the_cursor():
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    releases = [
        {
            "id": n,
            "tag_name": f"v{n}",
            "created_at": _z(old + timedelta(days=n)),
            "published_at": _z(old + timedelta(days=n)),
        }
        for n in range(40)
    ]
    repo = _Repo(releases=releases)
    state = {"waker_cursor": _z(T0)}
    moment = T0 + timedelta(hours=5)
    await _poll(repo, state, moment, [])
    assert _t(state["waker_cursor"]) == moment - timedelta(seconds=10)
    # The windowed scan stops at page 1; deeper pages are the sweep's, which
    # never holds the cursor.
    pages = [int(q["page"]) for p, q in repo.reads if p.endswith("/releases")]
    assert pages == [1, 2, 3]


def _pr(number, at):
    return {"number": number, "title": "p", "updated_at": _z(at), "head": {"sha": "abc"}}


def _commented(n, at):
    return {"id": 1000 + n, "state": "commented", "submitted_at": _z(at)}


@pytest.mark.asyncio
async def test_changes_requested_after_the_first_review_page_is_seen():
    reviews = [_commented(n, T0 - timedelta(days=1)) for n in range(20)]
    reviews.append(
        {"id": 2000, "state": "changes_requested", "submitted_at": _z(T0 + timedelta(minutes=5))}
    )
    repo = _Repo(pulls=[_pr(7, T0 + timedelta(minutes=5))], reviews={7: reviews})
    state = {"waker_cursor": _z(T0)}
    seen = []
    await _poll(repo, state, T0 + timedelta(hours=1), seen)
    assert [e for e in seen if e.kind == "pr_changes_requested"]


@pytest.mark.asyncio
async def test_review_pages_past_the_budget_hold_the_pr_and_resume():
    reviews = [_commented(n, T0 - timedelta(days=1)) for n in range(320)]
    reviews.append(
        {"id": 2000, "state": "changes_requested", "submitted_at": _z(T0 + timedelta(minutes=5))}
    )
    repo = _Repo(pulls=[_pr(7, T0 + timedelta(minutes=5))], reviews={7: reviews})
    state = {"waker_cursor": _z(T0)}
    seen = []
    first = T0 + timedelta(hours=1)
    await _poll(repo, state, first, seen)
    assert not [e for e in seen if e.kind == "pr_changes_requested"]
    assert state["waker_cursor"] == _z(T0)
    assert state["waker_pull_pending"] == [7]
    assert state["waker_review_pages"] == {"7": 4}
    await _poll(repo, state, first + timedelta(minutes=2), seen)
    assert [e for e in seen if e.kind == "pr_changes_requested"]
    review_pages = [
        int(params["page"]) for path, params in repo.reads if path.endswith("/reviews")
    ]
    assert review_pages == [1, 2, 3, 4]
    assert state["waker_review_pages"] == {}


@pytest.mark.asyncio
async def test_failing_sources_do_not_starve_a_healthy_one(caplog):
    states = {f"bad{i}": {} for i in range(3)}
    states["good"] = {}
    polled = []

    async def list_sources(*, limit=100):
        rows = [
            _source(dict(states[name]), source_id=name, repository=f"acme/{name}")
            for name in ("bad0", "bad1", "bad2", "good")
        ]
        return rows

    async def save(user_id, source_id, new_state):
        states[source_id].update(new_state)

    async def enqueue(events):
        return None

    async def fetch(row, path, params):
        if row["source_id"] != "good":
            raise RuntimeError(f"boom {_TOKEN}")
        polled.append(path)
        return []

    moment = T0 + timedelta(hours=1)
    with caplog.at_level(logging.ERROR):
        for k in range(2):
            await poll_configured_repositories(
                list_sources=list_sources,
                save_state=save,
                enqueue_events=enqueue,
                fetch=fetch,
                workspace_owner_check=_yes,
                feature_enabled=_yes,
                now=lambda k=k: moment + timedelta(seconds=k),
            )
    assert polled
    assert "waker_cursor" in states["good"]
    for name in ("bad0", "bad1", "bad2"):
        assert set(states[name]) == {"waker_next_poll_at"}
    assert _TOKEN not in caplog.text
    assert "repobar source bad0 failed" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_deferral_does_not_stop_the_pass(caplog):
    polled = []

    async def list_sources(*, limit=100):
        return [
            _source({}, source_id="bad", repository="acme/bad"),
            _source({}, source_id="good", repository="acme/good"),
        ]

    async def save(user_id, source_id, new_state):
        if source_id == "bad":
            raise RuntimeError(f"db down {_TOKEN}")

    async def fetch(row, path, params):
        if row["source_id"] == "bad":
            raise RuntimeError("list failed")
        polled.append(path)
        return []

    with caplog.at_level(logging.ERROR):
        await poll_configured_repositories(
            list_sources=list_sources,
            save_state=save,
            enqueue_events=lambda events: asyncio.sleep(0),
            fetch=fetch,
            workspace_owner_check=_yes,
            feature_enabled=_yes,
            now=lambda: T0,
        )
    assert polled
    assert _TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_fix_ci_wakes_create_one_ticket():
    rows = []

    async def find(origin):
        for row in rows:
            if row["origin"] == origin:
                return row["id"]
        return None

    async def create(draft):
        # Yield between the miss and the insert, as a real INSERT does.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        row = {"id": f"t{len(rows) + 1}", "origin": draft["origin_trigger_id"]}
        rows.append(row)
        return row["id"]

    draft = {"origin_trigger_id": "waker:repobar:ci_failure:x"}
    ids = await asyncio.gather(
        *[ensure_fix_ci_ticket(dict(draft), find=find, create=create) for _ in range(4)]
    )
    assert len(rows) == 1
    assert set(ids) == {"t1"}
    assert repobar._FIX_CI_LOCKS == {}


@pytest.mark.asyncio
async def test_fix_ci_lock_wraps_find_and_create():
    order = []

    class _Lock:
        def __init__(self, origin):
            order.append(("lock", origin))

        async def __aenter__(self):
            order.append("enter")

        async def __aexit__(self, *exc):
            order.append("exit")

    async def find(origin):
        order.append("find")
        return None

    async def create(draft):
        order.append("create")
        return "t1"

    await ensure_fix_ci_ticket(
        {"origin_trigger_id": "o1"}, find=find, create=create, lock=_Lock
    )
    assert order == [("lock", "o1"), "enter", "find", "create", "exit"]


class _Cursor:
    def __init__(self, log, answers):
        self.log = log
        self.answers = answers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=()):
        self.log.append((sql, params))

    async def fetchone(self):
        return (self.answers.pop(0) if self.answers else True,)


class _Conn:
    def __init__(self, answers):
        self.log = []
        self.answers = list(answers)
        self.closed = False

    def cursor(self):
        return _Cursor(self.log, self.answers)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_fix_ci_origin_lock_takes_and_releases_an_advisory_lock(monkeypatch):
    conn = _Conn([False, True])

    async def open_session():
        return conn

    monkeypatch.setattr("src.server.database.pool.open_session_connection", open_session)
    async with repobar.fix_ci_origin_lock("o1"):
        assert conn.log[-1][0].startswith("SELECT pg_try_advisory_lock")
    sqls = [sql for sql, _ in conn.log]
    assert sqls[:2] == [sqls[0], sqls[0]]  # retried while busy
    assert "pg_advisory_unlock" in sqls[-1]
    assert conn.log[-1][1] == ("fix-ci:o1", repobar._FIX_CI_LOCK_SALT)
    assert conn.closed


@pytest.mark.asyncio
async def test_default_fix_ci_path_uses_the_cross_process_lock(monkeypatch):
    from src.server.services.autonomy import wakers
    from src.server.services.autonomy.models import EffectRecord
    from src.server.services.autonomy.wakers.repobar import RepoBinding, repository_event

    used = []

    class _Lock:
        def __init__(self, origin):
            used.append(origin)

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    async def find(origin):
        return None

    async def create(draft):
        return "t1"

    monkeypatch.setattr(wakers, "fix_ci_origin_lock", _Lock)
    monkeypatch.setattr(wakers, "_find_fix_ci_ticket", find)
    monkeypatch.setattr(wakers, "_create_fix_ci_ticket", create)
    binding = RepoBinding(
        user_id="u1", workspace_id="ws1", repository="acme/app", fix_ci=True, source_id="s1"
    )
    event = repository_event(
        {
            "repository": "acme/app",
            "workflow_run": {"id": 9, "name": "ci", "conclusion": "failure"},
        },
        binding=binding,
    )
    effect = EffectRecord(
        effect_id="e1",
        user_id="u1",
        kind="wake",
        idempotency_key=event.idempotency_key,
        payload=event.as_payload(),
        attempts=1,
    )
    await wakers.apply_waker_effect(
        effect, bindings=[binding], workspace_owner=_yes
    )
    assert used == [event.idempotency_key]


@pytest.mark.asyncio
async def test_one_bad_source_does_not_stop_due_poll_enqueue(monkeypatch, caplog):
    from src.server.services.autonomy import poll

    sources = [
        {"source_id": "bad", "user_id": "u-bad", "kind": "github", "state": {}},
        {"source_id": "good", "user_id": "u-good", "kind": "github", "state": {}},
    ]
    queued = []

    async def list_enabled_sources(*, limit=100):
        return sources

    async def feature(user_id, name):
        if user_id == "u-bad":
            raise RuntimeError(f"lookup failed {_TOKEN}")
        return True

    async def enqueue_effect(**kwargs):
        queued.append(kwargs["payload"]["source_id"])
        return True

    monkeypatch.setattr(poll.source_db, "list_enabled_sources", list_enabled_sources)
    monkeypatch.setattr(poll, "user_feature_enabled", feature)
    monkeypatch.setattr(poll, "enqueue_effect", enqueue_effect)
    with caplog.at_level(logging.ERROR):
        assert await poll.enqueue_due_polls() == 1
    assert queued == ["good"]
    assert _TOKEN not in caplog.text
