"""A GitHub Source config is validated by its own wire model before it is stored.

The GitHub branch of ``_validate_config`` used to hand the caller's dict
straight to ``GitHubConfig.from_mapping`` and store it verbatim, so a config
the model would have refused — a missing field, a wrong type, a key nobody
reads — reached the database and only failed at the next poll. It now does
what the Email and Slack branches do: validate with ``GitHubSourceConfig``,
build the service config from the validated dump, and store that dump, which
is also what keeps ``trigger_phrase`` and ``require_approval`` on a Source
that was edited through a UI that does not send them: the route merges an
omitted ``require_approval`` out of the stored config, and the dump itself
leaves a flag nobody sent to the delivery channel's own default.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from src.server.app.autonomy import _validate_config
from src.server.models.autonomy import GitHubSourceConfig
from src.server.services.autonomy.channels import (
    ACTION_DELIVER,
    ACTION_HOLD,
    CHANNEL_EMAIL,
    decide_delivery,
    resolve_channel,
)
from src.server.services.autonomy.github import GitHubConfig
from src.server.services.autonomy.policy import effective_admission

from tests.conftest import create_test_app

USER_ID = "test-user-123"

BASE_CONFIG = {
    "repository": "acme/widgets",
    "secret_name": "GITHUB_TOKEN",
    "workspace_id": "w1",
    "allowed_logins": ["alice"],
}


def _vault(monkeypatch, *, present: str = "token") -> None:
    monkeypatch.setattr(
        "src.server.app.autonomy.reveal_user_secret",
        AsyncMock(return_value=present),
    )
    monkeypatch.setattr(
        "src.server.app.autonomy._workspace",
        AsyncMock(return_value={"workspace_id": "w1"}),
    )


# --- the validated dump is what gets stored --------------------------------


@pytest.mark.asyncio
async def test_a_valid_github_config_is_stored_as_the_validated_dump(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(USER_ID, dict(BASE_CONFIG), "github")

    expected = GitHubSourceConfig.model_validate(BASE_CONFIG).model_dump(
        exclude_none=True
    )
    # A flag nobody sent is not the model's own default: the delivery channel
    # decides it, the way a Source stored before this model existed has none.
    expected.pop("require_approval")
    assert stored == expected
    # An unset optional is not stored as ``None``: a later edit reads the
    # absence, not a null.
    assert "worker_daemon_id" not in stored
    assert "credential_allowlist" not in stored


@pytest.mark.asyncio
async def test_the_stored_dump_keeps_trigger_phrase_and_require_approval(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "trigger_phrase": "/conductor", "require_approval": True},
        "github",
    )

    assert stored["trigger_phrase"] == "/conductor"
    assert stored["require_approval"] is True


@pytest.mark.asyncio
async def test_an_edit_that_omits_the_two_flags_keeps_the_model_defaults(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(USER_ID, dict(BASE_CONFIG), "github")

    assert stored["trigger_phrase"] == ""
    # GitHub posts on its own, which is exactly what an absent flag means, and
    # a Source that asks for approval is edited with the flag in the request
    # (the route carries the stored one over when the edit omits it).
    assert "require_approval" not in stored
    assert (
        decide_delivery(resolve_channel("github", stored), config=stored).action
        == ACTION_DELIVER
    )


# --- what the caller set survives the dump ---------------------------------


@pytest.mark.asyncio
async def test_an_admission_override_survives_the_dump(monkeypatch):
    """A cell the caller set is the one the intake admits by.

    The stored dump is what an eval and the intake read, so a cell the
    validated dump dropped would turn a trigger this Source ignores back into
    an explicit one.
    """
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "admission": {"listed_addressed": "ignore"}},
        "github",
    )

    assert stored["admission"] == {"listed_addressed": "ignore"}
    assert effective_admission({}, stored)["listed_addressed"] == "ignore"
    # The cell nobody set keeps the policy's own default.
    assert effective_admission({}, BASE_CONFIG)["listed_addressed"] == "explicit"


@pytest.mark.asyncio
async def test_a_channel_override_keeps_its_own_approval_default(monkeypatch):
    """A GitHub Source may narrow its channel to email, which holds.

    ``decide_delivery`` reads ``require_approval`` off the Source config when
    the key is there, so a dump that inserts the GitHub channel's own ``False``
    would post on its own a reply the channel it names drafts for a person.
    """
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID, {**BASE_CONFIG, "channel": "email"}, "github"
    )

    assert stored["channel"] == "email"
    assert "require_approval" not in stored
    spec = resolve_channel("github", stored)
    assert spec.channel == CHANNEL_EMAIL
    assert decide_delivery(spec, config=stored).action == ACTION_HOLD


@pytest.mark.asyncio
async def test_an_explicit_approval_flag_still_wins_over_the_channel_default(
    monkeypatch,
):
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "channel": "email", "require_approval": False},
        "github",
    )

    assert stored["require_approval"] is False
    spec = resolve_channel("github", stored)
    assert spec.channel == CHANNEL_EMAIL
    assert decide_delivery(spec, config=stored).action == ACTION_DELIVER


@pytest.mark.asyncio
async def test_the_vault_is_read_by_the_normalized_secret_name(monkeypatch):
    """``GitHubConfig.from_mapping`` strips the name, and that is the one the
    poll reads the token by, so the vault is asked for it stripped too: a name
    typed with stray spaces is still the token the vault holds."""
    _vault(monkeypatch)
    asked: list[str] = []

    async def reveal(user_id: str, name: str) -> str:
        asked.append(name)
        return "token"

    monkeypatch.setattr("src.server.app.autonomy.reveal_user_secret", reveal)
    await _validate_config(
        USER_ID, {**BASE_CONFIG, "secret_name": " GITHUB_TOKEN "}, "github"
    )

    assert asked == ["GITHUB_TOKEN"]


@pytest.mark.asyncio
async def test_a_kind_whose_model_is_checked_later_still_answers_400(monkeypatch):
    """The 422 is GitHub's own answer; the shared refusal keeps its 400."""
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(
            USER_ID,
            {"chat_id": "oc_1", "workspace_id": "w1", "poll_seconds": 5},
            "feishu",
        )

    assert refusal.value.status_code == 400
    assert refusal.value.detail.startswith("poll_seconds")


@pytest.mark.asyncio
async def test_a_narrowed_credential_allowlist_survives_the_dump(monkeypatch):
    """A dimension left out of the allowlist keeps the derived default."""
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "credential_allowlist": {"methods": ["GET"]}},
        "github",
    )

    assert stored["credential_allowlist"] == {"methods": ["GET"]}
    allowlist = GitHubConfig.from_mapping(
        {**stored, "api_base_url": "https://github.example"}
    ).credential_allowlist
    assert allowlist is not None
    assert allowlist.hosts == frozenset({"github.example"})
    assert allowlist.methods == frozenset({"GET"})
    assert allowlist.path_prefixes == ("/repos/acme/widgets", "/user")


@pytest.mark.asyncio
async def test_an_empty_allowlist_dimension_denies_that_dimension(monkeypatch):
    """An explicit empty list is not the same as a dimension left out."""
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "credential_allowlist": {"hosts": []}},
        "github",
    )

    allowlist = GitHubConfig.from_mapping(
        {**stored, "api_base_url": "https://github.example"}
    ).credential_allowlist
    assert allowlist is not None
    assert allowlist.hosts == frozenset()
    assert allowlist.methods == frozenset({"GET", "POST"})


@pytest.mark.asyncio
async def test_an_explicit_null_allowlist_dimension_denies_that_dimension(monkeypatch):
    """A null the caller typed is a denial, not an absent dimension.

    ``exclude_none`` drops every ``None`` in the dump, so without the
    distinction ``CredentialAllowlistConfig`` keeps, a null would read as "the
    caller left it out" and silently restore the default derived from the
    endpoint and the repository — a credential allowed everywhere the default
    allows, when the caller asked for nowhere.
    """
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "credential_allowlist": {"hosts": None}},
        "github",
    )

    assert stored["credential_allowlist"] == {"hosts": None}
    allowlist = GitHubConfig.from_mapping(
        {**stored, "api_base_url": "https://github.example"}
    ).credential_allowlist
    assert allowlist is not None
    assert allowlist.hosts == frozenset()
    # The dimensions the caller left out keep their derived default.
    assert allowlist.methods == frozenset({"GET", "POST"})
    assert allowlist.path_prefixes == ("/repos/acme/widgets", "/user")


@pytest.mark.asyncio
async def test_an_explicit_null_in_any_allowlist_dimension_is_kept(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "credential_allowlist": {"methods": None, "path_prefixes": None}},
        "github",
    )

    assert stored["credential_allowlist"] == {
        "methods": None,
        "path_prefixes": None,
    }
    allowlist = GitHubConfig.from_mapping(
        {**stored, "api_base_url": "https://github.example"}
    ).credential_allowlist
    assert allowlist is not None
    assert allowlist.methods == frozenset()
    assert allowlist.path_prefixes == ()
    # hosts was left out, so it keeps the derived default.
    assert allowlist.hosts == frozenset({"github.example"})


@pytest.mark.asyncio
async def test_an_allowlist_dimension_nobody_sent_stays_out_of_the_dump(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "credential_allowlist": {"methods": ["GET"]}},
        "github",
    )

    allowlist = stored["credential_allowlist"]
    assert allowlist == {"methods": ["GET"]}
    assert "hosts" not in allowlist
    assert "path_prefixes" not in allowlist
    # An absent dimension is what keeps the default: a stored null would deny
    # it, and a caller reading this dump back must be able to tell them apart.
    assert GitHubConfig.from_mapping(
        {**stored, "api_base_url": "https://github.example"}
    ).credential_allowlist.hosts == frozenset({"github.example"})


@pytest.mark.asyncio
async def test_an_explicit_null_in_a_key_the_model_does_not_name_is_kept(monkeypatch):
    """``schemes`` is the allowlist's own key, and a null there denies it too."""
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "credential_allowlist": {"schemes": None}},
        "github",
    )

    assert stored["credential_allowlist"] == {"schemes": None}
    allowlist = GitHubConfig.from_mapping(
        {**stored, "api_base_url": "https://github.example"}
    ).credential_allowlist
    assert allowlist is not None
    assert allowlist.schemes == frozenset()


@pytest.mark.asyncio
async def test_a_non_default_routing_mode_round_trips(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID, {**BASE_CONFIG, "routing_mode": "ignore"}, "github"
    )

    assert stored["routing_mode"] == "ignore"


# --- a config the wire model refuses ---------------------------------------


@pytest.mark.asyncio
async def test_a_missing_workspace_is_the_models_own_refusal(monkeypatch):
    """The field is named, whatever the shared prechecks would have answered.

    The workspace lookup is a 404 and it runs before the GitHub branch used
    to; the model is now measured first, so a config that never had a chance
    is refused as the field it is missing.
    """
    _vault(monkeypatch)
    monkeypatch.setattr(
        "src.server.app.autonomy._workspace", AsyncMock(return_value=None)
    )
    config = {key: value for key, value in BASE_CONFIG.items() if key != "workspace_id"}
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, config, "github")

    assert refusal.value.status_code == 422
    assert refusal.value.detail.startswith("workspace_id")


@pytest.mark.asyncio
async def test_an_invalid_routing_mode_is_the_models_own_refusal(monkeypatch):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(
            USER_ID, {**BASE_CONFIG, "routing_mode": "read-everything"}, "github"
        )

    assert refusal.value.status_code == 422
    assert refusal.value.detail.startswith("routing_mode")


@pytest.mark.parametrize(
    "config, field",
    [
        ({**BASE_CONFIG, "poll_seconds": 5}, "poll_seconds"),
        ({**BASE_CONFIG, "trigger_phrase": "x" * 41}, "trigger_phrase"),
        ({k: v for k, v in BASE_CONFIG.items() if k != "repository"}, "repository"),
        ({**BASE_CONFIG, "worker_daemon_id": 5}, "worker_daemon_id"),
    ],
)
@pytest.mark.asyncio
async def test_a_config_the_model_refuses_names_its_field(monkeypatch, config, field):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, config, "github")

    # A body its own shape refuses is a 422, the way every route answers one.
    assert refusal.value.status_code == 422
    assert refusal.value.detail.startswith(field)
    # The refusal is the field and its rule, never the value that was sent.
    assert "Zq8mW3xLp0vTn5Rk" not in str(refusal.value.detail)


@pytest.mark.asyncio
async def test_the_route_answers_422_for_a_config_the_model_refuses(
    client, monkeypatch
):
    _vault(monkeypatch)
    monkeypatch.setattr(
        "src.server.app.autonomy.features.user_feature_enabled",
        AsyncMock(return_value=True),
    )
    response = await client.post(
        "/api/v1/autonomy/sources",
        json={"kind": "github", "config": {**BASE_CONFIG, "poll_seconds": 5}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("poll_seconds")


@pytest.mark.asyncio
async def test_an_unsupported_source_kind_is_still_refused(monkeypatch):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, dict(BASE_CONFIG), "carrier-pigeon")

    assert refusal.value.status_code == 400
    assert refusal.value.detail == "Unsupported Source kind"


# --- a config only the service refuses -------------------------------------


@pytest.mark.asyncio
async def test_a_repository_that_is_not_owner_slash_name_is_refused(monkeypatch):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, {**BASE_CONFIG, "repository": "widgets"}, "github")

    assert refusal.value.status_code == 400
    assert refusal.value.detail == "repository must be owner/name"


@pytest.mark.asyncio
async def test_a_trigger_phrase_the_service_refuses_is_still_refused(monkeypatch):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(
            USER_ID, {**BASE_CONFIG, "trigger_phrase": "@conductor"}, "github"
        )

    assert refusal.value.status_code == 400
    assert "trigger_phrase" in refusal.value.detail


@pytest.mark.asyncio
async def test_an_enterprise_base_url_the_service_refuses_is_refused(monkeypatch):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(
            USER_ID, {**BASE_CONFIG, "api_base_url": "http://github.example"}, "github"
        )

    assert refusal.value.status_code == 400
    assert "api_base_url" in refusal.value.detail


@pytest.mark.asyncio
async def test_a_credential_allowlist_key_nobody_takes_is_refused(monkeypatch):
    """The dump reaches ``CredentialAllowlist.from_mapping``, which owns the rule."""
    _vault(monkeypatch)
    secret = "Zq8mW3xLp0vTn5Rk"
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(
            USER_ID,
            {**BASE_CONFIG, "credential_allowlist": {secret: ["x"]}},
            "github",
        )

    assert refusal.value.status_code == 400
    assert "credential_allowlist" in refusal.value.detail
    assert secret not in str(refusal.value.detail)


# --- the checks that come after validation ---------------------------------


@pytest.mark.asyncio
async def test_an_empty_allowed_logins_is_refused(monkeypatch):
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, {**BASE_CONFIG, "allowed_logins": []}, "github")

    assert refusal.value.status_code == 400
    assert refusal.value.detail == "At least one allowed GitHub login is required"


@pytest.mark.asyncio
async def test_a_login_that_normalizes_to_nothing_leaves_nobody(monkeypatch):
    """The check is on the normalized list, which is what the poll matches.

    ``GitHubConfig.from_mapping`` strips each login and drops the empty ones,
    so a list of blanks is a list of nobody: every human sender is then refused
    by the intake, and the Source can never be addressed. The list the caller
    typed is non-empty, so checking that one accepts it.
    """
    _vault(monkeypatch)
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(
            USER_ID, {**BASE_CONFIG, "allowed_logins": [" ", "\t"]}, "github"
        )

    assert refusal.value.status_code == 400
    assert refusal.value.detail == "At least one allowed GitHub login is required"


@pytest.mark.asyncio
async def test_the_logins_that_survive_normalization_are_stored(monkeypatch):
    _vault(monkeypatch)
    stored = await _validate_config(
        USER_ID,
        {**BASE_CONFIG, "allowed_logins": [" alice ", "alice", "bob"]},
        "github",
    )

    assert GitHubConfig.from_mapping(stored).allowed_logins == ("alice", "bob")


@pytest.mark.asyncio
async def test_a_secret_that_is_not_in_the_vault_is_refused(monkeypatch):
    _vault(monkeypatch, present="")
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, dict(BASE_CONFIG), "github")

    assert refusal.value.status_code == 400
    assert refusal.value.detail == "vault secret not found"


@pytest.mark.asyncio
async def test_a_workspace_that_is_not_the_callers_is_refused(monkeypatch):
    _vault(monkeypatch)
    monkeypatch.setattr(
        "src.server.app.autonomy._workspace", AsyncMock(return_value=None)
    )
    with pytest.raises(HTTPException) as refusal:
        await _validate_config(USER_ID, dict(BASE_CONFIG), "github")

    assert refusal.value.status_code == 404
    assert refusal.value.detail == "Workspace not found"


# --- the route stores the dump ---------------------------------------------


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.asyncio
async def test_creating_a_github_source_stores_the_validated_dump(client, monkeypatch):
    _vault(monkeypatch)
    monkeypatch.setattr(
        "src.server.app.autonomy.features.user_feature_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "src.server.app.autonomy.check_github_source",
        AsyncMock(
            return_value={
                "agent_login": "conductor-bot",
                "repository": "acme/widgets",
                "can_push": True,
                "private": False,
            }
        ),
    )
    create = AsyncMock(
        return_value={
            "source_id": "11111111-1111-4111-8111-111111111111",
            "user_id": USER_ID,
            "kind": "github",
            "config": {},
            "state": {},
            "enabled": True,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    monkeypatch.setattr("src.server.app.autonomy.source_db.create_source", create)
    monkeypatch.setattr(
        "src.server.app.autonomy.source_db.update_source", AsyncMock(return_value=None)
    )
    response = await client.post(
        "/api/v1/autonomy/sources",
        json={
            "kind": "github",
            "config": {
                **BASE_CONFIG,
                "trigger_phrase": "/conductor",
                "require_approval": True,
            },
        },
    )

    assert response.status_code == 201
    stored = create.await_args.kwargs["config"]
    assert stored["trigger_phrase"] == "/conductor"
    assert stored["require_approval"] is True
    # The daemon binding is stamped on after validation, and is ``None`` here.
    assert stored == {
        **GitHubSourceConfig.model_validate(
            {**BASE_CONFIG, "trigger_phrase": "/conductor", "require_approval": True}
        ).model_dump(exclude_none=True),
        "worker_daemon_id": None,
    }
