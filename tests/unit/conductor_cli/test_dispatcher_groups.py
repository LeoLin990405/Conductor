"""Dispatcher groups: lines, dispatch, review, merge, deploy status, fleet, memory, quota."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.conductor_cli import cli
from src.conductor_cli.client import APIError, CLIError, ServerError
from src.conductor_cli.fleet import normalize_balance


class StubClient:
    """Fake APIClient: scripted GET/POST results, records every call."""

    def __init__(
        self,
        *,
        get: Any = None,
        post: Any = None,
    ) -> None:
        self._get = get
        self._post = post
        self.calls: list[tuple[str, str, Any]] = []
        self.timeouts: list[float | None] = []

    def get(self, path: str, *, params: dict | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if isinstance(self._get, Exception):
            raise self._get
        if callable(self._get):
            return self._get(path, params)
        return self._get

    def post(
        self, path: str, json: dict | None = None, *, timeout: float | None = None
    ) -> Any:
        self.calls.append(("POST", path, json))
        self.timeouts.append(timeout)
        if isinstance(self._post, Exception):
            raise self._post
        if callable(self._post):
            return self._post(path, json)
        return self._post

    def close(self) -> None:
        return None


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(path),
        },
    )
    return result.stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "sandbox"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    # _resolve_repo() insists on a Conductor-shaped checkout.
    (repo / "server.py").write_text("# stub\n", encoding="utf-8")
    (repo / "alembic.ini").write_text("# stub\n", encoding="utf-8")
    _git(repo, "add", "file.txt", "server.py", "alembic.ini")
    _git(repo, "commit", "-m", "base")
    return repo


@pytest.fixture
def wt_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(root))
    return root


def _add_line(repo: Path, root: Path, name: str, branch: str) -> Path:
    target = root / name
    _git(repo, "worktree", "add", "-b", branch, str(target))
    return target


# ---------------------------------------------------------------------------
# lines
# ---------------------------------------------------------------------------


def test_lines_list_show_and_logs(wt_root: Path, tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    line = _add_line(repo, wt_root, "feat-a", "feat/a")
    (line / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    logs = wt_root / "logs"
    logs.mkdir()
    (logs / "claude--feat-a.log").write_text("hello\n", encoding="utf-8")

    args = cli.build_parser().parse_args(["lines", "list"])
    result = args.handler(args)
    assert result["root"] == str(wt_root)
    assert result["lines"] == [
        {"name": "feat-a", "branch": "feat/a", "model": "claude", "state": "dirty"}
    ]

    args = cli.build_parser().parse_args(["lines", "show", "feat-a"])
    detail = args.handler(args)
    assert detail["path"] == str(line)
    assert len(detail["head"]) == 40
    assert detail["last_log"].endswith("feat-a.log")
    assert detail["ahead_of_integrate_r2"] is None

    args = cli.build_parser().parse_args(["lines", "logs", "feat-a", "-n", "1"])
    tailed = args.handler(args)
    assert tailed["lines"] == ["hello\n"]


def test_lines_unknown_line_lists_available(wt_root: Path) -> None:
    args = cli.build_parser().parse_args(["lines", "show", "nope"])
    with pytest.raises(CLIError, match="Available"):
        args.handler(args)


def test_lines_envelope_json(wt_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["lines", "list", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "lines list"
    assert envelope["data"]["lines"] == []
    assert envelope["errors"] is None


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_dispatch_falls_back_to_fake_when_api_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_HOME", str(tmp_path / "dispatches"))
    client = StubClient(post=APIError("HTTP 404 from Conductor", 404))
    args = cli.build_parser().parse_args(["dispatch", "start", "ship the thing"])
    record = args.handler(args, client)
    assert record["fake"] is True
    assert record["goal"] == "ship the thing"
    assert record["state"] == "SUBMITTED"
    assert record["id"].startswith("local-")
    assert (tmp_path / "dispatches" / f"{record['id']}.json").is_file()

    status = cli.build_parser().parse_args(["dispatch", "status"])
    active = status.handler(status, StubClient(get=APIError("HTTP 404 from Conductor", 404)))
    assert active["fake"] is True
    assert [item["id"] for item in active["active"]] == [record["id"]]

    one = cli.build_parser().parse_args(["dispatch", "status", record["id"]])
    shown = one.handler(one, StubClient(get=APIError("HTTP 404 from Conductor", 404)))
    assert shown["goal"] == "ship the thing"


def test_dispatch_uses_the_api_when_present() -> None:
    created = {"id": "d-1", "goal": "g", "state": "PLANNING"}
    client = StubClient(post=created, get={"id": "d-1", "state": "PLANNING"})
    args = cli.build_parser().parse_args(["dispatch", "start", "ship g"])
    assert args.handler(args, client) == created
    assert client.calls[0] == ("POST", "/api/v1/dispatch", {"goal": "ship g"})

    status = cli.build_parser().parse_args(["dispatch", "status", "d-1"])
    assert status.handler(status, client)["state"] == "PLANNING"
    assert client.calls[1][1] == "/api/v1/dispatch/d-1"


def test_dispatch_never_fakes_a_server_failure() -> None:
    client = StubClient(post=ServerError("HTTP 503 from Conductor"))
    args = cli.build_parser().parse_args(["dispatch", "start", "ship g"])
    with pytest.raises(ServerError):
        args.handler(args, client)


def test_dispatch_requires_a_goal() -> None:
    args = cli.build_parser().parse_args(["dispatch"])
    with pytest.raises(CLIError, match="pass a verb"):
        args.handler(args, StubClient())


def test_dispatch_status_envelope_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_HOME", str(tmp_path / "dispatches"))
    monkeypatch.setattr(
        cli,
        "APIClient",
        lambda **kwargs: StubClient(get=APIError("HTTP 404 from Conductor", 404)),
    )
    assert cli.main(["dispatch", "status", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "dispatch status"
    assert envelope["data"]["active"] == []


# ---------------------------------------------------------------------------
# review / merge (server-backed; the local worktree operations moved to the API)
# ---------------------------------------------------------------------------


def test_review_refuses_self_review(
    wt_root: Path, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    _add_line(repo, wt_root, "line-a", "feat/a")
    client = StubClient(post={})
    args = cli.build_parser().parse_args(["review", "run", "line-a", "--by", "line-a"])
    with pytest.raises(CLIError, match="refusing self-review"):
        args.handler(args, client)
    assert client.calls == []


def test_review_run_and_status_call_the_server_api(
    wt_root: Path, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    _add_line(repo, wt_root, "line-a", "feat/a")
    _add_line(repo, wt_root, "line-b", "feat/b")
    record = {"id": "r-1", "line": "line-a", "reviewer": "line-b", "state": "queued"}
    client = StubClient(post=record, get={"reviews": [{"id": "r-1"}]})

    args = cli.build_parser().parse_args(["review", "run", "line-a", "--by", "line-b"])
    assert args.handler(args, client) == record
    assert client.calls[-1] == (
        "POST",
        "/api/v1/reviews",
        {
            "line": "line-a",
            "reviewer": "line-b",
            "provider": "codex",
            "root": str(wt_root.resolve()),
        },
    )

    status = cli.build_parser().parse_args(["review", "status"])
    assert status.handler(status, client) == {"reviews": [{"id": "r-1"}]}
    assert client.calls[-1] == ("GET", "/api/v1/reviews", {"limit": 100})

    filtered = cli.build_parser().parse_args(["review", "status", "line-b"])
    filtered.handler(filtered, client)
    assert client.calls[-1] == (
        "GET",
        "/api/v1/reviews",
        {"limit": 100, "line": "line-b"},
    )

    missing = cli.build_parser().parse_args(["review", "run", "line-a", "--by", "ghost"])
    with pytest.raises(CLIError, match="Unknown line"):
        missing.handler(missing, StubClient())


def test_review_verdict_calls_the_server_api(wt_root: Path) -> None:
    client = StubClient(post={"id": "r-1", "verdict": "merge"})
    args = cli.build_parser().parse_args(
        [
            "review",
            "verdict",
            "11111111-1111-1111-1111-111111111111",
            "--verdict",
            "MERGE",
            "--note",
            "looks good",
        ]
    )

    assert args.handler(args, client)["verdict"] == "merge"
    assert client.calls[-1] == (
        "POST",
        "/api/v1/reviews/11111111-1111-1111-1111-111111111111/verdict",
        {
            "verdict": "merge",
            "note": "looks good",
            "root": str(wt_root.resolve()),
        },
    )


def test_merge_posts_to_the_server_and_keeps_the_result(
    wt_root: Path, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    _add_line(repo, wt_root, "line-a", "feat/a")
    result = {
        "merged": True,
        "line": "line-a",
        "branch": "feat/a",
        "into": "integrate/r2",
        "commit": "c" * 40,
        "tested_sha": "a" * 40,
        "tests": "green",
        "pushed": False,
    }
    client = StubClient(post=result)
    args = cli.build_parser().parse_args(
        ["merge", "line-a", "--into", "integrate/r2", "--test-cmd", "true"]
    )

    assert args.handler(args, client) == result
    assert client.calls[-1] == (
        "POST",
        "/api/v1/merges",
        {
            "line": "line-a",
            "into": "integrate/r2",
            "root": str(wt_root.resolve()),
            "test_cmd": "true",
        },
    )


def test_merge_maps_a_structured_server_refusal_to_cli_error(
    wt_root: Path, tmp_path: Path
) -> None:
    repo = _make_repo(tmp_path)
    _add_line(repo, wt_root, "line-a", "feat/a")
    refusal = APIError(
        "The merge rehearsal is red; the target branch was not touched.\n"
        "remediation: Fix the failing tests on the line branch, then retry.",
        409,
        code="tests_failed",
    )
    client = StubClient(post=refusal)
    args = cli.build_parser().parse_args(
        ["merge", "line-a", "--into", "integrate/r2"]
    )

    with pytest.raises(APIError, match="target branch was not touched") as error:
        args.handler(args, client)
    assert error.value.code == "tests_failed"


def test_merge_json_envelope_frames_the_server_result(
    wt_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _make_repo(tmp_path)
    _add_line(repo, wt_root, "line-a", "feat/a")
    payload = {"merged": True, "line": "line-a", "into": "integrate/r2", "tests": "green"}
    monkeypatch.setattr(
        cli, "APIClient", lambda **kwargs: StubClient(post=payload)
    )

    assert cli.main(["merge", "line-a", "--into", "integrate/r2", "--json"]) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["api_version"] == "v1"
    assert envelope["command"] == "merge"
    assert envelope["data"] == payload
    assert envelope["errors"] is None
    assert envelope["exit_code"] == 0


# ---------------------------------------------------------------------------
# deploy status
# ---------------------------------------------------------------------------


def test_deploy_status_reports_record_commit_health_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.conductor_cli import operations

    repo = _make_repo(tmp_path)
    record_path = tmp_path / "last-deploy.json"
    record_path.write_text(
        json.dumps(
            {
                "status": "deployed",
                "backup": str(tmp_path / "missing.dump"),
                "commit": "abc",
                "health": "healthy",
                "at": "2026-11-03T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    newest = backups / "conductor-newest.dump"
    newest.write_bytes(b"x")
    monkeypatch.setenv("CONDUCTOR_LAST_DEPLOY", str(record_path))
    monkeypatch.setenv("CONDUCTOR_REPO", str(repo))
    monkeypatch.setattr(operations, "BACKUP_DIR", backups)
    monkeypatch.setattr(
        operations, "_probe_health", lambda: {"status": "healthy"}
    )

    args = cli.build_parser().parse_args(["deploy", "status"])
    result = args.handler(args)
    assert result["last_deploy"]["status"] == "deployed"
    assert result["record_path"] == str(record_path)
    assert result["live_commit"]
    assert result["health"] == {"status": "healthy"}
    # recorded backup is missing on disk → fall back to the newest dump
    assert result["last_backup"] == str(newest)


def test_deploy_status_without_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.conductor_cli import operations

    repo = _make_repo(tmp_path)
    monkeypatch.setenv("CONDUCTOR_LAST_DEPLOY", str(tmp_path / "absent.json"))
    monkeypatch.setenv("CONDUCTOR_REPO", str(repo))
    monkeypatch.setattr(operations, "BACKUP_DIR", tmp_path / "no-backups")
    monkeypatch.setattr(
        operations, "_probe_health", lambda: {"status": "unreachable", "detail": "x"}
    )
    args = cli.build_parser().parse_args(["deploy", "status"])
    result = args.handler(args)
    assert result["last_deploy"] is None
    assert result["last_backup"] is None
    assert result["health"]["status"] == "unreachable"


def test_record_last_deploy_writes_private_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.conductor_cli import operations

    path = tmp_path / "cfg" / "last-deploy.json"
    monkeypatch.setenv("CONDUCTOR_LAST_DEPLOY", str(path))
    operations._record_last_deploy({"status": "deployed", "commit": "abc"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"status": "deployed", "commit": "abc"}


# ---------------------------------------------------------------------------
# agents fleet
# ---------------------------------------------------------------------------


def test_agents_fleet_normalizes_balance_to_ok_low() -> None:
    client = StubClient(
        get={
            "agents": [
                {"id": "a", "provider_balance": "ok"},
                {"id": "b", "provider_balance": "low"},
                {"id": "c", "provider_balance": "unknown"},
                {"id": "d"},
            ]
        }
    )
    args = cli.build_parser().parse_args(["agents", "fleet"])
    payload = args.handler(args, client)
    assert client.calls == [("GET", "/api/v1/agents/fleet", None)]
    balances = [agent["provider_balance"] for agent in payload["agents"]]
    assert balances == ["ok", "low", "low", "low"]
    assert set(balances) <= {"ok", "low"}


def test_agents_fleet_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli,
        "APIClient",
        lambda **kwargs: StubClient(get={"agents": [{"id": "x", "provider_balance": "ok"}]}),
    )
    assert cli.main(["agents", "fleet", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "agents fleet"
    assert envelope["data"]["agents"][0]["provider_balance"] == "ok"


def test_normalize_balance_mapping() -> None:
    assert normalize_balance("ok") == "ok"
    assert normalize_balance("low") == "low"
    assert normalize_balance("unknown") == "low"
    assert normalize_balance(None) == "low"
    assert normalize_balance("available") == "low"


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def test_memory_recall_posts_expected_query() -> None:
    client = StubClient(get={"hits": []})
    args = cli.build_parser().parse_args(["memory", "recall", "deploy notes", "--limit", "5"])
    result = args.handler(args, client)
    assert result == {"hits": []}
    assert client.calls == [
        ("GET", "/api/v1/memory/search", {"q": "deploy notes", "limit": 5})
    ]


def test_memory_absent_interface_is_a_clean_error() -> None:
    client = StubClient(get=APIError("HTTP 404 from Conductor", 404))
    args = cli.build_parser().parse_args(["memory", "recall", "q"])
    with pytest.raises(CLIError, match="not available on this server"):
        args.handler(args, client)


def test_memory_propose_and_decisions() -> None:
    client = StubClient(post={"proposal_id": "p-1"})
    args = cli.build_parser().parse_args(["memory", "propose", "a new fact"])
    assert args.handler(args, client) == {"proposal_id": "p-1"}
    assert client.calls[-1] == (
        "POST",
        "/api/v1/memory/proposals",
        {"text": "a new fact", "type": "user"},
    )

    approve = cli.build_parser().parse_args(["memory", "approve", "p-1"])
    approve.handler(approve, client)
    assert client.calls[-1] == (
        "POST",
        "/api/v1/memory/proposals/p-1/decision",
        {"approve": True},
    )

    reject = cli.build_parser().parse_args(["memory", "reject", "p-1"])
    reject.handler(reject, client)
    assert client.calls[-1] == (
        "POST",
        "/api/v1/memory/proposals/p-1/decision",
        {"approve": False},
    )


def test_memory_rejects_path_traversal_ids() -> None:
    args = cli.build_parser().parse_args(["memory", "approve", "../evil"])
    with pytest.raises(CLIError, match="Invalid proposal id"):
        args.handler(args, StubClient(post={}))


# ---------------------------------------------------------------------------
# models quota
# ---------------------------------------------------------------------------


def test_models_quota_uses_codexbar_snapshot_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.conductor_cli import usage

    seen: dict[str, Any] = {}

    def fake_usage_result(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {
            "providers": [
                {"provider": "codex", "status": "ok", "remaining_percent": 75},
                {"provider": "claude", "status": "unknown"},
            ]
        }

    monkeypatch.setattr(usage, "usage_result", fake_usage_result)
    args = cli.build_parser().parse_args(
        ["models", "quota", "--provider", "codex", "--no-refresh"]
    )
    result = args.handler(args, None)
    assert seen["show_balance"] is True
    assert seen["no_refresh"] is True
    assert [row["provider"] for row in result["providers"]] == ["codex"]


def test_models_quota_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.conductor_cli import usage

    monkeypatch.setattr(
        usage,
        "usage_result",
        lambda **kwargs: {"providers": [{"provider": "codex", "status": "ok"}]},
    )
    assert cli.main(["models", "quota", "--no-refresh", "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["command"] == "models quota"
    assert envelope["data"]["providers"][0]["provider"] == "codex"


# ---------------------------------------------------------------------------
# Regression: BLOCK review findings
# ---------------------------------------------------------------------------


def test_lines_reject_absolute_and_dotdot_names(wt_root: Path) -> None:
    for bad in ("/tmp", "..", ".", "a/b", "a\\b"):
        args = cli.build_parser().parse_args(["lines", "show", bad])
        with pytest.raises(CLIError, match="Unknown line"):
            args.handler(args)


def test_review_refuses_case_variant_self(
    wt_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a case-insensitive FS, LineA and linea are the same directory."""
    monkeypatch.setenv("CONDUCTOR_REVIEW_HOME", str(tmp_path / "reviews"))
    repo = _make_repo(tmp_path)
    # Create a line whose name differs only by case from the reviewer name.
    # require_line is case-sensitive on the name string but samefile() is not.
    line = _add_line(repo, wt_root, "LineA", "feat/a")
    # Simulate the macOS case: a second name that resolves to the same dir.
    # We cannot create two worktrees with names that differ only by case on
    # a case-sensitive FS, so call the handler with a reviewer path that
    # samefile() will match by monkeypatching require_line's returned path.
    from src.conductor_cli import review as review_mod

    real_require = review_mod.require_line

    def fake_require(root: Path, name: str) -> Path:
        if name.lower() == "linea":
            return line  # same directory as LineA
        return real_require(root, name)

    monkeypatch.setattr(review_mod, "require_line", fake_require)
    args = cli.build_parser().parse_args(
        ["review", "run", "LineA", "--by", "linea"]
    )
    client = StubClient(post={})
    with pytest.raises(CLIError, match="refusing self-review"):
        args.handler(args, client)
    assert client.calls == []


def test_memory_rejects_bare_dotdot_proposal_id() -> None:
    args = cli.build_parser().parse_args(["memory", "approve", ".."])
    with pytest.raises(CLIError, match="Invalid proposal id"):
        args.handler(args, StubClient(post={}))


def test_dispatch_status_rejects_dotdot_before_url() -> None:
    client = StubClient(get={"id": "should-not-be-called"})
    args = cli.build_parser().parse_args(["dispatch", "status", ".."])
    with pytest.raises(CLIError, match="Invalid dispatch id"):
        args.handler(args, client)
    assert client.calls == []


def test_human_render_prints_dispatch_with_empty_lines() -> None:
    from src.conductor_cli.output import human_render

    record = {
        "id": "local-abc",
        "goal": "ship it",
        "state": "SUBMITTED",
        "lines": [],
        "fake": True,
    }
    text = human_render(record)
    assert "ship it" in text
    assert "local-abc" in text
    # Non-empty lines still render as log text.
    assert human_render({"lines": ["a", "b"]}) == "a\nb"


def test_merge_default_test_command_survives_spaces_in_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server-side default argv is a list, so a spaced line path is safe."""
    from src.config.models import ReviewMergeConfig
    from src.server.services.autonomy import review_merge as service

    monkeypatch.setattr(
        service, "get_review_merge_config", lambda: ReviewMergeConfig()
    )
    worktree = tmp_path / "has space" / "line"
    worktree.mkdir(parents=True)
    command = service._test_command(worktree, None)
    assert command[0].endswith("python")
    assert command[1:] == ["-m", "pytest", "-q"]
    assert all(isinstance(part, str) for part in command)


# ---------------------------------------------------------------------------
# Follow-up review fixes: fleet JSON view and 501 absent-endpoint fallbacks
# ---------------------------------------------------------------------------


def test_fleet_json_applies_filter_and_sort_like_the_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "agents": [
            {"id": "a1", "role": "reviewer", "status": "running", "commits_ahead": 1},
            {"id": "a2", "role": "implementer", "status": "running", "commits_ahead": 9},
            {"id": "a3", "role": "reviewer", "status": "done", "commits_ahead": 5},
        ]
    }
    monkeypatch.setattr(cli, "APIClient", lambda **kwargs: StubClient(get=payload))

    assert (
        cli.main(["agents", "fleet", "--json", "--filter", "role=reviewer", "--sort", "commits"])
        == 0
    )

    envelope = json.loads(capsys.readouterr().out)
    assert [agent["id"] for agent in envelope["data"]["agents"]] == ["a3", "a1"]


def test_dispatch_falls_back_when_the_api_answers_501(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_HOME", str(tmp_path / "dispatches"))
    args = cli.build_parser().parse_args(["dispatch", "start", "ship it"])

    record = args.handler(
        args, StubClient(post=ServerError("HTTP 501 from Conductor", 501))
    )

    assert record["fake"] is True
    assert record["goal"] == "ship it"


def test_memory_reports_absence_when_the_api_answers_501() -> None:
    args = cli.build_parser().parse_args(["memory", "recall", "query"])

    with pytest.raises(CLIError, match="not available"):
        args.handler(args, StubClient(get=ServerError("HTTP 501 from Conductor", 501)))
