"""I2 builder unit tests: ``instructions apply`` / ``rollback`` / ``snapshots``.

Every test runs in a tmp HOME. Operator-mode tests patch
``core.operator_home`` to a tmp dir, so the real HOME is never reached.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.instructions_hub import apply as hub
from src.instructions_hub import core
from src.instructions_hub.core import HubError, atomic_write, resolve_home
from src.instructions_hub.store import diff_one, status
from src.instructions_hub.targets import known_targets



def managed(payload: str) -> str:
    """A document the hub could have written: its header digest is valid."""
    prefix = "<!-- managed_by=conductor source="
    suffix = f" -->\n\n{payload}"
    digest = hashlib.sha256(f"{prefix}{suffix}".encode()).hexdigest()[:12]
    return f"{prefix}{digest}{suffix}"


MANAGED = managed("old body\n")


def _files(home: Path) -> dict[str, bytes]:
    """Every file under ``home`` except the hub's own state."""
    hub_dir = home / ".conductor" / "instructions"
    out = {}
    for path in sorted(home.rglob("*")):
        if path.is_file() and not path.is_relative_to(hub_dir / "snapshots"):
            if path == hub_dir / ".lock":
                continue
            out[str(path.relative_to(home))] = path.read_bytes()
    return out


def _row(plan: dict, suffix: str) -> dict:
    return next(row for row in plan["files"] if row["path"].endswith(suffix))


# -- plan -------------------------------------------------------------------


def test_dry_run_plans_every_file_once_and_writes_nothing(home: Path) -> None:
    plan = hub.plan_apply(home)
    assert plan["dry_run"] is True
    assert plan["refused"] == 0
    paths = [row["path"] for row in plan["files"]]
    assert len(paths) == len(set(paths))
    shared = _row(plan, ".agents/AGENTS.md")
    assert sorted(shared["targets"]) == ["dsh", "kimi"]
    assert all(row["action"] == "create" for row in plan["files"])
    assert plan["changes"] == len(plan["files"]) == 12
    assert shared["current_digest"] is None
    assert len(shared["rendered_digest"]) == 64
    assert shared["unified_diff"].startswith("--- /dev/null")
    assert list(home.rglob("*")) == []


def test_unmanaged_file_is_refused_until_taken_over(home: Path) -> None:
    claude = home / ".claude" / "CLAUDE.md"
    claude.parent.mkdir()
    claude.write_text("my notes\n", encoding="utf-8")
    row = _row(hub.plan_apply(home), ".claude/CLAUDE.md")
    assert (row["action"], row["reason"]) == ("refused", "unmanaged")
    # take_over names go through the alias table.
    row = _row(hub.plan_apply(home, take_over=["claude-code"]), ".claude/CLAUDE.md")
    assert row["action"] == "update"
    assert "my notes" in row["unified_diff"]


def test_managed_header_rules(home: Path) -> None:
    grok = home / ".grok" / "AGENTS.md"
    grok.parent.mkdir()
    grok.write_text(MANAGED, encoding="utf-8")
    mdc = home / ".cursor" / "rules" / "conductor.mdc"
    mdc.parent.mkdir(parents=True)
    mdc.write_text("---\nalwaysApply: true\n---\n\n" + MANAGED, encoding="utf-8")
    stub = home / ".claude" / "CLAUDE.md"
    stub.parent.mkdir()
    stub.write_text("@~/.conductor/instructions/rendered/worker.md\n", encoding="utf-8")
    body = home / ".conductor" / "instructions" / "rendered" / "operator.md"
    body.parent.mkdir(parents=True)
    body.write_text(managed("anything\n"), encoding="utf-8")
    zcode = home / ".zcode" / "AGENTS.md"
    zcode.parent.mkdir()
    zcode.write_text("intro\n" + MANAGED, encoding="utf-8")  # header not first
    plan = hub.plan_apply(home)
    assert _row(plan, ".grok/AGENTS.md")["action"] == "update"
    assert _row(plan, "conductor.mdc")["action"] == "update"
    assert _row(plan, ".claude/CLAUDE.md")["action"] == "update"
    assert _row(plan, "rendered/operator.md")["action"] == "update"
    assert _row(plan, ".zcode/AGENTS.md")["reason"] == "unmanaged"


def test_symlink_and_escape_are_refused_even_with_take_over(
    home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("x\n", encoding="utf-8")
    (home / ".grok").mkdir()
    (home / ".grok" / "AGENTS.md").symlink_to(outside / "AGENTS.md")
    (home / ".zcode").symlink_to(outside, target_is_directory=True)
    plan = hub.plan_apply(home, take_over=known_targets())
    assert _row(plan, ".grok/AGENTS.md")["reason"] == "symlink"
    assert _row(plan, ".zcode/AGENTS.md")["reason"] == "outside_home"
    with pytest.raises(HubError, match="refused"):
        hub.apply(home, take_over=known_targets())
    assert (outside / "AGENTS.md").read_text(encoding="utf-8") == "x\n"
    assert not (home / ".codex").exists()


def test_non_utf8_is_unreadable(home: Path) -> None:
    path = home / ".grok" / "AGENTS.md"
    path.parent.mkdir()
    path.write_bytes(b"\xff\xfe bad")
    row = _row(hub.plan_apply(home, take_over=["grok"]), ".grok/AGENTS.md")
    assert row["action"] == "refused"
    assert row["reason"].startswith("unreadable")


def test_two_targets_writing_different_bodies_to_one_path_conflict(
    home: Path, make_library
) -> None:
    library = make_library(
        {"a": "# A\n\nshared\n", "b": "# B\n\ncodex only\n"},
        {"op": {"fragments": ["a"], "extra": {"codex": ["b"]}}},
    )
    environ = {"CODEX_HOME": str(home / ".grok")}
    plan = hub.plan_apply(
        home, profile="op", library=library, targets=["codex", "grok"], environ=environ
    )
    row = _row(plan, ".grok/AGENTS.md")
    assert (row["action"], row["reason"]) == ("refused", "conflicting_targets")


def test_unknown_profile_and_target_raise(home: Path) -> None:
    with pytest.raises(HubError, match="unknown profile"):
        hub.plan_apply(home, profile="nope")
    with pytest.raises(HubError, match="unknown target"):
        hub.plan_apply(home, targets=["nope"])
    with pytest.raises(HubError, match="unknown target"):
        hub.plan_apply(home, take_over=["nope"])


# -- apply / rollback ------------------------------------------------------


def test_apply_diff_rollback_is_byte_identical(home: Path) -> None:
    grok = home / ".grok" / "AGENTS.md"
    grok.parent.mkdir()
    grok.write_text(MANAGED, encoding="utf-8")
    os.chmod(grok, 0o640)
    before = _files(home)

    result = hub.apply(home)
    assert result["dry_run"] is False
    snap = result["snapshot_id"]
    assert snap
    assert (grok.stat().st_mode & 0o777) == 0o640  # mode kept
    assert (home / ".zcode" / "AGENTS.md").stat().st_mode & 0o777 == 0o644
    assert all(row["matches"] for row in status(home)["targets"])
    for name in known_targets():
        assert diff_one(home, "operator", name)["same"] is True, name

    snap_dir = home / ".conductor" / "instructions" / "snapshots" / snap
    assert snap_dir.stat().st_mode & 0o777 == 0o700
    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "applied"
    assert manifest["operator"] is False
    record = next(r for r in manifest["files"] if r["path"] == str(grok))
    assert record["existed"] is True and record["mode"] == 0o640
    assert (snap_dir / record["blob"]).read_bytes() == MANAGED.encode()
    assert record["pre_digest"] == hashlib.sha256(MANAGED.encode()).hexdigest()

    again = hub.apply(home)
    assert again["changes"] == 0 and again["snapshot_id"] is None
    assert len(hub.list_snapshots(home)) == 1

    rolled = hub.rollback(home)
    assert rolled["snapshot_id"] == snap
    assert rolled["restored"] == [str(grok)]
    assert len(rolled["deleted"]) == 11
    assert _files(home) == before
    assert (grok.stat().st_mode & 0o777) == 0o640
    assert hub.list_snapshots(home)[0]["state"] == "rolled_back"
    with pytest.raises(HubError, match="no applied snapshot"):
        hub.rollback(home)


def test_refusal_writes_nothing_and_takes_no_snapshot(home: Path) -> None:
    (home / ".grok").mkdir()
    (home / ".grok" / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    with pytest.raises(HubError, match=r"\.grok/AGENTS\.md \(unmanaged\)"):
        hub.apply(home)
    assert not (home / ".zcode").exists()
    assert hub.list_snapshots(home) == []


def test_a_second_apply_while_one_holds_the_lock_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def nested() -> None:
        monkeypatch.setattr(hub, "hold_lock_hook", None)
        for call in (lambda: hub.apply(home), lambda: hub.rollback(home)):
            with pytest.raises(HubError, match="another instructions apply is running"):
                call()
            seen.append("refused")

    hub.apply(home, targets=["zcode"])  # so rollback has a snapshot to want
    monkeypatch.setattr(hub, "hold_lock_hook", nested)
    hub.apply(home)
    assert seen == ["refused", "refused"]
    lock_dir = home / ".conductor" / "instructions"
    assert lock_dir.stat().st_mode & 0o777 == 0o700


def test_compare_and_swap_aborts_and_restores(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    grok = home / ".grok" / "AGENTS.md"
    grok.parent.mkdir()
    grok.write_text(MANAGED, encoding="utf-8")
    zcode = home / ".zcode" / "AGENTS.md"
    zcode.parent.mkdir()
    zcode.write_text(MANAGED, encoding="utf-8")
    real = core.atomic_write
    calls: list[Path] = []

    def racing(path: Path, data, *, mode: int = 0o600) -> None:
        real(path, data, mode=mode)
        if path.name == "AGENTS.md" and not path.is_relative_to(home / ".conductor"):
            calls.append(path)
            if len(calls) == 1:
                # Someone edits zcode after the plan read it.
                zcode.write_text(MANAGED + "edited\n", encoding="utf-8")

    monkeypatch.setattr(core, "atomic_write", racing)
    before = _files(home)
    with pytest.raises(HubError, match=r"zcode/AGENTS\.md changed during apply"):
        hub.apply(home, targets=["grok", "zcode"])
    after = _files(home)
    assert after[".grok/AGENTS.md"] == before[".grok/AGENTS.md"]
    assert after[".zcode/AGENTS.md"] == (MANAGED + "edited\n").encode()
    assert hub.list_snapshots(home)[0]["state"] == "aborted"
    with pytest.raises(HubError, match="no applied snapshot"):
        hub.rollback(home)


def test_rollback_refuses_changed_files_unless_forced(home: Path) -> None:
    snap = hub.apply(home, targets=["zcode"])["snapshot_id"]
    zcode = home / ".zcode" / "AGENTS.md"
    zcode.write_text("edited\n", encoding="utf-8")
    with pytest.raises(HubError, match="changed_since_apply"):
        hub.rollback(home)
    assert zcode.read_text(encoding="utf-8") == "edited\n"
    result = hub.rollback(home, snap, force=True)
    assert result["deleted"] == [str(zcode)]
    assert not zcode.exists()
    assert zcode.parent.is_dir()  # directories are left


def test_only_the_newest_applied_snapshot_rolls_back(home: Path) -> None:
    first = hub.apply(home, targets=["zcode"])["snapshot_id"]
    second = hub.apply(home, targets=["grok"])["snapshot_id"]
    assert [item["id"] for item in hub.list_snapshots(home)] == [second, first]
    with pytest.raises(HubError, match=f"roll back {second} first"):
        hub.rollback(home, first)
    with pytest.raises(HubError, match="unknown snapshot"):
        hub.rollback(home, "20990101T000000000000")
    with pytest.raises(HubError, match="unknown snapshot"):
        hub.rollback(home, "../../etc")
    hub.rollback(home, second)
    with pytest.raises(HubError, match="rolled_back, not applied"):
        hub.rollback(home, second)
    hub.rollback(home, first)
    assert not (home / ".zcode" / "AGENTS.md").exists()
    assert not (home / ".grok" / "AGENTS.md").exists()


# -- operator mode -------------------------------------------------------


def test_operator_mode_only_accepts_the_operator_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "operator_home", lambda: home.resolve())
    with pytest.raises(HubError, match="refuses the operator HOME"):
        hub.plan_apply(home)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(HubError, match="operator mode"):
        hub.plan_apply(other, operator=True)
    with pytest.raises(HubError, match="operator mode"):
        resolve_home(other, operator=True)
    result = hub.apply(home, operator=True, targets=["zcode"])
    manifest = json.loads(
        (
            home / ".conductor" / "instructions" / "snapshots"
            / result["snapshot_id"] / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["operator"] is True
    assert status(home, operator=True, targets=["zcode"])["targets"][0]["matches"]
    assert diff_one(home, "operator", "zcode", operator=True)["same"] is True
    hub.rollback(home, operator=True)
    assert not (home / ".zcode" / "AGENTS.md").exists()


# -- atomic_write --------------------------------------------------------


def test_atomic_write_uses_unique_temp_names_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.md"
    names: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        names.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(core.os, "replace", spy)
    atomic_write(target, "a", mode=0o640)
    atomic_write(target, b"b")
    assert target.read_bytes() == b"b"
    assert names[0] != names[1]
    assert str(os.getpid()) in names[0]
    assert target.stat().st_mode & 0o777 == 0o600

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(core.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, "c")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.md"]


# -- FIX round regressions -------------------------------------------------


def test_a_managed_file_edited_by_another_tool_is_modified(home: Path) -> None:
    grok = home / ".grok" / "AGENTS.md"
    grok.parent.mkdir()
    grok.write_text(MANAGED + "a line another tool appended\n", encoding="utf-8")
    row = _row(hub.plan_apply(home), ".grok/AGENTS.md")
    assert (row["action"], row["reason"]) == ("refused", "modified")
    with pytest.raises(HubError, match=r"\(modified\)"):
        hub.apply(home)
    row = _row(hub.plan_apply(home, take_over=["grok"]), ".grok/AGENTS.md")
    assert row["action"] == "update" and row["take_over"] is True
    # A forged header digest is modified, not managed.
    grok.write_text(
        "<!-- managed_by=conductor source=000000000000 -->\n\nbody\n", encoding="utf-8"
    )
    assert _row(hub.plan_apply(home), ".grok/AGENTS.md")["reason"] == "modified"


def test_the_empty_cli_extra_file_is_clean(home: Path, make_library) -> None:
    # The 51-byte file DeepSeek-era applies left: header plus an empty body.
    empty = "<!-- managed_by=conductor source=58ce7afdd354 -->\n\n"
    assert managed("") == empty and len(empty.encode()) == 51
    dsh = home / ".dsh" / "AGENTS.md"
    dsh.parent.mkdir()
    dsh.write_text(empty, encoding="utf-8")
    plan = hub.plan_apply(home, targets=["dsh"])
    assert _row(plan, ".dsh/AGENTS.md")["action"] == "unchanged"
    # With CLI-extra fragments the clean empty file is an ordinary update.
    library = make_library(
        {"a": "# A\n\nshared\n", "b": "# B\n\ndsh only\n"},
        {"op": {"fragments": ["a"], "extra": {"dsh": ["b"]}}},
    )
    plan = hub.plan_apply(home, profile="op", library=library, targets=["dsh"])
    assert _row(plan, ".dsh/AGENTS.md")["action"] == "update"


def test_mdc_wrapper_and_body_are_both_checked(home: Path) -> None:
    mdc = home / ".cursor" / "rules" / "conductor.mdc"
    mdc.parent.mkdir(parents=True)
    wrapper = "---\nalwaysApply: true\n---\n\n"
    mdc.write_text(wrapper + MANAGED, encoding="utf-8")
    assert _row(hub.plan_apply(home), "conductor.mdc")["action"] == "update"
    mdc.write_text(wrapper + MANAGED + "edited\n", encoding="utf-8")
    assert _row(hub.plan_apply(home), "conductor.mdc")["reason"] == "modified"
    mdc.write_text("---\nalwaysApply: false\n---\n\n" + MANAGED, encoding="utf-8")
    assert _row(hub.plan_apply(home), "conductor.mdc")["reason"] == "modified"


def test_an_edited_rendered_body_is_modified(home: Path) -> None:
    body = home / ".conductor" / "instructions" / "rendered" / "operator.md"
    body.parent.mkdir(parents=True)
    body.write_text("hand edited\n", encoding="utf-8")
    assert _row(hub.plan_apply(home), "rendered/operator.md")["reason"] == "modified"


def test_a_stub_line_only_counts_for_the_claude_stub(home: Path) -> None:
    grok = home / ".grok" / "AGENTS.md"
    grok.parent.mkdir()
    grok.write_text("@~/.conductor/instructions/rendered/operator.md\n", encoding="utf-8")
    assert _row(hub.plan_apply(home), ".grok/AGENTS.md")["reason"] == "unmanaged"


def test_an_interrupt_during_apply_restores_and_aborts(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = core.atomic_write
    count = {"n": 0}

    def interrupted(path: Path, data, *, mode: int = 0o600) -> None:
        if path.name == "AGENTS.md" and not path.is_relative_to(home / ".conductor"):
            count["n"] += 1
            if count["n"] == 2:
                raise KeyboardInterrupt
        real(path, data, mode=mode)

    monkeypatch.setattr(core, "atomic_write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        hub.apply(home, targets=["grok", "zcode"])
    assert not (home / ".grok" / "AGENTS.md").exists()
    assert not (home / ".zcode" / "AGENTS.md").exists()
    assert hub.list_snapshots(home)[0]["state"] == "aborted"


def test_rollback_after_a_killed_apply_skips_unwritten_files(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A kill leaves no chance to abort: the manifest stays "applied" with
    # only some files written.
    grok = home / ".grok" / "AGENTS.md"
    grok.parent.mkdir()
    grok.write_text(MANAGED, encoding="utf-8")
    real = core.atomic_write
    count = {"n": 0}

    def killed(path: Path, data, *, mode: int = 0o600) -> None:
        if path.name == "AGENTS.md" and not path.is_relative_to(home / ".conductor"):
            count["n"] += 1
            if count["n"] == 2:
                raise SystemExit(137)
        real(path, data, mode=mode)

    monkeypatch.setattr(core, "atomic_write", killed)
    monkeypatch.setattr(hub, "_abort", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        hub.apply(home, targets=["zcode", "grok"])
    monkeypatch.setattr(core, "atomic_write", real)
    assert hub.list_snapshots(home)[0]["state"] == "applied"
    assert (home / ".zcode" / "AGENTS.md").exists()
    result = hub.rollback(home)
    assert result["deleted"] == [str(home / ".zcode" / "AGENTS.md")]
    assert result["skipped"] == [str(grok)]
    assert grok.read_text(encoding="utf-8") == MANAGED
    assert hub.list_snapshots(home)[0]["state"] == "rolled_back"


def test_rollback_with_nothing_to_do_creates_nothing(home: Path) -> None:
    with pytest.raises(HubError, match="no applied snapshot"):
        hub.rollback(home)
    with pytest.raises(HubError, match="unknown snapshot"):
        hub.rollback(home, "20260101T000000000000")
    assert list(home.rglob("*")) == []


def test_cli_rollback_of_the_operator_home_needs_yes(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    import io

    import instructions_hub.apply as cli_hub
    import instructions_hub.core as cli_core
    from src.conductor_cli.cli import main

    monkeypatch.setattr(cli_core, "operator_home", lambda: home.resolve())
    cli_hub.apply(home, operator=True, targets=["zcode"])
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = main(["instructions", "rollback", "--operator-home"])
    assert code == 1 and "--yes" in err.getvalue()
    assert (home / ".zcode" / "AGENTS.md").exists()
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["instructions", "snapshots", "--operator-home"]) == 0
        assert main(["instructions", "rollback", "--operator-home", "--yes"]) == 0
    assert not (home / ".zcode" / "AGENTS.md").exists()
