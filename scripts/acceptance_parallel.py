#!/usr/bin/env python3
"""Exercise Main Agent clarification, deliberation, DAG work, and daemon reports.

The script uses the supplied backend URL and starts only its own loopback GitHub
fixture. It does not start or stop backend services; it creates tagged
acceptance resources through the API. Use a disposable backend and database.
Default mock workers do not call CLIs.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlencode, urlsplit


class ApiError(RuntimeError):
    def __init__(
        self, method: str, path: str, status: int,
        retry_after: str | None = None, conflict_kind: str | None = None,
    ):
        super().__init__(f"{method} {path} returned HTTP {status}")
        self.status = status
        self.retry_after = retry_after
        self.conflict_kind = conflict_kind
        self.conflict_run_id: str | None = None


class ApiClient:
    def __init__(self, base_url: str, *, user_id: str, bearer_token: str | None):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.bearer_token = bearer_token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        daemon_token: str | None = None,
        timeout: float = 30,
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.user_id:
            headers["X-User-Id"] = self.user_id
        if daemon_token:
            headers["X-Daemon-Token"] = daemon_token
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            # Preserve only a known conflict class; never retain arbitrary response text.
            conflict_kind = None
            conflict_run_id = None
            if exc.code == 409:
                try:
                    payload = json.loads(exc.read())
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    if isinstance(detail, dict) and detail.get("code") == "coordinator_busy":
                        conflict_kind = "coordinator_busy"
                    elif isinstance(detail, str):
                        match = re.fullmatch(r"run ([a-f0-9]+) changed while coordinator was working", detail)
                        if match:
                            conflict_kind = "run_revision_conflict"
                            conflict_run_id = match.group(1)
                        elif re.fullmatch(r"task [^\s]+ changed before retry", detail):
                            conflict_kind = "task_revision_conflict"
                        elif detail == "Ticket changed; retry":
                            conflict_kind = "ticket_revision_conflict"
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            exc.close()
            error = ApiError(
                method, path, exc.code, exc.headers.get("Retry-After"), conflict_kind
            )
            error.conflict_run_id = conflict_run_id
            raise error from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"{method} {path} transport failure: {exc}") from None


class MockGitHub:
    """Small GitHub API fixture used by the real GitHub source transport."""

    def __init__(self, repository: str = "acceptance/project") -> None:
        self.repository = repository
        self.requests: list[dict[str, str]] = []
        self.post_bodies: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = urlsplit(self.path).path
                owner.requests.append({"method": "GET", "path": path})
                if path == "/user":
                    self._json(200, {"login": "conductor-acceptance", "type": "User"})
                elif path == f"/repos/{owner.repository}":
                    self._json(
                        200,
                        {
                            "full_name": owner.repository,
                            "private": True,
                            "permissions": {"push": False},
                        },
                    )
                elif path == f"/repos/{owner.repository}/issues/comments":
                    self._json(200, [])
                elif path == f"/repos/{owner.repository}/issues/42/comments":
                    comments = [
                        {
                            "id": index + 1,
                            "body": str((item.get("body") or {}).get("body") or ""),
                            "user": {"login": "conductor-acceptance"},
                        }
                        for index, item in enumerate(owner.post_bodies)
                        if item.get("path") == path
                    ]
                    self._json(200, comments)
                elif path == f"/repos/{owner.repository}/pulls/comments":
                    self._json(200, [])
                else:
                    self._json(404, {"message": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                path = urlsplit(self.path).path
                owner.requests.append({"method": "POST", "path": path})
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
                owner.post_bodies.append({"path": path, "body": body})
                self._json(201, {"id": len(owner.post_bodies), "body": (body or {}).get("body", "")})

            def _json(self, status: int, payload: Any) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> MockGitHub:
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def discover_workspace(client: ApiClient, requested_id: str | None) -> str:
    if requested_id:
        return requested_id
    result = client.request("GET", "/api/v1/workspaces?include_home=true&limit=100")
    workspaces = result.get("workspaces", []) if isinstance(result, dict) else []
    for workspace in workspaces:
        if isinstance(workspace, dict) and workspace.get("workspace_id"):
            return str(workspace["workspace_id"])
    raise RuntimeError(
        "No owned workspace is available. Provision one first or pass --workspace-id; "
        "the acceptance runner will not allocate a computer or start a sandbox."
    )


def _artifact_goal(filename: str, content: str) -> str:
    return (
        "Create exactly one new Markdown file named "
        f"`<ticket-scratch>/{filename}` inside the absolute Ticket scratch folder "
        "named in this Worker's Isolation contract (under the daemon isolation root "
        "or CONDUCTOR_HOME, not inside the Git workspace). "
        f"Its complete content must be: {content!r} "
        "Do not modify existing repository files, call real GitHub or other external services, or push changes. "
        "Report completion after writing this acceptance artifact."
    )


class AcceptanceResources:
    """Track only resources created by this invocation for bounded cleanup."""

    def __init__(self, client: ApiClient, tag: str):
        self.client = client
        self.tag = tag
        self.secret_name = f"Accept{tag}"
        self.source_id: str | None = None
        self.daemons: dict[str, dict[str, str]] = {}
        self.ticket_ids: set[str] = set()
        self.cancellation_ticket_ids: list[str] = []
        self.cancellation_result: dict[str, str] | None = None
        self.cleanup_results: list[str] = []

    def create_secret(self) -> None:
        self.client.request(
            "POST",
            "/api/v1/mcp/vault/secrets",
            {
                "name": self.secret_name,
                "value": f"fixture-{uuid.uuid4().hex}",
                "description": f"Temporary acceptance fixture {self.tag}",
            },
        )

    def create_daemon(self, provider: str) -> dict[str, str]:
        row = self.client.request(
            "POST",
            "/api/v1/autonomy/daemons",
            {
                "name": f"acceptance-{self.tag}-{provider}",
                "provider": provider,
                "max_concurrency": 8,
            },
        )
        daemon = {"daemon_id": str(row["daemon_id"]), "token": str(row["token"])}
        self.daemons[provider] = daemon
        return daemon

    def create_source(self, *, workspace_id: str, github_base_url: str) -> str:
        repository = f"acceptance/conductor-{self.tag.lower()}"
        row = self.client.request(
            "POST",
            "/api/v1/autonomy/sources",
            {
                "kind": "github",
                "enabled": True,
                "config": {
                    "repository": repository,
                    "secret_name": self.secret_name,
                    "workspace_id": workspace_id,
                    "allowed_logins": ["conductor-acceptance"],
                    "poll_seconds": 60,
                    "api_base_url": github_base_url,
                    "routing_mode": "candidate",
                },
            },
        )
        self.source_id = str(row["source_id"])
        return self.source_id

    def cleanup(self) -> list[str]:
        if self.source_id:
            try:
                query = urlencode({"source_id": self.source_id, "limit": 200})
                result = self.client.request(
                    "GET", f"/api/v1/autonomy/tickets?{query}"
                )
                for ticket in result.get("tickets", []) if isinstance(result, dict) else []:
                    ticket_id = str(ticket.get("ticket_id") or "")
                    if ticket_id:
                        self.ticket_ids.add(ticket_id)
            except Exception:
                self.cleanup_results.append("source Ticket inventory failed")
        for ticket_id in sorted(self.ticket_ids):
            try:
                ticket = self.client.request(
                    "GET", f"/api/v1/autonomy/tickets/{ticket_id}"
                )
                if str(ticket.get("status") or "").lower() not in {
                    "completed", "cancelled"
                }:
                    self.client.request(
                        "POST", f"/api/v1/autonomy/tickets/{ticket_id}/stop"
                    )
                self.cleanup_results.append(f"ticket {ticket_id} stopped or terminal")
            except Exception:
                self.cleanup_results.append(f"ticket {ticket_id} cleanup failed")
        if self.source_id:
            try:
                self.client.request(
                    "DELETE", f"/api/v1/autonomy/sources/{self.source_id}"
                )
                self.cleanup_results.append("source deleted")
            except Exception:
                self.cleanup_results.append("source cleanup failed")
        for provider, daemon in self.daemons.items():
            try:
                self.client.request(
                    "DELETE", f"/api/v1/autonomy/daemons/{daemon['daemon_id']}"
                )
                self.cleanup_results.append(f"{provider} daemon deleted")
            except Exception:
                self.cleanup_results.append(f"{provider} daemon cleanup failed")
        try:
            self.client.request(
                "DELETE", f"/api/v1/mcp/vault/secrets/{self.secret_name}"
            )
            self.cleanup_results.append("vault secret deleted")
        except Exception:
            self.cleanup_results.append("vault secret cleanup failed")
        return list(self.cleanup_results)


def parse_daemon_map(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("daemon entries must be PROVIDER=ID")
        provider, daemon_id = (part.strip() for part in value.split("=", 1))
        provider = provider.lower()
        if provider not in {"codex", "kimi", "grok"} or not daemon_id:
            raise argparse.ArgumentTypeError(
                "daemon entries must map codex, kimi, or grok to a daemon id"
            )
        result[provider] = daemon_id
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Isolated Conductor backend URL")
    parser.add_argument("--user-id", default=os.getenv("AUTH_USER_ID", "local-dev-user"))
    parser.add_argument("--token-env", help="Environment variable containing a platform bearer token")
    parser.add_argument("--workspace-id", help="Existing owned workspace; discovery is read-only when omitted")
    parser.add_argument("--live", action="store_true", help="Use registered native CLI daemon IDs instead of mock workers")
    parser.add_argument("--daemon", action="append", default=[], metavar="PROVIDER=ID")
    parser.add_argument("--output", help="Write the JSON summary to this path (stdout is always used)")
    parser.add_argument("--keep-resources", action="store_true", help="Do not delete resources created by this run")
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("--base-url must be an absolute HTTP(S) URL")
    if parsed.port in {5173}:
        raise SystemExit("--base-url must point to the backend, not the frontend")
    bearer = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and not bearer:
        raise SystemExit(f"{args.token_env} is empty or unset")
    try:
        daemons = parse_daemon_map(args.daemon)
    except argparse.ArgumentTypeError as exc:
        raise SystemExit(str(exc)) from exc
    if args.live and set(daemons) != {"codex", "kimi", "grok"}:
        raise SystemExit("--live requires --daemon codex=ID --daemon kimi=ID --daemon grok=ID")
    client = ApiClient(args.base_url, user_id=args.user_id, bearer_token=bearer)
    # Scenario execution is filled from the Main Agent bridge contract. Keep
    # output creation last so failed or partial runs are never labeled accepted.
    try:
        summary = run_acceptance(client, args, daemons)
    except Exception as exc:
        summary = {
            "status": "blocked" if isinstance(exc, AcceptanceBlocked) else "failed",
            "mode": "live daemons" if args.live else "mock daemons",
            "error_type": type(exc).__name__,
            "error": exc.summary if isinstance(exc, AcceptanceBlocked) else _safe_error(exc),
            "partial": getattr(exc, "partial", {}),
        }
        rendered = json.dumps(summary, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        return 2 if summary["status"] == "blocked" else 1
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    return 0 if summary.get("status") == "passed" else 1


def run_acceptance(
    client: ApiClient, args: argparse.Namespace, daemons: dict[str, str]
) -> dict[str, Any]:
    tag = uuid.uuid4().hex[:10]
    repository = f"acceptance/conductor-{tag.lower()}"
    workspace_id = discover_workspace(client, args.workspace_id)
    resources = AcceptanceResources(client, tag)
    created_daemons: dict[str, dict[str, str]] = {}
    run_ids: list[str] = []
    assertions: list[str] = []
    children: list[dict[str, Any]] = []
    root_ticket_ids: list[str] = []
    try:
        resources.create_secret()
        if args.live:
            agent_daemons = dict(daemons)
        else:
            created_daemons = {key: resources.create_daemon(key) for key in _PROVIDERS}
            agent_daemons = {
                key: value["daemon_id"] for key, value in created_daemons.items()
            }
        with MockGitHub(repository) as github:
            source_id = resources.create_source(
                workspace_id=workspace_id, github_base_url=github.base_url
            )
            _assert(
                any(item["path"] == "/user" for item in github.requests)
                and any(item["path"] == f"/repos/{repository}" for item in github.requests),
                "Configured GitHub source performed authenticated fixture requests",
            )
            assertions.append("mock GitHub source check used its loopback HTTP fixture")

            mock_participants = [
                {"agent_key": "planner", "role": "planner"},
                {"agent_key": "codex", "role": "participant"},
                {"agent_key": "kimi", "role": "participant"},
                {"agent_key": "grok", "role": "participant"},
            ]
            daemon_participants = [
                {"agent_key": "codex", "role": "planner"},
                {"agent_key": "kimi", "role": "participant"},
                {"agent_key": "grok", "role": "participant"},
            ]
            deliberation_mode = "daemon" if args.live else "mock"
            participants = daemon_participants if args.live else mock_participants
            consensus_script = {"script": "consensus"} if not args.live else {}
            base_context = {
                "source_id": source_id,
                "workspace_id": workspace_id,
                "agent_daemons": agent_daemons,
                "thread_key": f"github:{repository.lower()}#42",
            }
            scenarios = [
                (
                    "help",
                    {
                        "mode": deliberation_mode,
                        "max_rounds": 2,
                        "participants": participants,
                        **({"script": "holdout", "holdout_key": "grok"} if not args.live else {}),
                    },
                ),
                (
                    _artifact_goal("retry-design.md", "Specify three retry attempts with exponential delays of 1, 2, and 4 seconds, then stop."),
                    {"mode": deliberation_mode, "max_rounds": 2, "participants": participants, **consensus_script},
                ),
                (
                    _artifact_goal("link-validation.md", "For a malformed workspace link, show the message: Workspace link is invalid."),
                    {"mode": deliberation_mode, "max_rounds": 2, "participants": participants, **consensus_script},
                ),
                (
                    _artifact_goal("lease-recovery-test-plan.md", "Check that an expired lease can be reclaimed and a stale lease cannot report progress."),
                    {"mode": deliberation_mode, "max_rounds": 2, "participants": participants, **consensus_script},
                ),
                (
                    _artifact_goal("webhook-retry-note.md", "Document the retry limit, next retry delay, and terminal failure status."),
                    {"mode": deliberation_mode, "max_rounds": 2, "participants": participants, **consensus_script},
                ),
            ]

            def submit(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
                goal, deliberation = item
                return client.request(
                    "POST",
                    "/api/v1/main-agent/runs",
                    {"goal": goal, "context": {**base_context, "deliberation": deliberation}},
                    timeout=args.timeout,
                )

            submitted_by_index: dict[int, dict[str, Any]] = {}
            failures: list[Exception] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
                futures = {
                    pool.submit(submit, scenario): index
                    for index, scenario in enumerate(scenarios)
                }
                for future in concurrent.futures.as_completed(futures):
                    try:
                        row = future.result()
                    except Exception as exc:
                        failures.append(exc)
                        continue
                    submitted_by_index[futures[future]] = row
                    run_id = str(row["id"])
                    run_ids.append(run_id)
                    ticket_id = str(
                        row.get("ticket_id")
                        or (row.get("request") or {}).get("ticket_id")
                        or ""
                    )
                    if ticket_id:
                        root_ticket_ids.append(ticket_id)
                        resources.ticket_ids.add(ticket_id)
            if failures:
                for failure in failures:
                    failed_run_id = (
                        failure.conflict_run_id
                        if isinstance(failure, ApiError)
                        else None
                    )
                    if failed_run_id and failed_run_id not in run_ids:
                        run_ids.append(failed_run_id)
                        try:
                            failed_run = _get_run(client, failed_run_id)
                        except Exception:
                            continue
                        failed_ticket_id = str(
                            failed_run.get("ticket_id")
                            or (failed_run.get("request") or {}).get("ticket_id")
                            or ""
                        )
                        if failed_ticket_id:
                            root_ticket_ids.append(failed_ticket_id)
                            resources.ticket_ids.add(failed_ticket_id)
                _discover_source_run_tickets(
                    client, source_id, run_ids, root_ticket_ids, resources
                )
                first = failures[0]
                if isinstance(first, ApiError):
                    raise first
                raise RuntimeError("one or more run submissions failed") from None
            submitted = [submitted_by_index[index] for index in range(len(scenarios))]
            run_ids[:] = [str(row["id"]) for row in submitted]
            root_ticket_ids[:] = [
                str(row.get("ticket_id") or (row.get("request") or {}).get("ticket_id") or "")
                for row in submitted
                if row.get("ticket_id") or (row.get("request") or {}).get("ticket_id")
            ]
            ambiguous = _wait_for_clarification(client, run_ids[0], args.timeout)
            _assert(
                ambiguous.get("status") == "clarification_required",
                "Ambiguous request parks before planning or execution",
            )
            assertions.append("ambiguous root stayed gated until an edited objective was confirmed")
            answered = _answer_clarification(
                client,
                run_ids[0],
                {"answer": _artifact_goal("clarification.md", "State the accepted objective: create a bounded retry design note for transient API failures." )},
                timeout=args.timeout,
            )
            _assert(answered.get("status") != "clarification_required", "Confirmation advances the run")

            runs = []
            for run_id in run_ids:
                run = _wait_for_run_tasks(client, run_id, args.timeout)
                runs.append(run)
            for run in runs:
                _assert(len(run.get("tasks") or []) >= 1, "Consensus produced DAG work items")
                task_map = {str(item["id"]): item for item in run["tasks"]}
                _assert(len(task_map) == len(run["tasks"]), "Consensus DAG task IDs are unique")
                for task in run["tasks"]:
                    _assert(all(str(dep) in task_map for dep in task.get("depends_on") or []), "DAG dependencies refer to approved task IDs")
                children.extend(
                    {"run_id": str(run["id"]), "task_id": str(task["id"]), "agent": str(task.get("deliberation_agent_id") or ""), "ticket_id": str(task.get("child_ticket_id") or "")}
                    for task in run["tasks"]
                )
                resources.ticket_ids.update(
                    str(task["child_ticket_id"])
                    for task in run["tasks"]
                    if task.get("child_ticket_id")
                )
            _assert(
                all(
                    task.get("child_ticket_id")
                    for run in runs
                    for task in run.get("tasks") or []
                    if not (task.get("depends_on") or [])
                ),
                "Every currently runnable DAG root became a durable child Ticket",
            )
            _assert(len(scenarios) >= 4, "Acceptance contains one ambiguous and at least three plain objectives")
            assertions.append("one ambiguous and four plain concurrent runs produced dependency-safe DAG child Ticket links")

            for run in runs:
                dissent = list((run.get("deliberation") or {}).get("dissent") or [])
                if args.live:
                    _assert(
                        bool((run.get("deliberation") or {}).get("decision_reason")) or bool(dissent),
                        "Live deliberation persisted a decision or capped dissent",
                    )
                elif str(run["id"]) == run_ids[0]:
                    _assert(bool(dissent), "Holdout participant dissent was persisted")
                else:
                    _assert(not dissent, "Consensus participants agreed on the canonical plan")
            assertions.append(
                "codex/kimi/grok live deliberation persisted decisions or capped dissent"
                if args.live else
                "codex/kimi/grok mock deliberation persisted consensus and configured Grok dissent"
            )

            if args.live:
                completed = _wait_for_live_completion(client, run_ids, args.timeout)
                if not completed:
                    raise AcceptanceBlocked(
                        "Live daemon workers did not claim and complete every task before timeout.",
                        partial={"run_ids": run_ids, "child_tickets": children},
                    )
            else:
                roots = {
                    str(run.get("ticket_id") or (run.get("request") or {}).get("ticket_id") or "")
                    for run in runs
                }
                children = _mock_execute_dags(client, runs, created_daemons, args.timeout, roots)

            final_runs = []
            for run_id in run_ids:
                final_runs.append(_wait_for_completed_run(client, run_id, args.timeout))
            for run in final_runs:
                _assert(run.get("status") == "completed", "All daemon reports reached Main Agent reconciliation")
                _assert(bool(run.get("merged_result")), "Completed DAG has a merged result")
                _assert(bool(run.get("reply")), "Completed run has a final merged reply")
                root_ticket_id = str(run.get("ticket_id") or (run.get("request") or {}).get("ticket_id") or "")
                _assert(bool(root_ticket_id), "Completed run retains its orchestration root Ticket ID")
                root_ticket = client.request("GET", f"/api/v1/autonomy/tickets/{root_ticket_id}")
                _assert(
                    str(root_ticket.get("status") or root_ticket.get("state") or "").lower() in {"completed", "done"},
                    "Orchestration root Ticket completed after all child work",
                )
                for task in run.get("tasks") or []:
                    ticket_id = str(task.get("child_ticket_id") or "")
                    _assert(bool(ticket_id), "Every approved DAG step has a durable child Ticket")
                    ticket = client.request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")
                    _assert(
                        str(ticket.get("status") or ticket.get("state") or "").lower() in {"completed", "done"},
                        "Every child Ticket reached a terminal completed status",
                    )
                deliberation_id = str((run.get("deliberation") or {}).get("deliberation_id") or "")
                _assert(bool(deliberation_id), "Completed run exposes its deliberation ID")
                transcript = client.request(
                    "GET", f"/api/v1/autonomy/deliberations/{deliberation_id}"
                )
                deliberation_objective = str(transcript.get("objective") or "")
                marker = "Assigned subquestions (JSON): "
                _assert(marker in deliberation_objective, "Persisted deliberation objective carries the accepted division")
                encoded_division = deliberation_objective.split(marker, 1)[1].split(
                    "\n\nDeliberation instructions:", 1
                )[0]
                persisted_division = json.loads(encoded_division)
                expected_division = list(run.get("division") or [])
                _assert(expected_division, "Run retains the original accepted division")

                def normalize_assignment(item: dict[str, Any]) -> dict[str, str]:
                    return {
                        "id": str(item.get("id") or ""),
                        "description": str(item.get("description") or ""),
                        "deliberation_agent_id": str(item.get("deliberation_agent_id") or ""),
                    }

                _assert(
                    [normalize_assignment(item) for item in persisted_division]
                    == [normalize_assignment(item) for item in expected_division],
                    "Persisted deliberation includes every original divided subquestion and agent assignment",
                )
                steps = transcript.get("steps") or []
                _assert(len(steps) == len(run.get("tasks") or []), "Persisted deliberation has every approved DAG step")
                for step in steps:
                    _assert(bool(step.get("child_ticket_id")), "Persisted deliberation step links to its child Ticket")
                    _assert(
                    str(step.get("status") or "").lower() in {"completed", "done"},
                        "Persisted deliberation step reflects completed daemon work",
                    )
                by_key = {str(task.get("id")): task for task in run.get("tasks") or []}
                for step in steps:
                    task = by_key.get(str(step.get("step_key")))
                    _assert(
                        task is not None and str(task.get("child_ticket_id")) == str(step.get("child_ticket_id")),
                        "Persisted deliberation step points to its matching approved task Ticket",
                    )
            expected_replies = [_reply_text(run.get("reply")) for run in final_runs]
            _assert(
                all(expected_replies),
                "Every completed run has final reply text for the GitHub fixture",
            )
            _wait_for_mock_posts(github, expected_replies, args.timeout)
            assertions.append(
                "GitHub fixture received one distinct post for every final merged reply"
            )
            assertions.append("durable reply effects posted each final reply through the GitHub fixture")
            assertions.append("completed daemon reports produced merged results and final replies")
            assertions.append("persisted deliberation steps link all completed child Tickets")
            assertions.append("persisted deliberation objective exactly carries the original division and agent assignments")
            cancel_daemon_id = agent_daemons["codex"]
            cancel_daemon_token = created_daemons.get("codex", {}).get("token")
            cancellation_result = _cancel_running_daemon_ticket(
                client,
                workspace_id=workspace_id,
                daemon_id=cancel_daemon_id,
                daemon_token=cancel_daemon_token,
                timeout=args.timeout,
                resources=resources,
            )
            assertions.append("a running daemon Ticket was stopped and verified cancelled")
            summary = {
                "status": "passed",
                "mode": "live daemons" if args.live else "mock daemons via normal HTTP claim/report",
                "workspace_id": workspace_id,
                "source_id": source_id,
                "run_count": len(final_runs),
                "task_count": sum(len(run.get("tasks") or []) for run in final_runs),
                "initial_parallel_claims": 3 if not args.live else None,
                "dissent_count": len((final_runs[0].get("deliberation") or {}).get("dissent") or []),
                "github_fixture_requests": Counter(item["method"] for item in github.requests),
                "github_reply_posts": len(github.post_bodies),
                "github_distinct_final_reply_posts": len(expected_replies),
                "github_total_post_requests": len(github.post_bodies),
                "run_ids": run_ids,
                "root_ticket_ids": root_ticket_ids,
                "child_ticket_ids": sorted(
                    {
                        str(task["child_ticket_id"])
                        for run in final_runs
                        for task in run.get("tasks") or []
                        if task.get("child_ticket_id")
                    }
                ),
                "cancellation_ticket_ids": list(resources.cancellation_ticket_ids),
                "cancellation": cancellation_result,
                "scenario_run_ids": {
                    "a_clarification_confirm": {
                        "run_id": run_ids[0],
                        "root_ticket_id": root_ticket_ids[0],
                        "child_ticket_ids": sorted(
                            str(task["child_ticket_id"])
                            for task in final_runs[0].get("tasks") or []
                            if task.get("child_ticket_id")
                        ),
                    },
                    "b_consensus_parallel_claims_and_merge": {
                        "run_ids": run_ids[1:],
                        "root_ticket_ids": root_ticket_ids[1:],
                        "child_ticket_ids": sorted(
                            {
                                str(task["child_ticket_id"])
                                for run in final_runs[1:]
                                for task in run.get("tasks") or []
                                if task.get("child_ticket_id")
                            }
                        ),
                    },
                    "c_cancel_running_daemon_ticket": cancellation_result,
                },
                "assertions": assertions,
            }
    except Exception as exc:
        cleanup = [] if args.keep_resources else resources.cleanup()
        partial = {
            "run_ids": run_ids,
            "root_ticket_ids": root_ticket_ids,
            "child_ticket_ids": sorted(
                {str(item["ticket_id"]) for item in children if item.get("ticket_id")}
            ),
            "cancellation_ticket_ids": list(resources.cancellation_ticket_ids),
            "cancellation": resources.cancellation_result,
            "cleanup": cleanup,
        }
        conflict_kind = exc.conflict_kind if isinstance(exc, ApiError) else None
        if conflict_kind:
            partial["conflict_kind"] = conflict_kind
        if isinstance(exc, AcceptanceBlocked):
            exc.partial = {**partial, **exc.partial}
            raise
        raise AcceptanceRunError(_safe_error(exc), partial=partial) from None
    cleanup = [] if args.keep_resources else resources.cleanup()
    summary["cleanup"] = cleanup
    summary["cleanup_performed"] = not args.keep_resources
    summary["retained_resources"] = [item for item in cleanup if "failed" in item]
    if summary["retained_resources"]:
        summary["status"] = "failed"
        summary["error_type"] = "ResourceCleanupError"
    return summary


_PROVIDERS = ("codex", "kimi", "grok")


class AcceptanceRunError(RuntimeError):
    def __init__(self, message: str, *, partial: dict[str, Any]):
        super().__init__(message)
        self.partial = partial


class AcceptanceBlocked(RuntimeError):
    def __init__(self, message: str, *, partial: dict[str, Any]):
        super().__init__(message)
        self.summary = message
        self.partial = partial


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return str(exc)
    if isinstance(exc, AcceptanceRunError):
        return str(exc)
    return f"{type(exc).__name__} (details omitted to keep credentials and prompts out of the summary)"


def _assert(value: bool, message: str) -> None:
    if not value:
        raise AcceptanceRunError(message, partial={})


def _get_run(client: ApiClient, run_id: str) -> dict[str, Any]:
    return client.request("GET", f"/api/v1/main-agent/runs/{run_id}")


def _retry_coordinator_busy(operation, timeout: float) -> Any:
    """Retry only the explicit guard-contention response, within this call's budget."""
    deadline = time.monotonic() + max(0.1, timeout)
    delay = 0.1
    while True:
        remaining = deadline - time.monotonic()
        try:
            return operation(min(30.0, max(0.1, remaining)))
        except ApiError as exc:
            if exc.conflict_kind != "coordinator_busy" or remaining <= 0:
                raise
            try:
                retry_after = float(exc.retry_after or delay)
            except ValueError:
                retry_after = delay
            time.sleep(min(max(0.05, retry_after), remaining))
            delay = min(1.0, delay * 2)


def _tick_run(client: ApiClient, run_id: str, *, timeout: float) -> dict[str, Any]:
    return _retry_coordinator_busy(
        lambda request_timeout: client.request(
            "POST",
            f"/api/v1/main-agent/runs/{run_id}/tick",
            timeout=request_timeout,
        ),
        timeout,
    )


def _answer_clarification(
    client: ApiClient, run_id: str, body: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    return _retry_coordinator_busy(
        lambda request_timeout: client.request(
            "POST",
            f"/api/v1/main-agent/runs/{run_id}/clarification",
            body,
            timeout=request_timeout,
        ),
        timeout,
    )


def _wait_for_clarification(
    client: ApiClient, run_id: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _get_run(client, run_id)
        if latest.get("status") == "clarification_required":
            return latest
        if latest.get("status") in {"failed", "completed"}:
            break
        _tick_run(client, run_id, timeout=30)
        time.sleep(0.2)
    raise AcceptanceBlocked(
        "The ambiguous request did not reach its clarification gate.",
        partial={"run_id": run_id, "last_status": latest.get("status"), "last_phase": latest.get("phase")},
    )


def _discover_source_run_tickets(
    client: ApiClient,
    source_id: str,
    run_ids: list[str],
    root_ticket_ids: list[str],
    resources: AcceptanceResources,
) -> None:
    query = urlencode({"source_id": source_id, "limit": 200})
    result = client.request("GET", f"/api/v1/autonomy/tickets?{query}")
    tickets = result.get("tickets", []) if isinstance(result, dict) else []
    for ticket in tickets:
        mandate = ticket.get("mandate") or {}
        run_id = str(mandate.get("main_agent_run_id") or "")
        ticket_id = str(ticket.get("ticket_id") or "")
        if run_id and ticket_id:
            if run_id not in run_ids:
                run_ids.append(run_id)
            if ticket_id not in root_ticket_ids:
                root_ticket_ids.append(ticket_id)
            resources.ticket_ids.add(ticket_id)


def _wait_for_run_tasks(client: ApiClient, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = _get_run(client, run_id)
        if latest.get("status") == "completed":
            return latest
        if latest.get("phase") in {"execute", "track"} and latest.get("tasks"):
            roots = [task for task in latest["tasks"] if not (task.get("depends_on") or [])]
            if roots and all(task.get("child_ticket_id") for task in roots):
                return latest
        _tick_run(client, run_id, timeout=argsafe(timeout))
        time.sleep(0.25)
    raise AcceptanceBlocked("Main Agent did not produce the expected DAG child Tickets before timeout.", partial={"run_id": run_id, "last_status": latest.get("status"), "last_phase": latest.get("phase")})


def _cancel_running_daemon_ticket(
    client: ApiClient,
    *,
    workspace_id: str,
    daemon_id: str,
    daemon_token: str | None,
    timeout: float,
    resources: AcceptanceResources,
) -> dict[str, str]:
    ticket = client.request(
        "POST",
        "/api/v1/autonomy/tasks",
        {
            "title": f"acceptance-cancel-{uuid.uuid4().hex[:10]}",
            "prompt": "Hold this acceptance task until the runner cancels it.",
            "workspace_id": workspace_id,
            "worker_daemon_id": daemon_id,
        },
    )
    ticket_id = str(ticket["ticket_id"])
    resources.ticket_ids.add(ticket_id)
    resources.cancellation_ticket_ids.append(ticket_id)
    if ticket.get("status") == "awaiting_clarification":
        client.request(
            "POST",
            f"/api/v1/autonomy/tickets/{ticket_id}/clarification/confirm",
            {"text": "Confirm the bounded acceptance task and wait for cancellation."},
        )

    deadline = time.monotonic() + timeout
    if daemon_token:
        claim_id = str(uuid.uuid4())
        while time.monotonic() < deadline:
            try:
                result = client.request(
                    "POST",
                    "/api/v1/autonomy/daemon/claim",
                    {"claim_id": claim_id},
                    daemon_token=daemon_token,
                    timeout=30,
                )
            except ApiError as exc:
                if exc.status != 503:
                    raise
                try:
                    delay = min(2.0, max(0.05, float(exc.retry_after or "0.1")))
                except ValueError:
                    delay = 0.1
                time.sleep(delay)
                continue
            claimed = result.get("ticket") if isinstance(result, dict) else None
            if claimed and str(claimed.get("ticket_id")) == ticket_id:
                current = client.request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")
                _assert(
                    str(current.get("status") or "").lower() == "running",
                    "Claimed daemon Ticket is durably running before cancellation",
                )
                break
            if claimed:
                raise AcceptanceRunError(
                    "The acceptance daemon claimed an unexpected Ticket.",
                    partial={"unexpected_ticket_id": str(claimed.get("ticket_id") or "")},
                )
            current = client.request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")
            if current.get("status") in {"completed", "cancelled", "failed"}:
                raise AcceptanceRunError(
                    "The cancellation Ticket ended before the runner could stop it.",
                    partial={"cancelled_ticket_id": ticket_id, "status": current.get("status")},
                )
            time.sleep(0.2)
        else:
            raise AcceptanceBlocked(
                "The mock daemon did not claim the acceptance Ticket before timeout.",
                partial={"cancelled_ticket_id": ticket_id},
            )
    else:
        while time.monotonic() < deadline:
            current = client.request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")
            if current.get("status") == "running":
                break
            if current.get("status") in {"completed", "cancelled", "failed"}:
                raise AcceptanceRunError(
                    "The cancellation Ticket ended before the runner could stop it.",
                    partial={"cancelled_ticket_id": ticket_id, "status": current.get("status")},
                )
            time.sleep(1)
        else:
            raise AcceptanceBlocked(
                "The live daemon did not start the acceptance Ticket before timeout.",
                partial={"cancelled_ticket_id": ticket_id},
            )

    stopped = client.request("POST", f"/api/v1/autonomy/tickets/{ticket_id}/stop")
    _assert(
        str(stopped.get("status") or "").lower() == "cancelled",
        "Running daemon Ticket reached cancelled status",
    )
    persisted = client.request("GET", f"/api/v1/autonomy/tickets/{ticket_id}")
    final_status = str(persisted.get("status") or "").lower()
    _assert(final_status == "cancelled", "Cancellation persisted to the Ticket record")
    result = {
        "ticket_id": ticket_id,
        "claimed_status": "running",
        "final_status": final_status,
    }
    resources.cancellation_result = result
    return result


def argsafe(timeout: float) -> float:
    return min(max(timeout, 1), 30)


def _parallel_claims(client: ApiClient, daemons: dict[str, dict[str, str]], providers: list[str]) -> list[dict[str, Any] | None]:
    def claim(provider: str):
        daemon = daemons[provider]
        result = client.request("POST", "/api/v1/autonomy/daemon/claim", {}, daemon_token=daemon["token"], timeout=30)
        return result.get("ticket") if isinstance(result, dict) else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(providers)) as pool:
        return list(pool.map(claim, providers))


def _mock_execute_dags(client: ApiClient, runs: list[dict[str, Any]], daemons: dict[str, dict[str, str]], timeout: float, root_ticket_ids: set[str]) -> list[dict[str, Any]]:
    def claim(provider: str):
        daemon = daemons[provider]
        claim_id = str(uuid.uuid4())
        deadline = time.monotonic() + min(timeout, 15.0)
        while time.monotonic() < deadline:
            try:
                response = client.request(
                    "POST", "/api/v1/autonomy/daemon/claim", {"claim_id": claim_id},
                    daemon_token=daemon["token"], timeout=30,
                )
            except ApiError as exc:
                if exc.status != 503:
                    raise
                try:
                    delay = min(2.0, max(0.05, float(exc.retry_after or "0.1")))
                except ValueError:
                    delay = 0.1
                time.sleep(delay)
                continue
            ticket = response.get("ticket") if isinstance(response, dict) else None
            if ticket:
                return {"provider": provider, **ticket}
            time.sleep(0.1)
        return {"provider": provider}
    deadline = time.monotonic() + timeout
    reported: list[dict[str, Any]] = []
    proved_parallelism = False
    claimed_providers: set[str] = set()
    while time.monotonic() < deadline:
        current = []
        for original in runs:
            run = _get_run(client, str(original["id"]))
            if run.get("status") != "completed":
                _tick_run(client, str(run["id"]), timeout=30)
                run = _get_run(client, str(original["id"]))
            current.append(run)
        if all(run.get("status") == "completed" for run in current):
            _assert(proved_parallelism, "Three daemon claims overlapped before reports")
            _assert(
                claimed_providers == set(_PROVIDERS),
                "Codex, Kimi, and Grok all claimed and reported consensus tasks",
            )
            return reported
        ready: list[dict[str, Any]] = []
        for run in current:
            states = {str(task.get("id")): str(task.get("status")) for task in run.get("tasks") or []}
            ready.extend(
                task for task in run.get("tasks") or []
                if task.get("child_ticket_id")
                and task.get("status") == "queued"
                and all(states.get(str(dep)) == "succeeded" for dep in task.get("depends_on") or [])
            )
        if not ready:
            time.sleep(0.1)
            continue
        wave = ready[:3]
        providers = [str(task.get("deliberation_agent_id") or "") for task in wave]
        ready_provider_by_ticket = {
            str(task["child_ticket_id"]): str(task.get("deliberation_agent_id") or "")
            for task in ready
            if task.get("child_ticket_id")
        }
        _assert(all(provider in daemons for provider in providers), "Every runnable task has a registered provider daemon")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
            claims = list(pool.map(claim, providers))
        _assert(all(item.get("ticket_id") and item.get("lease_id") for item in claims), "Ready daemon Tickets were claimed concurrently")
        _assert(len({item["ticket_id"] for item in claims}) == len(claims), "Concurrent claims received distinct Tickets")
        _assert(
            all(item["ticket_id"] in ready_provider_by_ticket for item in claims),
            "Daemon claims matched currently approved ready DAG child Tickets",
        )
        _assert(
            all(
                ready_provider_by_ticket[item["ticket_id"]] == item["provider"]
                for item in claims
            ),
            "Each daemon claimed a ready Ticket assigned to its provider",
        )
        _assert(not any(item["ticket_id"] in root_ticket_ids for item in claims), "Orchestration root Tickets are never daemon claimable")
        if not proved_parallelism:
            _assert(len(claims) >= 3, "Three independent daemon claims overlapped")
            proved_parallelism = True
        claimed_providers.update(providers)
        for ticket in claims:
            _report_ready(client, daemons[ticket["provider"]], ticket)
            reported.append(ticket)
    raise AcceptanceBlocked("Mock daemon execution did not finish all approved DAG tasks before timeout.", partial={"reported_ticket_count": len(reported)})


def _report_ready(client: ApiClient, daemon: dict[str, str], ticket: dict[str, Any]) -> None:
    client.request(
        "POST",
        f"/api/v1/autonomy/daemon/tickets/{ticket['ticket_id']}/report",
        {"lease_id": ticket["lease_id"], "state": "ready", "summary": "Acceptance worker completed its assigned DAG step."},
        daemon_token=daemon["token"],
        timeout=30,
    )


def _drain_ready(client: ApiClient, runs: list[dict[str, Any]], daemons: dict[str, dict[str, str]], *, excluded: set[str], limit: float) -> None:
    deadline = time.monotonic() + limit
    pending = {
        str(task.get("child_ticket_id")): task
        for run in runs for task in run.get("tasks", [])
        if task.get("id") in {"spec", "impl"} and task.get("child_ticket_id") and str(task["child_ticket_id"]) not in excluded
    }
    while pending and time.monotonic() < deadline:
        for provider in sorted({str(task.get("deliberation_agent_id") or "") for task in pending.values()}):
            daemon = daemons[provider]
            response = client.request("POST", "/api/v1/autonomy/daemon/claim", {}, daemon_token=daemon["token"], timeout=30)
            ticket = response.get("ticket") if isinstance(response, dict) else None
            ticket_id = str((ticket or {}).get("ticket_id") or "")
            if ticket_id in pending:
                _report_ready(client, daemon, {"provider": provider, **ticket})
                pending.pop(ticket_id)
        time.sleep(0.1)
    if pending:
        raise AcceptanceBlocked("Some independent DAG Tickets were not claimable before timeout.", partial={"pending_tasks": list(pending)})


def _wait_for_review_tickets(client: ApiClient, run_ids: list[str], timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tasks = []
        for run_id in run_ids:
            run = _get_run(client, run_id)
            tasks.append(next((task for task in run.get("tasks", []) if task.get("id") == "review" and task.get("child_ticket_id")), None))
        if all(tasks):
            return tasks  # type: ignore[return-value]
        for run_id in run_ids:
            _tick_run(client, run_id, timeout=30)
        time.sleep(0.1)
    raise AcceptanceBlocked("Dependent review Tickets were not created before timeout.", partial={"run_ids": run_ids})


def _wait_for_live_completion(client: ApiClient, run_ids: list[str], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = [_get_run(client, run_id) for run_id in run_ids]
        if all(run.get("status") == "completed" for run in runs):
            return True
        for run_id in run_ids:
            _tick_run(client, run_id, timeout=30)
        time.sleep(1)
    return False


def _all_replies_posted(
    post_bodies: list[dict[str, Any]], expected_replies: list[str]
) -> bool:
    if not expected_replies or any(not reply for reply in expected_replies):
        return False
    available = [
        str((post.get("body") or {}).get("body") or "")
        for post in post_bodies
        if isinstance(post.get("body"), dict)
    ]
    for reply in sorted(expected_replies, key=len, reverse=True):
        match_index = next(
            (index for index, body in enumerate(available) if reply in body), None
        )
        if match_index is None:
            return False
        available.pop(match_index)
    return True


def _wait_for_mock_posts(
    github: MockGitHub, expected_replies: list[str], timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _all_replies_posted(github.post_bodies, expected_replies):
            return
        time.sleep(0.1)
    raise AcceptanceBlocked(
        "The private reply effect runner did not deliver every final reply to the GitHub fixture.",
        partial={
            "github_reply_posts": len(github.post_bodies),
            "expected_reply_count": len(expected_replies),
        },
    )


def _reply_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("content") or value.get("summary") or "")
    return str(value or "")


def _wait_for_completed_run(client: ApiClient, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = _get_run(client, run_id)
        if run.get("status") == "completed":
            return run
        if run.get("status") == "failed":
            raise AcceptanceRunError("A Main Agent run failed during reconciliation.", partial={"run_id": run_id})
        _tick_run(client, run_id, timeout=30)
        time.sleep(0.1)
    raise AcceptanceBlocked("Main Agent did not produce its final merged reply before timeout.", partial={"run_id": run_id})


if __name__ == "__main__":
    raise SystemExit(main())
