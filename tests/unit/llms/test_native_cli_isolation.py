import json
import os
import shutil
import asyncio
import stat
from pathlib import Path

import pytest

from src.llms.native_cli.base import CliInvocation, NativeCliUnavailable
from src.llms.native_cli.isolation import (
    prepare_process,
    redact_diagnostic,
    read_workspace_policy,
    secure_private_write,
    worker_isolation,
)
from src.llms.native_cli.process import run_process


def test_workspace_environment_is_explicit_and_service_vars_are_reserved(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WORKER_SCOPED_TEST", "synthetic-credential")
    monkeypatch.setenv("PARENT_ONLY_TEST", "must-not-cross")
    work = tmp_path / "workspace"
    work.mkdir()
    with worker_isolation(
        workspace_id="workspace-a",
        root=tmp_path / "state",
        workdir=work,
        env_names={"WORKER_SCOPED_TEST", "PYTHONPATH", "CONDUCTOR_DAEMON_TOKEN"},
    ):
        _, env, _ = prepare_process(("/bin/true",), str(work), None)
    assert env is not None
    assert env["WORKER_SCOPED_TEST"] == "synthetic-credential"
    assert "PARENT_ONLY_TEST" not in env
    assert "PYTHONPATH" not in env
    assert "CONDUCTOR_DAEMON_TOKEN" not in env
    assert env["HOME"].startswith(str(tmp_path / "state"))


def test_redaction_removes_short_allowlisted_credentials(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    with worker_isolation(
        workspace_id="short",
        root=tmp_path / "state",
        workdir=work,
        env_names={"SHORT_TEST_SECRET"},
    ):
        assert (
            redact_diagnostic("credential=abc", {"SHORT_TEST_SECRET": "abc"})
            == "credential=[redacted]"
        )


def test_policy_requires_separate_roots_and_private_mode(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {"root": str(first), "env": []},
                "b": {"root": str(first), "env": []},
            }
        )
    )
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="overlap"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_policy_rejects_nested_roots_and_unsafe_mode(tmp_path):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {"root": str(parent), "env": []},
                "b": {"root": str(child), "env": []},
            }
        )
    )
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="overlap"):
        read_workspace_policy(policy, "a", tmp_path / "state")
    os.chmod(policy, 0o644)
    with pytest.raises(NativeCliUnavailable, match="0600"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_worker_home_rejects_symlink(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    state = tmp_path / "state"
    private = state / __import__("hashlib").sha256(b"a").hexdigest()
    private.mkdir(parents=True)
    os.chmod(state, 0o700)
    os.chmod(private, 0o700)
    (private / "home").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(NativeCliUnavailable, match="symlinks"):
        with worker_isolation(
            workspace_id="a", root=state, workdir=work, env_names=set()
        ):
            pass


def test_policy_missing_workspace_fails_closed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"other": {"root": str(workspace), "env": []}}))
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="no local isolation policy"):
        read_workspace_policy(policy, "missing", tmp_path / "state")


def test_zcode_does_not_read_ambient_key_and_private_write_rejects_symlink(
    tmp_path, monkeypatch
):
    from src.llms.native_cli.zcode import (
        _api_key,
        _personal_provider_path,
        write_personal_provider_config,
    )

    monkeypatch.setenv("NATIVE_CLI_ZCODE_API_KEY", "synthetic-zcode-key")
    work = tmp_path / "workspace"
    work.mkdir()
    with worker_isolation(
        workspace_id="a", root=tmp_path / "state", workdir=work, env_names=set()
    ):
        assert _api_key() is None
        target = _personal_provider_path()
        assert target.is_relative_to(tmp_path / "state")
        outside = tmp_path / "outside.json"
        (target.parent.parent).mkdir(parents=True, exist_ok=True)
        (target.parent.parent / "zcode").symlink_to(outside)
        with pytest.raises(NativeCliUnavailable):
            write_personal_provider_config(
                api_key="synthetic-zcode-key",
                base_url="https://example.invalid",
                model="test",
            )
        assert not outside.exists()


def test_secure_private_write_replaces_hardlink_without_modifying_external_inode(
    tmp_path,
):
    work = tmp_path / "workspace"
    work.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic-external-data")
    with worker_isolation(
        workspace_id="a", root=tmp_path / "state", workdir=work, env_names=set()
    ) as context:
        target = context.home / "linked.txt"
        os.link(outside, target)
        secure_private_write(target, b"workspace-private")
    assert outside.read_text() == "synthetic-external-data"
    assert target.read_bytes() == b"workspace-private"


def test_bwrap_arguments_do_not_expose_credentials(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(isolation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        isolation.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setenv("WORKER_SCOPED_TEST", "synthetic-credential")
    with worker_isolation(
        workspace_id="a",
        root=tmp_path / "state",
        workdir=work,
        env_names={"WORKER_SCOPED_TEST"},
    ):
        argv, env, _ = prepare_process(("/usr/bin/true",), str(work), None)
    assert env is not None and env["WORKER_SCOPED_TEST"] == "synthetic-credential"
    assert "synthetic-credential" not in " ".join(argv)
    assert "--setenv" not in argv
    binds = [
        argv[index + 1] for index, value in enumerate(argv[:-2]) if value == "--ro-bind"
    ]
    assert "/usr" not in binds
    assert "/usr/bin" in binds


@pytest.mark.asyncio
async def test_concurrent_workspace_contexts_keep_home_and_credentials_separate(
    tmp_path,
):
    async def inspect(name, secret):
        work = tmp_path / name
        work.mkdir()
        with worker_isolation(
            workspace_id=name,
            root=tmp_path / "state",
            workdir=work,
            env_names={secret},
        ):
            await asyncio.sleep(0)
            _, env, _ = prepare_process(("/bin/true",), str(work), {secret: name})
            return env["HOME"], env.get(secret)

    first, second = await asyncio.gather(
        inspect("one", "FIRST_TEST_KEY"), inspect("two", "SECOND_TEST_KEY")
    )
    assert first[0] != second[0]
    assert first[1] == "one" and second[1] == "two"


@pytest.mark.skipif(
    not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable"
)
@pytest.mark.asyncio
async def test_macos_worker_process_is_confined_to_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling"
    workspace.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("synthetic-sibling-data")
    (workspace / "leak").symlink_to(sibling / "secret.txt")
    (workspace / "readable.txt").write_text("safe-data")
    monkeypatch.setenv("WORKER_SCOPED_TEST", "synthetic-credential")
    monkeypatch.setenv("PARENT_ONLY_TEST", "must-not-cross")
    script = (
        'test "$WORKER_SCOPED_TEST" = synthetic-credential || exit 10; '
        'test -z "$PARENT_ONLY_TEST" || exit 11; '
        'test "$(cat readable.txt)" = safe-data || exit 17; '
        "cat ../sibling/secret.txt >/dev/null 2>&1 && exit 14; "
        "cat leak >/dev/null 2>&1 && exit 15; "
        "printf allowed > inside.txt || exit 12; "
        'printf temp > "$TMPDIR"/probe.txt || exit 16; '
        'test "$(cat "$TMPDIR"/probe.txt)" = temp || exit 18; '
        "printf denied > ../sibling/escape.txt 2>/dev/null && exit 13; "
        "printf ok"
    )
    with worker_isolation(
        workspace_id="workspace-a",
        root=tmp_path / "state",
        workdir=workspace,
        env_names={"WORKER_SCOPED_TEST"},
    ):
        result = await run_process(
            CliInvocation(argv=("/bin/sh", "-c", script), cwd=str(workspace))
        )
    assert result.stdout == "ok"
    assert (workspace / "inside.txt").read_text() == "allowed"
    assert (
        tmp_path
        / "state"
        / __import__("hashlib").sha256(b"workspace-a").hexdigest()
        / "tmp"
        / "probe.txt"
    ).read_text() == "temp"
    assert not (sibling / "escape.txt").exists()


def test_policy_rejects_system_temp_tree_runtime_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for temp in ("/tmp", "/var/tmp"):
        policy = tmp_path / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "a": {
                        "root": str(workspace),
                        "env": [],
                        "runtime_read_paths": [temp],
                    }
                }
            )
        )
        os.chmod(policy, 0o600)
        with pytest.raises(NativeCliUnavailable, match="system temp tree"):
            read_workspace_policy(policy, "a", tmp_path / "state")


def test_policy_rejects_home_and_credential_mounts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".aws").mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    policy = tmp_path / "policy.json"

    policy.write_text(
        json.dumps({"a": {"root": str(home), "env": []}})
    )
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="home directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")

    policy.write_text(
        json.dumps({"a": {"root": str(home / ".aws"), "env": []}})
    )
    with pytest.raises(NativeCliUnavailable, match="credential directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")

    policy.write_text(
        json.dumps(
            {
                "a": {
                    "root": str(workspace),
                    "env": [],
                    "runtime_read_paths": [str(home), str(home / ".ssh")],
                }
            }
        )
    )
    with pytest.raises(NativeCliUnavailable, match="home directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")

    policy.write_text(
        json.dumps(
            {
                "a": {
                    "root": str(workspace),
                    "env": [],
                    "runtime_read_paths": [str(home / ".ssh")],
                }
            }
        )
    )
    with pytest.raises(NativeCliUnavailable, match="credential directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(NativeCliUnavailable, match="credential directory"):
        with worker_isolation(
            workspace_id="a",
            root=tmp_path / "state",
            workdir=work,
            env_names=set(),
            runtime_read_paths=(str(home / ".ssh"),),
        ):
            pass


def test_policy_rejects_case_variant_root_overlap(tmp_path):
    first = tmp_path / "case-a"
    first.mkdir()
    variant = tmp_path / "CASE-A"
    if not os.path.samefile(first, variant):
        pytest.skip("volume is case-sensitive")
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {"root": str(first), "env": []},
                "b": {"root": str(variant), "env": []},
            }
        )
    )
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="overlap"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_worker_isolation_rejects_case_variant_state_root(tmp_path):
    base = tmp_path / "state"
    workspace = tmp_path / "STATE"
    workspace.mkdir()
    if not os.path.samefile(tmp_path / "state", workspace):
        pytest.skip("volume is case-sensitive")
    with pytest.raises(NativeCliUnavailable, match="disjoint"):
        with worker_isolation(
            workspace_id="a", root=base, workdir=workspace, env_names=set()
        ):
            pass


def _same_file_or_none(first, second) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def test_policy_rejects_case_variant_nested_root_with_distinct_inodes(tmp_path):
    # Distinct endpoints: the overlap only exists because the nested root's
    # parent is a case-variant spelling of the other root's directory, which
    # endpoint samefile cannot see (APFS resolves both spellings to one inode).
    parent = tmp_path / "proj"
    parent.mkdir()
    nested = tmp_path / "PROJ" / "nested"
    nested.mkdir(parents=True)
    if not _same_file_or_none(parent, tmp_path / "PROJ"):
        pytest.skip("volume is case-sensitive")
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {"root": str(parent), "env": []},
                "b": {"root": str(nested), "env": []},
            }
        )
    )
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="overlap"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_worker_isolation_rejects_case_variant_state_root_before_mkdir(tmp_path):
    # The state root does not exist yet at check time; the overlap only shows
    # once the case-variant parent is resolved against the workspace inode.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    base = tmp_path / "WS" / "state"
    if not _same_file_or_none(workspace, tmp_path / "WS"):
        pytest.skip("volume is case-sensitive")
    with pytest.raises(NativeCliUnavailable, match="disjoint"):
        with worker_isolation(
            workspace_id="a", root=base, workdir=workspace, env_names=set()
        ):
            pass


def test_unsafe_mount_root_rejects_case_variant_system_paths():
    from src.llms.native_cli.isolation import _unsafe_mount_root

    # macOS temp trees live under /private; the case-folded spellings are the
    # same inodes on a case-insensitive volume and must not slip past the deny.
    for variant, canonical in (
        ("/private/TMP", "/private/tmp"),
        ("/private/var/TMP", "/private/var/tmp"),
    ):
        if _same_file_or_none(variant, canonical):
            assert _unsafe_mount_root(Path(variant)) == "the system temp tree"
    if _same_file_or_none("/users", "/Users"):
        assert _unsafe_mount_root(Path("/users")) == "a system directory"


def test_policy_rejects_credential_ancestor_roots(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)
    (home / "work").mkdir()
    empty_home = tmp_path / "home-empty"
    (empty_home / "Library").mkdir(parents=True)
    (empty_home / "work").mkdir()
    monkeypatch.setenv("HOME", str(home))
    policy = tmp_path / "policy.json"

    # ~/Library carries ~/Library/Keychains: the ancestor of a credential
    # store may not be mounted even though no component is a credential name.
    policy.write_text(json.dumps({"a": {"root": str(home / "Library"), "env": []}}))
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="credential directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")

    # Fail closed: the ancestor is denied even before the store exists.
    monkeypatch.setenv("HOME", str(empty_home))
    policy.write_text(
        json.dumps({"a": {"root": str(empty_home / "Library"), "env": []}})
    )
    with pytest.raises(NativeCliUnavailable, match="credential directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")

    # Case-variant spelling of the ancestor on a case-insensitive volume.
    if _same_file_or_none(home / "Library", home / "LIBRARY"):
        monkeypatch.setenv("HOME", str(home))
        policy.write_text(
            json.dumps({"a": {"root": str(home / "LIBRARY"), "env": []}})
        )
        with pytest.raises(NativeCliUnavailable, match="credential directory"):
            read_workspace_policy(policy, "a", tmp_path / "state")


def test_policy_and_worker_reject_case_variant_credential_names(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {
                    "root": str(workspace),
                    "env": [],
                    "runtime_read_paths": [str(home / ".SSH")],
                }
            }
        )
    )
    os.chmod(policy, 0o600)
    monkeypatch.setenv("HOME", str(home))
    if _same_file_or_none(home / ".ssh", home / ".SSH"):
        with pytest.raises(NativeCliUnavailable, match="credential directory"):
            read_workspace_policy(policy, "a", tmp_path / "state")
    with pytest.raises(NativeCliUnavailable, match="credential directory"):
        with worker_isolation(
            workspace_id="a",
            root=tmp_path / "state",
            workdir=workspace,
            env_names=set(),
            runtime_read_paths=(str(home / ".ssh"),),
        ):
            pass


def test_policy_rejects_case_variant_home_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"a": {"root": str(tmp_path / "HOME"), "env": []}}))
    os.chmod(policy, 0o600)
    if not _same_file_or_none(home, tmp_path / "HOME"):
        pytest.skip("volume is case-sensitive")
    with pytest.raises(NativeCliUnavailable, match="home directory"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_worker_isolation_rejects_system_temp_tree_workdir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(NativeCliUnavailable, match="system temp tree"):
        with worker_isolation(
            workspace_id="a", root=tmp_path / "state", workdir="/tmp", env_names=set()
        ):
            pass


def test_policy_owner_check_ties_to_the_parsed_descriptor(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"a": {"root": str(workspace), "env": []}}))
    os.chmod(policy, 0o644)
    with pytest.raises(NativeCliUnavailable, match="0600"):
        read_workspace_policy(policy, "a", tmp_path / "state")
    os.chmod(policy, 0o600)
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(NativeCliUnavailable, match="owned by the daemon user"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_bwrap_keeps_tmpfs_tmp_when_workspace_is_allowed(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(isolation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        isolation.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    with worker_isolation(
        workspace_id="a", root=tmp_path / "state", workdir=work, env_names=set()
    ):
        argv, _, _ = prepare_process(("/usr/bin/true",), str(work), None)
    tmpfs_targets = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--tmpfs"]
    assert "/tmp" in tmpfs_targets
    bind_sources = [argv[i + 1] for i, value in enumerate(argv[:-2]) if value == "--bind"]
    assert "/tmp" not in bind_sources


def test_unsupported_platform_fails_before_process_launch(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(isolation.platform, "system", lambda: "UnsupportedOS")
    with worker_isolation(
        workspace_id="a", root=tmp_path / "state", workdir=work, env_names=set()
    ):
        with pytest.raises(NativeCliUnavailable, match="unsupported"):
            prepare_process(("/bin/true",), str(work), None)


def test_case_insensitive_probe_walks_past_numeric_ancestor_names(tmp_path):
    from src.llms.native_cli.isolation import _case_insensitive

    # A directory named only with digits ("123") is unchanged by swapcase, so
    # it cannot classify the volume; the probe must keep walking up. On native
    # APFS this used to report "case-sensitive" and let case-variant roots
    # through as disjoint.
    nested = tmp_path / "proj" / "123"
    nested.mkdir(parents=True)
    if not _same_file_or_none(tmp_path / "proj", tmp_path / "PROJ"):
        pytest.skip("volume is case-sensitive")
    assert _case_insensitive(nested)


def test_policy_rejects_numeric_case_variant_roots(tmp_path):
    # The review repro: PROJECT/123 and project/123/456 are one directory on
    # native APFS but were accepted as two disjoint workspace roots.
    outer = tmp_path / "PROJECT" / "123"
    outer.mkdir(parents=True)
    nested = tmp_path / "project" / "123" / "456"
    nested.mkdir(parents=True)
    if not _same_file_or_none(tmp_path / "PROJECT", tmp_path / "project"):
        pytest.skip("volume is case-sensitive")
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {"root": str(outer), "env": []},
                "b": {"root": str(nested), "env": []},
            }
        )
    )
    os.chmod(policy, 0o600)
    with pytest.raises(NativeCliUnavailable, match="overlap"):
        read_workspace_policy(policy, "a", tmp_path / "state")


def test_worker_isolation_rejects_numeric_case_variant_state_root(tmp_path):
    # Nested isolation state: the state root's nearest existing ancestor is
    # the numeric workspace directory itself, which used to fail the probe
    # open and accept the state root as disjoint from the workspace.
    workspace = tmp_path / "proj" / "123"
    workspace.mkdir(parents=True)
    base = tmp_path / "PROJ" / "123" / "state"
    if not _same_file_or_none(workspace, tmp_path / "PROJ" / "123"):
        pytest.skip("volume is case-sensitive")
    with pytest.raises(NativeCliUnavailable, match="disjoint"):
        with worker_isolation(
            workspace_id="a", root=base, workdir=workspace, env_names=set()
        ):
            pass


def test_macos_profile_read_only_takes_the_workspace_write_grant_away(tmp_path):
    from src.llms.native_cli.isolation import _mac_profile

    profile = _mac_profile(
        Path("/work"), Path("/home"), Path("/private-tmp"), read_only=True
    )
    # Still readable, no longer writable: private CLI state stays writable.
    assert '(allow file-read* (subpath "/work")' in profile
    assert '(allow file-write* (subpath "/work")' not in profile
    assert (
        '(allow file-write* (subpath "/home") (subpath "/private-tmp"))'
    ) in profile


def test_worker_isolation_carries_the_read_only_flag(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    with worker_isolation(
        workspace_id="read-only",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        read_only=True,
    ) as isolation:
        assert isolation.read_only is True
        argv, _, _ = prepare_process(("/bin/true",), str(work), None)
        home = str(isolation.home.resolve())
        private_tmp = str((isolation.root / "tmp").resolve())
    if os.uname().sysname == "Darwin":
        profile = argv[2]
        workspace = str(work.resolve())
        # The exact workspace write grant is gone; only private state is left.
        assert f'(allow file-write* (subpath {json.dumps(workspace)}))' not in profile
        assert (
            '(allow file-write* (subpath "' + home + '") (subpath "' + private_tmp + '"))'
            in profile
        )
        assert '(allow file-read* (subpath "' + workspace + '")' in profile
    elif argv and argv[0].endswith("bwrap"):
        workspace = str(work.resolve())
        # The workspace is mounted read-only, and the exact workspace mount is
        # not also present as a writable bind. bwrap argv is a flat sequence,
        # so compare contiguous argument triples rather than tuples-in-a-tuple.
        parts = tuple(argv)
        triples = {parts[index : index + 3] for index in range(len(parts) - 2)}
        assert ("--ro-bind", workspace, workspace) in triples
        assert ("--bind", workspace, workspace) not in triples


def test_a_read_only_run_refuses_a_workspace_runtime_write_grant(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    with pytest.raises(NativeCliUnavailable, match="write grant"):
        with worker_isolation(
            workspace_id="read-only-overlap",
            root=tmp_path / "state",
            workdir=work,
            env_names=set(),
            runtime_write_paths=(str(work),),
            read_only=True,
        ):
            pass


def test_a_read_only_run_checks_grants_against_the_full_workspace_root(tmp_path):
    """A narrowed cwd must not hide a write grant inside the workspace root."""
    workspace_root = tmp_path / "repo"
    workdir = workspace_root / "src"
    workdir.mkdir(parents=True)
    sibling = workspace_root / "output"
    sibling.mkdir()
    with pytest.raises(NativeCliUnavailable, match="write grant"):
        with worker_isolation(
            workspace_id="read-only-nested",
            root=tmp_path / "state",
            workdir=workdir,
            env_names=set(),
            runtime_write_paths=(str(sibling),),
            read_only=True,
            workspace_root=workspace_root,
        ):
            pass


def test_a_read_only_run_keeps_a_disjoint_runtime_write_grant(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    cli_state = tmp_path / "cli-state"
    cli_state.mkdir()
    with worker_isolation(
        workspace_id="read-only-disjoint",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        runtime_write_paths=(str(cli_state),),
        read_only=True,
    ) as isolation:
        assert isolation.read_only is True
        assert isolation.runtime_write_paths == (cli_state.resolve(),)
        argv, _, _ = prepare_process(("/bin/true",), str(work), None)
    if argv and argv[0].endswith("bwrap"):
        parts = tuple(argv)
        triples = {parts[index : index + 3] for index in range(len(parts) - 2)}
        grant = str(cli_state.resolve())
        assert ("--bind", grant, grant) in triples


def test_macos_profile_grants_readback_of_private_tmp(tmp_path):
    from src.llms.native_cli.isolation import _mac_profile

    profile = _mac_profile(Path("/work"), Path("/home"), Path("/private-tmp"))
    assert (
        '(allow file-read* (subpath "/work") (subpath "/home") '
        '(subpath "/private-tmp"))'
    ) in profile


def test_redaction_preserves_provider_json_with_numeric_config_values(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NATIVE_CLI_DSH_ACCOUNT", "1")
    work = tmp_path / "workspace"
    work.mkdir()
    payload = '{"usage": {"input_tokens": 151}, "ok": true}'
    with worker_isolation(
        workspace_id="json",
        root=tmp_path / "state",
        workdir=work,
        env_names={"NATIVE_CLI_DSH_ACCOUNT"},
    ):
        assert redact_diagnostic(payload, None) == payload
        assert redact_diagnostic(payload, {"NATIVE_CLI_DSH_ACCOUNT": "1"}) == payload
        assert json.loads(redact_diagnostic(payload, None))["usage"]["input_tokens"] == (
            151
        )


def test_redaction_still_removes_named_credentials_from_output(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_CLI_ZCODE_API_KEY", "sk-" "synthetic-zcode-key")
    work = tmp_path / "workspace"
    work.mkdir()
    with worker_isolation(
        workspace_id="cred",
        root=tmp_path / "state",
        workdir=work,
        env_names={"NATIVE_CLI_ZCODE_API_KEY"},
    ):
        out = redact_diagnostic(
            '{"key": "sk-' 'synthetic-zcode-key", "n": 1}',
            {"NATIVE_CLI_ZCODE_API_KEY": "sk-" "synthetic-zcode-key"},
        )
        parsed = json.loads(out)
        assert parsed["key"] == "[redacted]"
        assert parsed["n"] == 1


def test_macos_profile_denies_loopback_and_host_unix_sockets(tmp_path):
    from src.llms.native_cli.isolation import _HOST_SERVICE_PORTS, _mac_profile

    profile = _mac_profile(Path("/work"), Path("/home"), Path("/private-tmp"))
    # Public network stays open. The old `(allow network*)` also opened every
    # loopback port and every unix socket; loopback is now deny-by-default.
    assert "(allow network*)" not in profile
    assert "(allow network-outbound)" in profile
    assert '(deny network-outbound (remote ip "localhost:*"))' in profile
    assert '(deny network-inbound (local ip "localhost:*"))' in profile
    outbound = profile.index("(allow network-outbound)")
    loopback = profile.index('(deny network-outbound (remote ip "localhost:*"))')
    deny = profile.index("(deny network-outbound (to unix-socket))")
    reallow = profile.index(
        '(allow network-outbound (to unix-socket (subpath "/work") '
        '(subpath "/home") (subpath "/private-tmp")))'
    )
    mdns = profile.index(
        '(allow network-outbound (to unix-socket (path "/private/var/run/mDNSResponder")))'
    )
    # Seatbelt last-match wins: the loopback deny follows the broad grant,
    # and the unix-socket deny follows both so the Docker socket stays closed.
    assert outbound < loopback < deny < reallow < mdns
    for port in _HOST_SERVICE_PORTS:
        assert f'localhost:{port}' not in profile
    assert "/var/run/docker.sock" not in profile
    assert "/private/var/run/docker.sock" not in profile
    # Sink/entropy devices: `>/dev/null` and TLS entropy reads must not EPERM.
    assert '(allow file-write* (literal "/dev/null"))' in profile
    assert '(literal "/dev/urandom")' in profile


def test_macos_profile_allows_only_configured_localhost_ports():
    from src.llms.native_cli.isolation import _HOST_SERVICE_PORTS, _mac_profile

    profile = _mac_profile(
        Path("/work"), Path("/home"), Path("/private-tmp"), (5432,)
    )
    deny = profile.index('(deny network-outbound (remote ip "localhost:*"))')
    allow = profile.index('(allow network-outbound (remote tcp "localhost:5432"))')
    unix_deny = profile.index("(deny network-outbound (to unix-socket))")
    # The port allow has to follow the unix-socket deny. Placing it earlier
    # leaves the connect denied under sandbox-exec.
    assert deny < unix_deny < allow
    for port in _HOST_SERVICE_PORTS:
        if port == 5432:
            continue
        assert f'localhost:{port}' not in profile
    assert "/var/run/docker.sock" not in profile


def test_policy_localhost_ports_fail_closed_and_round_trip(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "a": {
                    "root": str(workspace),
                    "env": [],
                    "localhost_tcp_ports": [443, 443],
                }
            }
        )
    )
    os.chmod(policy, 0o600)
    parsed = read_workspace_policy(policy, "a", tmp_path / "state")
    assert parsed["localhost_tcp_ports"] == (443,)

    policy.write_text(json.dumps({"a": {"root": str(workspace), "env": []}}))
    parsed = read_workspace_policy(policy, "a", tmp_path / "state")
    assert parsed["localhost_tcp_ports"] == ()

    for bad in ([0], [65536], ["5432"], [True], "5432", None):
        policy.write_text(
            json.dumps(
                {
                    "a": {
                        "root": str(workspace),
                        "env": [],
                        "localhost_tcp_ports": bad,
                    }
                }
            )
        )
        with pytest.raises(NativeCliUnavailable, match="invalid"):
            read_workspace_policy(policy, "a", tmp_path / "state")


def test_macos_profile_emits_utf8_literals_for_unicode_paths(tmp_path):
    from src.llms.native_cli.isolation import _mac_profile

    profile = _mac_profile(Path("/tmp/caf\u00e9"), Path("/home"), Path("/private-tmp"))
    # Seatbelt does not decode JSON \uXXXX escapes to the on-disk byte path; a
    # profile built from them left a "café" workspace unable to read its files.
    assert "caf\u00e9" in profile
    assert "\\u00e9" not in profile


def test_path_compare_folds_case_only_on_case_insensitive_volumes(monkeypatch):
    import src.llms.native_cli.isolation as isolation

    # /work does not exist, so samefile cannot treat the spellings as one inode.
    nfc = Path("/work/caf\u00e9")
    nfd = Path("/work/cafe\u0301")
    monkeypatch.setattr(isolation, "_case_insensitive", lambda _path: False)
    assert isolation._path_eq(nfc, nfd)
    assert isolation._path_contains(nfc, nfd / "child")
    assert not isolation._path_eq(Path("/work/Cafe"), Path("/work/cafe"))
    assert not isolation._path_contains(Path("/work/Cafe"), Path("/work/cafe/child"))
    assert not isolation._path_eq(Path("/work/\u0130"), Path("/work/i\u0307"))

    monkeypatch.setattr(isolation, "_case_insensitive", lambda _path: True)
    assert isolation._path_eq(nfc, nfd)
    assert isolation._path_contains(nfc, nfd / "child")
    assert isolation._path_eq(Path("/work/Cafe"), Path("/work/cafe"))
    assert isolation._path_contains(Path("/work/Cafe"), Path("/work/cafe/child"))
    assert isolation._path_eq(Path("/work/\u0130"), Path("/work/i\u0307"))


def test_paths_overlap_treats_nfc_nfd_spellings_as_same():
    from src.llms.native_cli.isolation import _paths_overlap

    nfc = Path("/tmp/caf\u00e9")
    nfd = Path("/tmp/cafe\u0301")
    assert _paths_overlap(nfc, nfd)
    assert _paths_overlap(nfc, nfd / "state")
    # Normalization must not invent containment where names really differ.
    assert not _paths_overlap(nfc, Path("/tmp/cafe\u0301y"))


def test_worker_isolation_rejects_unicode_variant_state_root(tmp_path):
    # The review repro: workspace root created as NFC "café", isolation state
    # passed as the NFD spelling of the same directory. realpath on macOS
    # keeps the caller's spelling, so containment used to compare NFC against
    # NFD strings, miss the overlap, and nest worker state inside the
    # workspace where other workers can read it.
    workspace = tmp_path / "caf\u00e9"
    workspace.mkdir()
    state = tmp_path / "cafe\u0301" / "state"
    with pytest.raises(NativeCliUnavailable, match="disjoint"):
        with worker_isolation(
            workspace_id="a", root=state, workdir=workspace, env_names=set()
        ):
            pass


def test_bwrap_mounts_resolver_and_tls_config(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(isolation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        isolation.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    with worker_isolation(
        workspace_id="a", root=tmp_path / "state", workdir=work, env_names=set()
    ):
        argv, _, _ = prepare_process(("/usr/bin/true",), str(work), None)
    ro_binds = [
        (argv[index + 1], argv[index + 2])
        for index, value in enumerate(argv[:-2])
        if value == "--ro-bind"
    ]
    for name in isolation._LINUX_NETWORK_CONFIG:
        path = Path(name)
        if not path.exists():
            continue
        source = path.resolve() if path.is_symlink() else path
        assert (str(source), str(path)) in ro_binds


def test_trust_store_binds_rhel_paths_that_exist(tmp_path):
    from src.llms.native_cli.isolation import collect_trust_store_binds

    root = tmp_path / "fedora"
    (root / "etc/pki/ca-trust").mkdir(parents=True)
    (root / "etc/pki/tls/certs").mkdir(parents=True)
    (root / "etc/pki/tls/cert.pem").write_text("pem\n")
    binds = collect_trust_store_binds(root)
    assert [(str(source), str(target)) for source, target in binds] == [
        (str(root / "etc/pki/ca-trust"), "/etc/pki/ca-trust"),
        (str(root / "etc/pki/tls/certs"), "/etc/pki/tls/certs"),
        (str(root / "etc/pki/tls/cert.pem"), "/etc/pki/tls/cert.pem"),
    ]


def test_trust_store_binds_debian_paths_that_exist(tmp_path):
    from src.llms.native_cli.isolation import collect_trust_store_binds

    root = tmp_path / "debian"
    (root / "etc/ssl/certs").mkdir(parents=True)
    (root / "etc/ca-certificates").mkdir(parents=True)
    binds = collect_trust_store_binds(root)
    assert [(str(source), str(target)) for source, target in binds] == [
        (str(root / "etc/ssl/certs"), "/etc/ssl/certs"),
        (str(root / "etc/ca-certificates"), "/etc/ca-certificates"),
    ]


def test_trust_store_bind_follows_symlink_to_canonical_path(tmp_path):
    from src.llms.native_cli.isolation import collect_trust_store_binds

    root = tmp_path / "linked"
    root.mkdir()
    real = root / "real-cert.pem"
    real.write_text("pem\n")
    link_dir = root / "etc/pki/tls"
    link_dir.mkdir(parents=True)
    (link_dir / "cert.pem").symlink_to(real)
    assert collect_trust_store_binds(root) == [
        (real.resolve(), Path("/etc/pki/tls/cert.pem"))
    ]


def test_linux_network_limitation_is_macos_only_and_not_enforced(tmp_path, monkeypatch):
    """Loopback deny is a Seatbelt rule. Linux shares the host net namespace."""
    import src.llms.native_cli.isolation as isolation

    doc = (isolation.__doc__ or "").lower()
    port = Path("docs/ports/worker-isolation.md").read_text().lower()
    for text in (doc, port):
        assert "loopback tcp is denied on macos" in text
        assert "shares the host network namespace" in text
        assert "--unshare-net" in text
        assert "abstract-namespace" in text
        assert "tracked follow-up" in text
    work = tmp_path / "workspace"
    work.mkdir()
    monkeypatch.setattr(isolation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        isolation.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    with worker_isolation(
        workspace_id="a",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        localhost_tcp_ports=(5432,),
    ):
        argv, _, _ = prepare_process(("/usr/bin/true",), str(work), None)
    assert "--unshare-net" not in argv
    assert "5432" not in argv


def test_trust_store_follows_symlink_entries_outside_the_store(tmp_path):
    """Fedora entries under /etc/pki point at a bundle the top bind does not cover."""
    from src.llms.native_cli.isolation import collect_trust_store_binds

    root = tmp_path / "fedora"
    extracted = root / "usr/share/pki/ca-trust/extracted/pem"
    extracted.mkdir(parents=True)
    bundle = extracted / "tls-ca-bundle.pem"
    bundle.write_text("pem\n")
    certs = root / "etc/pki/tls/certs"
    certs.mkdir(parents=True)
    (certs / "ca-bundle.crt").symlink_to(bundle)
    ssl = root / "etc/ssl"
    ssl.mkdir(parents=True)
    (ssl / "certs").symlink_to(certs)

    binds = collect_trust_store_binds(root)
    targets = {str(target) for _source, target in binds}
    sources = {source.resolve() for source, _target in binds}
    assert "/etc/ssl/certs" in targets
    assert "/etc/pki/tls/certs" in targets
    assert "/usr/share/pki/ca-trust/extracted/pem" in targets
    assert extracted.resolve() in sources


def test_trust_store_does_not_mount_private_keys_or_arbitrary_files(tmp_path):
    """A cert symlink must not widen the namespace to keys or /etc/passwd."""
    from src.llms.native_cli.isolation import collect_trust_store_binds

    root = tmp_path / "debian"
    certs = root / "etc/ssl/certs"
    certs.mkdir(parents=True)
    private = root / "etc/ssl/private"
    private.mkdir()
    key = private / "key.pem"
    key.write_text("secret\n")
    (certs / "sneaky.pem").symlink_to(key)
    passwd = root / "etc/passwd"
    passwd.parent.mkdir(parents=True, exist_ok=True)
    passwd.write_text("root:x:0:0::/root:/bin/sh\n")
    (certs / "not-a-cert").symlink_to(passwd)
    bundle = certs / "ca-certificates.crt"
    bundle.write_text("pem\n")

    binds = collect_trust_store_binds(root)
    sources = {source.resolve() for source, _target in binds}
    targets = {str(target) for _source, target in binds}
    assert key.resolve() not in sources
    assert private.resolve() not in sources
    assert passwd.resolve() not in sources
    assert "/etc/passwd" not in targets
    assert "/etc/ssl/private" not in targets
    assert certs.resolve() in sources


def test_trust_store_resolves_a_multi_hop_symlink(tmp_path):
    from src.llms.native_cli.isolation import collect_trust_store_binds

    root = tmp_path / "chain"
    real = root / "usr/share/pki/ca-trust/extracted/pem"
    real.mkdir(parents=True)
    (real / "tls-ca-bundle.pem").write_text("pem\n")
    hop = root / "etc/pki/ca-trust-link"
    hop.parent.mkdir(parents=True)
    hop.symlink_to(real.parent)
    ssl = root / "etc/ssl"
    ssl.mkdir(parents=True)
    (ssl / "certs").symlink_to(hop / "pem")

    binds = collect_trust_store_binds(root)
    ssl_bind = [item for item in binds if str(item[1]) == "/etc/ssl/certs"]
    assert ssl_bind
    assert ssl_bind[0][0].resolve() == real.resolve()


def test_trust_store_binds_fail_closed_when_none_exist(tmp_path):
    from src.llms.native_cli.isolation import collect_trust_store_binds

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NativeCliUnavailable, match="trust store"):
        collect_trust_store_binds(empty)
    broken = tmp_path / "broken"
    link_dir = broken / "etc/ssl"
    link_dir.mkdir(parents=True)
    (link_dir / "certs").symlink_to(broken / "missing")
    with pytest.raises(NativeCliUnavailable, match="trust store"):
        collect_trust_store_binds(broken)


def test_network_config_bind_resolves_symlink_source(tmp_path):
    from src.llms.native_cli.isolation import _network_config_bind

    real = tmp_path / "real-resolv.conf"
    real.write_text("nameserver 127.0.0.1\n")
    link = tmp_path / "resolv.conf"
    link.symlink_to(real)
    assert _network_config_bind(link) == (real, link)

    plain = tmp_path / "hosts"
    plain.write_text("127.0.0.1 localhost\n")
    assert _network_config_bind(plain) == (plain, plain)

    assert _network_config_bind(tmp_path / "absent.conf") is None


# macOS sockaddr_un.sun_path is 104 bytes. CPython rejects a path of that
# length or longer: a 103-byte path binds, and 104 raises
# "AF_UNIX path too long". Linux allows 108; this probe is macOS-only.
_AF_UNIX_SUN_PATH_MAX = 104


def _skip_if_af_unix_path_too_long(*paths: Path) -> None:
    for path in paths:
        nbytes = len(os.fsencode(path))
        if nbytes >= _AF_UNIX_SUN_PATH_MAX:
            pytest.skip(
                f"AF_UNIX socket path is {nbytes} bytes; "
                f"macOS sun_path rejects lengths of {_AF_UNIX_SUN_PATH_MAX} or more"
            )


def test_af_unix_path_over_sun_path_limit_skips():
    at_limit = Path("a" * _AF_UNIX_SUN_PATH_MAX)
    over = Path("b" * (_AF_UNIX_SUN_PATH_MAX + 1))
    assert len(os.fsencode(at_limit)) == 104
    assert len(os.fsencode(over)) > 104
    with pytest.raises(pytest.skip.Exception, match="104"):
        _skip_if_af_unix_path_too_long(at_limit)
    with pytest.raises(pytest.skip.Exception, match="105"):
        _skip_if_af_unix_path_too_long(Path("/tmp/ok.sock"), over)


def test_af_unix_path_under_sun_path_limit_does_not_skip():
    short = Path("/tmp/a.sock")
    assert len(os.fsencode(short)) < _AF_UNIX_SUN_PATH_MAX
    try:
        _skip_if_af_unix_path_too_long(short)
    except pytest.skip.Exception as exc:
        raise AssertionError(f"short AF_UNIX path must not skip: {exc}") from exc


def _unix_echo_server(path: Path, stop: asyncio.Event, payload: bytes = b"pong"):
    import socket as socket_module
    import threading

    def serve() -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        server.bind(str(path))
        server.listen(2)
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket_module.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.sendall(payload + b":" + conn.recv(64))
        server.close()

    threading.Thread(target=serve, daemon=True).start()


def _tcp_echo_server(stop: asyncio.Event) -> int:
    import socket as socket_module
    import threading

    server = socket_module.socket()
    server.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.2)

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket_module.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.sendall(b"tcp-pong:" + conn.recv(64))

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1]


@pytest.mark.skipif(
    not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable"
)
@pytest.mark.asyncio
async def test_macos_worker_cannot_reach_host_unix_socket(socket_enabled):
    # AF_UNIX paths are capped at 104 bytes, so the socket files live in a
    # short repo-relative scratch dir instead of the deep pytest tmp_path.
    # A checkout whose absolute path still exceeds that cap skips instead of
    # failing inside bind(). Paths must be absolute: Seatbelt's unix-socket
    # subpath filter does not canonicalize the connect() path, and a relative
    # one never matches.
    scratch = Path.cwd() / ".test-tmp" / f"ipc-{os.getpid()}-{id(object()) % 0xFFFF:04x}"
    workspace = scratch / "w"
    sibling = scratch / "s"
    _skip_if_af_unix_path_too_long(sibling / "a.sock", workspace / "o.sock")
    workspace.mkdir(parents=True)
    sibling.mkdir(parents=True)
    stop = asyncio.Event()
    _unix_echo_server(sibling / "a.sock", stop)
    _unix_echo_server(workspace / "o.sock", stop)
    tcp_port = _tcp_echo_server(stop)
    await asyncio.sleep(0.3)
    # The review repro through the production wrapper: the sibling socket
    # (same-user credential-agent shape) must be unreachable while the
    # workspace socket keeps working. Unconfigured loopback TCP must also
    # fail: a successful read from 127.0.0.1 is the host-service leak.
    script = (
        f'if out=$(printf ping | /usr/bin/nc -U -w 3 "{sibling}/a.sock" 2>/dev/null) '
        '&& [ -n "$out" ]; then exit 21; fi; '
        f'out=$(printf ping | /usr/bin/nc -U -w 3 "{workspace}/o.sock" 2>/dev/null) '
        '|| exit 22; '
        '[ "$out" = pong:ping ] || exit 23; '
        f'if out=$(printf x | /usr/bin/nc -w 3 127.0.0.1 {tcp_port} 2>/dev/null) '
        '&& [ -n "$out" ]; then exit 24; fi; '
        'echo x >/dev/null 2>&1 || exit 26; '
        "printf ok"
    )
    try:
        with worker_isolation(
            workspace_id="workspace-a",
            root=scratch / "state",
            workdir=workspace,
            env_names=set(),
        ):
            result = await run_process(
                CliInvocation(argv=("/bin/sh", "-c", script), cwd=str(workspace))
            )
    finally:
        stop.set()
        shutil.rmtree(scratch, ignore_errors=True)
    assert result.stdout == "ok"


@pytest.mark.skipif(
    not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable"
)
@pytest.mark.asyncio
async def test_macos_worker_reaches_only_a_configured_localhost_port(socket_enabled):
    scratch = Path.cwd() / ".test-tmp" / f"tcp-{os.getpid()}-{id(object()) % 0xFFFF:04x}"
    workspace = scratch / "w"
    workspace.mkdir(parents=True)
    stop = asyncio.Event()
    tcp_port = _tcp_echo_server(stop)
    await asyncio.sleep(0.2)
    script = (
        f'out=$(printf x | /usr/bin/nc -w 3 127.0.0.1 {tcp_port} 2>/dev/null) '
        '|| exit 24; '
        '[ "$out" = tcp-pong:x ] || exit 25; '
        'if out=$(printf x | /usr/bin/nc -w 2 127.0.0.1 5432 2>/dev/null) '
        '&& [ -n "$out" ]; then exit 27; fi; '
        "printf ok"
    )
    try:
        with worker_isolation(
            workspace_id="workspace-a",
            root=scratch / "state",
            workdir=workspace,
            env_names=set(),
            localhost_tcp_ports=(tcp_port,),
        ):
            result = await run_process(
                CliInvocation(argv=("/bin/sh", "-c", script), cwd=str(workspace))
            )
    finally:
        stop.set()
        shutil.rmtree(scratch, ignore_errors=True)
    assert result.stdout == "ok"


@pytest.mark.skipif(
    not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable"
)
@pytest.mark.asyncio
async def test_macos_worker_reads_own_file_in_unicode_workspace(tmp_path):
    workspace = tmp_path / "caf\u00e9"
    workspace.mkdir()
    (workspace / "input.txt").write_text("synthetic-workspace-data")
    with worker_isolation(
        workspace_id="unicode",
        root=tmp_path / "state",
        workdir=workspace,
        env_names=set(),
    ):
        result = await run_process(
            CliInvocation(
                argv=("/bin/cat", "input.txt"), cwd=str(workspace), timeout_seconds=8
            )
        )
    assert result.stdout == "synthetic-workspace-data"


def test_user_info_is_opt_in_policy_and_profile(tmp_path):
    from src.llms.native_cli.isolation import _mac_profile

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = tmp_path / "policy.json"
    state = tmp_path / "state"

    for configured, expected in ((False, False), (True, True)):
        row = {"root": str(workspace), "env": []}
        if configured:
            row["user_info"] = True
        policy.write_text(json.dumps({"a": row}), encoding="utf-8")
        os.chmod(policy, 0o600)
        parsed = read_workspace_policy(policy, "a", state)
        assert parsed["user_info"] is expected
        with worker_isolation(
            workspace_id="a",
            root=state,
            workdir=workspace,
            env_names=parsed["env"],
            user_info=parsed["user_info"],
        ) as isolation:
            profile = _mac_profile(
                isolation.workdir, isolation.home, isolation.root / "tmp"
            )
        lookup = '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))'
        assert (lookup in profile) is expected


def test_missing_runtime_write_path_is_created_private_and_logged(tmp_path, caplog):
    import logging

    work = tmp_path / "workspace"
    work.mkdir()
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    grant = parent / ".cursor"

    with caplog.at_level(logging.WARNING, logger="src.llms.native_cli.isolation"):
        with worker_isolation(
            workspace_id="cursor-runtime",
            root=tmp_path / "isolation",
            workdir=work,
            env_names=set(),
            runtime_write_paths=(str(grant),),
        ) as isolation:
            assert isolation.runtime_write_paths == (grant.resolve(),)
            assert stat.S_IMODE(grant.stat().st_mode) == 0o700

    assert "creating missing policy runtime write path" in caplog.text


def test_runtime_write_path_rejects_symlink_before_creation(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NativeCliUnavailable, match="cannot follow symlinks"):
        with worker_isolation(
            workspace_id="cursor-runtime-symlink",
            root=tmp_path / "isolation",
            workdir=work,
            env_names=set(),
            runtime_write_paths=(str(alias / ".cursor"),),
        ):
            pytest.fail("symlink runtime write path should fail closed")


def test_read_only_worker_rejects_missing_runtime_write_path_overlapping_workspace(
    tmp_path,
):
    work = tmp_path / "workspace"
    work.mkdir()
    grant = work / ".cursor"

    with pytest.raises(NativeCliUnavailable, match="cannot hold a write grant"):
        with worker_isolation(
            workspace_id="cursor-runtime-overlap",
            root=tmp_path / "isolation",
            workdir=work,
            env_names=set(),
            runtime_write_paths=(str(grant),),
            read_only=True,
        ):
            pytest.fail("workspace write grant should fail closed")
    assert not grant.exists()


def test_runtime_write_path_normalizes_fixed_tmp_alias_without_creating_it():
    from src.llms.native_cli.isolation import _canonical_runtime_write_path

    candidate = Path("/tmp/codex-native-diag-test-only/.cursor")
    normalized = _canonical_runtime_write_path(candidate)
    assert normalized == Path("/tmp").resolve() / "codex-native-diag-test-only/.cursor"


def test_policy_keeps_missing_runtime_write_path_for_startup_creation(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    parent = tmp_path / "runtime"
    parent.mkdir()
    missing = parent / "cursor-state"
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "workspace": {
                    "root": str(work),
                    "env": [],
                    "runtime_write_paths": [str(missing)],
                }
            }
        ),
        encoding="utf-8",
    )
    os.chmod(policy, 0o600)

    parsed = read_workspace_policy(policy, "workspace", tmp_path / "state")

    assert parsed["runtime_write_paths"] == (str(missing),)
    assert not missing.exists()


# --- per-file write grants (runtime_write_files) ---


def _file_grant_dirs(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    return work, store


def test_runtime_write_files_are_literal_rules_never_subpath(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    monkeypatch.setattr(isolation.platform, "system", lambda: "Darwin")
    work, store = _file_grant_dirs(tmp_path)
    granted = store / "ref"
    lock = store / "ref.lock"
    granted.write_text("x\n")
    with worker_isolation(
        workspace_id="a",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        runtime_write_files=(str(granted), str(lock)),
    ) as current:
        assert current.runtime_write_files == (granted.resolve(), lock.resolve())
        profile = isolation._mac_profile(current.workdir, current.home, current.root / "tmp")
    for path in (granted, lock):
        assert f'(literal "{path.resolve()}")' in profile
        assert f'(subpath "{path.resolve()}")' not in profile
    assert f'(subpath "{store.resolve()}")' not in profile


def test_runtime_write_files_keep_existing_directory_grants_unchanged(
    tmp_path, monkeypatch
):
    import src.llms.native_cli.isolation as isolation

    monkeypatch.setattr(isolation.platform, "system", lambda: "Darwin")
    work, store = _file_grant_dirs(tmp_path)
    with worker_isolation(
        workspace_id="a",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        runtime_write_paths=(str(store),),
    ) as current:
        assert current.runtime_write_files == ()
        profile = isolation._mac_profile(current.workdir, current.home, current.root / "tmp")
    assert f'(subpath "{store.resolve()}")' in profile
    assert "(literal" not in profile.split('(allow file-write* (literal "/dev/null"))')[1]


def test_runtime_write_files_are_refused_off_macos(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    monkeypatch.setattr(isolation.platform, "system", lambda: "Linux")
    work, store = _file_grant_dirs(tmp_path)
    with pytest.raises(NativeCliUnavailable, match="macOS"):
        with worker_isolation(
            workspace_id="a",
            root=tmp_path / "state",
            workdir=work,
            env_names=set(),
            runtime_write_files=(str(store / "ref"),),
        ):
            pass


def test_bwrap_refuses_a_file_grant_it_cannot_express(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    system = {"name": "Darwin"}
    monkeypatch.setattr(isolation.platform, "system", lambda: system["name"])
    monkeypatch.setattr(
        isolation.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    work, store = _file_grant_dirs(tmp_path)
    with worker_isolation(
        workspace_id="a",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        runtime_write_files=(str(store / "ref"),),
    ):
        system["name"] = "Linux"
        with pytest.raises(NativeCliUnavailable, match="per-file"):
            prepare_process(("/usr/bin/true",), str(work), None)


@pytest.mark.parametrize("shape", ["symlink", "directory", "symlink-parent", "no-parent", "state"])
def test_runtime_write_file_validation_fails_closed(tmp_path, monkeypatch, shape):
    import src.llms.native_cli.isolation as isolation

    monkeypatch.setattr(isolation.platform, "system", lambda: "Darwin")
    work, store = _file_grant_dirs(tmp_path)
    target = store / "ref"
    if shape == "symlink":
        (store / "main").write_text("m\n")
        target.symlink_to(store / "main")
    elif shape == "directory":
        target.mkdir()
    elif shape == "symlink-parent":
        (tmp_path / "alias").symlink_to(store)
        target = tmp_path / "alias" / "ref"
    elif shape == "no-parent":
        target = store / "missing" / "ref"
    else:
        target = tmp_path / "state" / "ref"
    with pytest.raises(NativeCliUnavailable):
        with worker_isolation(
            workspace_id="a",
            root=tmp_path / "state",
            workdir=work,
            env_names=set(),
            runtime_write_files=(str(target),),
        ):
            pass


def test_read_only_run_refuses_a_workspace_file_grant(tmp_path, monkeypatch):
    import src.llms.native_cli.isolation as isolation

    monkeypatch.setattr(isolation.platform, "system", lambda: "Darwin")
    work, _store = _file_grant_dirs(tmp_path)
    with pytest.raises(NativeCliUnavailable, match="read-only"):
        with worker_isolation(
            workspace_id="a",
            root=tmp_path / "state",
            workdir=work,
            env_names=set(),
            read_only=True,
            runtime_write_files=(str(work / "file"),),
        ):
            pass


@pytest.mark.skipif(
    not shutil.which("sandbox-exec"), reason="macOS Seatbelt unavailable"
)
@pytest.mark.asyncio
async def test_macos_file_grant_covers_the_file_and_nothing_below_or_beside_it(
    tmp_path,
):
    work, store = _file_grant_dirs(tmp_path)
    granted = store / "ref"
    lock = store / "ref.lock"
    granted.write_text("old\n")
    script = (
        f'printf new > "{lock}" || exit 10; '
        f'mv "{lock}" "{granted}" || exit 11; '
        f'test "$(cat "{granted}")" = new || exit 12; '
        f'printf x > "{store}/sibling" 2>/dev/null && exit 13; '
        f'rm "{granted}" || exit 14; '
        f'mkdir "{granted}" || exit 15; '
        f'printf x > "{granted}/below" 2>/dev/null && exit 16; '
        "printf ok"
    )
    with worker_isolation(
        workspace_id="a",
        root=tmp_path / "state",
        workdir=work,
        env_names=set(),
        runtime_write_files=(str(granted), str(lock)),
    ):
        result = await run_process(
            CliInvocation(argv=("/bin/sh", "-c", script), cwd=str(work))
        )
    assert result.stdout == "ok", (result.returncode, result.stderr)
    assert not (store / "sibling").exists()
    assert not (granted / "below").exists()


def test_macos_profile_always_reads_developer_dir_without_policy(monkeypatch):
    from src.llms.native_cli import isolation

    developer = "/Library/Developer/SyntheticCommandLineTools"
    monkeypatch.setattr(isolation, "developer_dir_read_paths", lambda: (developer,))
    assert isolation.current_worker_isolation() is None

    profile = isolation._mac_profile(Path("/work"), Path("/home"), Path("/private-tmp"))

    rules = [
        rule
        for rule in profile.split("(allow ")
        if rule.startswith("file-read* ") and "file-write*" not in rule
    ]
    assert any(f'(subpath "{developer}")' in rule for rule in rules)


def test_selected_developer_dir_validates_xcode_select_output(monkeypatch):
    import subprocess

    from src.llms.native_cli import isolation

    if not Path("/usr/bin/xcode-select").is_file():
        pytest.skip("xcode-select is unavailable")

    outputs: list[bytes] = []

    def fake_run(argv, **kwargs):
        assert list(argv) == ["/usr/bin/xcode-select", "-p"]
        assert kwargs["env"] == {"PATH": os.defpath}
        assert kwargs["timeout"] == 2
        return subprocess.CompletedProcess(argv, 0, stdout=outputs[-1], stderr=b"")

    monkeypatch.setattr(isolation.subprocess, "run", fake_run)
    try:
        for output, expected in (
            (b"/tmp/attacker/Developer\n", ()),
            (b"/Users/x/Xcode.app/Contents/Developer\n", ()),
            (
                b"/Library/Developer/CommandLineTools\n",
                ("/Library/Developer/CommandLineTools",),
            ),
            (
                b"/Applications/Xcode-beta.app/Contents/Developer\n",
                ("/Applications/Xcode-beta.app/Contents/Developer",),
            ),
        ):
            outputs.append(output)
            isolation.selected_developer_dir.cache_clear()
            assert isolation.selected_developer_dir() == expected
    finally:
        isolation.selected_developer_dir.cache_clear()


@pytest.mark.skipif(
    not (Path("/usr/bin/sandbox-exec").is_file() and Path("/usr/bin/git").is_file()),
    reason="macOS sandbox-exec or /usr/bin/git is unavailable",
)
def test_macos_profile_runs_system_git_xcrun_shim(tmp_path):
    import subprocess

    from src.llms.native_cli.isolation import _mac_profile

    root = tmp_path.resolve()
    work, home, tmp = root / "work", root / "home", root / "tmp"
    for path in (work, home, tmp):
        path.mkdir()
    profile = _mac_profile(work, home, tmp)

    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/git", "--version"],
        cwd=work,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(tmp)},
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.startswith(b"git version")
