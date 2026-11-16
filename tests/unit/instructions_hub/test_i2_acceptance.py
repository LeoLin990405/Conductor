"""Instructions hub I2 acceptance: apply, diff, rollback for all ten targets.

Written against the I2 contract only (``inbox/i2-contract.md``). Every test
runs in a tmp HOME; the operator HOME is never touched.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

import pytest

import instructions_hub.apply as hub_apply
from instructions_hub.core import HubError
from instructions_hub.store import diff_one, status
from instructions_hub.targets import known_targets
from src.conductor_cli.cli import main

HEADER = "<!-- managed_by=conductor source="
def hub_written(payload: str) -> str:
    """A managed document with a valid header digest, as render.py builds it."""
    prefix = "<!-- managed_by=conductor source="
    suffix = f" -->\n\n{payload}"
    digest = hashlib.sha256(f"{prefix}{suffix}".encode("utf-8")).hexdigest()[:12]
    return f"{prefix}{digest}{suffix}"


STALE = hub_written("stale body\n")
# Four targets that had no global file before the hub.
NEW_GLOBAL_FILES = {
    "grok": (".grok", "AGENTS.md"),
    "mcode": (".minimax", "AGENTS.md"),
    "step": (".stepcode", "agent", "AGENTS.md"),
    "zcode": (".zcode", "AGENTS.md"),
}


def run_cli(argv: list[str]) -> tuple[int, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    text = stdout.getvalue().strip()
    err = stderr.getvalue().strip()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text or err


def tree(root: Path) -> dict[str, tuple[object, int]]:
    """Every file under ``root`` -> (bytes or link target, mode). No dirs."""
    found: dict[str, tuple[object, int]] = {}
    for base, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            path = Path(base) / name
            rel = str(path.relative_to(root))
            if name == ".lock":
                continue
            if path.is_symlink():
                found[rel] = (("link", os.readlink(path)), 0)
            else:
                found[rel] = (path.read_bytes(), path.stat().st_mode & 0o7777)
    return found


def tree_no_snapshots(root: Path) -> dict[str, tuple[object, int]]:
    snap = ".conductor/instructions/snapshots"
    return {k: v for k, v in tree(root).items() if not k.startswith(snap)}


def seed_mixed(home: Path) -> None:
    """Files that exist before the first apply, with distinct modes."""
    plain = {
        (".codex", "AGENTS.md"): ("user codex\n", 0o640),
        (".grok", "AGENTS.md"): (STALE, 0o600),
        (".claude", "CLAUDE.md"): ("user claude notes\n", 0o644),
        (".agents", "AGENTS.md"): ("user shared agents\n", 0o664),
    }
    for parts, (text, mode) in plain.items():
        path = home.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, mode)


MIXED_TAKE_OVER = ["codex", "claude", "kimi", "dsh"]


def snapshot_manifest(home: Path, snapshot_id: str) -> dict:
    path = home / ".conductor" / "instructions" / "snapshots" / snapshot_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --- dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing(home: Path) -> None:
    seed_mixed(home)
    before = tree(home)
    plan = hub_apply.plan_apply(home, take_over=MIXED_TAKE_OVER)
    assert plan["dry_run"] is True
    assert plan["refused"] == 0
    assert plan["changes"] > 0
    assert tree(home) == before
    # plan_apply must not even create the library or snapshot directories.
    assert not (home / ".conductor" / "instructions" / "snapshots").exists()


def test_plan_lists_each_shared_path_once(home: Path) -> None:
    plan = hub_apply.plan_apply(home)
    paths = [row["path"] for row in plan["files"]]
    assert len(paths) == len(set(paths))
    # claude stub + rendered, codex, shared, kimi cli, grok, cursor, mcode,
    # step, zcode, dsh cli, opencode.
    assert len(paths) == 12
    assert str((home / ".agents" / "AGENTS.md").resolve()) in paths
    assert {row["action"] for row in plan["files"]} == {"create"}
    for row in plan["files"]:
        assert row["current_digest"] is None
        assert row["rendered_digest"]
        assert row["bytes"] > 0


def test_plan_reports_update_unchanged_and_refused(home: Path) -> None:
    seed_mixed(home)
    plan = hub_apply.plan_apply(home, targets=["codex", "grok"])
    by_target = {row["target"]: row for row in plan["files"]}
    assert by_target["codex"]["action"] == "refused"
    assert by_target["codex"]["reason"] == "unmanaged"
    assert by_target["grok"]["action"] == "update"
    assert by_target["grok"]["unified_diff"]
    assert plan["refused"] == 1
    hub_apply.apply(home, targets=["grok"])
    again = hub_apply.plan_apply(home, targets=["grok"])
    assert {row["action"] for row in again["files"]} == {"unchanged"}
    assert again["changes"] == 0


# --- apply / diff / rollback round trip -------------------------------------


@pytest.mark.parametrize("target", known_targets())
def test_each_target_round_trips_on_an_empty_home(home: Path, target: str) -> None:
    before = tree(home)
    result = hub_apply.apply(home, targets=[target])
    assert result["dry_run"] is False
    assert result["snapshot_id"]
    assert all(row["action"] == "create" for row in result["files"])
    rows = status(home, targets=[target])["targets"]
    assert all(row["matches"] for row in rows)
    assert diff_one(home, "operator", target)["same"] is True
    hub_apply.rollback(home)
    assert tree_no_snapshots(home) == before


def test_all_ten_targets_apply_status_diff_rollback_byte_identical(home: Path) -> None:
    seed_mixed(home)
    before = tree(home)
    result = hub_apply.apply(home, take_over=MIXED_TAKE_OVER)
    assert result["snapshot_id"]
    assert result["dry_run"] is False
    report = status(home)
    assert len(report["targets"]) == 10
    assert all(row["matches"] for row in report["targets"])
    for target in known_targets():
        assert diff_one(home, "operator", target)["same"] is True

    # Modes survive on files that already existed; new files are 0o644.
    assert (home / ".codex" / "AGENTS.md").stat().st_mode & 0o777 == 0o640
    assert (home / ".grok" / "AGENTS.md").stat().st_mode & 0o777 == 0o600
    assert (home / ".agents" / "AGENTS.md").stat().st_mode & 0o777 == 0o664
    assert (home / ".zcode" / "AGENTS.md").stat().st_mode & 0o777 == 0o644
    assert (home / ".zcode").stat().st_mode & 0o777 == 0o755

    rolled = hub_apply.rollback(home)
    assert rolled["snapshot_id"] == result["snapshot_id"]
    assert tree_no_snapshots(home) == before
    # Files that did not exist are gone again; the others were restored.
    assert not (home / ".zcode" / "AGENTS.md").exists()
    assert len(rolled["deleted"]) + len(rolled["restored"]) == 12
    assert rolled["restored"]
    assert rolled["deleted"]
    after = status(home)
    assert not all(row["matches"] for row in after["targets"])


def test_rollback_to_nothing_leaves_no_managed_file(home: Path) -> None:
    hub_apply.apply(home)
    hub_apply.rollback(home)
    leftovers = [
        rel
        for rel in tree_no_snapshots(home)
        if rel != ".conductor/instructions/.lock"
    ]
    assert leftovers == []


def test_new_global_files_for_grok_mcode_step_zcode(home: Path) -> None:
    for target, parts in NEW_GLOBAL_FILES.items():
        path = home.joinpath(*parts)
        assert not path.exists(), target
    hub_apply.apply(home, targets=list(NEW_GLOBAL_FILES))
    for target, parts in NEW_GLOBAL_FILES.items():
        path = home.joinpath(*parts)
        assert path.is_file(), target
        assert path.read_text(encoding="utf-8").startswith(HEADER)


def test_kimi_and_dsh_share_agents_md_written_once(home: Path) -> None:
    result = hub_apply.apply(home, targets=["kimi", "dsh"])
    shared = (home / ".agents" / "AGENTS.md").resolve()
    rows = [row for row in result["files"] if Path(row["path"]) == shared]
    assert len(rows) == 1
    manifest = snapshot_manifest(home, result["snapshot_id"])
    shared_entries = [
        entry for entry in manifest["files"] if Path(entry["path"]) == shared
    ]
    assert len(shared_entries) == 1
    # The CLI files are separate: shared, .kimi-code and .dsh.
    assert len(manifest["files"]) == 3
    assert (home / ".kimi-code" / "AGENTS.md").is_file()
    assert (home / ".dsh" / "AGENTS.md").is_file()


def test_second_apply_with_no_changes_has_no_snapshot(home: Path) -> None:
    first = hub_apply.apply(home)
    assert first["snapshot_id"]
    before = tree(home)
    listed = hub_apply.list_snapshots(home)
    second = hub_apply.apply(home)
    assert second["snapshot_id"] is None
    assert second["changes"] == 0
    assert second["dry_run"] is False
    assert tree(home) == before
    assert len(hub_apply.list_snapshots(home)) == len(listed) == 1


def test_snapshot_manifest_shape(home: Path) -> None:
    seed_mixed(home)
    result = hub_apply.apply(home, take_over=MIXED_TAKE_OVER)
    manifest = snapshot_manifest(home, result["snapshot_id"])
    assert manifest["id"] == result["snapshot_id"]
    assert manifest["state"] == "applied"
    assert manifest["profile"] == "operator"
    assert manifest["operator"] is False
    assert len(manifest["files"]) == 12
    existed = {Path(e["path"]).name: e for e in manifest["files"] if e["existed"]}
    assert existed
    for entry in manifest["files"]:
        if entry["existed"]:
            assert entry["blob"] and entry["mode"] is not None
            blob = (
                home / ".conductor" / "instructions" / "snapshots"
                / result["snapshot_id"] / entry["blob"]
            )
            assert blob.is_file()
        else:
            assert entry["blob"] is None and entry["mode"] is None
        assert entry["post_digest"]
    snap_dir = home / ".conductor" / "instructions" / "snapshots" / result["snapshot_id"]
    assert snap_dir.stat().st_mode & 0o777 == 0o700


def test_list_snapshots_newest_first_and_states(home: Path) -> None:
    first = hub_apply.apply(home, profile="operator")
    second = hub_apply.apply(home, profile="worker")
    assert second["snapshot_id"] and second["snapshot_id"] != first["snapshot_id"]
    listed = hub_apply.list_snapshots(home)
    assert [item["id"] for item in listed] == [second["snapshot_id"], first["snapshot_id"]]
    assert {item["state"] for item in listed} == {"applied"}
    assert listed[0]["profile"] == "worker"
    assert listed[0]["files"] > 0
    hub_apply.rollback(home)
    states = {item["id"]: item["state"] for item in hub_apply.list_snapshots(home)}
    assert states[second["snapshot_id"]] == "rolled_back"
    assert states[first["snapshot_id"]] == "applied"


def test_rollback_twice_has_nothing_left(home: Path) -> None:
    hub_apply.apply(home)
    hub_apply.rollback(home)
    with pytest.raises(HubError):
        hub_apply.rollback(home)


def test_rollback_named_snapshot_must_exist(home: Path) -> None:
    hub_apply.apply(home)
    with pytest.raises(HubError):
        hub_apply.rollback(home, "20990101T000000000000")


# --- CLI ---------------------------------------------------------------------


def test_help_advertises_apply_rollback_and_snapshots() -> None:
    stdout = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stdout(stdout):
            main(["instructions", "--help"])
    assert caught.value.code == 0
    text = stdout.getvalue()
    for name in ("list", "status", "render", "diff", "apply", "rollback", "snapshots"):
        assert name in text


def test_cli_apply_dry_run_then_apply_then_rollback(home: Path) -> None:
    before = tree(home)
    code, plan = run_cli(["instructions", "apply", "--home", str(home)])
    assert code == 0
    assert plan["dry_run"] is True
    assert plan["changes"] == 12
    assert tree(home) == before

    code, applied = run_cli(["instructions", "apply", "--home", str(home), "--apply"])
    assert code == 0
    assert applied["dry_run"] is False
    snapshot_id = applied["snapshot_id"]
    assert snapshot_id
    code, report = run_cli(["instructions", "status", "--home", str(home)])
    assert code == 0
    assert all(row["matches"] for row in report["targets"])

    code, listed = run_cli(["instructions", "snapshots", "--home", str(home)])
    assert code == 0
    rows = listed if isinstance(listed, list) else listed["snapshots"]
    assert [row["id"] for row in rows] == [snapshot_id]
    assert rows[0]["state"] == "applied"

    code, rolled = run_cli(["instructions", "rollback", "--home", str(home)])
    assert code == 0
    assert rolled["snapshot_id"] == snapshot_id
    assert len(rolled["deleted"]) == 12
    assert tree_no_snapshots(home) == before

    code, listed = run_cli(["instructions", "snapshots", "--home", str(home)])
    rows = listed if isinstance(listed, list) else listed["snapshots"]
    assert rows[0]["state"] == "rolled_back"


def test_cli_rollback_names_a_snapshot(home: Path) -> None:
    code, applied = run_cli(["instructions", "apply", "--home", str(home), "--apply"])
    assert code == 0
    code, rolled = run_cli(
        ["instructions", "rollback", applied["snapshot_id"], "--home", str(home)]
    )
    assert code == 0
    assert rolled["snapshot_id"] == applied["snapshot_id"]


def test_cli_target_and_take_over_are_repeatable(home: Path) -> None:
    seed_mixed(home)
    code, plan = run_cli(
        [
            "instructions", "apply", "--home", str(home),
            "--target", "codex", "--target", "grok",
            "--take-over", "codex",
        ]
    )
    assert code == 0
    assert {row["target"] for row in plan["files"]} == {"codex", "grok"}
    assert plan["refused"] == 0
    code, applied = run_cli(
        [
            "instructions", "apply", "--home", str(home), "--apply",
            "--target", "codex", "--target", "grok", "--take-over", "codex",
        ]
    )
    assert code == 0
    assert (home / ".codex" / "AGENTS.md").read_text().startswith(HEADER)


def test_cli_dry_run_with_refusals_exits_zero_apply_fails(home: Path) -> None:
    seed_mixed(home)
    before = tree(home)
    code, plan = run_cli(["instructions", "apply", "--home", str(home), "--target", "codex"])
    assert code == 0
    assert plan["refused"] == 1
    code, message = run_cli(
        ["instructions", "apply", "--home", str(home), "--target", "codex", "--apply"]
    )
    assert code == 1
    assert str(home / ".codex" / "AGENTS.md") in str(message) or "unmanaged" in str(message)
    assert tree(home) == before


def test_cli_home_and_operator_home_are_mutually_exclusive(home: Path) -> None:
    for argv in (
        ["instructions", "apply", "--home", str(home), "--operator-home"],
        ["instructions", "apply"],
        ["instructions", "snapshots"],
        ["instructions", "rollback"],
        ["instructions", "status"],
    ):
        with contextlib.redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as caught:
                main(argv)
        assert caught.value.code == 2, argv


def test_cli_rollback_without_snapshot_fails(home: Path) -> None:
    code, message = run_cli(["instructions", "rollback", "--home", str(home)])
    assert code == 1
    assert message


def test_cli_snapshots_on_a_fresh_home_is_empty(home: Path) -> None:
    code, listed = run_cli(["instructions", "snapshots", "--home", str(home)])
    assert code == 0
    rows = listed if isinstance(listed, list) else listed.get("snapshots", listed)
    assert rows == []


def test_modules_under_test_are_loaded_once() -> None:
    # The CLI and these tests must reach the same apply module, or the
    # monkeypatched hooks in the red-team file would miss the CLI.
    assert "instructions_hub.apply" in sys.modules
