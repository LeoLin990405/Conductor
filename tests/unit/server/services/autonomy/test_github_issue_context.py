"""Fake-broker regressions: one issue read per new thread, then the capped view."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.github import (
    GitHubCredentialBroker,
    GitHubSourceError,
    github_credential_allowlist,
    poll_github_comments,
)
from src.server.services.autonomy.issue_context import build_issue_context

_TOKEN = "ghp_" + "a" * 36
_CONFIG = {
    "repository": "acme/widgets",
    "secret_name": "GITHUB_TOKEN",
    "workspace_id": "ws",
    "allowed_logins": ["alice"],
}
_STATE = {"agent_login": "conductor-bot"}


def _comment(comment_id: int, number: int, *, kind: str = "issue") -> dict:
    item = {
        "id": comment_id,
        "user": {"login": "alice", "type": "User"},
        "body": f"please look at {number}",
        "html_url": f"https://github.com/acme/widgets/issues/{number}#c{comment_id}",
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
    }
    url = f"https://api.github.com/repos/acme/widgets/issues/{number}"
    if kind == "review":
        item["pull_request_url"] = url.replace("/issues/", "/pulls/")
    else:
        item["issue_url"] = url
    return item


class FakeBroker:
    """Stands in for the credential broker. It never sees a token."""

    def __init__(
        self,
        *,
        comments: list[dict] | None = None,
        reviews: list[dict] | None = None,
        issues: dict[int, dict] | None = None,
        fail_numbers: set[int] | None = None,
        fail_with: Exception | None = None,
        source_secret: str = _TOKEN,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.comments = comments or []
        self.reviews = reviews or []
        self.issues = issues or {}
        self.fail_numbers = fail_numbers or set()
        self.fail_with = fail_with
        self.source_secret = source_secret
        # (path, since, page) per list request; a hook may edit rows between.
        self.pages: list[tuple[str, str, int]] = []
        self.after_list = None
        # Most tests poll with no cursor (a lookback from now), so the fake
        # filters by ``since`` only when a test opts in.
        self.honor_since = False

    async def request_json(self, session, method, path, **kwargs):
        encoded = json.dumps({"method": method, "path": path, "kwargs": kwargs}, default=str)
        assert _TOKEN not in encoded
        self.calls.append((method, path))
        listed = {
            "/repos/acme/widgets/issues/comments": self.comments,
            "/repos/acme/widgets/pulls/comments": self.reviews,
        }
        if path in listed:
            # GitHub semantics: ``since`` is inclusive, rows sorted by update.
            params = kwargs.get("params") or {}
            page = int(params.get("page") or 1)
            size = int(params.get("per_page") or 30)
            since = str(params.get("since") or "")
            self.pages.append((path, since, page))
            rows = sorted(
                (
                    row
                    for row in listed[path]
                    if not self.honor_since or _stamp(row) >= _stamp_text(since)
                ),
                key=_stamp,
            )
            batch = [dict(row) for row in rows[(page - 1) * size : page * size]]
            if self.after_list is not None:
                self.after_list(path, len(self.pages))
            return batch
        match = re.fullmatch(r"/repos/acme/widgets/issues/(\d+)", path)
        assert method == "GET" and match, f"unexpected GitHub request: {method} {path}"
        number = int(match.group(1))
        if number in self.fail_numbers:
            raise self.fail_with or GitHubSourceError("GitHub API 404: missing")
        raw = self.issues.get(
            number,
            {
                "title": "Latch",
                "body": f"sticks {_TOKEN}",
                "html_url": f"https://github.com/acme/widgets/issues/{number}",
                "token": _TOKEN,
            },
        )
        scrub = kwargs.get("scrub")
        # The real broker hands ``scrub`` the Source's own secret.
        return scrub(raw, (self.source_secret,)) if scrub else raw


def _stamp_text(text: str) -> datetime:
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _stamp(row: dict) -> datetime:
    return _stamp_text(str(row.get("updated_at") or row.get("created_at") or ""))


def _issue_gets(calls: list[tuple[str, str]]) -> list[str]:
    return [
        path
        for method, path in calls
        if method == "GET" and re.fullmatch(r"/repos/acme/widgets/issues/\d+", path)
    ]


async def _poll(broker: FakeBroker, state: dict | None = None):
    with patch(
        "src.server.services.autonomy.github._credential_broker", return_value=broker
    ):
        return await poll_github_comments(
            source_id="s1",
            user_id="u1",
            config=_CONFIG,
            state=dict(_STATE if state is None else state),
        )


def test_default_allowlist_covers_exact_issue_get():
    allowlist = github_credential_allowlist("https://api.github.com", "acme/widgets")
    assert allowlist.check(
        "GET", "https://api.github.com/repos/acme/widgets/issues/12"
    ).allowed
    assert allowlist.check(
        "GET", "https://api.github.com/repos/acme/widgets/issues/0"
    ).allowed
    other = allowlist.check(
        "GET", "https://api.github.com/repos/other/widgets/issues/12"
    )
    assert other.allowed is False


@pytest.mark.asyncio
async def test_broker_allows_exact_get_issue_and_redacts_the_token():
    token = _TOKEN

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {
                "title": "Save button",
                "body": f"the note contains {token}",
                "html_url": "https://github.com/acme/widgets/issues/7",
                "token": token,
            }

    class Session:
        def __init__(self):
            self.urls: list[str] = []

        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
            self.urls.append(url)
            return Response()

    session = Session()
    with patch(
        "src.server.services.autonomy.github.reveal_user_secret",
        AsyncMock(return_value=token),
    ):
        result = await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets"
        ).request_json(session, "GET", "/repos/acme/widgets/issues/7")

    assert session.urls == ["https://api.github.com/repos/acme/widgets/issues/7"]
    encoded = json.dumps(result)
    assert token not in encoded
    assert "[REDACTED]" in result["body"]
    assert result["token"] == "[REDACTED]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/repos/acme/widgets/issues/7"),
        ("GET", "/repos/acme/widgets/issues/7/labels"),
        ("GET", "/repos/acme/widgets/issues/"),
        ("GET", "/repos/other/widgets/issues/7"),
    ],
)
async def test_broker_rejects_non_exact_issue_reads_before_reveal(method, path):
    """A write, a sub-resource, or another repository never reveals the token."""
    reveal = AsyncMock(return_value=_TOKEN)
    broker = GitHubCredentialBroker(
        "u1", "gh", "https://api.github.com", "acme/widgets"
    )
    with (
        patch("src.server.services.autonomy.github.reveal_user_secret", reveal),
        pytest.raises(GitHubSourceError, match="outside the configured repository"),
    ):
        await broker.request_json(object(), method, path)
    reveal.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_allows_a_zero_and_a_leading_zero_issue_path():
    """``<digits>`` is the whole path segment, including ``0`` and ``07``."""

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {"title": "Zero", "body": "ok"}

    class Session:
        def request(self, method, url, **kwargs):
            return Response()

    reveal = AsyncMock(return_value=_TOKEN)
    broker = GitHubCredentialBroker(
        "u1", "gh", "https://api.github.com", "acme/widgets"
    )
    with patch("src.server.services.autonomy.github.reveal_user_secret", reveal):
        zero = await broker.request_json(
            Session(), "GET", "/repos/acme/widgets/issues/0"
        )
        padded = await broker.request_json(
            Session(), "GET", "/repos/acme/widgets/issues/07"
        )
    assert zero["title"] == "Zero"
    assert padded["title"] == "Zero"
    assert reveal.await_count == 2


@pytest.mark.asyncio
async def test_poll_fetches_each_new_issue_once_and_attaches_context():
    issue = {
        "title": "Latch sticks",
        "body": f"see {_TOKEN}",
        "html_url": "https://github.com/acme/widgets/issues/12",
        "token": _TOKEN,
    }
    review_issue = {
        "title": "Review me",
        "body": "the diff",
        "html_url": "https://github.com/acme/widgets/pull/34",
    }
    broker = FakeBroker(
        comments=[_comment(1, 12), _comment(2, 12)],
        reviews=[_comment(3, 34, kind="review")],
        issues={12: issue, 34: review_issue},
    )
    drafts, state = await _poll(broker)

    assert _issue_gets(broker.calls) == [
        "/repos/acme/widgets/issues/12",
        "/repos/acme/widgets/issues/34",
    ]
    assert [draft["payload"]["issue_number"] for draft in drafts] == [12, 12, 34]
    expected_12 = build_issue_context(issue, repository="acme/widgets", number=12)
    expected_34 = build_issue_context(review_issue, repository="acme/widgets", number=34)
    assert drafts[0]["payload"]["issue_context"] == expected_12
    assert drafts[1]["payload"]["issue_context"] == expected_12
    assert drafts[2]["payload"]["issue_context"] == expected_34
    assert drafts[0]["payload"]["issue_context"]["body"] == "see [REDACTED]"
    encoded = json.dumps({"drafts": drafts, "state": state})
    assert _TOKEN not in encoded
    assert state["issue_context_seen"] == [12, 34]


@pytest.mark.asyncio
async def test_poll_skips_seen_numbers_and_keeps_at_most_1000():
    seen = list(range(1, 1001))
    broker = FakeBroker(
        comments=[_comment(1, 12), _comment(2, 1001)],
        issues={1001: {"title": "New", "body": "fresh"}},
    )
    drafts, state = await _poll(broker, {**_STATE, "issue_context_seen": seen})

    assert _issue_gets(broker.calls) == ["/repos/acme/widgets/issues/1001"]
    by_number = {draft["payload"]["issue_number"]: draft for draft in drafts}
    assert "issue_context" not in by_number[12]["payload"]
    assert by_number[1001]["payload"]["issue_context"]["title"] == "New"
    assert state["issue_context_seen"] == list(range(2, 1002))
    assert len(state["issue_context_seen"]) == 1000
    assert 1 not in state["issue_context_seen"]
    assert 1001 in state["issue_context_seen"]


@pytest.mark.asyncio
async def test_poll_normalizes_a_dirty_seen_list_and_skips_those_numbers():
    broker = FakeBroker(comments=[_comment(1, 12), _comment(2, 99)])
    drafts, state = await _poll(
        broker,
        {
            **_STATE,
            "issue_context_seen": ["12", True, 0, 12, -3, "nope", "34", "١"],
        },
    )

    assert _issue_gets(broker.calls) == ["/repos/acme/widgets/issues/99"]
    contexts = [draft["payload"].get("issue_context") for draft in drafts]
    assert contexts[0] is None
    assert contexts[1]["title"] == "Latch"
    assert state["issue_context_seen"] == [12, 34, 99]


@pytest.mark.asyncio
async def test_bot_comments_do_not_fetch_or_mark_the_issue_seen():
    bot = _comment(1, 12)
    bot["user"] = {"login": "ci[bot]", "type": "Bot"}
    broker = FakeBroker(comments=[bot])
    drafts, state = await _poll(broker)

    assert drafts == []
    assert _issue_gets(broker.calls) == []
    assert state["issue_context_seen"] == []


@pytest.mark.asyncio
async def test_failed_issue_read_is_swallowed_and_retried(caplog):
    broker = FakeBroker(
        comments=[_comment(1, 12), _comment(2, 34)],
        fail_numbers={12},
        issues={34: {"title": "Other", "body": "ok"}},
    )
    with caplog.at_level(logging.WARNING):
        drafts, state = await _poll(broker)

    assert [draft["payload"]["issue_number"] for draft in drafts] == [12, 34]
    assert "issue_context" not in drafts[0]["payload"]
    assert drafts[1]["payload"]["issue_context"]["title"] == "Other"
    assert state["issue_context_seen"] == [34]
    assert "GitHub issue context unavailable" in caplog.text
    assert "number=12" in caplog.text
    assert _TOKEN not in caplog.text

    broker.fail_numbers.clear()
    broker.calls.clear()
    broker.issues[12] = {"title": "Recovered", "body": "now"}
    again, again_state = await _poll(broker, state)

    assert _issue_gets(broker.calls) == ["/repos/acme/widgets/issues/12"]
    recovered = [draft for draft in again if draft["payload"]["issue_number"] == 12]
    skipped = [draft for draft in again if draft["payload"]["issue_number"] == 34]
    assert recovered[0]["payload"]["issue_context"]["title"] == "Recovered"
    assert "issue_context" not in skipped[0]["payload"]
    assert again_state["issue_context_seen"] == [34, 12]


@pytest.mark.asyncio
async def test_unlisted_comment_does_not_block_context_on_a_later_admitted_poll():
    """An unlisted comment is discarded by intake and must not consume the issue.

    Marking the number seen on that first poll left the later allowed
    ``/conductor`` comment with no fetch and no ``issue_context``.
    """
    issue = {
        "title": "Latch sticks",
        "body": "the pin",
        "html_url": "https://github.com/acme/widgets/issues/12",
    }
    unlisted = _comment(1, 12)
    unlisted["user"] = {"login": "mallory", "type": "User"}
    unlisted["body"] = "/conductor look at this"
    broker = FakeBroker(comments=[unlisted], issues={12: issue})
    discarded, state = await _poll(broker)

    assert [draft["actor_login"] for draft in discarded] == ["mallory"]
    assert "issue_context" not in discarded[0]["payload"]
    assert _issue_gets(broker.calls) == []
    assert state["issue_context_seen"] == []

    admitted = _comment(2, 12)
    admitted["body"] = "/conductor please fix the latch"
    broker.comments = [admitted]
    broker.calls.clear()
    drafts, again_state = await _poll(broker, state)

    assert _issue_gets(broker.calls) == ["/repos/acme/widgets/issues/12"]
    assert drafts[0]["actor_login"] == "alice"
    assert drafts[0]["payload"]["issue_context"]["title"] == "Latch sticks"
    assert drafts[0]["payload"]["issue_context"]["body"] == "the pin"
    assert again_state["issue_context_seen"] == [12]


def _stamped(comment_id: int, number: int, minute: int, **kwargs) -> dict:
    item = _comment(comment_id, number, **kwargs)
    stamp = f"2026-04-01T{minute // 60:02d}:{minute % 60:02d}:00Z"
    item["created_at"] = item["updated_at"] = stamp
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        GitHubSourceError("GitHub API 500"),
        RuntimeError("vault unavailable"),
        PermissionError("credential use denied"),
    ],
)
async def test_any_issue_read_failure_degrades_and_still_advances_the_cursor(
    error, caplog
):
    """A persistent issue read failure must not pin the Source cursor.

    The poll still returns the draft (without issue context) and a cursor at
    the newest comment, so the next poll does not re-read the same window and
    hit the same failure forever.
    """
    broker = FakeBroker(
        comments=[_stamped(1, 12, 5), _stamped(2, 12, 9)],
        fail_numbers={12},
        fail_with=error,
    )
    old_cursor = "2026-04-01T00:00:00+00:00"
    with caplog.at_level(logging.WARNING):
        drafts, state = await _poll(broker, {**_STATE, "cursor": old_cursor})

    assert [draft["payload"]["comment_id"] for draft in drafts] == [1, 2]
    assert all("issue_context" not in draft["payload"] for draft in drafts)
    assert state["cursor"] == "2026-04-01T00:09:00+00:00"
    assert state["issue_context_seen"] == []
    assert state["last_error"] == ""
    assert "GitHub issue context unavailable" in caplog.text


@pytest.mark.asyncio
async def test_poll_reads_every_comment_page_not_only_the_first_100():
    comments = [_stamped(i, 1000 + i, i) for i in range(1, 251)]
    reviews = [_stamped(9001, 77, 300, kind="review")]
    broker = FakeBroker(comments=comments, reviews=reviews)
    broker.honor_since = True
    drafts, state = await _poll(broker, {**_STATE, "cursor": "2026-04-01T00:00:00Z"})

    # Each next page starts at the last update read, not at a page number.
    assert [(path.rsplit("/", 2)[-2], since, page) for path, since, page in broker.pages] == [
        ("issues", "2026-03-31T23:59:50Z", 1),
        ("issues", "2026-04-01T01:40:00Z", 1),
        ("issues", "2026-04-01T03:19:00Z", 1),
        ("pulls", "2026-03-31T23:59:50Z", 1),
    ]
    assert len(drafts) == 251
    assert state["cursor"] == "2026-04-01T05:00:00+00:00"


@pytest.mark.asyncio
async def test_a_list_cut_at_the_page_limit_bounds_the_cursor():
    """Newer review comments must not carry the cursor past unread issue comments."""
    from src.server.services.autonomy import github

    comments = [_stamped(i, 12, i) for i in range(1, 6)]
    reviews = [_stamped(9001, 77, 600, kind="review")]
    broker = FakeBroker(comments=comments, reviews=reviews)
    broker.honor_since = True
    with (
        patch.object(github, "_COMMENT_PAGE_SIZE", 2),
        patch.object(github, "_COMMENT_PAGE_LIMIT", 2),
    ):
        drafts, state = await _poll(
            broker, {**_STATE, "cursor": "2026-04-01T00:00:00Z"}
        )

    # Two requests of two with an inclusive ``since`` read comments 1 to 3.
    assert [draft["payload"]["comment_id"] for draft in drafts] == [1, 2, 3, 9001]
    # Comments 4 and 5 are unread, so the cursor stops at comment 3.
    assert state["cursor"] == "2026-04-01T00:03:00+00:00"


@pytest.mark.asyncio
async def test_an_edit_during_a_multi_page_poll_does_not_skip_a_comment():
    """Page-number paging skipped a comment when an earlier one was edited.

    Comment 1 is edited to the newest update after the first page is read.
    With ``page=2`` the list shifts left by one and comment 3 is never
    returned; with ``since`` = last update read it still is.
    """
    from src.server.services.autonomy import github

    comments = [_stamped(i, 12, i) for i in range(1, 6)]
    broker = FakeBroker(comments=comments)
    broker.honor_since = True

    def edit_first(path, request_count):
        if path.endswith("/issues/comments") and request_count == 1:
            comments[0]["updated_at"] = "2026-04-01T00:30:00Z"

    broker.after_list = edit_first
    with patch.object(github, "_COMMENT_PAGE_SIZE", 2):
        drafts, state = await _poll(
            broker, {**_STATE, "cursor": "2026-04-01T00:00:00Z"}
        )

    assert sorted({draft["payload"]["comment_id"] for draft in drafts}) == [1, 2, 3, 4, 5]
    assert state["cursor"] == "2026-04-01T00:30:00+00:00"


@pytest.mark.asyncio
async def test_a_full_page_on_one_timestamp_steps_to_the_next_page():
    from src.server.services.autonomy import github

    comments = [_stamped(i, 12, 7) for i in range(1, 6)]
    broker = FakeBroker(comments=comments)
    broker.honor_since = True
    with patch.object(github, "_COMMENT_PAGE_SIZE", 2):
        drafts, _state = await _poll(
            broker, {**_STATE, "cursor": "2026-04-01T00:00:00Z"}
        )
    assert [draft["payload"]["comment_id"] for draft in drafts] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_stored_issue_view_is_redacted_against_the_source_secret():
    """Router cards read the stored view, so it must not hold the Source secret.

    The secret has no token shape, so only the Source's own value catches it,
    and it straddles the body cap, so it must be redacted before the clip.
    """
    from src.server.services.autonomy.evals import candidate_card
    from src.server.services.autonomy.issue_context import ISSUE_BODY_CAP

    secret = "plain-source-secret-0b7d4e"
    broker = FakeBroker(
        comments=[_comment(1, 12)],
        source_secret=secret,
        issues={
            12: {
                "title": f"Title {secret}",
                "body": "B" * (ISSUE_BODY_CAP - 8) + secret + "C" * 100,
                "html_url": "https://github.com/acme/widgets/issues/12",
            }
        },
    )
    drafts, _state = await _poll(broker)

    view = drafts[0]["payload"]["issue_context"]
    encoded = json.dumps(view)
    assert secret not in encoded
    assert secret[:8] not in encoded
    assert view["truncated"] is True
    trigger = {
        "trigger_id": "t1",
        "thread_key": "github:acme/widgets#12",
        "actor_login": "alice",
        "payload": drafts[0]["payload"],
    }
    card = candidate_card(trigger, {"kind": "github", "config": {"repository": "acme/widgets"}})
    assert secret not in json.dumps(card)
    assert card["issue_context"]["url"] == "https://github.com/acme/widgets/issues/12"


@pytest.mark.asyncio
async def test_real_broker_scrubs_with_the_source_secret_inside_its_scope():
    secret = "plain-source-secret-91aa"

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {"title": "T", "body": f"x {secret} y"}

    class Session:
        def request(self, method, url, **kwargs):
            return Response()

    seen: list[tuple[str, ...]] = []

    def scrub(raw, secrets):
        seen.append(secrets)
        return build_issue_context(raw, secrets=secrets)

    with patch(
        "src.server.services.autonomy.github.reveal_user_secret",
        AsyncMock(return_value=secret),
    ):
        view = await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets"
        ).request_json(Session(), "GET", "/repos/acme/widgets/issues/7", scrub=scrub)

    assert seen == [(secret,)]
    assert secret not in json.dumps(view)
