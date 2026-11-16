"""L2 dispatch delivery: an approved dispatch pushes and opens its PR.

Fakes only: no request reaches GitHub and no push leaves the machine. The
token used here is a made-up string; every test that sees it asserts it
never lands in a record, a message, a log line or an API response.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_delivery import (
    DELIVERY,
    NEEDS_TOKEN,
    NOT_APPROVED,
    OPENED,
    PR_FAILED,
    PUSH_FAILED,
    PUSHED,
    DispatchDelivery,
    GitHubPullRequestClient,
    GitHubRepo,
    parse_github_remote,
    public_remote,
)
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)

pytestmark = pytest.mark.asyncio

TOKEN = "ghp_" "L2FAKETOKEN0123456789abcdefABCDEF"
REMOTE = "https://github.com/acme/widgets.git"
USER_ID = "test-user-123"


def _basic() -> str:
    return base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode("ascii")


class FakePusher:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error

    async def __call__(self, dispatch, **kwargs) -> None:
        self.calls.append({"branch": dispatch.integrate_branch, **kwargs})
        if self.error is not None:
            raise self.error


class FakePullRequests:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error

    async def open(self, **kwargs) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"number": 7, "url": "https://github.com/acme/widgets/pull/7"}


def _reveal(value: str | None):
    calls: list[tuple[str, str]] = []

    async def reveal(user_id: str, name: str) -> str | None:
        calls.append((user_id, name))
        return value

    reveal.calls = calls  # type: ignore[attr-defined]
    return reveal


async def _approved(tmp_path: Path, gate: FileApprovalGate, *, state=DispatchState.COMPLETE) -> Dispatch:
    dispatch = Dispatch(
        id="d-l2",
        goal="ship the widget",
        user_id=USER_ID,
        created_at="now",
        integrate_branch="integrate/auto-dispatch-d-l2",
        lines=[Line(id="line-0", name="n", task="t", status=LineStatus.PASSED)],
    )
    token = await gate.request_approval(dispatch)
    await gate.resolve(token, True)
    dispatch.approval_token = token
    dispatch.state = state
    return dispatch


def _delivery(tmp_path, gate, *, token=TOKEN, remote=REMOTE, pusher=None, prs=None):
    async def remote_for(dispatch, repo_root):
        return remote, remote

    return DispatchDelivery(
        gate=gate,
        repo_root_for=lambda _d: tmp_path / "repo",
        worktrees_dir_for=lambda _d: tmp_path / "worktrees",
        reveal=_reveal(token),
        pull_requests=prs or FakePullRequests(),
        pusher=pusher or FakePusher(),
        remote_for=remote_for,
        base_branch="main",
    )


def _no_token_anywhere(*values: object) -> None:
    for value in values:
        text = json.dumps(value, default=str) if not isinstance(value, str) else value
        assert TOKEN not in text
        assert _basic() not in text


# -- remote parsing --------------------------------------------------------------


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/acme/widgets.git",
        "https://github.com/acme/widgets",
        "git@github.com:acme/widgets.git",
        "ssh://git@github.com/acme/widgets.git",
        f"https://x-access-token:{TOKEN}@github.com/acme/widgets.git",
    ],
)
async def test_github_remotes_name_their_repository(remote: str) -> None:
    assert parse_github_remote(remote) == GitHubRepo("github.com", "acme/widgets")


async def test_other_remotes_are_not_github_and_credentials_are_stripped() -> None:
    assert parse_github_remote("/srv/git/widgets.git") is None
    assert parse_github_remote("https://gitlab.com/acme/widgets.git") is None
    shown = public_remote(f"https://x-access-token:{TOKEN}@github.com/acme/widgets.git")
    assert shown == "https://github.com/acme/widgets.git"


# -- DispatchDelivery ------------------------------------------------------------


async def test_an_approved_dispatch_pushes_and_opens_a_pr_without_leaking_the_token(
    tmp_path, caplog
) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(tmp_path, gate)
    pusher, prs = FakePusher(), FakePullRequests()
    caplog.set_level(logging.DEBUG)

    record = await _delivery(tmp_path, gate, pusher=pusher, prs=prs).deliver(dispatch)

    assert record["state"] == OPENED
    assert record["pr_url"] == "https://github.com/acme/widgets/pull/7"
    assert record["pr_number"] == 7
    assert record["repository"] == "acme/widgets"
    # The push went to the workspace remote from the integrate branch, with the
    # credential only as environment configuration, never an argv word.
    (call,) = pusher.calls
    assert call["branch"] == dispatch.integrate_branch
    assert call["remote"] == "https://github.com/acme/widgets.git"
    assert call["auth"] == {
        "http.https://github.com/.extraheader": f"AUTHORIZATION: basic {_basic()}"
    }
    assert call["worktrees_dir"] == tmp_path / "worktrees"
    (opened,) = prs.calls
    assert opened["head"] == dispatch.integrate_branch
    assert opened["base"] == "main"
    assert opened["token"] == TOKEN
    _no_token_anywhere(record, caplog.text)


async def test_without_a_token_nothing_is_pushed_and_the_message_says_how_to_fix_it(
    tmp_path,
) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(tmp_path, gate)
    pusher, prs = FakePusher(), FakePullRequests()

    record = await _delivery(tmp_path, gate, token=None, pusher=pusher, prs=prs).deliver(
        dispatch
    )

    assert record["state"] == NEEDS_TOKEN
    assert "GITHUB_TOKEN" in record["message"]
    assert "conductor github auth" in record["message"]
    assert f"conductor dispatch deliver {dispatch.id}" in record["message"]
    assert pusher.calls == [] and prs.calls == []


async def test_an_unapproved_gate_pushes_nothing(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(tmp_path, gate)
    other = Dispatch(id="pending", goal="g", user_id=USER_ID, created_at="now")
    dispatch.approval_token = await gate.request_approval(other)
    pusher = FakePusher()

    record = await _delivery(tmp_path, gate, pusher=pusher).deliver(dispatch)

    assert record["state"] == NOT_APPROVED
    assert pusher.calls == []


async def test_a_failed_push_is_recorded_with_the_token_redacted(tmp_path, caplog) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(tmp_path, gate)
    pusher = FakePusher(RuntimeError(f"fatal: auth {TOKEN} basic {_basic()} rejected"))
    prs = FakePullRequests()
    caplog.set_level(logging.DEBUG)

    record = await _delivery(tmp_path, gate, pusher=pusher, prs=prs).deliver(dispatch)

    assert record["state"] == PUSH_FAILED
    assert "rejected" in record["message"]
    assert prs.calls == []
    _no_token_anywhere(record, caplog.text)


async def test_a_failed_pr_is_recorded_with_the_token_redacted(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(tmp_path, gate)
    prs = FakePullRequests(RuntimeError(f"GitHub API 401: bad credentials {TOKEN}"))

    record = await _delivery(tmp_path, gate, prs=prs).deliver(dispatch)

    assert record["state"] == PR_FAILED
    _no_token_anywhere(record)


async def test_a_remote_off_github_is_pushed_without_a_token_or_pr(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(tmp_path, gate)
    pusher, prs = FakePusher(), FakePullRequests()
    reveal = _reveal(TOKEN)
    delivery = _delivery(tmp_path, gate, remote="/srv/git/widgets.git", pusher=pusher, prs=prs)
    delivery._reveal = reveal

    record = await delivery.deliver(dispatch)

    assert record["state"] == PUSHED
    assert pusher.calls[0]["auth"] == {}
    assert prs.calls == [] and reveal.calls == []


# -- the GitHub client -------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _Session:
    def __init__(self, replies: list[tuple[int, object]]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        status, payload = self.replies.pop(0)
        return _Resp(status, payload)


async def test_the_pr_client_reuses_an_open_pr_and_redacts_error_bodies() -> None:
    session = _Session(
        [
            (422, {"message": "A pull request already exists"}),
            (200, [{"number": 3, "html_url": "https://github.com/acme/widgets/pull/3"}]),
        ]
    )
    client = GitHubPullRequestClient(session_factory=lambda: session)
    repo = GitHubRepo("github.com", "acme/widgets")

    opened = await client.open(
        token=TOKEN, repo=repo, head="integrate/x", base="main", title="t", body="b"
    )

    assert opened == {"number": 3, "url": "https://github.com/acme/widgets/pull/3"}
    (post, get) = session.requests
    assert post[0] == "POST" and post[1] == "https://api.github.com/repos/acme/widgets/pulls"
    assert post[2]["allow_redirects"] is False
    assert get[2]["params"] == {"head": "acme:integrate/x", "state": "open"}

    failing = _Session([(401, {"message": f"Bad credentials {TOKEN}"})])
    client = GitHubPullRequestClient(session_factory=lambda: failing)
    with pytest.raises(Exception) as caught:
        await client.open(
            token=TOKEN, repo=repo, head="integrate/x", base="main", title="t", body="b"
        )
    assert TOKEN not in str(caught.value)


# -- the service and the API --------------------------------------------------------


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch, delivery) -> None:
        self.proposed = tmp_path / "proposed"
        self.store = InMemoryDispatchStore()
        self.gate = FileApprovalGate(self.proposed)
        monkeypatch.setattr(
            "src.server.services.dispatch._schedule", lambda key, factory: None
        )
        self.service = DispatchService(
            DispatchSettings(proposed_dir=self.proposed, runs_dir=tmp_path / "runs"),
            store=self.store,
            gate=self.gate,
            delivery=delivery,
        )


async def test_an_approved_drive_that_completes_delivers_once(tmp_path, monkeypatch) -> None:
    gate_holder: dict[str, object] = {}
    pusher, prs = FakePusher(), FakePullRequests()

    class LateDelivery:
        async def deliver(self, dispatch):
            return await _delivery(
                tmp_path, gate_holder["gate"], pusher=pusher, prs=prs
            ).deliver(dispatch)

    harness = Harness(tmp_path, monkeypatch, LateDelivery())
    gate_holder["gate"] = harness.gate
    started = await harness.service.start("ship the widget", USER_ID)
    parked = await harness.service._drive(started.id)
    assert parked.state is DispatchState.APPROVAL
    assert pusher.calls == []

    await harness.service.approve(parked.id)
    driven = await harness.service._drive(parked.id)

    assert driven.state is DispatchState.COMPLETE
    record = driven.gates[DELIVERY]
    assert record["state"] == OPENED and record["attempts"] == 1
    assert "opened https://github.com/acme/widgets/pull/7" in driven.message
    assert len(pusher.calls) == 1
    # A second drive of the finished row does not push again.
    await harness.service._drive(parked.id)
    assert len(pusher.calls) == 1
    _no_token_anywhere((await harness.store.get(parked.id)).to_dict())


async def test_deliver_retries_after_a_missing_token_and_keeps_an_opened_pr(
    tmp_path, monkeypatch
) -> None:
    state = {"token": None}
    pusher, prs = FakePusher(), FakePullRequests()
    holder: dict[str, object] = {}

    class Delivery:
        async def deliver(self, dispatch):
            return await _delivery(
                tmp_path, holder["gate"], token=state["token"], pusher=pusher, prs=prs
            ).deliver(dispatch)

    harness = Harness(tmp_path, monkeypatch, Delivery())
    holder["gate"] = harness.gate
    row = await _approved(tmp_path, harness.gate)
    await harness.store.create(row)

    first = await harness.service.deliver(row.id)
    assert first.state is DispatchState.COMPLETE
    assert first.gates[DELIVERY]["state"] == NEEDS_TOKEN
    assert "conductor github auth" in first.message

    state["token"] = TOKEN
    second = await harness.service.deliver(row.id)
    assert second.gates[DELIVERY]["state"] == OPENED
    assert second.gates[DELIVERY]["attempts"] == 2
    third = await harness.service.deliver(row.id)
    assert third.gates[DELIVERY]["attempts"] == 2
    assert len(pusher.calls) == 1


async def test_deliver_refuses_a_dispatch_that_is_not_complete(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch, AsyncMock())
    row = await _approved(tmp_path, harness.gate, state=DispatchState.APPROVAL)
    await harness.store.create(row)
    with pytest.raises(ValueError, match="only a COMPLETE"):
        await harness.service.deliver(row.id)


async def test_dev_adapters_have_no_delivery(tmp_path, monkeypatch) -> None:
    harness = Harness(tmp_path, monkeypatch, None)
    row = await _approved(tmp_path, harness.gate)
    await harness.store.create(row)
    with pytest.raises(ValueError, match="^delivery_disabled"):
        await harness.service.deliver(row.id)


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    from src.server.app.dispatch import router
    from tests.conftest import create_test_app

    holder: dict[str, object] = {}

    class Delivery:
        async def deliver(self, dispatch):
            return await _delivery(tmp_path, holder["gate"]).deliver(dispatch)

    harness = Harness(tmp_path, monkeypatch, Delivery())
    holder["gate"] = harness.gate
    app = create_test_app(router)
    with patch("src.server.app.dispatch._service", return_value=harness.service):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http, harness


async def test_the_deliver_route_returns_the_record_and_never_the_token(api, tmp_path) -> None:
    http, harness = api
    row = await _approved(tmp_path, harness.gate)
    await harness.store.create(row)

    response = await http.post(f"/api/v1/dispatch/{row.id}/deliver")

    assert response.status_code == 200
    body = response.json()
    assert body["gates"]["delivery"]["state"] == OPENED
    assert TOKEN not in response.text and _basic() not in response.text

    pending = Dispatch(id="d-open", goal="g", user_id=USER_ID, created_at="now")
    pending.state = DispatchState.MONITORING
    await harness.store.create(pending)
    refused = await http.post("/api/v1/dispatch/d-open/deliver")
    assert refused.status_code == 409
