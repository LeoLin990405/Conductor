"""N13: host-side Git delivery invocations pin change-hiding configuration.

These are fake-repository regressions for the Docker Worker Git delivery
check. Every host-side invocation must pin ``core.fileMode`` and the other
settings a workspace can use to hide a change, scrub system/global config and
neutralise filter drivers. A repository whose config pulls in another file
through any include form Git accepts is refused before a status command runs,
and nothing is written into (or beside) the repository's Git directory.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server.services.autonomy import docker_worker_runtime
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    _filter_names_with,
    _run_git,
    capture_git_snapshot,
    git_command_runner,
    git_pinned_config_args,
    git_probe_env,
    run_isolated_git_mutation,
    run_isolated_git_readonly,
    safe_git_workspace_summary,
    uncommitted_git_summary,
)


_PINNED = {
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.untrackedCache=false",
    "core.excludesFile=/dev/null",
    "core.attributesFile=/dev/null",
    "core.fileMode=true",
    "core.symlinks=true",
    "core.autocrlf=false",
    "core.ignorecase=false",
    "core.checkStat=default",
    "core.trustctime=true",
    "core.ignoreStat=false",
    "core.useReplaceRefs=false",
    "diff.external=",
    "submodule.recurse=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "commit.gpgSign=false",
    "merge.verifySignatures=false",
    "diff.ignoreSubmodules=none",
    "diff.relative=false",
    "status.showUntrackedFiles=all",
    "status.relativePaths=true",
    "status.renames=true",
    "core.quotePath=true",
}


@pytest.fixture(autouse=True)
def _trusted_temp_repo_runner():
    """Keep filesystem fixtures local; production never uses this host runner."""

    def runner(argv, *, cwd, timeout, env):
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(runner):
        yield


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _record_git_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict]]:
    """Record every Git argv and environment a check runs with."""
    real_run = subprocess.run
    observed: list[tuple[list[str], dict[str, str]]] = []

    def record_git_commands(args, *call_args, **kwargs):
        if args and Path(args[0]).name == "git":
            observed.append((list(args), dict(kwargs.get("env") or {})))
        return real_run(args, *call_args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.run",
        record_git_commands,
    )
    # N56 streams Git output through Popen with a byte cap; record those
    # runs the same way.
    real_popen = subprocess.Popen

    def record_git_popen(args, *call_args, **kwargs):
        if args and Path(args[0]).name == "git":
            observed.append((list(args), dict(kwargs.get("env") or {})))
        return real_popen(args, *call_args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.Popen",
        record_git_popen,
    )
    return observed


def _case_sensitive(path: Path) -> bool:
    probe = path / ".case-probe-a"
    peer = path / ".case-probe-A"
    probe.write_text("a", encoding="utf-8")
    try:
        return not peer.exists()
    finally:
        probe.unlink(missing_ok=True)


def test_git_pinned_config_args_include_change_hiding_settings() -> None:
    # ``git_pinned_config_args`` is a flat ``(-c, key=value, ...)`` sequence.
    args = git_pinned_config_args()
    values = {args[index + 1] for index, part in enumerate(args) if part == "-c"}
    assert _PINNED <= values


def test_git_probe_env_disables_system_and_global_config() -> None:
    env = git_probe_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_every_host_check_pins_the_configuration_a_workspace_cannot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "pinned")
    (workspace / "untracked.txt").write_text("worker\n", encoding="utf-8")
    observed = _record_git_commands(monkeypatch)

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert observed
    for args, env in observed:
        assert _PINNED <= set(args), args
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_the_untracked_file_mode_is_fixed_against_the_workspace_config(
    tmp_path: Path,
) -> None:
    """A Worker cannot hide the files it wrote by configuring Git."""
    workspace = _repo(tmp_path / "untracked-mode")
    _git(workspace, "config", "status.showUntrackedFiles", "no")
    (workspace / "worker-file.txt").write_text("work\n", encoding="utf-8")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "worker-file.txt" in summary


def test_a_mode_change_is_reported_although_the_repository_disables_file_mode(
    tmp_path: Path,
) -> None:
    """A Worker cannot hide a chmod by turning the file-mode comparison off."""
    workspace = _repo(tmp_path / "file-mode")
    _git(workspace, "config", "core.fileMode", "false")
    (workspace / "tracked.txt").chmod(0o755)

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "tracked.txt" in summary


def test_a_symlink_replaced_by_a_regular_file_is_reported(tmp_path: Path) -> None:
    """``core.symlinks`` must not make a replaced link compare equal."""
    workspace = _repo(tmp_path / "symlinks")
    (workspace / "real.txt").write_text("target\n", encoding="utf-8")
    (workspace / "link.txt").symlink_to("real.txt")
    _git(workspace, "add", "real.txt", "link.txt")
    _git(workspace, "commit", "-q", "-m", "link")
    _git(workspace, "config", "core.symlinks", "false")
    (workspace / "link.txt").unlink()
    (workspace / "link.txt").write_text("real.txt", encoding="utf-8")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "link.txt" in summary


def test_a_case_only_rename_is_reported(tmp_path: Path) -> None:
    """``core.ignorecase`` must not make a renamed file compare equal."""
    if not _case_sensitive(tmp_path):
        pytest.skip("filesystem is case-insensitive; case-only rename is invisible")
    workspace = _repo(tmp_path / "case-rename")
    _git(workspace, "config", "core.ignorecase", "true")
    (workspace / "tracked.txt").rename(workspace / "Tracked.txt")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "tracked.txt" in summary.lower()


def test_a_global_ignore_file_cannot_hide_an_untracked_file(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "global-ignore")
    ignore = tmp_path / "global-ignore-file"
    ignore.write_text("hidden.txt\n", encoding="utf-8")
    _git(workspace, "config", "core.excludesFile", str(ignore))
    (workspace / "hidden.txt").write_text("work\n", encoding="utf-8")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "hidden.txt" in summary


def test_autocrlf_cannot_hide_a_line_ending_change(tmp_path: Path) -> None:
    """``core.autocrlf`` must not rewrite the worktree before comparison."""
    workspace = _repo(tmp_path / "autocrlf")
    _git(workspace, "config", "core.autocrlf", "true")
    (workspace / "tracked.txt").write_bytes(b"baseline\r\n")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "tracked.txt" in summary


def test_host_probe_overrides_filter_clean_smudge_and_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every named filter driver is emptied so Git cannot run it."""
    workspace = _repo(tmp_path / "filters")
    _git(workspace, "config", "filter.evil.clean", "sed 's/.*/clean/'")
    _git(workspace, "config", "filter.evil.smudge", "sed 's/.*/smudge/'")
    _git(workspace, "config", "filter.evil.process", "false")
    _git(workspace, "config", "filter.evil.required", "true")
    (workspace / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
    observed = _record_git_commands(monkeypatch)

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "tracked.txt" in summary
    # Filter overrides travel through GIT_CONFIG_KEY_*/VALUE_* so a -c list
    # cannot be outranked by an include that redefines the same filter.
    filter_keys = {
        value
        for _args, env in observed
        for key, value in env.items()
        if key.startswith("GIT_CONFIG_KEY_") and value.startswith("filter.evil.")
    }
    assert "filter.evil.clean" in filter_keys
    assert "filter.evil.smudge" in filter_keys
    assert "filter.evil.process" in filter_keys
    assert "filter.evil.required" in filter_keys


_UNKNOWN = "Git workspace status is unknown"


def _append_config(workspace: Path, text: str) -> None:
    config = workspace / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + text, encoding="utf-8")


def _hiding_include(tmp_path: Path) -> Path:
    """An included file that would hide untracked files and a mode change."""
    include = tmp_path / "hiding.conf"
    include.write_text(
        "[status]\n\tshowUntrackedFiles = no\n[core]\n\tfileMode = false\n",
        encoding="utf-8",
    )
    return include


def _include_forms(workspace: Path, include: Path) -> dict[str, str]:
    git_dir = (workspace / ".git").resolve()
    return {
        "include.path": f"\n[include]\n\tpath = {include}\n",
        "capitalized": f"\n[Include]\n\tpath = {include}\n",
        "trailing-comment": f"\n[include] # note\n\tpath = {include}\n",
        "includeIf-gitdir": f'\n[includeIf "gitdir:{git_dir}"]\n\tpath = {include}\n',
        "includeIf-gitdir-i": f'\n[includeIf "gitdir/i:{git_dir}"]\n\tpath = {include}\n',
        "includeIf-onbranch": f'\n[includeIf "onbranch:*"]\n\tpath = {include}\n',
        "includeIf-hasconfig": (
            f'\n[includeIf "hasconfig:remote.*.url:example.invalid"]\n\tpath = {include}\n'
        ),
        "IncludeIf-mixed-case": f'\n[IncludeIF "gitdir:{git_dir}"]\n\tpath = {include}\n',
    }


@pytest.mark.parametrize(
    "form",
    [
        "include.path",
        "capitalized",
        "trailing-comment",
        "includeIf-gitdir",
        "includeIf-gitdir-i",
        "includeIf-onbranch",
        "includeIf-hasconfig",
        "IncludeIf-mixed-case",
    ],
)
def test_every_include_form_git_accepts_is_refused_before_status_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """No include spelling Git honours can make a dirty tree look clean.

    Git's own parser lists the keys, so ``[Include]``, ``[include] # note``
    and every ``includeIf`` condition are seen exactly as Git would load
    them, and the check stops at "unknown" before any status command.
    """
    workspace = _repo(tmp_path / "include-form")
    _append_config(workspace, _include_forms(workspace, _hiding_include(tmp_path))[form])
    (workspace / "hidden.txt").write_text("work\n", encoding="utf-8")
    (workspace / "tracked.txt").chmod(0o755)
    observed = _record_git_commands(monkeypatch)

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None and summary.startswith(_UNKNOWN), summary
    assert "includes other files" in summary
    assert observed
    assert all("config" in args for args, _env in observed)
    assert not any("status" in args or "rev-parse" in args for args, _env in observed)


def test_an_include_in_a_linked_worktree_config_is_refused(tmp_path: Path) -> None:
    """``config.worktree`` is a config file Git reads, so it is gated too."""
    workspace = _repo(tmp_path / "worktree-config")
    _git(workspace, "config", "extensions.worktreeConfig", "true")
    linked = tmp_path / "worktree-config-linked"
    _git(workspace, "worktree", "add", "-q", "-b", "linked", str(linked))
    include = _hiding_include(tmp_path)
    _git(linked, "config", "--worktree", "include.path", str(include))
    (linked / "hidden.txt").write_text("work\n", encoding="utf-8")

    summary = uncommitted_git_summary(str(linked))

    assert summary is not None and summary.startswith(_UNKNOWN), summary


def test_an_included_filter_driver_never_runs_on_any_read_only_path(
    tmp_path: Path,
) -> None:
    """Marker-file proof for every read-only entry point, not only the summary."""
    workspace = _repo(tmp_path / "included-filter")
    marker = tmp_path / "included-filter-ran"
    script = tmp_path / "included-filter.sh"
    script.write_text(
        f"#!/bin/sh\nprintf ran >> {marker}\nsed 's/.*/baseline/'\n", encoding="utf-8"
    )
    script.chmod(0o700)
    include = tmp_path / "included-filter.conf"
    include.write_text(
        '[filter "evil"]\n'
        f"\tclean = {script}\n\tsmudge = {script}\n\tprocess = {script}\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    (workspace / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    _git(workspace, "config", "include.path", str(include))
    (workspace / "tracked.txt").write_text("changed-by-worker\n", encoding="utf-8")
    marker.unlink(missing_ok=True)

    summary = uncommitted_git_summary(str(workspace))
    assert summary is not None and summary.startswith(_UNKNOWN)
    with pytest.raises(GitDeliveryError):
        capture_git_snapshot(str(workspace))
    for command in (
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"),
        ("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"),
        ("ls-files", "--stage", "-z"),
    ):
        with pytest.raises(GitDeliveryError):
            run_isolated_git_readonly(str(workspace), command)
    baseline = SimpleNamespace(root_id=None, gitdir_id=None, entries={})
    assert safe_git_workspace_summary(str(workspace), baseline) is not None

    assert not marker.exists()


def test_filter_discovery_names_drivers_that_only_an_include_defines(
    tmp_path: Path,
) -> None:
    """``--includes`` discovery: an include added after the gate is still overridden.

    The include gate is bypassed here to model a config that changed between
    the gate and the command; the driver the include defines must still be in
    the override list.
    """
    workspace = _repo(tmp_path / "discovery")
    include = tmp_path / "discovery.conf"
    include.write_text('[filter "late"]\n\tclean = false\n', encoding="utf-8")
    _git(workspace, "config", "include.path", str(include))

    def run(args, timeout):
        if "--no-includes" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return _run_git(str(workspace), list(args), timeout=timeout)

    assert "late" in _filter_names_with(run, str(workspace)).filters


def _tree_state(root: Path) -> dict[str, tuple[int, int, bytes]]:
    state = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = os.readlink(path).encode() if path.is_symlink() else (
            path.read_bytes() if path.is_file() else b""
        )
        state[str(path.relative_to(root))] = (info.st_mode, info.st_size, content)
    return state


def test_a_check_writes_nothing_into_or_through_the_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No overlay or copy is written on the host, so a planted symlink is inert.

    Earlier code wrote an overlay under ``.git``; a Worker could pre-plant
    ``.git/conductor-probe*/config`` as a symlink to a host file. Now the
    check writes nothing there at all, and the outside file is unchanged.
    """
    workspace = _repo(tmp_path / "no-writes")
    outside = tmp_path / "outside-host-file"
    outside.write_text("trusted-original\n", encoding="utf-8")
    for name in ("conductor-probe", "conductor-probe-planted"):
        planted = workspace / ".git" / name
        planted.mkdir()
        (planted / "config").symlink_to(outside)
        (planted / "HEAD").symlink_to(outside)
    (workspace / "dirty.txt").write_text("work\n", encoding="utf-8")
    before = _tree_state(workspace / ".git")
    during: list[dict[str, tuple[int, int, bytes]]] = []
    real_run = subprocess.run

    def snapshot_while_git_runs(args, *call_args, **kwargs):
        if args and Path(args[0]).name == "git":
            during.append(_tree_state(workspace / ".git"))
        return real_run(args, *call_args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.run",
        snapshot_while_git_runs,
    )

    summary = uncommitted_git_summary(str(workspace))
    run_isolated_git_readonly(str(workspace), ("worktree", "list", "--porcelain"))

    assert summary is not None and "dirty.txt" in summary
    assert outside.read_text(encoding="utf-8") == "trusted-original\n"
    assert during and all(state == before for state in during)
    assert _tree_state(workspace / ".git") == before


def test_host_checks_run_against_the_real_git_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``--git-dir``/``--work-tree`` redirection, so paths Git reports are real."""
    workspace = _repo(tmp_path / "real-gitdir")
    observed = _record_git_commands(monkeypatch)

    listing = run_isolated_git_readonly(str(workspace), ("worktree", "list", "--porcelain"))
    identity = run_isolated_git_readonly(
        str(workspace),
        ("rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir"),
    )

    assert listing.returncode == 0
    first = listing.stdout.decode().splitlines()[0]
    assert Path(first.removeprefix("worktree ")).resolve() == workspace.resolve()
    gitdir, common = identity.stdout.decode().splitlines()
    assert Path(gitdir).resolve() == Path(common).resolve() == (workspace / ".git").resolve()
    assert all("--git-dir" not in args for args, _env in observed)


@contextlib.contextmanager
def _passthrough_mutation_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run mutations on the host but record the isolation grants they request."""
    grants: list[dict] = []
    monkeypatch.setenv("CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "isolation-state"))

    @contextlib.contextmanager
    def passthrough_isolation(**kwargs):
        grants.append(kwargs)
        yield object()

    def passthrough_prepare(argv, cwd, env, **_kwargs):
        return tuple(argv), dict(env), cwd

    monkeypatch.setattr("src.llms.native_cli.isolation.current_worker_isolation", lambda: None)
    monkeypatch.setattr("src.llms.native_cli.isolation.worker_isolation", passthrough_isolation)
    monkeypatch.setattr("src.llms.native_cli.isolation.prepare_process", passthrough_prepare)
    yield grants


def test_a_current_branch_rename_moves_the_real_head_and_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation runs on the real Git directory, so HEAD follows the rename."""
    repo = _repo(tmp_path / "rename")
    worktrees = tmp_path / "rename-worktrees"
    worktrees.mkdir()
    main = _git(repo, "branch", "--show-current")
    config_before = (repo / ".git" / "config").read_text(encoding="utf-8")

    with _passthrough_mutation_isolation(monkeypatch, tmp_path) as grants:
        result = run_isolated_git_mutation(
            str(repo),
            ["branch", "-m", main, "renamed-main"],
            repository_root=str(repo),
            worktrees_root=str(worktrees),
            timeout=10,
        )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "renamed-main"
    assert (repo / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/renamed-main\n"
    assert _git(repo, "rev-parse", "--verify", "refs/heads/renamed-main")
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{main}"],
        capture_output=True,
        check=False,
    ).returncode != 0
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == config_before
    assert [grant["runtime_write_paths"] for grant in grants] == [
        (str((repo / ".git").resolve()),)
    ]


def test_linked_worktree_mutations_write_only_the_common_dir_and_stay_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree added through the mutation path keeps working afterwards.

    The regression: an overlay ``--git-dir`` made ``worktree add`` record the
    overlay path in the new worktree's ``.git`` pointer; once the overlay was
    deleted every later check of that worktree failed. The new worktree's
    pointer must name the real common directory, a later read-only check and
    a current-branch rename there must work, and the only writable grant is
    the common directory, which contains the linked gitdir.
    """
    repo = _repo(tmp_path / "linked-repo")
    worktrees = tmp_path / "linked-worktrees"
    worktrees.mkdir()
    target = worktrees / "line-1"
    common = (repo / ".git").resolve()
    main = _git(repo, "branch", "--show-current")

    with _passthrough_mutation_isolation(monkeypatch, tmp_path) as grants:
        added = run_isolated_git_mutation(
            str(repo),
            ["worktree", "add", "-b", "line-1", str(target), main],
            repository_root=str(repo),
            worktrees_root=str(worktrees),
            timeout=10,
        )
        assert added.returncode == 0, added.stderr.decode("utf-8", "replace")
        pointer = (target / ".git").read_text(encoding="utf-8").strip()
        linked_gitdir = Path(pointer.removeprefix("gitdir: ")).resolve()
        assert linked_gitdir.parent == common / "worktrees"
        assert linked_gitdir.is_dir()

        status = run_isolated_git_readonly(
            str(target), ("status", "--porcelain", "--untracked-files=all")
        )
        assert status.returncode == 0
        (target / "worker.txt").write_text("work\n", encoding="utf-8")
        summary = uncommitted_git_summary(str(target))
        assert summary is not None and "worker.txt" in summary

        renamed = run_isolated_git_mutation(
            str(target),
            ["branch", "-m", "line-1", "line-1-renamed"],
            repository_root=str(repo),
            worktrees_root=str(worktrees),
            timeout=10,
        )
        assert renamed.returncode == 0, renamed.stderr.decode("utf-8", "replace")

    assert _git(target, "branch", "--show-current") == "line-1-renamed"
    assert (linked_gitdir / "HEAD").read_text(encoding="utf-8") == (
        "ref: refs/heads/line-1-renamed\n"
    )
    assert all(grant["runtime_write_paths"] == (str(common),) for grant in grants)
    assert linked_gitdir.is_relative_to(common)


def test_a_mutation_refuses_an_include_and_never_runs_its_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An include-only filter driver cannot run during a mutation either."""
    repo = _repo(tmp_path / "mutation-include")
    worktrees = tmp_path / "mutation-worktrees"
    worktrees.mkdir()
    marker = tmp_path / "include-filter-ran"
    script = tmp_path / "evil-filter.sh"
    script.write_text(f"#!/bin/sh\nprintf ran >> {marker}\ncat\n", encoding="utf-8")
    script.chmod(0o700)
    include = tmp_path / "mutation-include.conf"
    include.write_text(
        '[filter "evil"]\n'
        f"\tclean = {script}\n\tsmudge = {script}\n\tprocess = {script}\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    _git(repo, "config", "include.path", str(include))
    main = _git(repo, "branch", "--show-current")
    config_before = (repo / ".git" / "config").read_text(encoding="utf-8")
    head_before = (repo / ".git" / "HEAD").read_bytes()

    with _passthrough_mutation_isolation(monkeypatch, tmp_path) as grants:
        for command in (
            ["branch", "-m", main, "renamed-main"],
            ["worktree", "add", "-b", "line-x", str(worktrees / "line-x"), main],
        ):
            with pytest.raises(GitDeliveryError):
                run_isolated_git_mutation(
                    str(repo),
                    command,
                    repository_root=str(repo),
                    worktrees_root=str(worktrees),
                    timeout=10,
                )

    assert grants == []
    assert not marker.exists()
    assert not (worktrees / "line-x").exists()
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == config_before
    assert (repo / ".git" / "HEAD").read_bytes() == head_before


def test_a_mutation_overrides_every_configured_filter_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directly configured driver is emptied for the mutation's own Git run."""
    repo = _repo(tmp_path / "mutation-filter")
    worktrees = tmp_path / "mutation-filter-worktrees"
    worktrees.mkdir()
    marker = tmp_path / "direct-filter-ran"
    script = tmp_path / "direct-filter.sh"
    script.write_text(f"#!/bin/sh\nprintf ran >> {marker}\ncat\n", encoding="utf-8")
    script.chmod(0o700)
    for key in ("clean", "smudge", "process"):
        _git(repo, "config", f"filter.evil.{key}", str(script))
    _git(repo, "config", "filter.evil.required", "true")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    main = _git(repo, "branch", "--show-current")
    observed = _record_git_commands(monkeypatch)

    with _passthrough_mutation_isolation(monkeypatch, tmp_path):
        added = run_isolated_git_mutation(
            str(repo),
            ["worktree", "add", "-b", "line-f", str(worktrees / "line-f"), main],
            repository_root=str(repo),
            worktrees_root=str(worktrees),
            timeout=10,
        )

    assert added.returncode == 0, added.stderr.decode("utf-8", "replace")
    assert (worktrees / "line-f" / "tracked.txt").read_text(encoding="utf-8") == "baseline\n"
    assert not marker.exists()
    mutation_env = next(env for args, env in observed if "worktree" in args and "add" in args)
    overrides = {
        mutation_env[f"GIT_CONFIG_KEY_{index}"]: mutation_env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(mutation_env["GIT_CONFIG_COUNT"]))
    }
    assert overrides == {
        "filter.evil.clean": "",
        "filter.evil.smudge": "",
        "filter.evil.process": "",
        "filter.evil.required": "false",
    }


@pytest.mark.asyncio
async def test_container_version_probe_runs_the_production_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the docker exec is faked; the probe, runner and gate are real."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen: list[tuple[str, ...]] = []

    def fake_docker_sync(argv, *, timeout_seconds):
        seen.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, b"git version 2.45.0\n", b"")

    monkeypatch.setattr(docker_worker_runtime, "_run_docker_sync", fake_docker_sync)
    settings = SimpleNamespace(docker_bin="docker", user="1000:1000")

    assert await docker_worker_runtime._container_git_available(
        settings, "conductor-cloud-worker-test", workspace
    )

    assert len(seen) == 1
    command = list(seen[0])
    git_at = command.index("git", command.index("-i"))
    assert command[git_at:] == ["git", *git_pinned_config_args(), "--version"]
    assert _PINNED <= set(command[git_at:])
    env = dict(part.split("=", 1) for part in command[command.index("-i") + 1 : git_at])
    assert env == git_probe_env()
