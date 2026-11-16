"""Tests for src.server.services.agent_scanner.

Every observation command goes through an injected runner, so these tests
exercise the parsers and the scan orchestration against fixtures: no real
subprocess, no real worktree and no real log of the operator's machine is
touched.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.server.models.agent import AgentRole, AgentStatus
from src.server.services.agent_scanner import (
    AgentLogNotFound,
    CommandError,
    ProcessFacts,
    ScanSettings,
    _associate_processes,
    assign_log_files,
    balance_state,
    detect_status,
    infer_role,
    log_has_error,
    model_for_command,
    parse_branch,
    parse_commit_count,
    parse_elapsed,
    parse_lsof,
    parse_ps_rows,
    parse_test_state,
    parse_worktree_paths,
    provider_for_command,
    read_agent_logs,
    scan_agents,
    valid_agent_id,
)

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


class FakeRunner:
    """Answers the scanner's bounded commands from in-memory fixtures."""

    def __init__(
        self,
        *,
        ps: str = "",
        worktrees: dict[str, str] | None = None,
        lsof: dict[int, str] | None = None,
        git: dict[tuple[str, tuple[str, ...]], str] | None = None,
    ) -> None:
        self.ps = ps
        self.worktrees = worktrees or {}
        self.lsof = lsof or {}
        self.git = git or {}
        self.calls: list[list[str]] = []

    async def __call__(self, argv, timeout: float) -> str:
        self.calls.append([str(item) for item in argv])
        if argv[0] == "ps":
            return self.ps
        if argv[0] == "lsof":
            return self.lsof.get(int(argv[2]), "")
        if argv[0] == "git":
            path, args = str(argv[2]), tuple(str(item) for item in argv[3:])
            if args == ("worktree", "list", "--porcelain"):
                if path not in self.worktrees:
                    raise CommandError(f"{path} is not a worktree")
                return self.worktrees[path]
            try:
                return self.git[(path, args)]
            except KeyError:
                raise CommandError(f"git {args} failed in {path}") from None
        raise CommandError(f"unexpected command {argv[0]}")


def _settings(root: Path, **overrides) -> ScanSettings:
    base = dict(
        root=root,
        logs_dir=root / "logs",
        prompts_dir=root / "prompts",
        stall_secs=300,
        base_ref="main",
        log_tail_bytes=4096,
        max_worktrees=64,
        command_timeout=5.0,
    )
    base.update(overrides)
    return ScanSettings(**base)


def _touch(path: Path, *, mtime: datetime, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = mtime.timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def fleet(tmp_path):
    """A three-worktree fleet with one live, one stalled and one dead agent."""
    root = tmp_path / "wt"
    names = ("alpha", "beta", "gamma")
    for name in names:
        (root / name).mkdir(parents=True)
    logs = root / "logs"
    logs.mkdir()
    _touch(logs / "alpha.log", mtime=NOW - timedelta(seconds=30), text="starting\n")
    _touch(logs / "beta.log", mtime=NOW - timedelta(hours=2), text="starting\n")
    _touch(logs / "gamma.log", mtime=NOW - timedelta(seconds=90), text="starting\n")
    _touch(
        root / "prompts" / "alpha.txt",
        mtime=NOW,
        text="Write a concise implementation plan for the fleet dashboard",
    )
    # Any worktree of the repository answers `worktree list` with all of them.
    listing = "".join(
        f"worktree {root / name}\nHEAD deadbeef\nbranch refs/heads/feat/{name}\n\n"
        for name in names
    )
    worktrees = {str(root / name): listing for name in names}
    git = {
        (str(root / name), ("rev-parse", "--abbrev-ref", "HEAD")): f"feat/{name}\n"
        for name in names
    }
    git.update(
        {
            (str(root / name), ("rev-list", "--count", "main..HEAD")): str(index)
            for index, name in enumerate(names, start=1)
        }
    )
    runner = FakeRunner(
        ps=(
            "  101 00:12:30 /opt/homebrew/bin/codex exec -C %s/alpha - < p\n"
            "  202 01:02:03 /usr/local/bin/kimi -p do the thing\n"
            "  303 00:00:09 /usr/bin/grep -i something\n"
        )
        % root,
        worktrees=worktrees,
        lsof={
            101: "p101\nfcwd\nn%s/alpha\nftxt\nn/opt/homebrew/bin/codex\n" % root,
            202: (
                "p202\nfcwd\nn/private/tmp\nf1\nn%s/logs/beta.log\n" % root
            ),
        },
        git=git,
    )
    return root, runner


def _patch_headroom(states):
    return patch(
        "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
        new=AsyncMock(return_value=states),
    )


class TestParsers:
    def test_ps_rows_keep_only_pid_etime_command(self):
        rows = parse_ps_rows("  101 00:12:30 codex exec\nbad line\n202 01:00 grep\n")
        assert [(row.pid, row.elapsed, row.command) for row in rows] == [
            (101, "00:12:30", "codex exec"),
            (202, "01:00", "grep"),
        ]

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("00:05", 5),
            ("12:34", 754),
            ("01:02:03", 3723),
            ("01-02:03:04", 93784),
            ("  3:04:05 ", 11045),
            ("7", None),
            ("", None),
            ("aa:bb", None),
            ("99:99", None),
        ],
    )
    def test_elapsed_formats(self, value, expected):
        assert parse_elapsed(value) == expected

    def test_worktree_paths(self):
        output = (
            "worktree /a/one\nHEAD x\n\nworktree /a/two\nbranch refs/heads/main\n"
        )
        assert parse_worktree_paths(output) == ["/a/one", "/a/two"]

    def test_branch_of_detached_head_is_empty(self):
        assert parse_branch("HEAD\n") == ""
        assert parse_branch("feat/agent-fleet\n") == "feat/agent-fleet"
        assert parse_branch("") == ""

    def test_commit_count(self):
        assert parse_commit_count("7\n") == 7
        assert parse_commit_count("fatal: bad revision\n") is None
        assert parse_commit_count("") is None

    def test_lsof_splits_cwd_from_open_files(self):
        cwd, paths = parse_lsof("p202\nfcwd\nn/private/tmp\nf1\nn/x/logs/beta.log\nf2\nn/dev/null\n")
        assert cwd == "/private/tmp"
        assert paths == ("/x/logs/beta.log", "/dev/null")

    def test_provider_and_model_from_command(self):
        assert provider_for_command("/opt/homebrew/bin/codex exec -C /wt/alpha") == "codex"
        assert provider_for_command("zcode-cli -p x --model GLM-5.3") == "zcode"
        assert provider_for_command("dsh --profile headless") == "deepseek-dsh"
        assert provider_for_command("mmx text chat") == "minimax"
        assert provider_for_command("step -p --no-session") == "stepfun"
        assert provider_for_command("mimo run --yolo") == "mimo"
        assert provider_for_command("grok --prompt-file p") == "grok"
        assert provider_for_command("/usr/bin/grep -i x") is None
        assert model_for_command("zcode-cli -p x --model GLM-5.3 --provider bigmodel") == "GLM-5.3"
        assert model_for_command("claude -p --model haiku do it") == "haiku"
        assert model_for_command("codex exec -c model_reasoning_effort=ultra") == ""

    def test_balance_state_projection(self):
        assert balance_state("codex", {"codex": "available"}) == "ok"
        assert balance_state("deepseek-dsh", {"deepseek-dsh": "low"}) == "low"
        assert balance_state("deepseek-dsh", {"deepseek-dsh": "exhausted"}) == "low"
        assert balance_state("codex", {"codex": "unknown"}) == "unknown"
        assert balance_state("", {"codex": "available"}) == "unknown"
        assert balance_state("kimi", {}) == "unknown"

    def test_valid_agent_id_rejects_paths(self):
        assert valid_agent_id("agent-fleet")
        assert valid_agent_id("port_codexbar.1")
        assert not valid_agent_id("../etc/passwd")
        assert not valid_agent_id("logs/alpha")
        assert not valid_agent_id("")
        assert not valid_agent_id("a" * 80)


class TestStatusDetection:
    def test_dead_process_is_done_even_with_a_fresh_log(self):
        assert (
            detect_status(
                alive=False,
                log_present=True,
                log_age_secs=1,
                stall_secs=300,
                error_seen=False,
            )
            is AgentStatus.DONE
        )

    def test_live_process_without_a_log_is_unknown(self):
        assert (
            detect_status(
                alive=True,
                log_present=False,
                log_age_secs=None,
                stall_secs=300,
                error_seen=False,
            )
            is AgentStatus.UNKNOWN
        )

    def test_error_marker_outranks_a_fresh_log(self):
        assert (
            detect_status(
                alive=True,
                log_present=True,
                log_age_secs=1,
                stall_secs=300,
                error_seen=True,
            )
            is AgentStatus.ERROR
        )

    def test_cold_log_is_stalled(self):
        assert (
            detect_status(
                alive=True,
                log_present=True,
                log_age_secs=301,
                stall_secs=300,
                error_seen=False,
            )
            is AgentStatus.STALLED
        )

    def test_fresh_log_is_running(self):
        assert (
            detect_status(
                alive=True,
                log_present=True,
                log_age_secs=299,
                stall_secs=300,
                error_seen=False,
            )
            is AgentStatus.RUNNING
        )

    def test_an_unreadable_process_table_is_unknown_not_done(self):
        # The scan could not read the process table, so "no process" is not
        # evidence the agent finished. Done is what a dead process reads as,
        # and a failed `ps` must not be able to produce it.
        assert (
            detect_status(
                alive=False,
                log_present=True,
                log_age_secs=1.0,
                stall_secs=300,
                error_seen=False,
                processes_known=False,
            )
            is AgentStatus.UNKNOWN
        )
        assert (
            detect_status(
                alive=False,
                log_present=True,
                log_age_secs=1.0,
                stall_secs=300,
                error_seen=False,
            )
            is AgentStatus.DONE
        )


class TestLogInterpretation:
    def test_error_markers(self):
        assert log_has_error("Traceback (most recent call last):\n  File x")
        assert log_has_error("FAILED tests/unit/test_x.py::test_a - assert 1 == 2")
        assert log_has_error("ERROR tests/unit/test_x.py::test_b")
        assert log_has_error("RuntimeError: boom")
        assert log_has_error("panic: runtime error")
        assert log_has_error("sh: codex: command not found")
        assert not log_has_error("12 passed in 3.45s")
        assert not log_has_error("handled the error gracefully, 0 failures")
        assert not log_has_error("")

    def test_test_state_from_terminal_summary(self):
        text = (
            "============================= test session starts ==============================\n"
            "FAILED tests/unit/test_x.py::test_a - assert 1 == 2\n"
            "================= 2 failed, 12 passed, 1 skipped in 3.45s =================\n"
        )
        assert parse_test_state(text) == "12 passed, 2 failed, 1 skipped"

    def test_test_state_ignores_prose_and_partial_counts(self):
        assert parse_test_state("1 passed while I was reading the docs") is None
        assert parse_test_state("no tests ran") is None

    def test_role_inference(self):
        assert infer_role("Code review only, do not edit files") is AgentRole.REVIEW
        assert infer_role("Write a concise implementation plan, no code") is AgentRole.PLAN
        assert infer_role("Research the memory layers") is AgentRole.RESEARCH
        assert infer_role("Implement the fleet dashboard") is AgentRole.DEV
        assert infer_role("") is AgentRole.DEV


class TestLogAssignment:
    def test_exact_log_wins_and_longest_name_claims_a_suffix(self, tmp_path):
        logs = tmp_path / "logs"
        _touch(logs / "port-codexbar.log", mtime=NOW)
        _touch(logs / "port-codexbar-snapshot.log", mtime=NOW)
        _touch(logs / "port-codexbar-run2.log", mtime=NOW - timedelta(hours=1))
        assigned = assign_log_files(
            ["port-codexbar", "port-codexbar-snapshot"], logs
        )
        assert assigned["port-codexbar"].name == "port-codexbar.log"
        assert assigned["port-codexbar-snapshot"].name == "port-codexbar-snapshot.log"

    def test_suffixed_log_falls_to_the_only_matching_agent(self, tmp_path):
        logs = tmp_path / "logs"
        _touch(logs / "agent-fleet.log", mtime=NOW)
        _touch(logs / "agent-fleet-step.log", mtime=NOW - timedelta(minutes=5))
        assigned = assign_log_files(["agent-fleet"], logs)
        assert assigned["agent-fleet"].name == "agent-fleet.log"

    def test_newest_suffix_is_chosen_when_no_exact_log(self, tmp_path):
        logs = tmp_path / "logs"
        _touch(logs / "hivemind-r2-step.log", mtime=NOW - timedelta(minutes=30))
        _touch(logs / "hivemind-r2-codex.log", mtime=NOW - timedelta(minutes=1))
        assigned = assign_log_files(["hivemind-r2"], logs)
        assert assigned["hivemind-r2"].name == "hivemind-r2-codex.log"

    def test_missing_logs_directory_is_not_an_error(self, tmp_path):
        assert assign_log_files(["alpha"], tmp_path / "nope") == {}


class TestScanAgents:
    @pytest.mark.asyncio
    async def test_scans_every_worktree_with_status_and_metadata(self, fleet):
        root, runner = fleet
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        by_id = {agent.id: agent for agent in agents}
        assert set(by_id) == {"alpha", "beta", "gamma"}

        alpha = by_id["alpha"]
        assert alpha.status is AgentStatus.RUNNING
        assert alpha.pid == 101
        assert alpha.provider == "codex"
        assert alpha.model == "codex"
        assert alpha.branch == "feat/alpha"
        assert alpha.commits_ahead == 1
        assert alpha.role is AgentRole.PLAN
        assert alpha.elapsed_secs == 750
        assert alpha.last_activity_ts == NOW - timedelta(seconds=30)
        assert alpha.log_file == "alpha.log"

        # beta's process was matched through the log file it holds open, and its
        # log is two hours cold, so it reads stalled.
        beta = by_id["beta"]
        assert beta.status is AgentStatus.STALLED
        assert beta.pid == 202
        assert beta.provider == "kimi"

        # gamma has a fresh log but no process.
        gamma = by_id["gamma"]
        assert gamma.status is AgentStatus.DONE
        assert gamma.pid is None
        assert gamma.commits_ahead == 3

    @pytest.mark.asyncio
    async def test_live_process_without_a_log_is_unknown(self, tmp_path):
        root = tmp_path / "wt"
        (root / "orphan").mkdir(parents=True)
        runner = FakeRunner(
            worktrees={str(root / "orphan"): f"worktree {root / 'orphan'}\nHEAD x\n\n"},
            git={
                (str(root / "orphan"), ("rev-parse", "--abbrev-ref", "HEAD")): "feat/orphan\n",
            },
            ps="  909 00:01:00 mimo run --yolo\n",
            lsof={909: "p909\nfcwd\nn%s/orphan\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert len(agents) == 1
        assert agents[0].status is AgentStatus.UNKNOWN
        assert agents[0].log_tail == ""
        assert agents[0].log_file == ""

    @pytest.mark.asyncio
    async def test_error_marker_in_log_wins(self, tmp_path):
        root = tmp_path / "wt"
        (root / "broken").mkdir(parents=True)
        _touch(
            root / "logs" / "broken.log",
            mtime=NOW - timedelta(seconds=5),
            text="starting\nTraceback (most recent call last):\n  File 'x.py'\n",
        )
        runner = FakeRunner(
            worktrees={str(root / "broken"): f"worktree {root / 'broken'}\nHEAD x\n\n"},
            ps="  11 00:02:00 codex exec\n",
            lsof={11: "p11\nfcwd\nn%s/broken\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert agents[0].status is AgentStatus.ERROR

    @pytest.mark.asyncio
    async def test_status_and_test_counts_come_from_bounded_raw_tail(self, tmp_path):
        root = tmp_path / "wt"
        (root / "broken").mkdir(parents=True)
        _touch(
            root / "logs" / "broken.log",
            mtime=NOW - timedelta(seconds=5),
            text=(
                "ERROR\n"
                "12 passed, 1 failed in 3.45s\n"
                "-----END RSA PRIVATE KEY-----\n"
            ),
        )
        runner = FakeRunner(
            worktrees={str(root / "broken"): f"worktree {root / 'broken'}\nHEAD x\n\n"},
            ps="  11 00:02:00 codex exec\n",
            lsof={11: "p11\nfcwd\nn%s/broken\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert agents[0].status is AgentStatus.ERROR
        assert agents[0].tests_state == "12 passed, 1 failed"
        assert "[REDACTED]" in agents[0].log_tail

    @pytest.mark.asyncio
    async def test_a_failure_marker_survives_redaction(self, tmp_path):
        # The status is judged from the redacted tail, so a redaction rule that
        # eats a failure marker keeps a broken agent reading running: a lone
        # `FAILED` line above a stray END marker was read as key material and
        # replaced by `[REDACTED]`, and the marker never reached the check.
        root = tmp_path / "wt"
        (root / "broken").mkdir(parents=True)
        _touch(
            root / "logs" / "broken.log",
            mtime=NOW - timedelta(seconds=5),
            text="starting\nFAILED\n-----END RSA PRIVATE KEY-----\n",
        )
        runner = FakeRunner(
            worktrees={str(root / "broken"): f"worktree {root / 'broken'}\nHEAD x\n\n"},
            ps="  11 00:02:00 codex exec\n",
            lsof={11: "p11\nfcwd\nn%s/broken\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert agents[0].status is AgentStatus.ERROR
        assert "FAILED" in agents[0].log_tail

    @pytest.mark.asyncio
    async def test_the_longest_worktree_path_claims_the_process(self, tmp_path):
        # A worktree path is a prefix of its siblings' — `dev` sits inside
        # `dev-review`'s path — so a substring match taken in worktree-list
        # order handed `dev-review`'s agent to `dev`, and the review's own
        # worktree, left with no process to claim, read done.
        root = tmp_path / "wt"
        for name in ("dev", "dev-review"):
            (root / name).mkdir(parents=True)
        listing = "".join(
            f"worktree {root / name}\nHEAD x\n\n" for name in ("dev", "dev-review")
        )
        runner = FakeRunner(
            worktrees={str(root / name): listing for name in ("dev", "dev-review")},
            ps=(
                f"  11 00:02:00 /usr/local/bin/codex exec -C {root}/dev-review\n"
                f"  22 00:03:00 /usr/local/bin/codex exec -C {root}/dev\n"
            ),
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        by_id = {agent.id: agent for agent in agents}
        assert by_id["dev-review"].pid == 11
        assert by_id["dev"].pid == 22

    @pytest.mark.asyncio
    async def test_a_cwd_the_kernel_resolved_matches_its_worktree(self, tmp_path):
        # `git worktree list` reports the path a worktree was created with and
        # `lsof` the path the kernel resolved it to, so a cwd spelled through a
        # symlink missed the worktree and fell through to the same wrong
        # substring owner. Both spellings of a worktree answer for it now.
        root = tmp_path / "wt"
        for name in ("dev", "dev-review"):
            (root / name).mkdir(parents=True)
        resolved = tmp_path / "resolved"
        resolved.symlink_to(root)
        listing = "".join(
            f"worktree {root / name}\nHEAD x\n\n" for name in ("dev", "dev-review")
        )
        runner = FakeRunner(
            worktrees={str(root / name): listing for name in ("dev", "dev-review")},
            ps=(
                f"  11 00:02:00 /usr/local/bin/codex exec -C {root}/dev-review\n"
                f"  22 00:03:00 /usr/local/bin/codex exec -C {root}/dev\n"
            ),
            lsof={
                11: f"p11\nfcwd\nn{resolved}/dev-review\n",
                22: f"p22\nfcwd\nn{resolved}/dev\n",
            },
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        by_id = {agent.id: agent for agent in agents}
        assert by_id["dev-review"].pid == 11
        assert by_id["dev"].pid == 22

    @pytest.mark.asyncio
    async def test_log_tail_is_sanitized_and_test_state_parsed(self, tmp_path):
        root = tmp_path / "wt"
        (root / "leaky").mkdir(parents=True)
        _touch(
            root / "logs" / "leaky.log",
            mtime=NOW - timedelta(seconds=5),
            text=(
                "OPENAI_API_KEY=sk-" "ant-api03-ABCDEFGHIJKLMNOPQRSTUV\n"
                "=========== 3 failed, 9 passed in 12.5s ===========\n"
            ),
        )
        runner = FakeRunner(
            worktrees={str(root / "leaky"): f"worktree {root / 'leaky'}\nHEAD x\n\n"},
            ps="  12 00:02:00 codex exec\n",
            lsof={12: "p12\nfcwd\nn%s/leaky\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert "sk-" "ant-api03" not in agents[0].log_tail
        assert "[REDACTED]" in agents[0].log_tail
        assert agents[0].tests_state == "9 passed, 3 failed"

    @pytest.mark.asyncio
    async def test_a_key_body_cut_by_the_tail_window_is_redacted(self, tmp_path):
        # log_tail_bytes bounds the read from the end of the file, so a key
        # dumped above the window keeps only its END marker. The body in front
        # of it is the key, and a rule that needs the BEGIN marker too leaves
        # it in the clear.
        root = tmp_path / "wt"
        (root / "leaky").mkdir(parents=True)
        body = "MIIEabcPrivateKeyBody1234567890"
        _touch(
            root / "logs" / "leaky.log",
            mtime=NOW - timedelta(seconds=5),
            text=(
                "noise line\n" * 10
                + "-----BEGIN " "RSA PRIVATE KEY-----\n"
                + f"{body}\n"
                + "-----END RSA PRIVATE KEY-----\n"
                + "tail line\n"
            ),
        )
        runner = FakeRunner(
            worktrees={str(root / "leaky"): f"worktree {root / 'leaky'}\nHEAD x\n\n"},
            ps="  12 00:02:00 codex exec\n",
            lsof={12: "p12\nfcwd\nn%s/leaky\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(
                _settings(root, log_tail_bytes=64), now=NOW, run=runner
            )
        assert body not in agents[0].log_tail
        assert "-----END RSA PRIVATE KEY-----" not in agents[0].log_tail
        assert "[REDACTED]" in agents[0].log_tail
        assert "tail line" in agents[0].log_tail

    @pytest.mark.asyncio
    async def test_provider_balance_is_an_enum_only(self, fleet):
        root, runner = fleet
        with _patch_headroom({"codex": "available", "kimi": "low"}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        by_id = {agent.id: agent for agent in agents}
        assert by_id["alpha"].provider_balance == "ok"
        assert by_id["beta"].provider_balance == "low"
        assert by_id["gamma"].provider_balance == "unknown"

    @pytest.mark.asyncio
    async def test_snapshot_reader_failure_does_not_fail_the_scan(self, fleet):
        root, runner = fleet
        with patch(
            "src.server.services.native_runtime.usage_headroom.load_usage_headroom",
            new=AsyncMock(side_effect=OSError("snapshot gone")),
        ):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert all(agent.provider_balance == "unknown" for agent in agents)

    @pytest.mark.asyncio
    async def test_unreadable_root_returns_nothing(self, tmp_path):
        runner = FakeRunner()
        assert await scan_agents(_settings(tmp_path / "missing"), now=NOW, run=runner) == []
        assert runner.calls == []

    @pytest.mark.asyncio
    async def test_process_listing_failure_still_reports_the_fleet(self, fleet):
        root, _ = fleet
        base = _settings(root)
        names = ("alpha", "beta", "gamma")
        listing = "".join(
            f"worktree {root / name}\nHEAD x\n\n" for name in names
        )
        worktrees = {str(root / name): listing for name in names}
        git = {
            (str(root / name), ("rev-parse", "--abbrev-ref", "HEAD")): f"feat/{name}\n"
            for name in names
        }

        class PsLess(FakeRunner):
            async def __call__(self, argv, timeout):
                if argv[0] == "ps":
                    raise CommandError("ps unavailable")
                return await super().__call__(argv, timeout)

        runner = PsLess(worktrees=worktrees, git=git)
        with _patch_headroom({}):
            agents = await scan_agents(base, now=NOW, run=runner)
        # `ps` answering nothing is not the same as `ps` answering "no agent is
        # running": the first marks every row done and reports work that may
        # still be going as finished, so an unreadable table reads unknown.
        assert {agent.status for agent in agents} == {AgentStatus.UNKNOWN}
        assert [agent.id for agent in agents] == ["alpha", "beta", "gamma"]
        assert {agent.branch for agent in agents} == {
            "feat/alpha",
            "feat/beta",
            "feat/gamma",
        }
        assert all(agent.pid is None for agent in agents)

    @pytest.mark.asyncio
    async def test_role_comes_from_the_head_of_a_long_prompt(self, tmp_path):
        # A prompt opens with the role it was given. Reading its last 2 KiB
        # instead — which is what this did — loses those instructions on any
        # prompt a launcher appended history to, and every agent reads dev.
        root = tmp_path / "wt"
        (root / "alpha").mkdir(parents=True)
        (root / "prompts").mkdir()
        (root / "prompts" / "alpha.txt").write_text(
            "You are doing a code review of this branch. Read only, do not edit.\n"
            + "the worktree has a long history of commits behind it\n" * 300,
            encoding="utf-8",
        )
        runner = FakeRunner(
            worktrees={str(root / "alpha"): f"worktree {root / 'alpha'}\nHEAD x\n\n"},
            git={
                (str(root / "alpha"), ("rev-parse", "--abbrev-ref", "HEAD")): "feat/x\n"
            },
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root), now=NOW, run=runner)
        assert agents[0].role is AgentRole.REVIEW

    @pytest.mark.asyncio
    async def test_worktrees_are_capped(self, tmp_path):
        root = tmp_path / "wt"
        for index in range(5):
            (root / f"wt{index}").mkdir(parents=True)
        listing = "".join(
            f"worktree {root / f'wt{index}'}\nHEAD x\n\n" for index in range(5)
        )
        runner = FakeRunner(
            worktrees={str(root / f"wt{index}"): listing for index in range(5)}
        )
        with _patch_headroom({}):
            agents = await scan_agents(_settings(root, max_worktrees=2), now=NOW, run=runner)
        assert [agent.id for agent in agents] == ["wt0", "wt1"]

    @pytest.mark.asyncio
    async def test_the_process_cap_counts_agents_not_processes(self, tmp_path):
        # The cap bounds how many agents are inspected. Applied to the raw `ps`
        # rows instead, an agent that shares the machine with more unrelated
        # processes than the limit is dropped and reads as done.
        root = tmp_path / "wt"
        (root / "alpha").mkdir(parents=True)
        _touch(root / "logs" / "alpha.log", mtime=NOW - timedelta(seconds=10), text="starting\n")
        noise = "".join(
            f"  {1000 + index} 00:00:01 /usr/bin/grep -i pattern{index}\n" for index in range(5)
        )
        runner = FakeRunner(
            worktrees={str(root / "alpha"): f"worktree {root / 'alpha'}\nHEAD x\n\n"},
            git={(str(root / "alpha"), ("rev-parse", "--abbrev-ref", "HEAD")): "feat/alpha\n"},
            ps=noise + "  4242 00:02:00 codex exec\n",
            lsof={4242: "p4242\nfcwd\nn%s/alpha\n" % root},
        )
        with _patch_headroom({}):
            agents = await scan_agents(
                _settings(root, max_processes=2), now=NOW, run=runner
            )
        assert len(agents) == 1
        assert agents[0].pid == 4242
        assert agents[0].status is AgentStatus.RUNNING

    @pytest.mark.asyncio
    async def test_every_command_is_bounded(self, fleet):
        root, runner = fleet
        with _patch_headroom({}):
            await scan_agents(_settings(root), now=NOW, run=runner)
        assert runner.calls
        for argv, timeout in zip(runner.calls, [5.0] * len(runner.calls)):
            assert argv[0] in {"ps", "lsof", "git"}
            assert timeout == 5.0


class TestReadAgentLogs:
    @pytest.mark.asyncio
    async def test_returns_bounded_redacted_lines(self, tmp_path):
        root = tmp_path / "wt"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "alpha.log").write_text(
            "\n".join(
                [
                    "line one",
                    "Authorization: Bearer abcdef1234567890",
                    *[f"line {index}" for index in range(50)],
                ]
            ),
            encoding="utf-8",
        )
        result = await read_agent_logs(
            "alpha", _settings(root), max_lines=60, run=FakeRunner()
        )
        assert result.agent_id == "alpha"
        assert result.log_file == "alpha.log"
        assert result.truncated is False
        assert len(result.lines) == 52
        assert "Bearer abcdef1234567890" not in "\n".join(result.lines)
        assert "[REDACTED]" in "\n".join(result.lines)

    @pytest.mark.asyncio
    async def test_redacts_a_multiline_private_key(self, tmp_path):
        # A PEM block spans several lines, so the tail has to be redacted as one
        # text before it is split: a line-at-a-time pass never sees the
        # BEGIN/END pair that bounds it and returns the key body.
        root = tmp_path / "wt"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "alpha.log").write_text(
            "\n".join(
                [
                    "starting",
                    "-----BEGIN " "RSA PRIVATE KEY-----",
                    "MIIEabcPrivateKeyBody1234567890",
                    "-----END RSA PRIVATE KEY-----",
                    "done",
                ]
            ),
            encoding="utf-8",
        )
        result = await read_agent_logs(
            "alpha", _settings(root), max_lines=50, run=FakeRunner()
        )
        joined = "\n".join(result.lines)
        assert "MIIEabcPrivateKeyBody" not in joined
        assert "-----BEGIN " "RSA PRIVATE KEY-----" not in joined
        assert result.lines == ["starting", "[REDACTED]", "done"]

    @pytest.mark.asyncio
    async def test_redacts_a_body_whose_begin_marker_is_outside_the_window(self, tmp_path):
        # read_text_tail returns the last max_bytes of the file, so a window
        # that opens inside a PEM body holds only the END marker. The base64 in
        # front of it is the key all the same.
        root = tmp_path / "wt"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "alpha.log").write_text(
            "\n".join(
                [
                    "-----BEGIN " "RSA PRIVATE KEY-----",
                    "MIIEabcPrivateKeyBody1234567890",
                    "-----END RSA PRIVATE KEY-----",
                    "done",
                ]
            ),
            encoding="utf-8",
        )
        result = await read_agent_logs(
            "alpha", _settings(root), max_lines=50, max_bytes=40, run=FakeRunner()
        )
        joined = "\n".join(result.lines)
        assert "7890" not in joined
        assert "-----END RSA PRIVATE KEY-----" not in joined
        assert "done" in joined

    @pytest.mark.asyncio
    async def test_redacts_a_block_whose_end_marker_is_past_the_window(self, tmp_path):
        # A key still being written has no END marker, so the body runs to the
        # end of the tail and is redacted with it.
        root = tmp_path / "wt"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "alpha.log").write_text(
            "\n".join(
                [
                    "starting",
                    "-----BEGIN " "RSA PRIVATE KEY-----",
                    "MIIEabcPrivateKeyBody1234567890",
                ]
            ),
            encoding="utf-8",
        )
        result = await read_agent_logs(
            "alpha", _settings(root), max_lines=50, run=FakeRunner()
        )
        assert result.lines == ["starting", "[REDACTED]"]

    @pytest.mark.asyncio
    async def test_redacts_a_timestamp_prefixed_body_line(self, tmp_path):
        # A CLI writes a key body behind its own log prefix, so the line the
        # body sits on is not base64 and nothing else: the walk back from an
        # orphan END marker stopped at it and the body reached the dashboard.
        root = tmp_path / "wt"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "alpha.log").write_text(
            "\n".join(
                [
                    "2026-11-02T12:00:00.000Z INFO  dumping environment",
                    "-----BEGIN " "RSA PRIVATE KEY-----",
                    "2026-11-02T12:00:01.000Z INFO  MIIEabcPrivateKeyBody1234567890",
                    "-----END RSA PRIVATE KEY-----",
                    "done",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        # The window opens on the prefixed body line: the BEGIN marker that
        # would have bounded the block is outside it.
        result = await read_agent_logs(
            "alpha", _settings(root), max_lines=50, max_bytes=99, run=FakeRunner()
        )
        assert result.lines == ["[REDACTED]", "done"]

    @pytest.mark.asyncio
    async def test_the_whole_tail_pass_does_not_shorten_the_slice(self, tmp_path):
        # Redacting before the split must not apply a whole-text length cap: the
        # read is already bounded by max_bytes, and a shorter window would make
        # `max_lines` a lie about how much of the tail the caller can see.
        root = tmp_path / "wt"
        logs = root / "logs"
        logs.mkdir(parents=True)
        (logs / "alpha.log").write_text(
            "\n".join(f"line {index:04d} " + "x" * 40 for index in range(200)),
            encoding="utf-8",
        )
        result = await read_agent_logs(
            "alpha", _settings(root), max_lines=200, run=FakeRunner()
        )
        assert len(result.lines) == 200
        assert result.truncated is False
        assert result.lines[-1].startswith("line 0199")

    @pytest.mark.asyncio
    async def test_a_log_its_owner_claimed_is_not_handed_to_another_agent(
        self, tmp_path
    ):
        # The scan assigns every file at most once and lets the longest id
        # claim a suffixed one, so `worker-helper.log` belongs to
        # `worker-helper` when that is a worktree. Resolving with the requested
        # id alone cannot see the owner and hands `worker` the helper's log.
        root = tmp_path / "wt"
        for name in ("worker", "worker-helper"):
            (root / name).mkdir(parents=True)
        logs = root / "logs"
        logs.mkdir()
        (logs / "worker-helper.log").write_text("helper output\n", encoding="utf-8")
        listing = "".join(
            f"worktree {root / name}\nHEAD x\n\n"
            for name in ("worker", "worker-helper")
        )
        runner = FakeRunner(worktrees={str(root / "worker"): listing})
        with pytest.raises(AgentLogNotFound):
            await read_agent_logs("worker", _settings(root), run=runner)
        result = await read_agent_logs("worker-helper", _settings(root), run=runner)
        assert result.log_file == "worker-helper.log"
        assert result.lines == ["helper output"]

    @pytest.mark.asyncio
    async def test_a_suffixed_log_still_falls_to_the_only_agent_that_names_it(
        self, tmp_path
    ):
        # The other side of the same rule: a helper log whose owner is not a
        # worktree of its own is the agent's log, exactly as the scan assigns
        # it, so the log panel can still open it.
        root = tmp_path / "wt"
        (root / "worker").mkdir(parents=True)
        logs = root / "logs"
        logs.mkdir()
        (logs / "worker-helper.log").write_text("helper output\n", encoding="utf-8")
        listing = f"worktree {root / 'worker'}\nHEAD x\n\n"
        runner = FakeRunner(worktrees={str(root / "worker"): listing})
        result = await read_agent_logs("worker", _settings(root), run=runner)
        assert result.log_file == "worker-helper.log"
        assert result.lines == ["helper output"]

    @pytest.mark.asyncio
    async def test_rejects_traversal_and_missing_logs(self, tmp_path):
        root = tmp_path / "wt"
        (root / "logs").mkdir(parents=True)
        settings = _settings(root)
        for agent_id in ("../etc/passwd", "logs/alpha", ".", ""):
            with pytest.raises(AgentLogNotFound):
                await read_agent_logs(agent_id, settings, run=FakeRunner())
        with pytest.raises(AgentLogNotFound):
            await read_agent_logs("nobody", settings, run=FakeRunner())


class TestProcessAttribution:
    def test_command_matches_exact_worktree_after_longer_path(self, tmp_path):
        root = tmp_path / "fleet"
        short = root / "dev"
        long = root / "dev-review"
        settings = ScanSettings(
            root=root,
            logs_dir=root / "logs",
            prompts_dir=root / "prompts",
            stall_secs=300,
            base_ref="main",
            log_tail_bytes=4096,
            max_worktrees=64,
            command_timeout=5.0,
        )
        fact = ProcessFacts(
            pid=7,
            command=f"codex exec --cd {long}",
            elapsed_secs=1,
            cwd=None,
            open_paths=(),
        )
        assert _associate_processes([fact], [short, long], {}, settings) == {
            "dev-review": 7
        }

    def test_nested_cwd_and_explicit_cwd_option_choose_deepest_worktree(self, tmp_path):
        root = tmp_path / "fleet"
        parent = root / "workspace"
        nested = parent / "agent"
        child_cwd = nested / "src"
        child_cwd.mkdir(parents=True)
        parent_cwd = parent / "src"
        parent_cwd.mkdir()
        settings = ScanSettings(
            root=root,
            logs_dir=root / "logs",
            prompts_dir=root / "prompts",
            stall_secs=300,
            base_ref="main",
            log_tail_bytes=4096,
            max_worktrees=64,
            command_timeout=5.0,
        )
        cwd_fact = ProcessFacts(3, "codex", 1, str(parent_cwd), ())
        option_fact = ProcessFacts(4, f"codex --cd {child_cwd}", 1, None, ())
        assert _associate_processes([cwd_fact, option_fact], [parent, nested], {}, settings) == {
            "workspace": 3,
            "agent": 4,
        }

    def test_adjacent_worktree_name_is_not_a_path_match(self, tmp_path):
        root = tmp_path / "fleet"
        short = root / "agent"
        adjacent = root / "agent-helper"
        settings = ScanSettings(
            root=root,
            logs_dir=root / "logs",
            prompts_dir=root / "prompts",
            stall_secs=300,
            base_ref="main",
            log_tail_bytes=4096,
            max_worktrees=64,
            command_timeout=5.0,
        )
        fact = ProcessFacts(5, f"codex --cd {adjacent}", 1, None, ())
        assert _associate_processes([fact], [short], {}, settings) == {}

    def test_explicit_external_cwd_prevents_positional_fallback(self, tmp_path):
        root = tmp_path / "fleet"
        worktree = root / "dev"
        settings = ScanSettings(
            root=root,
            logs_dir=root / "logs",
            prompts_dir=root / "prompts",
            stall_secs=300,
            base_ref="main",
            log_tail_bytes=4096,
            max_worktrees=64,
            command_timeout=5.0,
        )
        fact = ProcessFacts(6, f"codex --cd {tmp_path / 'outside'} {worktree}", 1, None, ())
        assert _associate_processes([fact], [worktree], {}, settings) == {}

    def test_canonical_cwd_match_and_ambiguous_command(self, tmp_path):
        root = tmp_path / "fleet"
        short = root / "dev"
        long = root / "dev-review"
        long.mkdir(parents=True)
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        settings = ScanSettings(
            root=root,
            logs_dir=root / "logs",
            prompts_dir=root / "prompts",
            stall_secs=300,
            base_ref="main",
            log_tail_bytes=4096,
            max_worktrees=64,
            command_timeout=5.0,
        )
        cwd_fact = ProcessFacts(1, "codex", 1, str(alias / "fleet" / "dev-review"), ())
        assert _associate_processes([cwd_fact], [short, long], {}, settings) == {
            "dev-review": 1
        }
        ambiguous = ProcessFacts(2, f"codex {short} {long}", 1, None, ())
        assert _associate_processes([ambiguous], [short, long], {}, settings) == {}
