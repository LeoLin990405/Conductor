"""Pure parsing rules of the GitHub Source (no network, no database)."""

from __future__ import annotations

import logging
import re
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.credentials import CredentialAllowlist
from src.server.services.autonomy.github import (
    GitHubConfig,
    GitHubSourceError,
    _addressed,
    _draft_from_comment,
    _issue_number,
    stable_trigger_id,
)


def test_config_requires_owner_name_repository_and_workspace():
    cfg = GitHubConfig.from_mapping(
        {
            "repository": "acme/widgets",
            "secret_name": "GITHUB_TOKEN",
            "workspace_id": "w1",
            "allowed_logins": ["alice", "alice", " bob "],
            "bot_logins": ["dependabot[bot]", " dependabot[bot] "],
            "poll_seconds": 60,
        }
    )
    assert cfg.repository == "acme/widgets"
    assert cfg.allowed_logins == ("alice", "bob")
    assert cfg.bot_logins == ("dependabot[bot]",)
    with pytest.raises(GitHubSourceError):
        GitHubConfig.from_mapping({"repository": "not-a-repo", "secret_name": "x", "workspace_id": "w1"})


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.github.com",
        "https://user:pass@github.example/api/v3",
        "https://github.example/api/v3?token=x",
        "https://github.example/api/../v3",
        "https://github.example/api%2fv3",
    ],
)
def test_config_rejects_unsafe_api_base_urls(base_url):
    with pytest.raises(GitHubSourceError):
        GitHubConfig.from_mapping(
            {
                "repository": "acme/widgets",
                "secret_name": "gh",
                "workspace_id": "w1",
                "api_base_url": base_url,
            }
        )


@pytest.mark.parametrize(
    "base_url",
    ["https://github.example/api/v3", "http://127.0.0.1:8080"],
)
def test_config_accepts_https_enterprise_and_loopback_test_urls(base_url):
    cfg = GitHubConfig.from_mapping(
        {
            "repository": "acme/widgets",
            "secret_name": "gh",
            "workspace_id": "w1",
            "api_base_url": base_url,
        }
    )
    assert cfg.api_base_url == base_url


def test_trigger_id_is_stable_for_the_same_source_event():
    a = stable_trigger_id("source-1", "issue_comment:1:2026-01-01T00:00:00Z")
    b = stable_trigger_id("source-1", "issue_comment:1:2026-01-01T00:00:00Z")
    c = stable_trigger_id("source-2", "issue_comment:1:2026-01-01T00:00:00Z")
    assert a == b
    assert a != c


def test_comment_parser_drops_self_and_bot_and_marks_mentions():
    item = {
        "id": 7,
        "user": {"login": "alice", "type": "User"},
        "body": "please fix this @conductor-bot",
        "html_url": "https://github.com/acme/widgets/issues/12#issuecomment-7",
        "issue_url": "https://api.github.com/repos/acme/widgets/issues/12",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    draft = _draft_from_comment(
        source_id="s1",
        repository="acme/widgets",
        agent_login="conductor-bot",
        item=item,
        kind="issue_comment",
        thread_number=12,
        url=item["html_url"],
    )
    assert draft and draft["thread_key"] == "github:acme/widgets#12"
    assert draft["actor_login"] == "alice"
    assert draft["addressed"] is True
    assert draft["trigger_id"].startswith("trg_")

    self_item = {**item, "user": {"login": "conductor-bot", "type": "User"}}
    assert _draft_from_comment(
        source_id="s1", repository="acme/widgets", agent_login="conductor-bot",
        item=self_item, kind="issue_comment", thread_number=12, url="",
    ) is None
    bot_item = {**item, "user": {"login": "dependabot[bot]", "type": "Bot"}}
    assert _draft_from_comment(
        source_id="s1", repository="acme/widgets", agent_login="conductor-bot",
        item=bot_item, kind="issue_comment", thread_number=12, url="",
    ) is None


def test_issue_number_and_mention_helpers():
    assert _issue_number("https://api.github.com/repos/a/b/issues/12") == 12
    assert _issue_number("https://api.github.com/repos/a/b/pulls/4") == 4
    assert _issue_number("https://api.github.com/repos/a/b") is None
    assert _addressed("Hey @Conductor-Bot please", "conductor-bot")
    assert not _addressed("no mention", "conductor-bot")


def test_comment_secret_is_removed_before_trigger_storage():
    token = "ghp_" + "a" * 36
    item = {
        "id": 8,
        "user": {"login": "alice", "type": "User"},
        "body": f"Please check this {token}",
        "issue_url": "https://api.github.com/repos/acme/widgets/issues/12",
    }
    draft = _draft_from_comment(
        source_id="s1",
        repository="acme/widgets",
        agent_login="conductor-bot",
        item=item,
        kind="issue_comment",
        thread_number=12,
        url="",
        secrets=(token,),
    )
    assert draft
    assert token not in draft["text"]
    assert "[REDACTED]" in draft["text"]


@pytest.mark.asyncio
async def test_credential_broker_redacts_exact_secret_from_provider_payload():
    from src.server.services.autonomy.github import GitHubCredentialBroker

    token = "private-token-value"

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return [{"body": f"comment echoed {token}"}]

    class Session:
        def request(self, *args, **kwargs):
            assert kwargs["headers"]["Authorization"] == f"Bearer {token}"
            assert kwargs["allow_redirects"] is False
            return Response()

    with patch(
        "src.server.services.autonomy.github.reveal_user_secret",
        AsyncMock(return_value=token),
    ):
        result = await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets"
        ).request_json(Session(), "GET", "/repos/acme/widgets/issues/comments")
    assert token not in result[0]["body"]
    assert "[REDACTED]" in result[0]["body"]


@pytest.mark.asyncio
async def test_credential_broker_uses_vault_secret_not_environment_token(monkeypatch):
    from src.server.services.autonomy.github import GitHubCredentialBroker

    vault_token = "vault-token-value"
    monkeypatch.setenv("GH_TOKEN", "environment-token-must-be-ignored")
    monkeypatch.setenv("GITHUB_TOKEN", "legacy-environment-token-must-be-ignored")

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {"login": "conductor"}

    class Session:
        def request(self, *args, **kwargs):
            assert kwargs["headers"]["Authorization"] == f"Bearer {vault_token}"
            return Response()

    reveal = AsyncMock(return_value=vault_token)
    with patch("src.server.services.autonomy.github.reveal_user_secret", reveal):
        result = await GitHubCredentialBroker(
            "u1", "GITHUB_TOKEN", "https://api.github.com", "acme/widgets"
        ).request_json(Session(), "GET", "/user")

    assert result == {"login": "conductor"}
    reveal.assert_awaited_once_with("u1", "GITHUB_TOKEN")


@pytest.mark.asyncio
async def test_github_error_redacts_token_before_truncation():
    from src.server.services.autonomy.github import _json

    token = "credential-value-abcdefghijklmnopqrstuvwxyz"

    class Response:
        status = 500

        async def text(self):
            return "x" * 290 + token + "y" * 30

    with pytest.raises(GitHubSourceError) as exc:
        await _json(Response(), secrets=(token,))
    assert token not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


@pytest.mark.asyncio
async def test_broker_rejects_paths_outside_repository_before_revealing_secret(caplog):
    from src.server.services.autonomy.github import GitHubCredentialBroker

    reveal = AsyncMock(return_value="secret")
    with (
        patch("src.server.services.autonomy.github.reveal_user_secret", reveal),
        caplog.at_level(logging.WARNING),
        pytest.raises(GitHubSourceError, match="outside the configured repository"),
    ):
        await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets"
        ).request_json(object(), "GET", "/repos/attacker/project/issues/comments")
    reveal.assert_not_awaited()
    assert "event=credential_denied" in caplog.text
    assert "credential=gh" in caplog.text


@pytest.mark.asyncio
async def test_broker_rejects_redirects(caplog):
    from src.server.services.autonomy.github import GitHubCredentialBroker

    token = "private-token-value"

    class Response:
        status = 302

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Session:
        def request(self, *args, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response()

    with patch(
        "src.server.services.autonomy.github.reveal_user_secret",
        AsyncMock(return_value=token),
    ), caplog.at_level(logging.WARNING), pytest.raises(
        GitHubSourceError, match="redirect refused"
    ):
        await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets"
        ).request_json(Session(), "GET", "/repos/acme/widgets")
    assert token not in caplog.text
    assert "event=credential_denied" in caplog.text


def _github_config(**overrides):
    values = {
        "repository": "acme/widgets",
        "secret_name": "gh",
        "workspace_id": "w1",
        "allowed_logins": ["alice"],
    }
    values.update(overrides)
    return GitHubConfig.from_mapping(values)


def test_config_derives_a_default_credential_allowlist():
    cfg = _github_config(api_base_url="https://github.example/api/v3")
    assert cfg.credential_allowlist is not None
    assert cfg.credential_allowlist.hosts == frozenset({"github.example"})
    assert cfg.credential_allowlist.methods == frozenset({"GET", "POST"})
    assert cfg.credential_allowlist.path_prefixes == (
        "/repos/acme/widgets",
        "/user",
    )


def test_config_uses_an_explicit_credential_allowlist():
    cfg = _github_config(
        credential_allowlist={
            "hosts": ["github.example"],
            "methods": ["GET"],
            "path_prefixes": ["/repos/acme/widgets"],
        }
    )
    assert cfg.credential_allowlist is not None
    assert cfg.credential_allowlist.hosts == frozenset({"github.example"})
    assert cfg.credential_allowlist.methods == frozenset({"GET"})
    assert cfg.credential_allowlist.path_prefixes == ("/repos/acme/widgets",)


def test_config_narrows_only_the_dimensions_it_names():
    cfg = _github_config(
        api_base_url="https://github.example/api/v3",
        credential_allowlist={"methods": ["GET"]},
    )
    assert cfg.credential_allowlist is not None
    # The omitted dimensions keep the least-privilege default.
    assert cfg.credential_allowlist.hosts == frozenset({"github.example"})
    assert cfg.credential_allowlist.methods == frozenset({"GET"})
    assert cfg.credential_allowlist.path_prefixes == (
        "/repos/acme/widgets",
        "/user",
    )
    assert cfg.credential_allowlist.check(
        "POST", "https://github.example/repos/acme/widgets"
    ).allowed is False


def test_config_treats_an_explicit_empty_list_as_a_denial():
    cfg = _github_config(credential_allowlist={"hosts": []})
    assert cfg.credential_allowlist is not None
    assert cfg.credential_allowlist.hosts == frozenset()
    assert cfg.credential_allowlist.methods == frozenset({"GET", "POST"})
    assert cfg.credential_allowlist.path_prefixes == (
        "/repos/acme/widgets",
        "/user",
    )
    assert cfg.credential_allowlist.check(
        "GET", "https://api.github.com/user"
    ).allowed is False


def test_config_rejects_a_malformed_credential_allowlist():
    with pytest.raises(GitHubSourceError, match="credential_allowlist"):
        _github_config(
            credential_allowlist={
                "hosts": ["api.github.com"],
                "methods": ["GET"],
                "path_prefixes": ["repos"],
            }
        )
    with pytest.raises(GitHubSourceError, match="credential_allowlist"):
        _github_config(credential_allowlist={"hosts": ["api.github.com"], "extra": []})
    with pytest.raises(GitHubSourceError, match="credential_allowlist"):
        _github_config(credential_allowlist={"hosts": [{}]})
    with pytest.raises(GitHubSourceError, match="credential_allowlist"):
        _github_config(credential_allowlist={"methods": [[]]})


def test_config_rejects_a_repository_that_cannot_form_a_safe_scope():
    with pytest.raises(GitHubSourceError, match="credential_allowlist"):
        _github_config(repository="../widgets")


@pytest.mark.asyncio
async def test_broker_denies_a_host_outside_the_allowlist_before_revealing_secret(caplog):
    from src.server.services.autonomy.github import GitHubCredentialBroker

    reveal = AsyncMock(return_value="secret")
    allowlist = CredentialAllowlist(
        hosts={"other.example"}, methods={"GET"}, path_prefixes={"/user"}
    )
    with (
        patch("src.server.services.autonomy.github.reveal_user_secret", reveal),
        caplog.at_level(logging.WARNING),
        pytest.raises(GitHubSourceError, match="host is not in the credential allowlist"),
    ):
        await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets", allowlist
        ).request_json(object(), "GET", "/user")
    reveal.assert_not_awaited()
    assert "event=credential_denied" in caplog.text
    assert "host=api.github.com" in caplog.text


@pytest.mark.asyncio
async def test_broker_denies_a_method_outside_the_allowlist_before_revealing_secret():
    from src.server.services.autonomy.github import GitHubCredentialBroker

    reveal = AsyncMock(return_value="secret")
    allowlist = CredentialAllowlist(
        hosts={"api.github.com"},
        methods={"GET"},
        path_prefixes={"/repos/acme/widgets"},
    )
    with (
        patch("src.server.services.autonomy.github.reveal_user_secret", reveal),
        pytest.raises(GitHubSourceError, match="method is not in the credential allowlist"),
    ):
        await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets", allowlist
        ).request_json(
            object(), "POST", "/repos/acme/widgets/issues/1/comments"
        )
    reveal.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_denies_a_path_outside_the_allowlist_before_revealing_secret():
    from src.server.services.autonomy.github import GitHubCredentialBroker

    reveal = AsyncMock(return_value="secret")
    allowlist = CredentialAllowlist(
        hosts={"api.github.com"},
        methods={"GET"},
        path_prefixes={"/repos/acme/widgets"},
    )
    with (
        patch("src.server.services.autonomy.github.reveal_user_secret", reveal),
        pytest.raises(GitHubSourceError, match="path is outside the credential allowlist"),
    ):
        await GitHubCredentialBroker(
            "u1", "gh", "https://api.github.com", "acme/widgets", allowlist
        ).request_json(object(), "GET", "/user")
    reveal.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_treats_enterprise_base_path_as_api_relative():
    from src.server.services.autonomy.github import GitHubCredentialBroker

    seen = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {"login": "conductor"}

    class Session:
        def request(self, method, url, **kwargs):
            seen["url"] = url
            return Response()

    cfg = _github_config(api_base_url="https://github.example/api/v3")
    with patch(
        "src.server.services.autonomy.github.reveal_user_secret",
        AsyncMock(return_value="secret"),
    ):
        result = await GitHubCredentialBroker(
            "u1",
            cfg.secret_name,
            cfg.api_base_url,
            cfg.repository,
            cfg.credential_allowlist,
        ).request_json(Session(), "GET", "/user")
    assert result == {"login": "conductor"}
    assert seen["url"] == "https://github.example/api/v3/user"


# --- trigger-phrase mode: the Source runs on the operator's own token ------

from src.server.models.autonomy import GitHubSourceConfig  # noqa: E402
from src.server.services.autonomy.github import REPLY_MARKER_PREFIX  # noqa: E402
from src.server.services.autonomy.replies import reply_marker  # noqa: E402

_BASE_CONFIG = {
    "repository": "acme/widgets",
    "secret_name": "GITHUB_TOKEN",
    "workspace_id": "ws",
    "allowed_logins": ["leo"],
}


def _own_comment(body: str) -> dict:
    return {
        "id": 9,
        "user": {"login": "leo", "type": "User"},
        "body": body,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _draft(item: dict, *, trigger_phrase: str = "") -> dict | None:
    return _draft_from_comment(
        source_id="s1",
        repository="acme/widgets",
        agent_login="leo",
        item=item,
        kind="issue_comment",
        thread_number=12,
        url="",
        trigger_phrase=trigger_phrase,
    )


def test_default_mode_still_drops_the_token_owners_own_comments():
    assert _draft(_own_comment("/conductor please fix")) is None


def test_trigger_phrase_mode_reads_the_owners_own_request():
    draft = _draft(_own_comment("/conductor please fix the flaky test"), trigger_phrase="/conductor")
    assert draft is not None
    assert draft["actor_login"] == "leo"
    assert draft["addressed"] is True


def test_trigger_phrase_mode_keeps_an_unaddressed_own_comment_unaddressed():
    draft = _draft(_own_comment("just a note to myself"), trigger_phrase="/conductor")
    assert draft is not None
    assert draft["addressed"] is False


def test_trigger_phrase_mode_drops_a_reply_this_system_posted():
    marker = reply_marker("outbox-key-1")
    body = f"### Summary\nDone. /conductor\n\n{marker}"
    assert marker.startswith(REPLY_MARKER_PREFIX)
    assert re.fullmatch(
        re.escape(REPLY_MARKER_PREFIX) + r"[0-9a-f]{32} -->", marker
    )
    assert _draft(_own_comment(body), trigger_phrase="/conductor") is None


def test_trigger_phrase_mode_keeps_a_quoted_prefix_with_non_hex_text():
    body = (
        f"> {REPLY_MARKER_PREFIX}not-a-hex-digest -->\n"
        "/conductor please look at that quote"
    )
    draft = _draft(_own_comment(body), trigger_phrase="/conductor")
    assert draft is not None
    assert draft["actor_login"] == "leo"
    assert draft["addressed"] is True


def test_trigger_phrase_mode_still_drops_bots():
    item = {**_own_comment("/conductor"), "user": {"login": "ci[bot]", "type": "Bot"}}
    assert _draft(item, trigger_phrase="/conductor") is None


@pytest.mark.parametrize(
    "body, addressed",
    [
        ("/conductor fix it", True),
        ("please /CONDUCTOR fix it", True),
        ("(/conductor) fix", True),
        ("/conductors fix it", False),
        ("x/conductor fix", False),
        ("@conductor fix", False),
    ],
)
def test_trigger_phrase_matches_only_the_whole_token(body, addressed):
    draft = _draft(_own_comment(body), trigger_phrase="/conductor")
    assert draft is not None
    assert draft["addressed"] is addressed


def test_config_reads_a_trigger_phrase():
    cfg = GitHubConfig.from_mapping({**_BASE_CONFIG, "trigger_phrase": " /conductor "})
    assert cfg.trigger_phrase == "/conductor"
    assert GitHubConfig.from_mapping(_BASE_CONFIG).trigger_phrase == ""


@pytest.mark.parametrize("phrase", ["@conductor", "x", "has space", "a" * 41, "/con<ductor>"])
def test_config_rejects_an_unsafe_trigger_phrase(phrase):
    with pytest.raises(GitHubSourceError, match="trigger_phrase"):
        GitHubConfig.from_mapping({**_BASE_CONFIG, "trigger_phrase": phrase})


def test_source_model_carries_trigger_phrase_and_require_approval():
    cfg = GitHubSourceConfig.model_validate(
        {**_BASE_CONFIG, "trigger_phrase": "/conductor", "require_approval": True}
    )
    assert cfg.trigger_phrase == "/conductor"
    assert cfg.require_approval is True
    assert GitHubSourceConfig.model_validate(_BASE_CONFIG).require_approval is False


def test_a_github_source_can_hold_every_reply():
    from src.server.services.autonomy.channels import (
        ACTION_DELIVER,
        ACTION_HOLD,
        CHANNEL_GITHUB,
        CHANNEL_SPECS,
        decide_delivery,
    )

    spec = CHANNEL_SPECS[CHANNEL_GITHUB]
    assert decide_delivery(spec, config={}).action == ACTION_DELIVER
    assert decide_delivery(spec, config={"require_approval": True}).action == ACTION_HOLD


@pytest.mark.asyncio
async def test_poll_with_trigger_phrase_addresses_comments_and_drops_system_replies():
    from src.server.services.autonomy.github import poll_github_comments

    marker = reply_marker("outbox-key-poll")
    assert marker.startswith(REPLY_MARKER_PREFIX)
    issue_comment = {
        **_own_comment("/conductor fix the issue poll"),
        "id": 101,
        "issue_url": "https://api.github.com/repos/acme/widgets/issues/12",
        "html_url": "https://github.com/acme/widgets/issues/12#issuecomment-101",
    }
    system_reply = {
        **_own_comment(f"Done. /conductor\n\n{marker}"),
        "id": 102,
        "issue_url": "https://api.github.com/repos/acme/widgets/issues/12",
        "html_url": "https://github.com/acme/widgets/issues/12#issuecomment-102",
    }
    review_comment = {
        **_own_comment("/conductor fix the review poll"),
        "id": 201,
        "pull_request_url": "https://api.github.com/repos/acme/widgets/pulls/34",
        "html_url": "https://github.com/acme/widgets/pull/34#discussion_r201",
    }

    class FakeBroker:
        def __init__(self):
            self.calls = []

        async def request_json(self, session, method, path, **kwargs):
            self.calls.append((method, path))
            if path == "/repos/acme/widgets/issues/comments":
                return [issue_comment, system_reply]
            if path == "/repos/acme/widgets/pulls/comments":
                return [review_comment]
            if path in {
                "/repos/acme/widgets/issues/12",
                "/repos/acme/widgets/issues/34",
            }:
                return {"title": "Latch", "body": "sticks"}
            raise AssertionError(f"unexpected GitHub request: {method} {path}")

    broker = FakeBroker()
    with patch("src.server.services.autonomy.github._credential_broker", return_value=broker):
        drafts, _ = await poll_github_comments(
            source_id="s1",
            user_id="u1",
            config={**_BASE_CONFIG, "trigger_phrase": "/conductor"},
            state={"agent_login": "leo"},
        )

    assert [
        (draft["kind"], draft["addressed"], draft["payload"]["comment_id"])
        for draft in drafts
    ] == [("issue_comment", True, 101), ("review_comment", True, 201)]
    assert broker.calls == [
        ("GET", "/repos/acme/widgets/issues/comments"),
        ("GET", "/repos/acme/widgets/pulls/comments"),
        ("GET", "/repos/acme/widgets/issues/12"),
        ("GET", "/repos/acme/widgets/issues/34"),
    ]
