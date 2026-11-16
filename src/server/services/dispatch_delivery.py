"""L2 dispatch delivery: push an approved integrate branch and open its PR.

The machine ends an approved dispatch in COMPLETE with the integrate branch
merged locally. This module is what makes that run deliver: the service calls
:meth:`DispatchDelivery.deliver` after the drive that reached COMPLETE has
released the row (and again from ``POST /dispatch/{id}/deliver``), and stores
the record it returns under ``gates["delivery"]``.

* The push runs from the merger's dedicated integration worktree
  (``GitMerger.push``), never the shared checkout, and only for a token the
  dispatch's approval gate holds as APPROVED.
* The GitHub token is read from the user's vault (``GITHUB_TOKEN`` unless
  ``CONDUCTOR_DISPATCH_GITHUB_SECRET`` names another secret), only after the
  destination passed the credential allowlist. It reaches Git as an
  ``http.extraHeader`` in the environment, never in an argv word, and it is
  redacted from every message, record and log line this module produces.
* Without a token the dispatch is not failed: the record is ``needs_token``
  with a message that says how to add one and retry.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from src.server.services.autonomy.security import redact_external_text
from src.tools.auto_dispatch.protocols import ApprovalDecision
from src.tools.auto_dispatch.types import Dispatch, DispatchState

logger = logging.getLogger(__name__)

#: Where the delivery record lives on the dispatch row.
DELIVERY = "delivery"
#: The vault secret that holds the GitHub token, and its default name (the
#: one ``conductor github auth`` stores).
SECRET_ENV = "CONDUCTOR_DISPATCH_GITHUB_SECRET"
DEFAULT_SECRET = "GITHUB_TOKEN"
#: ``off`` turns automatic delivery off; anything else leaves it on for live.
DELIVERY_ENV = "CONDUCTOR_DISPATCH_DELIVERY"
#: Optional base branch for the PR; empty uses the repository's default branch.
BASE_ENV = "CONDUCTOR_DISPATCH_PR_BASE"

GITHUB_HOST = "github.com"
GITHUB_API = "https://api.github.com"

OPENED = "opened"
PUSHED = "pushed"
NEEDS_TOKEN = "needs_token"
NO_REMOTE = "no_remote"
NOT_APPROVED = "not_approved"
PUSH_FAILED = "push_failed"
PR_FAILED = "pr_failed"
VAULT_UNAVAILABLE = "vault_unavailable"
#: States after which an automatic delivery is not tried again; a person
#: retries with ``dispatch deliver``.
FINAL_STATES = frozenset({OPENED, PUSHED})


class DeliveryError(RuntimeError):
    """A push or PR step failed; the message is already redacted."""


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    host: str
    repository: str
    api_base: str = GITHUB_API


_REPO_PART = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?"
_HTTPS = re.compile(rf"https://(?:[^@/]+@)?github\.com/({_REPO_PART})(?:\.git)?/?")
_SCP = re.compile(rf"[A-Za-z0-9._-]+@github\.com:({_REPO_PART})(?:\.git)?")
_SSH = re.compile(rf"ssh://[A-Za-z0-9._-]+@github\.com(?::\d+)?/({_REPO_PART})(?:\.git)?/?")


def parse_github_remote(remote: str) -> GitHubRepo | None:
    """``owner/name`` of a github.com remote, or None for any other remote."""
    text = str(remote or "").strip()
    for pattern in (_HTTPS, _SCP, _SSH):
        match = pattern.fullmatch(text)
        if match:
            return GitHubRepo(host=GITHUB_HOST, repository=match.group(1))
    return None


def public_remote(remote: str) -> str:
    """The remote without any user or password part, safe to store and show."""
    text = str(remote or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme in {"http", "https"} and parts.netloc and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    return text


def push_target(remote: str) -> str:
    """The explicit URL ``git push`` is handed; never a remote name.

    A GitHub https remote is canonicalised to ``https://github.com/<owner>/<repo>.git``
    (the only URL a token is ever attached to); any other remote is pushed as
    stored, with any user or password part removed.
    """
    repo = parse_github_remote(remote)
    shown = public_remote(remote)
    if repo is not None and shown.startswith("https://"):
        return f"https://{repo.host}/{repo.repository}.git"
    return shown


#: Where ``start`` snapshots the workspace remote on the row (gates JSON, so
#: no schema change); delivery pushes there, not to the live registry.
WORKSPACE_REMOTE = "workspace_remote"


def auth_config(repo: GitHubRepo, token: str) -> dict[str, str]:
    """The Git configuration that authenticates one https push to ``repo``.

    It is handed to Git through ``GIT_CONFIG_KEY_*`` in the environment.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {f"http.https://{repo.host}/.extraheader": f"AUTHORIZATION: basic {basic}"}


def _secrets(token: str) -> tuple[str, ...]:
    if not token:
        return ()
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return (token, basic)


def _clean(text: object, secrets: tuple[str, ...]) -> str:
    return redact_external_text(str(text or ""), secrets=secrets)[:500]


class PullRequestClient(Protocol):
    async def open(
        self,
        *,
        token: str,
        repo: GitHubRepo,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Open (or find the already open) PR; return ``{"number", "url"}``."""
        ...


Pusher = Callable[..., Awaitable[None]]


class GitHubPullRequestClient:
    """The GitHub REST calls a delivery makes, held to one repository.

    Every request is checked against a credential allowlist (the API host,
    GET/POST, ``/repos/<owner>/<name>``) before the token is put on it, and
    no redirect is followed. Error bodies are redacted against the token.
    """

    def __init__(self, *, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        import aiohttp

        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30, connect=10))

    async def _request(
        self,
        session: Any,
        token: str,
        repo: GitHubRepo,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        from src.server.services.autonomy.credentials import CredentialAllowlist

        allowlist = CredentialAllowlist(
            hosts=frozenset({urlsplit(repo.api_base).hostname or ""}),
            methods=frozenset({"GET", "POST"}),
            path_prefixes=(f"/repos/{repo.repository}",),
        )
        url = f"{repo.api_base}{path}"
        if not allowlist.check(method, url).allowed:
            raise DeliveryError("GitHub request is outside the delivery allowlist")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "conductor-dispatch",
            "Authorization": f"Bearer {token}",
        }
        secrets = _secrets(token)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=dict(params) if params else None,
                json=dict(json_body) if json_body is not None else None,
                allow_redirects=False,
            ) as resp:
                status = int(resp.status)
                if 300 <= status < 400:
                    raise DeliveryError("GitHub API redirect refused")
                if status >= 400:
                    text = _clean(await resp.text(), secrets)[:300]
                    return status, text
                return status, await resp.json()
        except DeliveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - network errors carry no secret we print
            raise DeliveryError(f"GitHub API request failed ({type(exc).__name__})") from None

    async def open(
        self,
        *,
        token: str,
        repo: GitHubRepo,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        prefix = f"/repos/{repo.repository}"
        async with self._session() as session:
            if not base:
                status, info = await self._request(session, token, repo, "GET", prefix)
                if status >= 400 or not isinstance(info, Mapping):
                    raise DeliveryError(f"GitHub API {status}: could not read the repository")
                base = str(info.get("default_branch") or "main")
            status, created = await self._request(
                session,
                token,
                repo,
                "POST",
                f"{prefix}/pulls",
                json_body={"title": title, "head": head, "base": base, "body": body},
            )
            if status < 400 and isinstance(created, Mapping):
                return _pr(created)
            if status == 422:
                owner = repo.repository.split("/", 1)[0]
                found_status, found = await self._request(
                    session,
                    token,
                    repo,
                    "GET",
                    f"{prefix}/pulls",
                    params={"head": f"{owner}:{head}", "state": "open"},
                )
                if found_status < 400 and isinstance(found, list) and found:
                    if isinstance(found[0], Mapping):
                        return _pr(found[0])
            raise DeliveryError(f"GitHub API {status}: {created}")


def _pr(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": payload.get("number"),
        "url": str(payload.get("html_url") or payload.get("url") or ""),
    }


async def git_pusher(
    dispatch: Dispatch,
    *,
    repo_root: Path,
    worktrees_dir: Path,
    gate: Any,
    remote: str,
    auth: Mapping[str, str],
    secrets: tuple[str, ...],
) -> None:
    """Push the integrate branch from the dedicated integration worktree.

    ``GitMerger.push`` re-checks the approval gate and recreates the
    integration worktree the terminal cleanup removed; it is removed again
    here, keeping the branch, so delivery leaves no checkout behind.
    """
    from src.tools.auto_dispatch.execution import GitMerger

    merger = GitMerger(repo_root, dispatch, gate=gate, worktrees_dir=worktrees_dir)
    try:
        await merger.push(
            dispatch.integrate_branch,
            dispatch.approval_token or "",
            remote=remote,
            auth_config=auth,
            secrets=secrets,
        )
    finally:
        problem = await merger.cleanup_worktree()
        if problem:
            logger.warning("Delivery worktree cleanup for %s: %s", dispatch.id, problem)


async def origin_url(repo_root: Path) -> str:
    """``remote.origin.url`` from the checkout's own config file, unrewritten.

    ``git config --file`` reads that one file: no includes and no
    ``insteadOf``/``pushInsteadOf`` rewriting a Worker could have planted.
    """
    import asyncio

    from src.tools.auto_dispatch.execution import ScriptLauncher

    try:
        common, _admin = ScriptLauncher._repository_gitdirs(Path(repo_root).resolve())
    except Exception:  # noqa: BLE001 - not a git checkout: no origin
        return ""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "config", "--file", str(common / "config"), "--get", "remote.origin.url",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, TimeoutError):
        return ""
    return out.decode("utf-8", "replace").strip() if process.returncode == 0 else ""


async def default_remote(dispatch: Dispatch, repo_root: Path) -> tuple[str, str]:
    """``(remote, origin)``: the remote snapshotted at start, else ``origin``'s URL.

    The registry is not read here: editing or deleting a workspace after the
    dispatch started cannot move where its branch is pushed.
    """
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    snapshot = str(gates.get(WORKSPACE_REMOTE) or "").strip()
    origin = await origin_url(repo_root)
    return (snapshot or origin), origin


def delivery_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(DELIVERY_ENV, "") or "").strip().lower() != "off"


class DispatchDelivery:
    """Delivers one COMPLETE, approved dispatch; returns the record to store.

    ``deliver`` does not write the row and does not raise for an expected
    failure: every outcome is a record with a ``state`` and a ``message``.
    """

    def __init__(
        self,
        *,
        gate: Any,
        repo_root_for: Callable[[Dispatch], Path],
        worktrees_dir_for: Callable[[Dispatch], Path],
        secret_name: str | None = None,
        reveal: Callable[[str, str], Awaitable[str | None]] | None = None,
        pull_requests: PullRequestClient | None = None,
        pusher: Pusher | None = None,
        remote_for: Callable[[Dispatch, Path], Awaitable[tuple[str, str]]] | None = None,
        base_branch: str | None = None,
    ) -> None:
        self._gate = gate
        self._repo_root_for = repo_root_for
        self._worktrees_dir_for = worktrees_dir_for
        self._secret_name = (
            secret_name or os.environ.get(SECRET_ENV, "").strip() or DEFAULT_SECRET
        )
        self._reveal = reveal
        self._pull_requests = pull_requests or GitHubPullRequestClient()
        self._pusher = pusher or git_pusher
        self._remote_for = remote_for or default_remote
        self._base = (
            base_branch if base_branch is not None else os.environ.get(BASE_ENV, "").strip()
        )

    @property
    def secret_name(self) -> str:
        return self._secret_name

    async def _token(self, user_id: str) -> str:
        reveal = self._reveal
        if reveal is None:
            from src.server.database.user_vault_secrets import reveal_user_secret

            reveal = reveal_user_secret
        return str(await reveal(user_id, self._secret_name) or "").strip()

    def _record(self, dispatch: Dispatch, state: str, message: str, **extra: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "state": state,
            "message": message,
            "branch": dispatch.integrate_branch,
            "at": datetime.now(UTC).isoformat(),
        }
        record.update({key: value for key, value in extra.items() if value not in (None, "")})
        return record

    async def deliver(self, dispatch: Dispatch) -> dict[str, Any]:
        branch = dispatch.integrate_branch
        if dispatch.state is not DispatchState.COMPLETE or not branch:
            return self._record(
                dispatch,
                NOT_APPROVED,
                "Only a COMPLETE dispatch with an integrate branch is delivered",
            )
        decision = await self._gate.decision(dispatch.approval_token or "")
        if decision is not ApprovalDecision.APPROVED:
            return self._record(
                dispatch,
                NOT_APPROVED,
                f"The approval gate is {decision.value}; nothing was pushed",
            )
        repo_root = Path(self._repo_root_for(dispatch))
        remote, origin = await self._remote_for(dispatch, repo_root)
        shown = public_remote(remote)
        if not remote:
            return self._record(
                dispatch,
                NO_REMOTE,
                f"No git remote is configured for this checkout; {branch} stays local. "
                "Register the workspace with --remote (or set origin), then run "
                f"`conductor dispatch deliver {dispatch.id}`",
            )
        repo = parse_github_remote(remote)
        token = ""
        if repo is not None:
            try:
                token = await self._token(dispatch.user_id)
            except Exception as exc:  # noqa: BLE001 - the vault error text is not shown
                logger.warning("Delivery vault read failed: %s", type(exc).__name__)
                return self._record(
                    dispatch,
                    VAULT_UNAVAILABLE,
                    "The Conductor vault could not be read; nothing was pushed. "
                    f"Retry with `conductor dispatch deliver {dispatch.id}`",
                    remote=shown,
                    repository=repo.repository,
                )
            if not token:
                return self._record(
                    dispatch,
                    NEEDS_TOKEN,
                    f"No GitHub token in the vault under {self._secret_name!r}; "
                    f"{branch} was not pushed and no pull request was opened. "
                    "Add one with `conductor github auth`, then run "
                    f"`conductor dispatch deliver {dispatch.id}`",
                    remote=shown,
                    repository=repo.repository,
                    secret_name=self._secret_name,
                )
        secrets = _secrets(token)
        from src.tools.auto_dispatch.execution import github_push_url

        target = push_target(remote)
        # The token goes only to https://github.com/<owner>/<repo>; an ssh
        # remote is pushed with the operator's own keys and no token.
        auth = (
            auth_config(repo, token)
            if repo is not None and token and github_push_url(target)
            else {}
        )
        try:
            await self._pusher(
                dispatch,
                repo_root=repo_root,
                worktrees_dir=Path(self._worktrees_dir_for(dispatch)),
                gate=self._gate,
                remote=target,
                auth=auth,
                secrets=secrets,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, redacted, never re-raised
            logger.warning("Delivery push failed for %s: %s", dispatch.id, type(exc).__name__)
            return self._record(
                dispatch,
                PUSH_FAILED,
                f"Pushing {branch} failed: {_clean(exc, secrets)}",
                remote=shown,
                repository=repo.repository if repo else "",
            )
        if repo is None:
            return self._record(
                dispatch,
                PUSHED,
                f"Pushed {branch} to {shown}; the remote is not on GitHub, so no "
                "pull request was opened",
                remote=shown,
            )
        try:
            opened = await self._pull_requests.open(
                token=token,
                repo=repo,
                head=branch,
                base=self._base,
                title=_title(dispatch),
                body=_body(dispatch),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, redacted, never re-raised
            logger.warning("Delivery PR failed for %s: %s", dispatch.id, type(exc).__name__)
            return self._record(
                dispatch,
                PR_FAILED,
                f"Pushed {branch}, but opening the pull request failed: "
                f"{_clean(exc, secrets)}",
                remote=shown,
                repository=repo.repository,
            )
        url = _clean(opened.get("url"), secrets)
        return self._record(
            dispatch,
            OPENED,
            f"Pushed {branch} and opened {url or 'a pull request'}",
            remote=shown,
            repository=repo.repository,
            pr_url=url,
            pr_number=opened.get("number"),
        )


def _title(dispatch: Dispatch) -> str:
    goal = " ".join(str(dispatch.goal or "").split())
    return (f"dispatch: {goal}" if goal else f"dispatch {dispatch.id}")[:200]


def _body(dispatch: Dispatch) -> str:
    lines = [
        f"Auto-dispatch `{dispatch.id}`, approved through the dispatch gate.",
        "",
        "Lines:",
    ]
    for line in dispatch.lines:
        lines.append(f"- `{line.id}` {line.name} ({line.status.value})")
    return "\n".join(lines)[:8000]


__all__ = [
    "DELIVERY",
    "WORKSPACE_REMOTE",
    "DispatchDelivery",
    "GitHubPullRequestClient",
    "GitHubRepo",
    "auth_config",
    "delivery_enabled",
    "parse_github_remote",
    "public_remote",
]
