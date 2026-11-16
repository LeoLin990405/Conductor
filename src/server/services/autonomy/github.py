"""The first autonomy Source: GitHub issue and review comments.

GitHub is polled rather than webhooked in V1. That keeps a local deployment
behind NAT usable and makes the cursor/idempotency contract explicit: comments
are read from a small overlap window and stored under stable ids, so a restart
or an overlapping poll cannot create a second Trigger.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn
from urllib.parse import urlsplit

import aiohttp

from src.server.database.user_vault_secrets import reveal_user_secret
from src.server.services.autonomy.credentials import (
    CredentialAllowlist,
    CredentialAllowlistError,
    CredentialBroker,
    audit_denial,
)
from src.server.services.autonomy.issue_context import build_issue_context
from src.server.services.autonomy.routing import source_mode
from src.server.services.autonomy.security import redact_external_data, redact_external_text

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "conductor-superagent",
}
_POLL_OVERLAP = timedelta(seconds=10)
_INITIAL_LOOKBACK = timedelta(hours=1)


class GitHubSourceError(RuntimeError):
    """A Source check or poll failed; the message is safe for the UI."""


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    repository: str
    secret_name: str
    workspace_id: str
    allowed_logins: tuple[str, ...]
    bot_logins: tuple[str, ...] = ()
    poll_seconds: int = 60
    api_base_url: str = GITHUB_API
    #: The destinations this Source's credential may be used for. ``None``
    #: derives the default from ``api_base_url`` and ``repository``.
    credential_allowlist: CredentialAllowlist | None = None
    #: A phrase such as ``/conductor`` that addresses the agent when the
    #: Source runs on the operator's own token. Empty keeps the default:
    #: ``@<token login>`` addresses it and the token's own comments are
    #: ignored. When set, the token's own comments are read, except the
    #: replies this system posted (they carry a full ``replies.reply_marker()``).
    trigger_phrase: str = ""

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "GitHubConfig":
        repository = str(config.get("repository") or "").strip()
        secret_name = str(config.get("secret_name") or "").strip()
        workspace_id = str(config.get("workspace_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise GitHubSourceError("repository must be owner/name")
        if not secret_name:
            raise GitHubSourceError("secret_name is required")
        if not workspace_id:
            raise GitHubSourceError("workspace_id is required")
        allowed = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (config.get("allowed_logins") or [])
                    if str(item).strip()
                }
            )
        )
        bots = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in (config.get("bot_logins") or [])
                    if str(item).strip()
                }
            )
        )
        poll_seconds = int(config.get("poll_seconds") or 60)
        if not 15 <= poll_seconds <= 3600:
            raise GitHubSourceError("poll_seconds must be between 15 and 3600")
        api_base_url = _validated_base_url(
            str(config.get("api_base_url") or GITHUB_API)
        )
        try:
            default_scope = github_credential_allowlist(api_base_url, repository)
            explicit_scope = config.get("credential_allowlist")
            credential_allowlist = (
                default_scope
                if explicit_scope is None
                else CredentialAllowlist.from_mapping(
                    explicit_scope, defaults=default_scope
                )
            )
        except CredentialAllowlistError as exc:
            raise GitHubSourceError(f"credential_allowlist: {exc}") from exc
        trigger_phrase = str(config.get("trigger_phrase") or "").strip()
        if trigger_phrase and not _TRIGGER_PHRASE.fullmatch(trigger_phrase):
            raise GitHubSourceError(
                "trigger_phrase must be 2-40 characters: letters, digits, '/', '-' or '_', "
                "not starting with '@'"
            )
        return cls(
            repository=repository,
            secret_name=secret_name,
            workspace_id=workspace_id,
            allowed_logins=allowed,
            bot_logins=bots,
            poll_seconds=poll_seconds,
            api_base_url=api_base_url,
            credential_allowlist=credential_allowlist,
            trigger_phrase=trigger_phrase,
        )


def stable_trigger_id(source_id: str, external_id: str) -> str:
    """Stable id for one source event; the same event under another poll gets
    the same id, which is what makes the queue idempotent."""
    digest = hashlib.sha256(f"{source_id}:{external_id}".encode("utf-8")).hexdigest()
    return f"trg_{digest[:32]}"


async def _json(resp: aiohttp.ClientResponse, *, secrets: tuple[str, ...] = ()) -> Any:
    if resp.status >= 400:
        text = redact_external_text(await resp.text(), secrets=secrets)[:300]
        raise GitHubSourceError(f"GitHub API {resp.status}: {text}")
    return await resp.json()


def _validated_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise GitHubSourceError("api_base_url is invalid") from exc
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
        or parsed.scheme not in ({"https", "http"} if loopback else {"https"})
        or "\\" in parsed.path
        or "%" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
    ):
        raise GitHubSourceError(
            "api_base_url must be HTTPS without credentials or query data; HTTP is allowed only on loopback"
        )
    _ = port  # Access above validates the port syntax and range.
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def github_credential_allowlist(base_url: str, repository: str) -> CredentialAllowlist:
    """The scope a GitHub Source gets when it does not configure one.

    It is the API host plus the operations the adapter actually uses. Path
    prefixes are API-relative, after any configured enterprise base path.
    ``/repos/<repository>`` covers ``GET /repos/<repository>/issues/<digits>``.
    """
    parsed = urlsplit(base_url)
    return CredentialAllowlist(
        hosts=frozenset({parsed.hostname or ""}),
        methods=frozenset({"GET", "POST"}),
        path_prefixes=("/user", f"/repos/{repository}"),
    )


@dataclass(frozen=True, slots=True)
class GitHubCredentialBroker:
    """Keep a Source's vault credential inside the HTTP request boundary."""

    user_id: str
    secret_name: str
    base_url: str
    repository: str
    allowlist: CredentialAllowlist | None = None
    #: Injection seam for the vault read; ``None`` resolves the module-level
    #: :func:`reveal_user_secret` at call time (what the tests patch).
    reveal: Callable[[str, str], Awaitable[str]] | None = None

    def _deny(self, reason: str, *, method: str, path: str) -> NoReturn:
        audit_denial(
            credential_name=self.secret_name,
            user_id=self.user_id,
            method=method,
            host=urlsplit(self.base_url).hostname or "",
            path=path,
            reason=reason,
        )
        raise GitHubSourceError(reason)

    def _vault_broker(self, allowlist: CredentialAllowlist) -> CredentialBroker:
        """The generic broker that owns the single vault read for this Source."""
        return CredentialBroker(
            user_id=self.user_id,
            credential_name=self.secret_name,
            allowlist=allowlist,
            reveal_secret=self.reveal or reveal_user_secret,
        )

    async def request_json(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        scrub: Callable[[Any, tuple[str, ...]], Any] | None = None,
    ) -> Any:
        """One allowed request. The response is redacted against the token.

        ``scrub`` is called with the redacted response and the Source's own
        secret, so a caller that reshapes the response (and clips it) can
        redact again before any cut. The secret never leaves this scope.
        """
        base_url = _validated_base_url(self.base_url)
        allowlist = self.allowlist or github_credential_allowlist(
            base_url, self.repository
        )
        parsed_base = urlsplit(base_url)
        path = str(path or "")
        if method not in {"GET", "POST"}:
            self._deny("GitHub operation is not allowed", method=method, path=path)
        try:
            parsed_path = urlsplit(path)
        except ValueError:
            self._deny("GitHub request path is invalid", method=method, path=path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or "?" in path
            or "#" in path
            or "\\" in path
            or "%" in path
            or any(segment in {".", ".."} for segment in parsed_path.path.split("/"))
        ):
            self._deny("GitHub request path is invalid", method=method, path=path)
        repo_path = f"/repos/{self.repository}"
        issue_comment = re.fullmatch(re.escape(repo_path) + r"/issues/\d+/comments", path)
        # Exact issue or pull-request read. Pull requests share this route.
        # ``/issues/comments`` is not digits, and a sub-resource does not match.
        issue_read = re.fullmatch(re.escape(repo_path) + r"/issues/\d+", path)
        allowed = (method == "GET" and path in {
            "/user",
            repo_path,
            repo_path + "/issues/comments",
            repo_path + "/pulls/comments",
        }) or (issue_read is not None and method == "GET") or (
            path == repo_path + "/issues/comments" and method == "GET"
        ) or (
            issue_comment is not None and method in {"GET", "POST"}
        )
        if not allowed:
            self._deny(
                "GitHub operation is outside the configured repository",
                method=method,
                path=path,
            )
        # The allowlist sees the API-relative path, after any enterprise base
        # path. The host comes from the configured base URL.
        decision = allowlist.check(
            method, f"{parsed_base.scheme}://{parsed_base.netloc}{path}"
        )
        if not decision.allowed:
            self._deny(
                f"credential use denied: {decision.reason}",
                method=method,
                path=path,
            )
        token = await self._vault_broker(allowlist).reveal()
        if not token:
            raise GitHubSourceError(
                f"GitHub token secret {self.secret_name!r} is missing from the vault"
            )
        try:
            async with session.request(
                method,
                f"{base_url}{path}",
                headers={**_HEADERS, "Authorization": f"Bearer {token}"},
                params=params or None,
                json=json_body,
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    self._deny(
                        "GitHub API redirect refused", method=method, path=path
                    )
                payload = await _json(resp, secrets=(token,))
        except GitHubSourceError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubSourceError("GitHub API request failed") from None
        redacted = redact_external_data(payload, secrets=(token,))
        if scrub is not None:
            return scrub(redacted, (token,))
        return redacted


def _credential_broker(user_id: str, cfg: GitHubConfig) -> GitHubCredentialBroker:
    return GitHubCredentialBroker(
        user_id,
        cfg.secret_name,
        cfg.api_base_url,
        cfg.repository,
        cfg.credential_allowlist,
        reveal_user_secret,
    )


async def check_github_source(
    *, user_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Verify the PAT and repository access before polling starts."""
    cfg = GitHubConfig.from_mapping(config)
    broker = _credential_broker(user_id, cfg)
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        user = await broker.request_json(session, "GET", "/user")
        repo = await broker.request_json(session, "GET", f"/repos/{cfg.repository}")
    login = str(user.get("login") or "")
    if not login:
        raise GitHubSourceError("GitHub did not return a login for the token")
    permissions = repo.get("permissions") if isinstance(repo, dict) else {}
    return {
        "agent_login": login,
        "repository": str(repo.get("full_name") or cfg.repository),
        "can_push": bool((permissions or {}).get("push")),
        "private": bool(repo.get("private")),
    }


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _thread_key(repository: str, number: int) -> str:
    return f"github:{repository.lower()}#{number}"


def _issue_number(url: str | None) -> int | None:
    if not url:
        return None
    match = re.search(r"/(?:issues|pulls)/(\d+)(?:$|[/?#])", str(url))
    return int(match.group(1)) if match else None


def _actor(item: dict[str, Any]) -> tuple[str, bool]:
    user = item.get("user") or {}
    login = str(user.get("login") or "")
    return login, str(user.get("type") or "").lower() == "bot"


#: Every reply this system posts carries a marker that starts with this text
#: (``replies.reply_marker``). In trigger-phrase mode it is how a reply posted
#: under the operator's own login is told apart from the operator's request.
REPLY_MARKER_PREFIX = "<!-- conductor-autonomy:"

#: Full ``replies.reply_marker()`` shape: prefix, exactly 32 lowercase hex
#: chars, then `` -->``. A bare or quoted prefix alone must not drop an
#: own-login request that happens to mention the marker text.
_REPLY_MARKER_RE = re.compile(
    re.escape(REPLY_MARKER_PREFIX) + r"[0-9a-f]{32} -->"
)

#: No leading ``@``: a mention would notify whichever GitHub account owns the
#: name.
_TRIGGER_PHRASE = re.compile(r"[A-Za-z0-9/_-][A-Za-z0-9/_-]{1,39}")


def _addressed(text: str, agent_login: str) -> bool:
    return bool(agent_login) and f"@{agent_login.lower()}" in text.lower()


def _has_phrase(text: str, phrase: str) -> bool:
    """The phrase as a whole token: ``/conductor`` matches, ``/conductors`` does not."""
    pattern = rf"(?<![A-Za-z0-9/_-]){re.escape(phrase)}(?![A-Za-z0-9/_-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _draft_from_comment(
    *,
    source_id: str,
    repository: str,
    agent_login: str,
    item: dict[str, Any],
    kind: str,
    thread_number: int,
    url: str,
    secrets: tuple[str, ...] = (),
    trigger_phrase: str = "",
) -> dict[str, Any] | None:
    login, is_bot = _actor(item)
    if not login or is_bot:
        return None
    raw_body = str(item.get("body") or "")
    own = login.lower() == agent_login.lower()
    if own and (not trigger_phrase or _REPLY_MARKER_RE.search(raw_body)):
        return None
    body = redact_external_text(raw_body, secrets=secrets)
    addressed = (
        _has_phrase(body, trigger_phrase)
        if trigger_phrase
        else _addressed(body, agent_login)
    )
    external_id = f"{kind}:{item.get('id')}:{item.get('updated_at') or item.get('created_at') or ''}"
    return {
        "trigger_id": stable_trigger_id(source_id, external_id),
        "external_id": external_id,
        "kind": kind,
        "thread_key": _thread_key(repository, thread_number),
        "actor_login": login,
        "actor_is_bot": False,
        "addressed": addressed,
        "text": body[:20000],
        "url": url,
        "payload": {
            "comment_id": item.get("id"),
            "issue_number": thread_number,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        },
    }


# One Source remembers which issues it has already read. The list is the
# oldest number first; a poll that passes the cap drops from the front.
_ISSUE_CONTEXT_SEEN_LIMIT = 1000


def _coerce_issue_number(value: object) -> int | None:
    """A positive issue number. Booleans are ints, so they are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            number = int(value)
        except ValueError:
            return None
    else:
        return None
    if number < 1:
        return None
    return number


def _load_issue_context_seen(state: Mapping[str, Any]) -> list[int]:
    """Numbers already fetched, oldest first, capped at 1000."""
    raw = state.get("issue_context_seen")
    if not isinstance(raw, list):
        return []
    seen: list[int] = []
    found: set[int] = set()
    for item in raw:
        number = _coerce_issue_number(item)
        if number is None or number in found:
            continue
        found.add(number)
        seen.append(number)
    if len(seen) > _ISSUE_CONTEXT_SEEN_LIMIT:
        seen = seen[-_ISSUE_CONTEXT_SEEN_LIMIT:]
    return seen


def _draft_issue_number(draft: Mapping[str, Any]) -> int | None:
    payload = draft.get("payload")
    if not isinstance(payload, dict):
        return None
    return _coerce_issue_number(payload.get("issue_number"))


def _intake_keeps_draft(config: Mapping[str, Any], draft: Mapping[str, Any]) -> bool:
    """Whether ``receive_trigger`` would persist this draft.

    The gates match intake before the database insert: ignore mode, bots,
    an unaddressed comment on a respond-only-explicit source, and a login
    missing from ``allowed_logins``. A duplicate row is a database result
    and is not part of this check. Marking a number seen for a draft intake
    drops would skip the fetch when a later comment is actually admitted.
    """
    mode = source_mode(dict(config))
    if mode == "ignore":
        return False
    login = str(draft.get("actor_login") or "")
    if not login or draft.get("actor_is_bot"):
        return False
    if mode == "respond-only-explicit" and not bool(draft.get("addressed")):
        return False
    allowed = {
        str(item).strip().lower()
        for item in (config.get("allowed_logins") or [])
        if str(item).strip()
    }
    return bool(allowed) and login.lower() in allowed


async def _attach_issue_contexts(
    broker: GitHubCredentialBroker,
    session: aiohttp.ClientSession,
    *,
    repository: str,
    config: Mapping[str, Any],
    drafts: list[dict[str, Any]],
    seen: list[int],
) -> list[int]:
    """Read each newly admitted thread's issue once and store the capped view.

    A number is fetched only when this poll has a draft intake would keep,
    and a number in ``seen`` is not requested again. A comment intake drops
    does not consume its number, so a later admitted draft still fetches.
    Any read error is swallowed: that draft keeps no ``issue_context``, and
    the number stays unseen so a later admitted comment can retry. The returned list
    is what ``next_state`` persists, at most ``_ISSUE_CONTEXT_SEEN_LIMIT``
    entries.
    """
    known = set(seen)
    pending: list[int] = []
    for draft in drafts:
        number = _draft_issue_number(draft)
        if number is None or number in known or number in pending:
            continue
        if not _intake_keeps_draft(config, draft):
            continue
        pending.append(number)
    fetched: dict[int, dict[str, Any]] = {}
    remembered = list(seen)
    for number in pending:
        def _view(
            raw: Any, secrets: tuple[str, ...], number: int = number
        ) -> dict[str, Any]:
            # Redacted against the Source's own secret before the title and
            # body are clipped. Router cards and Worker prompts read this view.
            return build_issue_context(
                raw if isinstance(raw, Mapping) else None,
                repository=repository,
                number=number,
                secrets=secrets,
            )

        try:
            view = await broker.request_json(
                session,
                "GET",
                f"/repos/{repository}/issues/{number}",
                scrub=_view,
            )
        except GitHubSourceError as exc:
            logger.warning(
                "[autonomy] GitHub issue context unavailable repository=%s number=%s error=%s",
                repository,
                number,
                exc,
            )
            continue
        except Exception as exc:
            # A vault or broker refusal is not a GitHubSourceError. It must
            # not fail the poll either, or the cursor would stay pinned and
            # every later poll would hit the same issue read again. Only the
            # type is logged: an unexpected error may carry request detail.
            logger.warning(
                "[autonomy] GitHub issue context unavailable repository=%s number=%s error=%s",
                repository,
                number,
                type(exc).__name__,
            )
            continue
        if not isinstance(view, dict):
            continue
        fetched[number] = view
        remembered.append(number)
    if len(remembered) > _ISSUE_CONTEXT_SEEN_LIMIT:
        remembered = remembered[-_ISSUE_CONTEXT_SEEN_LIMIT:]
    for draft in drafts:
        number = _draft_issue_number(draft)
        if number not in fetched:
            continue
        payload = draft.get("payload")
        if isinstance(payload, dict):
            payload["issue_context"] = fetched[number]
    return remembered


# Comment lists are read in pages of 100, at most this many requests per
# list per poll. A list still full at the limit bounds the cursor (see below).
_COMMENT_PAGE_SIZE = 100
_COMMENT_PAGE_LIMIT = 10


async def _list_comments_since(
    broker: GitHubCredentialBroker,
    session: aiohttp.ClientSession,
    path: str,
    since_text: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Every comment updated since ``since_text``, oldest update first.

    Pages by timestamp, not page number: each request's ``since`` is the
    last update already read. A comment edited mid-poll moves to the end of
    the ``updated`` order; with page numbers that shift pushes an unread
    comment back onto a page already read, and it is skipped. ``since`` is
    inclusive, so the overlap is dropped by comment id and update; an edited
    comment read again with a newer update is kept, as a later poll would
    keep it. A full page that adds
    nothing new (every row shares one timestamp) steps to the next page of
    that same ``since`` so the read still moves forward.

    Returns the items and whether the list was read to its end. ``False``
    means the request limit was reached with more comments still waiting.
    """
    items: list[dict[str, Any]] = []
    seen_ids: set[tuple[object, str]] = set()
    since = since_text
    page = 1
    for _request in range(_COMMENT_PAGE_LIMIT):
        params: dict[str, Any] = {
            "since": since,
            "per_page": _COMMENT_PAGE_SIZE,
            "sort": "updated",
            "direction": "asc",
        }
        if page > 1:
            params["page"] = page
        batch = await broker.request_json(session, "GET", path, params=params)
        if not isinstance(batch, list):
            return items, True
        fresh = 0
        last_stamp = ""
        for item in batch:
            if not isinstance(item, dict):
                continue
            stamp = str(item.get("updated_at") or item.get("created_at") or "")
            if stamp:
                last_stamp = stamp
            key = (item.get("id"), stamp)
            if item.get("id") is not None:
                if key in seen_ids:
                    continue
                seen_ids.add(key)
            items.append(item)
            fresh += 1
        if len(batch) < _COMMENT_PAGE_SIZE:
            return items, True
        if fresh and last_stamp and last_stamp != since:
            since, page = last_stamp, 1
        else:
            page += 1
    return items, False


def _latest_update(items: list[dict[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for item in items:
        updated = _iso(str(item.get("updated_at") or item.get("created_at") or ""))
        if updated and (latest is None or updated > latest):
            latest = updated
    return latest


def _cursor_after(
    cursor: datetime | None,
    *lists: tuple[list[dict[str, Any]], bool],
) -> datetime | None:
    """Next cursor for lists read in one poll. It never moves backwards.

    The cursor is one timestamp for every list. A list cut at the page limit
    stops it at that list's last read update, so the unread rest of that
    list is still after the cursor. Another list's newer comments are read
    again on the next poll, and intake de-duplicates them.
    """
    stamps = [_latest_update(items) for items, _complete in lists]
    latest = max((stamp for stamp in (cursor, *stamps) if stamp), default=None)
    for stamp, (_items, complete) in zip(stamps, lists, strict=True):
        if complete or stamp is None or latest is None or stamp >= latest:
            continue
        latest = max(stamp, cursor) if cursor else stamp
    return latest


async def poll_github_comments(
    *,
    source_id: str,
    user_id: str,
    config: dict[str, Any],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read one poll window and return drafts plus a new source state.

    The caller persists drafts atomically with the cursor. A failure raises and
    leaves the old cursor in place; the 10-second overlap makes a replayed page
    harmless when the next poll starts slightly earlier.

    Each new thread number is read once, through the broker, as
    ``GET /repos/{repository}/issues/<digits>``, and only when this poll has
    a draft intake would keep. Numbers listed in ``issue_context_seen`` are
    skipped. A comment intake drops does not add its number, so a later
    admitted comment still fetches. That list keeps at most 1000 entries
    and is returned on ``next_state``. ``build_issue_context`` is stored on
    ``draft["payload"]["issue_context"]``. Any error from the issue read is
    swallowed: the draft is still returned, without that key, the number is
    not marked seen, and the cursor still advances.

    Each comment list is read in pages of 100 keyed by the last update read,
    up to 10 requests. A list cut at that limit bounds the cursor at its last
    read update (``_cursor_after``).
    """
    cfg = GitHubConfig.from_mapping(config)
    broker = _credential_broker(user_id, cfg)
    agent_login = str(state.get("agent_login") or "")
    if not agent_login:
        checked = await check_github_source(user_id=user_id, config=config)
        agent_login = checked["agent_login"]

    cursor_dt = _iso(state.get("cursor"))
    since = cursor_dt - _POLL_OVERLAP if cursor_dt else datetime.now(timezone.utc) - _INITIAL_LOOKBACK
    since_text = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    timeout = aiohttp.ClientTimeout(total=45, connect=10)
    drafts: list[dict[str, Any]] = []
    issue_context_seen = _load_issue_context_seen(state)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        comments, comments_complete = await _list_comments_since(
            broker, session, f"/repos/{cfg.repository}/issues/comments", since_text
        )
        reviews, reviews_complete = await _list_comments_since(
            broker, session, f"/repos/{cfg.repository}/pulls/comments", since_text
        )
        # The cursor is one timestamp for both lists. A list cut at the page
        # limit stops it at that list's last read update, so the unread rest
        # of that list is still after the cursor; the other list's newer
        # comments are read again next poll, which intake de-duplicates.
        latest = _cursor_after(
            cursor_dt,
            (comments, comments_complete),
            (reviews, reviews_complete),
        )
        for item in comments:
            number = _issue_number(str(item.get("issue_url") or ""))
            if number is None:
                continue
            url = str(item.get("html_url") or "")
            draft = _draft_from_comment(
                source_id=source_id,
                repository=cfg.repository,
                agent_login=agent_login,
                item=item,
                kind="issue_comment",
                thread_number=number,
                url=url,
                trigger_phrase=cfg.trigger_phrase,
            )
            if draft:
                drafts.append(draft)

        for item in reviews:
            number = _issue_number(str(item.get("pull_request_url") or ""))
            if number is None:
                continue
            url = str(item.get("html_url") or "")
            draft = _draft_from_comment(
                source_id=source_id,
                repository=cfg.repository,
                agent_login=agent_login,
                item=item,
                kind="review_comment",
                thread_number=number,
                url=url,
                trigger_phrase=cfg.trigger_phrase,
            )
            if draft:
                drafts.append(draft)

        issue_context_seen = await _attach_issue_contexts(
            broker,
            session,
            repository=cfg.repository,
            config=config,
            drafts=drafts,
            seen=issue_context_seen,
        )

    next_state = {
        "agent_login": agent_login,
        "repository": cfg.repository,
        "cursor": (latest or datetime.now(timezone.utc)).isoformat(),
        "last_poll_at": datetime.now(timezone.utc).isoformat(),
        "last_error": "",
        "issue_context_seen": issue_context_seen,
    }
    return drafts, next_state


async def post_github_comment(
    *,
    user_id: str,
    config: dict[str, Any],
    thread_key: str,
    body: str,
) -> None:
    """Post one comment to an Issue or PR conversation."""
    cfg = GitHubConfig.from_mapping(config)
    broker = _credential_broker(user_id, cfg)
    try:
        number = int(thread_key.rsplit("#", 1)[1])
    except (IndexError, ValueError) as exc:
        raise GitHubSourceError("thread key does not name a GitHub issue") from exc
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await broker.request_json(
            session,
            "POST",
            f"/repos/{cfg.repository}/issues/{number}/comments",
            json_body={"body": body},
        )


async def find_marker_comment(
    *, user_id: str, config: dict[str, Any], thread_key: str, marker: str
) -> bool:
    """Whether an issue/PR already holds a comment with this marker.

    The outbox retries a send that may have committed remotely before its local
    acknowledgement; checking the destination is the idempotency fence.
    """
    cfg = GitHubConfig.from_mapping(config)
    broker = _credential_broker(user_id, cfg)
    try:
        number = int(thread_key.rsplit("#", 1)[1])
    except (IndexError, ValueError):
        return False
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        comments = await broker.request_json(
            session,
            "GET",
            f"/repos/{cfg.repository}/issues/{number}/comments",
            params={"per_page": 100},
        )
    for item in comments if isinstance(comments, list) else []:
        if marker in str(item.get("body") or ""):
            return True
    return False
