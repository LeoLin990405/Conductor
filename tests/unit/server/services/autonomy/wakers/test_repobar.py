"""RepoBar stays record-only unless the repository opted into fix-CI."""

from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.autonomy.models import EffectRecord
from src.server.services.autonomy.wakers import apply_waker_effect, run_waker_pass
from src.server.services.autonomy.wakers.repobar import (
    RepoBarWaker,
    RepoBinding,
    ensure_fix_ci_ticket,
    fix_ci_draft,
    fix_ci_enabled,
    poll_configured_repositories,
    repo_action,
    repository_event,
    workspace_allows,
)

_TOKEN = "ghp_" + ("c" * 24)


def _failure(**extra):
    body = {
        "repository": {"full_name": "acme/app"},
        "workflow_run": {
            "id": 9,
            "name": f"build {_TOKEN}",
            "conclusion": "failure",
            "html_url": "https://example.test/run",
            "updated_at": "2026-11-10T00:00:00Z",
        },
        "api_key": "super-secret",
        "user_id": "user-1",
        "workspace_id": "ws-1",
    }
    body.update(extra)
    return body


def _effect(event, *, user_id="user-1"):
    return EffectRecord(
        effect_id="effect-1",
        user_id=user_id,
        kind="wake",
        idempotency_key=event.idempotency_key,
        payload=event.as_payload(),
        attempts=1,
    )


def test_disabled_waker_drops_the_delivery():
    assert RepoBarWaker(enabled=False).collect(_failure()) == []


def test_projection_keeps_the_event_and_drops_secrets():
    event = repository_event(_failure())
    blob = str(event.as_payload())
    assert event is not None
    assert event.kind == "ci_failure"
    assert event.repository == "acme/app"
    assert _TOKEN not in blob
    assert "super-secret" not in blob
    assert "api_key" not in event.payload
    assert "[REDACTED]" in event.payload["name"]
    assert event.payload["url"] == "https://example.test/run"


def test_pull_requests_issues_releases_and_green_ci_are_records():
    pull = repository_event(
        {
            "repository": "acme/app",
            "pull_request": {
                "number": 4,
                "title": "Add wakers",
                "html_url": "https://example.test/4",
            },
        }
    )
    issue = repository_event(
        {"repo": "acme/app", "issue": {"number": 2, "title": "Bug"}}
    )
    release = repository_event(
        {"repository": "acme/app", "release": {"id": 3, "tag_name": "v1"}}
    )
    success = repository_event(
        {
            "repository": "acme/app",
            "workflow_run": {"id": 1, "conclusion": "success", "name": "ci"},
        }
    )
    assert [pull.kind, issue.kind, release.kind, success.kind] == [
        "pull_request",
        "issue",
        "release",
        "ci_success",
    ]
    assert repo_action(pull.kind, fix_ci=True) == "record"
    assert repo_action(success.kind, fix_ci=True) == "record"
    assert repo_action("ci_failure", fix_ci=False) == "record"
    assert repo_action("ci_failure", fix_ci=True) == "create_ticket"


def test_fix_ci_is_per_repository():
    assert fix_ci_enabled("Acme/App", {"acme/app"}) is True
    assert fix_ci_enabled("other/app", {"acme/app"}) is False
    assert fix_ci_enabled("acme/app", set()) is False
    assert fix_ci_enabled("acme/app", None) is False


def test_a_pull_request_shaped_issue_is_not_recorded_as_an_issue():
    event = repository_event(
        {
            "repository": "acme/app",
            "issue": {
                "number": 8,
                "title": "Actually a PR",
                "pull_request": {"url": "x"},
            },
        }
    )
    assert event is None


def _binding(**extra):
    fields = {
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "repository": "acme/app",
        "fix_ci": False,
    }
    fields.update(extra)
    return RepoBinding(**fields)


async def _allow(user_id, workspace_id, repository):
    return (
        user_id == "user-1" and workspace_id == "ws-1" and repository == "acme/app"
    )


@pytest.mark.asyncio
async def test_fix_ci_ticket_is_created_only_for_the_enabled_repo():
    binding = _binding()
    event = repository_event(_failure(), binding=binding)
    created = []

    async def find(key):
        return None

    async def create(draft):
        created.append(draft)
        return "ticket-1"

    await apply_waker_effect(
        _effect(event),
        codexbar_enabled=False,
        repobar_enabled=True,
        fix_ci_repos={"other/repo"},
        bindings=[binding],
        workspace_owner=_allow,
        find_ticket=find,
        create_ticket=create,
    )
    assert created == []

    await apply_waker_effect(
        _effect(event),
        codexbar_enabled=False,
        repobar_enabled=True,
        fix_ci_repos={"acme/app"},
        bindings=[binding],
        workspace_owner=_allow,
        find_ticket=find,
        create_ticket=create,
    )
    assert len(created) == 1
    assert created[0]["origin_trigger_id"] == event.idempotency_key
    assert created[0]["user_id"] == "user-1"
    assert created[0]["workspace_id"] == "ws-1"
    assert created[0]["title"].startswith("Fix CI failure on acme/app")
    assert _TOKEN not in str(created)
    assert "super-secret" not in str(created)
    assert "forged" not in str(created)


@pytest.mark.asyncio
async def test_missing_workspace_does_not_create_a_ticket():
    event = repository_event(_failure(workspace_id=""))
    created = []

    async def create(draft):
        created.append(draft)
        return "ticket-1"

    await apply_waker_effect(
        _effect(event),
        codexbar_enabled=False,
        repobar_enabled=True,
        fix_ci_repos={"acme/app"},
        create_ticket=create,
    )
    assert created == []


@pytest.mark.asyncio
async def test_an_existing_fix_ci_ticket_is_reused():
    event = repository_event(_failure())

    async def find(key):
        assert key == event.idempotency_key
        return "ticket-existing"

    async def create(draft):
        raise AssertionError("create")

    found = await ensure_fix_ci_ticket(fix_ci_draft(event), find=find, create=create)
    assert found == "ticket-existing"


@pytest.mark.asyncio
async def test_disabled_repobar_handler_does_not_create():
    event = repository_event(_failure())

    async def create(draft):
        raise AssertionError("create")

    result = await apply_waker_effect(
        _effect(event),
        codexbar_enabled=True,
        repobar_enabled=False,
        fix_ci_repos={"acme/app"},
        create_ticket=create,
    )
    assert result == []


def test_flat_and_nested_events_keep_distinct_ids():
    failed = repository_event(
        {"repository": "acme/app", "event": "ci_failed", "id": "run-1"}
    )
    review = repository_event(
        {
            "repository": "acme/app",
            "action": "review_requested",
            "pull_request": {"number": 4, "title": "Add", "updated_at": "t1"},
            "requested_reviewer": {"id": 7, "login": "octo"},
        }
    )
    flat_review = repository_event(
        {
            "repository": "acme/app",
            "event": "pr_review_requested",
            "id": "4",
            "requested_reviewer": {"id": 7},
        }
    )
    changes = repository_event(
        {
            "repository": "acme/app",
            "action": "submitted",
            "review": {"id": 11, "state": "changes_requested"},
            "pull_request": {"number": 4, "title": "Add"},
        }
    )
    flat_changes = repository_event(
        {"repository": "acme/app", "event": "pr_changes_requested", "id": "11"}
    )
    opened = repository_event(
        {
            "repository": "acme/app",
            "action": "opened",
            "pull_request": {
                "number": 4,
                "title": "Add",
                "head": {"sha": "aaa"},
                "updated_at": "t1",
            },
        }
    )
    synced = repository_event(
        {
            "repository": "acme/app",
            "action": "synchronize",
            "pull_request": {
                "number": 4,
                "title": "Add",
                "head": {"sha": "bbb"},
                "updated_at": "t2",
            },
        }
    )
    issue = repository_event(
        {
            "repository": "acme/app",
            "action": "opened",
            "issue": {"number": 2, "title": "Bug"},
        }
    )
    flat_issue = repository_event(
        {"repository": "acme/app", "event": "issue_opened", "id": "2"}
    )
    release = repository_event(
        {
            "repository": "acme/app",
            "action": "published",
            "release": {"id": 3, "tag_name": "v1"},
        }
    )
    flat_release = repository_event(
        {"repository": "acme/app", "event": "release_published", "id": "3"}
    )
    assert failed is not None and failed.kind == "ci_failure"
    assert review is not None and review.kind == "pr_review_requested"
    assert ":7" in review.idempotency_key
    assert flat_review is not None and flat_review.kind == "pr_review_requested"
    assert changes is not None and changes.kind == "pr_changes_requested"
    assert "11" in changes.idempotency_key
    assert "pull_request:4" not in changes.idempotency_key
    assert flat_changes is not None and flat_changes.kind == "pr_changes_requested"
    assert opened is not None and synced is not None
    assert opened.kind == synced.kind == "pull_request"
    assert opened.idempotency_key != synced.idempotency_key
    assert issue is not None and issue.kind == "issue_opened"
    assert flat_issue is not None and flat_issue.kind == "issue_opened"
    assert release is not None and release.kind == "release_published"
    assert flat_release is not None and flat_release.kind == "release_published"
    assert repo_action(review.kind, fix_ci=True) == "record"
    assert repo_action(failed.kind, fix_ci=True) == "create_ticket"


def test_binding_scopes_the_key_and_ignores_payload_identity():
    left = _binding(user_id="owner-a", workspace_id="ws-a")
    right = _binding(user_id="owner-b", workspace_id="ws-b")
    forged = _failure(user_id="forged-user", workspace_id="forged-ws")
    first = repository_event(forged, binding=left)
    second = repository_event(forged, binding=right)
    unbound = repository_event(forged)
    assert first is not None and second is not None and unbound is not None
    assert first.user_id == "owner-a"
    assert first.workspace_id == "ws-a"
    assert "owner-a:ws-a:acme/app" in first.idempotency_key
    assert "forged-user" not in first.idempotency_key
    assert "forged-ws" not in first.idempotency_key
    assert first.idempotency_key != second.idempotency_key
    assert unbound.user_id == ""
    assert unbound.workspace_id == ""
    assert unbound.idempotency_key != first.idempotency_key
    assert repository_event(forged, binding=_binding(repository="other/app")) is None


def test_workspace_policy_uses_the_configured_repository_set():
    owned = {"user_id": "user-1", "workspace_id": "ws-1", "config": {}}
    assert workspace_allows("user-1", "ws-1", "acme/app", owned) is True
    declared = {
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "config": {"github_repositories": ["Acme/App"]},
    }
    assert workspace_allows("user-1", "ws-1", "acme/app", declared) is True
    assert workspace_allows("user-1", "ws-1", "other/app", declared) is False
    assert workspace_allows("other-user", "ws-1", "acme/app", owned) is False
    assert workspace_allows("user-1", "ws-1", "acme/app", None) is False


@pytest.mark.asyncio
async def test_workspace_policy_blocks_the_fix_ci_ticket():
    binding = _binding(fix_ci=True)
    event = repository_event(_failure(user_id="forged", workspace_id="forged"), binding=binding)

    async def deny(user_id, workspace_id, repository):
        assert user_id == "user-1"
        assert workspace_id == "ws-1"
        return False

    async def create(draft):
        raise AssertionError(draft)

    await apply_waker_effect(
        _effect(event),
        codexbar_enabled=False,
        repobar_enabled=True,
        bindings=[binding],
        workspace_owner=deny,
        create_ticket=create,
    )


@pytest.mark.asyncio
async def test_default_poller_is_used_only_when_repobar_is_on(monkeypatch):
    called = []

    async def poll():
        called.append("poll")
        return []

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.poll_configured_repositories", poll
    )

    async def sink(*, user_id, idempotency_key, payload):
        return True

    off = await run_waker_pass(
        enqueue=sink, codexbar_enabled=False, repobar_enabled=False
    )
    assert off.enqueued == ()
    assert called == []
    await run_waker_pass(enqueue=sink, codexbar_enabled=False, repobar_enabled=True)
    assert called == ["poll"]


def _source(**extra):
    config = {
        "repository": "acme/app",
        "workspace_id": "ws-1",
        "fix_ci": True,
        "poll_seconds": 60,
    }
    config.update(extra.pop("config", {}))
    row = {
        "source_id": "source-1",
        "user_id": "owner-1",
        "kind": "github",
        "enabled": True,
        "config": config,
        "state": {},
    }
    row.update(extra)
    return row


@pytest.mark.asyncio
async def test_configured_poll_projects_ci_failure_with_trusted_identity():
    saved = []

    async def list_sources(*, limit=100):
        return [_source()]

    async def save_state(user_id, source_id, state):
        saved.append((user_id, source_id, dict(state)))

    async def allow(user_id, workspace_id, repository):
        assert (user_id, workspace_id, repository) == ("owner-1", "ws-1", "acme/app")
        return True

    async def enabled(user_id):
        assert user_id == "owner-1"
        return True

    async def fetch(row, path, params):
        assert row["config"]["repository"] == "acme/app"
        assert "?" not in path
        assert "paginate" not in path
        assert params.get("per_page") == "20"
        if path.endswith("/actions/runs"):
            return {
                "workflow_runs": [
                    {
                        "id": 9,
                        "name": "build",
                        "conclusion": "failure",
                        "html_url": "https://example.test/run",
                        "updated_at": "2026-11-10T00:30:00Z",
                    }
                ]
            }
        return []

    moment = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
    events = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=allow,
        feature_enabled=enabled,
        now=lambda: moment,
    )
    assert len(events) == 1
    assert events[0].kind == "ci_failure"
    assert events[0].repository == "acme/app"
    assert events[0].user_id == "owner-1"
    assert events[0].workspace_id == "ws-1"
    assert "owner-1:ws-1:acme/app" in events[0].idempotency_key
    assert saved and saved[0][0] == "owner-1"
    assert "waker_cursor" in saved[0][2]
    assert "waker_next_poll_at" in saved[0][2]


@pytest.mark.asyncio
async def test_gh_poll_is_repository_scoped_and_not_paginated(caplog):
    import logging

    token = "ghp_" + ("b" * 24)
    argv_seen = []
    saved = []

    async def list_sources(*, limit=100):
        return [_source()]

    async def save_state(user_id, source_id, state):
        saved.append(state)

    async def allow(user_id, workspace_id, repository):
        return True

    async def enabled(user_id):
        return True

    async def gh_run(argv):
        argv_seen.append(list(argv))
        assert argv[:3] == ["gh", "api", "--method"]
        assert "GET" in argv
        assert "--paginate" not in argv
        assert any(part.startswith("repos/acme/app/") for part in argv)
        assert "attacker" not in " ".join(argv)
        return 1, b"", f"denied {token}".encode()

    with caplog.at_level(logging.ERROR):
        events = await poll_configured_repositories(
            list_sources=list_sources,
            save_state=save_state,
            gh_run=gh_run,
            workspace_owner_check=allow,
            feature_enabled=enabled,
            now=lambda: datetime(2026, 11, 10, 1, tzinfo=timezone.utc),
        )
    assert events == []
    assert argv_seen
    # Only the retry time moves (so a broken repo cannot hold a pass slot);
    # the cursor and continuation stay where they were.
    assert [set(state) for state in saved] == [{"waker_next_poll_at"}]
    assert token not in caplog.text
    assert "gh api failed" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_fetch_does_not_move_the_cursor():
    saved = []

    async def list_sources(*, limit=100):
        return [_source()]

    async def save_state(user_id, source_id, state):
        saved.append(state)

    async def fetch(row, path, params):
        raise RuntimeError("ghp_" + ("e" * 24))

    async def allow(user_id, workspace_id, repository):
        return True

    events = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=allow,
        feature_enabled=_yes,
        now=lambda: datetime(2026, 11, 10, 1, tzinfo=timezone.utc),
    )
    assert events == []
    # Only the retry time moves (so a broken repo cannot hold a pass slot);
    # the cursor and continuation stay where they were.
    assert [set(state) for state in saved] == [{"waker_next_poll_at"}]


async def _yes(user_id):
    return True


def _moment():
    return datetime(2026, 11, 10, 1, tzinfo=timezone.utc)


def _ci_body():
    return {
        "workflow_runs": [
            {
                "id": 9,
                "name": "build",
                "conclusion": "failure",
                "html_url": "https://example.test/run",
                "updated_at": "2026-11-10T00:30:00Z",
            }
        ]
    }


async def _allow(user_id, workspace_id, repository):
    return True


@pytest.mark.asyncio
async def test_a_failed_enqueue_does_not_advance_the_cursor():
    saved = []
    seen = []

    async def list_sources(*, limit=100):
        return [_source()]

    async def save_state(user_id, source_id, state):
        saved.append(state)

    async def enqueue_events(events):
        seen.extend(event.kind for event in events)
        raise RuntimeError("effect store unavailable")

    async def fetch(row, path, params):
        if path.endswith("/actions/runs"):
            return _ci_body()
        return []

    events = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        enqueue_events=enqueue_events,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert seen == ["ci_failure"]
    assert events == []
    # Only the retry time moves (so a broken repo cannot hold a pass slot);
    # the cursor and continuation stay where they were.
    assert [set(state) for state in saved] == [{"waker_next_poll_at"}]


@pytest.mark.asyncio
async def test_enqueue_is_persisted_before_the_cursor():
    order = []

    async def list_sources(*, limit=100):
        return [_source()]

    async def save_state(user_id, source_id, state):
        order.append(("cursor", "waker_cursor" in state))

    async def enqueue_events(events):
        order.append(("effects", [event.kind for event in events]))

    async def fetch(row, path, params):
        if path.endswith("/actions/runs"):
            return _ci_body()
        return []

    events = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        enqueue_events=enqueue_events,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert [event.kind for event in events] == ["ci_failure"]
    assert order == [("effects", ["ci_failure"]), ("cursor", True)]


@pytest.mark.asyncio
async def test_default_poll_commits_wakes_and_cursor_together(monkeypatch):
    commits = []

    async def commit_source_wakes(**kwargs):
        commits.append(kwargs)

    async def update_source(*args, **kwargs):
        raise AssertionError("cursor committed separately")

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.commit_source_wakes",
        commit_source_wakes,
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.update_source",
        update_source,
    )

    async def list_sources(*, limit=100):
        return [_source()]

    async def fetch(row, path, params):
        if path.endswith("/actions/runs"):
            return _ci_body()
        return []

    events = await poll_configured_repositories(
        list_sources=list_sources,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert [event.kind for event in events] == ["ci_failure"]
    assert len(commits) == 1
    wakes = commits[0]["wakes"]
    assert wakes[0][1] == events[0].idempotency_key
    assert "waker_cursor" in commits[0]["state"]
    assert commits[0]["user_id"] == "owner-1"

    async def fail(**kwargs):
        raise RuntimeError("effect store unavailable")

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.commit_source_wakes", fail
    )
    again = await poll_configured_repositories(
        list_sources=list_sources,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert again == []


@pytest.mark.asyncio
async def test_polling_emits_issue_opened_without_a_webhook_action():
    async def list_sources(*, limit=100):
        return [_source()]

    async def save_state(user_id, source_id, state):
        return None

    async def fetch(row, path, params):
        if path.endswith("/issues"):
            return [
                {
                    "number": 8,
                    "title": "New",
                    "created_at": "2026-11-10T00:20:00Z",
                    "updated_at": "2026-11-10T00:20:00Z",
                    "html_url": "https://example.test/issues/8",
                },
                {
                    "number": 3,
                    "title": "Old",
                    "created_at": "2026-11-01T00:00:00Z",
                    "updated_at": "2026-11-10T00:25:00Z",
                    "html_url": "https://example.test/issues/3",
                },
            ]
        return []

    events = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    opened = [event for event in events if event.kind == "issue_opened"]
    updated = [event for event in events if event.kind == "issue"]
    assert len(opened) == 1
    assert "issue_opened:8" in opened[0].idempotency_key
    assert len(updated) == 1
    assert "issue:3" in updated[0].idempotency_key
    assert "issue_opened" not in updated[0].idempotency_key


@pytest.mark.asyncio
async def test_poll_source_effect_emits_issue_opened_for_github(monkeypatch):
    """``issue_opened`` must come from ``poll_source_effect`` on a GitHub source.

    Drives the real path: ``poll_source_effect`` → ``_poll_github_repo_events``
    → ``poll_repobar_source`` (offline fetch only). The RepoBar switch must be
    on; the default-off install is covered separately.
    """
    from src.server.services.autonomy.models import EFFECT_POLL_SOURCE
    from src.server.services.autonomy.poll import poll_source_effect
    from src.server.services.autonomy.wakers.repobar import poll_repobar_source

    source = _source(user_id="user-1", kind="github")
    seen: list[str] = []
    comments_ran = {"value": False}

    async def get_source(source_id):
        assert source_id == "source-1"
        return source

    async def fetch(row, path, params):
        if path.endswith("/issues"):
            return [
                {
                    "number": 8,
                    "title": "New",
                    "created_at": "2026-11-10T00:20:00Z",
                    "updated_at": "2026-11-10T00:20:00Z",
                    "html_url": "https://example.test/issues/8",
                }
            ]
        return []

    async def save_state(user_id, source_id, state):
        return None

    async def enqueue_events(events):
        seen.extend(event.kind for event in events)

    real_poll = poll_repobar_source

    async def offline_poll(row, **kwargs):
        assert str(row.get("kind")) == "github"
        return await real_poll(
            row,
            fetch=fetch,
            save_state=save_state,
            enqueue_events=enqueue_events,
            workspace_owner_check=_allow,
            feature_enabled=_yes,
            now=_moment,
        )

    async def no_comments(**kwargs):
        comments_ran["value"] = True
        return [], {"last_poll_at": "2026-11-10T01:00:00+00:00"}

    async def update_source(*args, **kwargs):
        return None

    async def receive_trigger(**kwargs):
        return False

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.get_source_any_user", get_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.repobar.poll_repobar_source",
        offline_poll,
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.poll.poll_github_comments", no_comments
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.update_source", update_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.intake.receive_trigger", receive_trigger
    )

    await poll_source_effect(
        EffectRecord(
            effect_id="effect-poll",
            user_id="user-1",
            kind=EFFECT_POLL_SOURCE,
            idempotency_key="autonomy:poll:source-1:initial",
            payload={"source_id": "source-1"},
            attempts=1,
        )
    )
    assert "issue_opened" in seen
    assert seen.count("issue_opened") == 1
    assert comments_ran["value"] is True


@pytest.mark.asyncio
async def test_poll_source_effect_skips_repobar_when_switch_is_off(monkeypatch):
    """Default-off RepoBar must not list GitHub or enqueue wakes on poll."""
    from src.server.services.autonomy.models import EFFECT_POLL_SOURCE
    from src.server.services.autonomy.poll import poll_source_effect

    source = _source(user_id="user-1", kind="github")
    comments_ran = {"value": False}

    async def get_source(source_id):
        return source

    async def boom_poll(*args, **kwargs):
        raise AssertionError("poll_repobar_source must not run when off")

    async def no_comments(**kwargs):
        comments_ran["value"] = True
        return [], {"last_poll_at": "2026-11-10T01:00:00+00:00"}

    async def update_source(*args, **kwargs):
        return None

    async def receive_trigger(**kwargs):
        return False

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.get_source_any_user", get_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.repobar.poll_repobar_source",
        boom_poll,
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.poll.poll_github_comments", no_comments
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.update_source", update_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.intake.receive_trigger", receive_trigger
    )

    await poll_source_effect(
        EffectRecord(
            effect_id="effect-poll-off",
            user_id="user-1",
            kind=EFFECT_POLL_SOURCE,
            idempotency_key="autonomy:poll:source-1:off",
            payload={"source_id": "source-1"},
            attempts=1,
        )
    )
    assert comments_ran["value"] is True


@pytest.mark.asyncio
async def test_repobar_poll_failure_does_not_block_comment_intake(monkeypatch):
    """A RepoBar list failure must not stop GitHub comment intake on the same effect."""
    from src.server.services.autonomy.models import EFFECT_POLL_SOURCE
    from src.server.services.autonomy.poll import poll_source_effect
    from src.server.services.autonomy.wakers.repobar import RepoBarPollError

    source = _source(user_id="user-1", kind="github")
    comments_ran = {"value": False}
    comment_states: list[dict] = []

    async def get_source(source_id):
        return source

    async def boom_poll(*args, **kwargs):
        raise RepoBarPollError("list failed")

    async def comments(**kwargs):
        comments_ran["value"] = True
        return [{"body": "hi"}], {"comment_cursor": "c1"}

    async def update_source(user_id, source_id, *, state=None, **kwargs):
        if state is not None:
            comment_states.append(dict(state))
        return None

    async def receive_trigger(**kwargs):
        return True

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.get_source_any_user", get_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.repobar.poll_repobar_source",
        boom_poll,
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.poll.poll_github_comments", comments
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.sources.update_source", update_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.intake.receive_trigger", receive_trigger
    )

    await poll_source_effect(
        EffectRecord(
            effect_id="effect-poll-isolate",
            user_id="user-1",
            kind=EFFECT_POLL_SOURCE,
            idempotency_key="autonomy:poll:source-1:isolate",
            payload={"source_id": "source-1"},
            attempts=1,
        )
    )
    assert comments_ran["value"] is True
    assert any("comment_cursor" in state for state in comment_states)
    assert not any(state.get("last_error") for state in comment_states)


def _pull(number: int) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "updated_at": f"2026-11-10T00:{10 + number:02d}:00Z",
        "html_url": f"https://example.test/pull/{number}",
    }


@pytest.mark.asyncio
async def test_unvisited_pull_requests_are_read_on_the_next_poll():
    state: dict = {}
    read: list[int] = []

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.clear()
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            return [_pull(number) for number in (1, 2, 3, 4)]
        if "/reviews" in path and "/pulls/" in path:
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            read.append(number)
            return [
                {
                    "id": 100 + number,
                    "state": "commented",
                    "submitted_at": "2026-11-10T00:30:00Z",
                }
            ]
        return []

    first = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert read == [1, 2, 3]
    assert state["waker_pull_pending"] == [4]
    assert [mark["number"] for mark in state["waker_pull_done"]] == [1, 2, 3]
    assert state["waker_cursor"] == "2026-11-10T00:00:00Z"
    first_reviews = [event for event in first if event.kind == "pull_request_review"]
    assert len(first_reviews) == 3

    # The first poll scheduled the next one a minute later. Same clock would
    # skip the source and leave the unread PR behind.
    later = datetime(2026, 11, 10, 1, 2, tzinfo=timezone.utc)
    second = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=lambda: later,
    )
    assert read == [1, 2, 3, 4]
    second_reviews = [event for event in second if event.kind == "pull_request_review"]
    assert len(second_reviews) == 1
    assert "pull_request_review:4:" in second_reviews[0].idempotency_key
    assert state["waker_pull_pending"] == []
    assert state["waker_pull_done"] == []
    assert state["waker_cursor"] == "2026-11-10T01:01:50Z"
    keys = {event.idempotency_key for event in first_reviews + second_reviews}
    assert len(keys) == 4


@pytest.mark.asyncio
async def test_pull_list_pages_beyond_the_first_twenty():
    """A 21st PR must survive past the first ``per_page`` window."""
    state: dict = {"waker_cursor": "2026-11-10T00:00:00Z"}
    read: list[int] = []
    pages: list[int] = []

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.clear()
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            page = int(params.get("page") or "1")
            pages.append(page)
            start = (page - 1) * 20
            return [_pull(number) for number in range(start + 1, start + 21)]
        if "/reviews" in path and "/pulls/" in path:
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            read.append(number)
            return [
                {
                    "id": 100 + number,
                    "state": "commented",
                    "submitted_at": "2026-11-10T00:30:00Z",
                }
            ]
        return []

    clock = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
    for step in range(8):
        await poll_configured_repositories(
            list_sources=list_sources,
            save_state=save_state,
            fetch=fetch,
            workspace_owner_check=_allow,
            feature_enabled=_yes,
            now=lambda: clock + timedelta(minutes=step),
        )
    assert 21 in read
    assert 2 in pages
    assert state.get("waker_pulls_page", 1) >= 2 or 21 in read


@pytest.mark.asyncio
async def test_a_new_review_on_a_visited_pr_is_read_before_the_cursor_moves():
    state: dict = {
        "waker_cursor": "2026-11-10T00:00:00Z",
        "waker_pull_pending": [2, 3, 4],
        "waker_pull_done": [
            {"number": 1, "updated_at": "2026-11-10T00:11:00Z"},
        ],
        "waker_pulls_page": 1,
    }
    read: list[int] = []
    pulls = {
        1: {
            **_pull(1),
            "updated_at": "2026-11-10T00:40:00Z",
        },
        2: _pull(2),
        3: _pull(3),
        4: _pull(4),
    }

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.clear()
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            return [pulls[number] for number in (1, 2, 3, 4)]
        if "/reviews" in path and "/pulls/" in path:
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            read.append(number)
            stamp = "2026-11-10T00:41:00Z" if number == 1 else "2026-11-10T00:30:00Z"
            return [
                {
                    "id": 200 + number,
                    "state": "commented",
                    "submitted_at": stamp,
                }
            ]
        return []

    events = await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert read[0] == 1
    assert 2 in read and 3 in read
    reviews = [event for event in events if event.kind == "pull_request_review"]
    assert any("pull_request_review:1:" in event.idempotency_key for event in reviews)
    done_one = next(
        mark for mark in state["waker_pull_done"] if mark["number"] == 1
    )
    assert done_one["updated_at"] == "2026-11-10T00:40:00Z"
    assert state["waker_cursor"] == "2026-11-10T00:00:00Z"


@pytest.mark.asyncio
async def test_revisited_updates_cannot_starve_pending_pulls():
    state: dict = {
        "waker_cursor": "2026-11-10T00:00:00Z",
        "waker_pull_pending": [4, 5, 6],
        "waker_pull_done": [
            {"number": 1, "updated_at": "2026-11-10T00:11:00Z"},
            {"number": 2, "updated_at": "2026-11-10T00:12:00Z"},
            {"number": 3, "updated_at": "2026-11-10T00:13:00Z"},
        ],
        "waker_pulls_page": 1,
    }
    read: list[int] = []
    pulls = {
        number: {
            **_pull(number),
            "updated_at": f"2026-11-10T00:{40 + number:02d}:00Z",
        }
        for number in (1, 2, 3, 4, 5, 6)
    }

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.clear()
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            return [pulls[number] for number in (1, 2, 3, 4, 5, 6)]
        if "/reviews" in path and "/pulls/" in path:
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            read.append(number)
            return [
                {
                    "id": 300 + number,
                    "state": "commented",
                    "submitted_at": "2026-11-10T00:50:00Z",
                }
            ]
        return []

    await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert 4 in read
    assert len(read) == 3
    assert state["waker_pull_pending"]
    assert 5 in state["waker_pull_pending"] or 6 in state["waker_pull_pending"]


@pytest.mark.asyncio
async def test_multi_page_scan_keeps_since_until_page_one_rescans():
    state: dict = {
        "waker_cursor": "2026-11-10T00:00:00Z",
        "waker_pulls_page": 2,
    }
    pages: list[int] = []

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.clear()
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            page = int(params.get("page") or "1")
            pages.append(page)
            if page == 2:
                # Short final page with no leftover detail backlog.
                return [_pull(21), _pull(22)]
            return [_pull(number) for number in range(1, 21)]
        return []

    later = datetime(2026, 11, 10, 2, tzinfo=timezone.utc)
    await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=lambda: later,
    )
    assert pages == [2]
    assert state["waker_pulls_page"] == 1
    assert state["waker_cursor"] == "2026-11-10T00:00:00Z"

    await poll_configured_repositories(
        list_sources=list_sources,
        save_state=save_state,
        fetch=fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=lambda: later + timedelta(minutes=2),
    )
    assert 1 in pages
    # Only after a page-1 short read may the shared cursor leave the scan window.


def _desc_pull_pages(pulls: dict[int, dict]):
    """GitHub-shaped pulls list: whole history, ``updated_at`` descending."""

    def page_of(page: int) -> list[dict]:
        ordered = sorted(pulls.values(), key=lambda p: p["updated_at"], reverse=True)
        start = (page - 1) * 20
        return [dict(p) for p in ordered[start : start + 20]]

    return page_of


@pytest.mark.asyncio
async def test_a_multi_page_scan_ends_and_moves_the_cursor_to_its_start():
    """30 due PRs (more than one page) are all read, and the cursor moves.

    The old rule ("a short page sends the scan back to page 1 with the same
    since") never ends for a repo with 20+ PRs: page 1 of the whole-history
    list is always full, so the cursor stayed at ``since`` forever.
    The cursor lands at the moment the scan left page 1 (minus overlap), not
    at the final poll, so a PR updated while page 2 drained stays due.
    """
    state: dict = {"waker_cursor": "2026-11-10T00:00:00Z"}
    read: list[int] = []
    pulls = {n: _pull(n) for n in range(1, 31)}
    pages: list[int] = []
    page_of = _desc_pull_pages(pulls)
    review_at: dict[int, str] = {}

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            page = int(params.get("page") or "1")
            pages.append(page)
            return page_of(page)
        if "/reviews" in path and "/pulls/" in path:
            number = int(path.split("/pulls/", 1)[1].split("/", 1)[0])
            read.append(number)
            return [
                {
                    "id": 1000 * number + len(read),
                    "state": "commented",
                    "submitted_at": review_at.get(number, "2026-11-10T00:45:00Z"),
                }
            ]
        return []

    start = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
    left_page_one = None
    events: list = []
    for step in range(30):
        moment = start + timedelta(minutes=step)
        before_page = state.get("waker_pulls_page", 1)
        if before_page >= 2 and 15 not in review_at:
            # PR 15 (already read on page 1) gets a new review mid-scan.
            pulls[15] = {**pulls[15], "updated_at": "2026-11-10T01:20:00Z"}
            review_at[15] = "2026-11-10T01:20:00Z"
        events.extend(
            await poll_configured_repositories(
                list_sources=list_sources,
                save_state=save_state,
                fetch=fetch,
                workspace_owner_check=_allow,
                feature_enabled=_yes,
                now=lambda moment=moment: moment,
            )
        )
        if left_page_one is None and state.get("waker_pulls_page") == 2:
            left_page_one = moment
        if state["waker_cursor"] != "2026-11-10T00:00:00Z":
            break
    assert set(read) >= set(range(1, 31))
    assert 2 in pages
    assert left_page_one is not None
    # Scan start minus the overlap, not the final poll's time.
    assert state["waker_cursor"] == (left_page_one - timedelta(seconds=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert state["waker_pulls_page"] == 1
    assert state["waker_scan_started_at"] == ""

    # The next scan still sees PR 15's mid-scan update as due.
    for step in range(30, 40):
        moment = start + timedelta(minutes=step)
        events.extend(
            await poll_configured_repositories(
                list_sources=list_sources,
                save_state=save_state,
                fetch=fetch,
                workspace_owner_check=_allow,
                feature_enabled=_yes,
                now=lambda moment=moment: moment,
            )
        )
    keys = [e.idempotency_key for e in events if e.kind == "pull_request_review"]
    assert sum(1 for key in keys if "pull_request_review:15:" in key) >= 2


@pytest.mark.asyncio
async def test_a_full_page_that_reaches_older_prs_ends_the_scan():
    """Page 2 is not read when page 1 already reaches a PR older than since."""
    state: dict = {"waker_cursor": "2026-11-10T00:15:00Z"}
    pages: list[int] = []
    pulls = {n: _pull(n) for n in range(1, 21)}  # PRs 1..4 are older than since
    page_of = _desc_pull_pages(pulls)

    async def list_sources(*, limit=100):
        return [_source(state=dict(state))]

    async def save_state(user_id, source_id, new_state):
        state.update(new_state)

    async def fetch(row, path, params):
        if path.endswith("/pulls") and "/pulls/" not in path:
            page = int(params.get("page") or "1")
            pages.append(page)
            return page_of(page)
        return []

    start = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)
    for step in range(8):
        moment = start + timedelta(minutes=step)
        await poll_configured_repositories(
            list_sources=list_sources,
            save_state=save_state,
            fetch=fetch,
            workspace_owner_check=_allow,
            feature_enabled=_yes,
            now=lambda moment=moment: moment,
        )
        if state["waker_cursor"] != "2026-11-10T00:15:00Z":
            break
    assert 2 not in pages
    assert state["waker_pulls_page"] == 1
    assert state["waker_cursor"] > "2026-11-10T00:15:00Z"


@pytest.mark.asyncio
async def test_source_wake_commit_inserts_effects_before_the_cursor(monkeypatch):
    from contextlib import asynccontextmanager

    from src.server.services.autonomy.wakers.source_store import commit_source_wakes

    class _Cursor:
        def __init__(self, log: list[str], *, fail_update: bool) -> None:
            self.log = log
            self.fail_update = fail_update
            self.rowcount = 0
            self._row = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, sql, params=None):
            text = " ".join(sql.split())
            if "INSERT INTO autonomy_effects" in text:
                self.log.append("insert")
                self._row = ("effect",)
                self.rowcount = 1
                return
            if "UPDATE autonomy_sources" in text:
                self.log.append("cursor")
                self._row = None
                self.rowcount = 0 if self.fail_update else 1
                return
            raise AssertionError(text)

        async def fetchone(self):
            return self._row

    class _Conn:
        def __init__(self, log: list[str], *, fail_update: bool) -> None:
            self.log = log
            self.fail_update = fail_update

        def transaction(self):
            return self

        async def __aenter__(self):
            self.log.append("begin")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.log.append("rollback" if exc_type else "commit")
            return False

        def cursor(self):
            return _Cursor(self.log, fail_update=self.fail_update)

    log: list[str] = []

    @asynccontextmanager
    async def connect(conn=None):
        yield _Conn(log, fail_update=False)

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.get_db_connection", connect
    )
    accepted = await commit_source_wakes(
        user_id="owner-1",
        source_id="source-1",
        wakes=[
            ("owner-1", "waker:one", {"source": "repobar", "kind": "ci_failure"}),
            ("owner-1", "waker:two", {"source": "repobar", "kind": "issue_opened"}),
        ],
        state={"waker_cursor": "2026-11-10T00:59:50Z"},
    )
    assert accepted == ["waker:one", "waker:two"]
    assert log == ["begin", "insert", "insert", "cursor", "commit"]

    failed: list[str] = []

    @asynccontextmanager
    async def connect_fail(conn=None):
        yield _Conn(failed, fail_update=True)

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.get_db_connection", connect_fail
    )
    with pytest.raises(RuntimeError, match="source cursor"):
        await commit_source_wakes(
            user_id="owner-1",
            source_id="missing",
            wakes=[("owner-1", "waker:one", {"kind": "ci_failure"})],
            state={"waker_cursor": "2026-11-10T00:59:50Z"},
        )
    assert failed == ["begin", "insert", "cursor", "rollback"]


@pytest.mark.asyncio
async def test_only_the_github_source_kind_is_a_repository_binding(monkeypatch):
    """``repobar`` is not a SourceKind; a row claiming it must not be polled."""
    from contextlib import asynccontextmanager

    from src.server.services.autonomy.wakers.repobar import binding_from_source
    from src.server.services.autonomy.wakers.source_store import fix_ci_repositories

    assert binding_from_source(_source()) is not None
    assert binding_from_source(_source(kind="repobar")) is None
    polled = await poll_configured_repositories(
        list_sources=lambda **_: _async_value([_source(kind="repobar")]),
        save_state=_no_save,
        fetch=_never_fetch,
        workspace_owner_check=_allow,
        feature_enabled=_yes,
        now=_moment,
    )
    assert polled == []

    seen_sql: list[str] = []

    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql, params=None):
            seen_sql.append(" ".join(sql.split()))

        async def fetchall(self):
            return [("acme/app",)]

    class _Conn:
        def cursor(self):
            return _Cursor()

    @asynccontextmanager
    async def connect(conn=None):
        yield _Conn()

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.get_db_connection", connect
    )
    assert await fix_ci_repositories("owner-1") == {"acme/app"}
    assert "kind = 'github'" in seen_sql[0]
    assert "repobar" not in seen_sql[0]


async def _async_value(value):
    return value


async def _no_save(user_id, source_id, state):
    raise AssertionError("a non-github row must not write a cursor")


async def _never_fetch(row, path, params):
    raise AssertionError("a non-github row must not be fetched")
