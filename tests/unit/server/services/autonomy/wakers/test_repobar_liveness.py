"""RepoBar liveness: repeated pages, slow GitHub, re-runs and late releases."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.autonomy.wakers import repobar
from src.server.services.autonomy.wakers.repobar import (
    RepoBinding,
    poll_configured_repositories,
    repository_event,
)

T0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


def _z(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


async def _yes(*_args):
    return True


def _page(items, params):
    size = int(params.get("per_page") or 30)
    page = int(params.get("page") or 1)
    return items[(page - 1) * size : page * size]


class _GH:
    def __init__(self):
        self.issues, self.runs, self.releases, self.pulls = [], [], [], []
        self.reviews = {}
        self.latency = 0.0

    async def fetch(self, row, path, params):
        if self.latency:
            await asyncio.sleep(self.latency)
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
            return _page(self.reviews.get(number, []), params)
        if path.endswith("/requested_reviewers"):
            return {"users": []}
        return []


class _Src:
    def __init__(self):
        self.state = {"waker_cursor": _z(T0)}
        self.seen = []

    async def poll(self, fetch, moment):
        async def list_sources(*, limit=100):
            return [
                {
                    "source_id": "s1",
                    "user_id": "u1",
                    "kind": "github",
                    "enabled": True,
                    "config": {
                        "repository": "acme/app",
                        "workspace_id": "ws1",
                        "poll_seconds": 60,
                    },
                    "state": dict(self.state),
                }
            ]

        async def save(user_id, source_id, new_state):
            self.state.update(new_state)

        async def enqueue(events):
            self.seen.extend(events)

        await poll_configured_repositories(
            list_sources=list_sources,
            save_state=save,
            enqueue_events=enqueue,
            fetch=fetch,
            workspace_owner_check=_yes,
            feature_enabled=_yes,
            now=lambda: moment,
        )

    def keys(self, kind):
        return {e.idempotency_key for e in self.seen if e.kind == kind}


def _issue(n, at):
    return {"number": n, "title": f"i{n}", "created_at": _z(at), "updated_at": _z(at)}


def _run(n, created, updated=None, conclusion="success", **extra):
    return {
        "id": n,
        "name": "ci",
        "conclusion": conclusion,
        "created_at": _z(created),
        "updated_at": _z(updated or created),
        **extra,
    }


@pytest.mark.asyncio
async def test_a_repeated_pulls_page_ends_the_scan_without_passing_its_rows():
    pulls = [{"number": n, "updated_at": _z(T0 + timedelta(minutes=n))} for n in range(1, 21)]

    async def fetch(row, path, params):
        if path.endswith("/pulls"):
            return list(pulls)  # ignores ``page``
        if path.endswith("/requested_reviewers"):
            return {"users": []}
        return []

    src = _Src()
    start = T0 + timedelta(hours=1)
    pages = []
    for k in range(40):
        await src.poll(fetch, start + timedelta(minutes=2 * k))
        pages.append(src.state["waker_pulls_page"])
    # The scan ends instead of walking towards page 1000.
    assert max(pages) <= 2
    assert pages[-1] == 1
    # Held at the oldest row the server showed, not moved past it.
    assert src.state["waker_cursor"] == _z(T0 + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_a_legit_page_two_is_not_mistaken_for_a_repeat():
    # 25 due PRs: page 2 differs from page 1, so the scan continues there.
    gh = _GH()
    gh.pulls = [
        {"number": n, "updated_at": _z(T0 + timedelta(minutes=n))} for n in range(1, 26)
    ]
    src = _Src()
    moment = T0 + timedelta(hours=1)
    for k in range(20):
        await src.poll(gh.fetch, moment + timedelta(minutes=2 * k))
    visited = {
        int(e.idempotency_key.rsplit(":pull_request:", 1)[1].split(":")[0])
        for e in src.seen
        if e.kind == "pull_request"
    }
    assert visited == set(range(1, 26))
    assert _t(src.state["waker_cursor"]) > T0 + timedelta(minutes=25)


@pytest.mark.asyncio
async def test_a_slow_github_commits_the_pages_it_read(monkeypatch):
    # Two sequential reads (list, then reviews) take longer than the source
    # timeout. Without a budget below it the poll is cancelled and saves
    # nothing, forever.
    monkeypatch.setattr(repobar, "_SOURCE_TIMEOUT_SECONDS", 0.6)
    monkeypatch.setattr(repobar, "_POLL_BUDGET_SECONDS", 0.45, raising=False)
    monkeypatch.setattr(repobar, "_LIST_BUDGET_SECONDS", 0.1)
    gh = _GH()
    gh.latency = 0.35
    gh.issues = [_issue(n, T0 + timedelta(seconds=30 * n)) for n in range(1, 46)]
    gh.pulls = [{"number": 1, "updated_at": _z(T0 + timedelta(minutes=30))}]
    gh.reviews[1] = [
        {"id": 77, "state": "changes_requested", "submitted_at": _z(T0 + timedelta(minutes=30))}
    ]
    src = _Src()
    moment = T0 + timedelta(hours=1)
    await src.poll(gh.fetch, moment)
    # Page 1 was read and is committed, with the continuation after it.
    assert len(src.keys("issue_opened")) == 20
    assert src.state["waker_list_scan"]["issues"]["page"] == 2
    assert src.state["waker_cursor"] == _z(T0)
    for k in range(1, 5):
        await src.poll(gh.fetch, moment + timedelta(minutes=2 * k))
    assert len(src.keys("issue_opened")) == 45
    assert src.keys("pr_changes_requested")
    assert _t(src.state["waker_cursor"]) > T0


@pytest.mark.asyncio
async def test_a_rerun_of_an_old_workflow_run_is_seen():
    gh = _GH()
    rerun = _run(
        1,
        T0 - timedelta(days=3),
        updated=T0 + timedelta(minutes=30),
        conclusion="failure",
        run_attempt=2,
    )
    idle = [_run(100 + n, T0 - timedelta(days=2) + timedelta(minutes=n)) for n in range(40)]
    gh.runs = [rerun, *idle]
    src = _Src()
    moment = T0 + timedelta(hours=1)
    for k in range(3):
        await src.poll(gh.fetch, moment + timedelta(minutes=2 * k))
    failures = src.keys("ci_failure")
    assert any("workflow_run:1:attempt:2" in key for key in failures)
    # The sweep found it without holding the shared cursor.
    assert _t(src.state["waker_cursor"]) > T0 + timedelta(hours=1)


@pytest.mark.asyncio
async def test_a_draft_published_late_is_seen_behind_newer_releases():
    gh = _GH()
    old = datetime(2025, 6, 1, tzinfo=timezone.utc)
    gh.releases = [
        {
            "id": n,
            "tag_name": f"v{n}",
            "created_at": _z(old + timedelta(days=n)),
            "published_at": _z(old + timedelta(days=n)),
        }
        for n in range(1, 31)
    ]
    gh.releases.append(
        {
            "id": 999,
            "tag_name": "v-late",
            "created_at": _z(old - timedelta(days=30)),
            "published_at": _z(T0 + timedelta(minutes=10)),
        }
    )
    src = _Src()
    moment = T0 + timedelta(hours=1)
    for k in range(3):
        await src.poll(gh.fetch, moment + timedelta(minutes=2 * k))
    assert any("release_published:999" in key for key in src.keys("release_published"))


def test_a_rerun_attempt_gets_its_own_wake_key():
    binding = RepoBinding(
        user_id="u1", workspace_id="ws1", repository="acme/app", source_id="s1"
    )

    def key(attempt):
        run = {"id": 5, "name": "ci", "conclusion": "failure", "run_attempt": attempt}
        return repository_event(
            {"repository": "acme/app", "workflow_run": run}, binding=binding
        ).idempotency_key

    assert key(1).endswith("workflow_run:5")
    assert key(2) != key(1)
    assert key(2) != key(3)


# --- N69: slow-GitHub sweep starvation, gh outliving its deadline ----------


@pytest.mark.asyncio
async def test_a_slow_github_still_moves_the_sweep(monkeypatch):
    # The windowed scan alone uses the soft budget up on every poll. The
    # sweep then never read a page, so a re-run behind idle rows was never
    # seen.
    monkeypatch.setattr(repobar, "_SOURCE_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(repobar, "_POLL_BUDGET_SECONDS", 2.5, raising=False)
    monkeypatch.setattr(repobar, "_LIST_BUDGET_SECONDS", 0.05)
    gh = _GH()
    gh.latency = 0.08
    rerun = _run(
        1,
        T0 - timedelta(days=3),
        updated=T0 + timedelta(minutes=30),
        conclusion="failure",
        run_attempt=2,
    )
    idle = [_run(100 + n, T0 - timedelta(days=2) + timedelta(minutes=n)) for n in range(40)]
    gh.runs = [rerun, *idle]
    src = _Src()
    moment = T0 + timedelta(hours=1)
    for k in range(5):
        await src.poll(gh.fetch, moment + timedelta(minutes=2 * k))
    assert any("workflow_run:1:attempt:2" in key for key in src.keys("ci_failure"))


@pytest.mark.asyncio
async def test_a_gh_process_cancelled_by_the_deadline_is_killed(monkeypatch):
    import sys

    started = []
    real_exec = asyncio.create_subprocess_exec

    async def recording_exec(*argv, **kwargs):
        proc = await real_exec(*argv, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(repobar.asyncio, "create_subprocess_exec", recording_exec)
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(repobar._default_gh_run(argv), timeout=0.5)

    assert len(started) == 1
    assert started[0].returncode is not None
