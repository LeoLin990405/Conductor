"""Instructions hub I2 red team: apply and rollback must refuse, not guess.

Written against the I2 contract only (``inbox/i2-contract.md``). Every test
runs in a tmp HOME; ``operator_home`` is patched to a tmp dir wherever it
matters, so the real login HOME is never reached.
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
from src.conductor_cli.cli import main

HEADER = "<!-- managed_by=conductor source="
def hub_written(payload: str) -> str:
    """A managed document with a valid header digest, as render.py builds it."""
    prefix = "<!-- managed_by=conductor source="
    suffix = f" -->\n\n{payload}"
    digest = hashlib.sha256(f"{prefix}{suffix}".encode("utf-8")).hexdigest()[:12]
    return f"{prefix}{digest}{suffix}"


STALE = hub_written("stale body\n")
SPOOF = HEADER + "abcdef012345 -->\n"


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


def tree(root: Path) -> dict[str, object]:
    found: dict[str, object] = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in [*names, *[d for d in dirs if (Path(base) / d).is_symlink()]]:
            path = Path(base) / name
            if name == ".lock":
                continue
            rel = str(path.relative_to(root))
            if path.is_symlink():
                found[rel] = ("link", os.readlink(path))
            else:
                found[rel] = (path.read_bytes(), path.stat().st_mode & 0o7777)
    return found


def put(path: Path, text: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def patch_operator_home(monkeypatch: pytest.MonkeyPatch, where: Path) -> Path:
    """Point ``operator_home`` at ``where`` in every loaded copy of the hub."""
    where.mkdir(parents=True, exist_ok=True)
    real = where.resolve()
    for name, module in list(sys.modules.items()):
        if module is None or not hasattr(module, "operator_home"):
            continue
        if "instructions" in name:
            monkeypatch.setattr(module, "operator_home", lambda: real)
    return real


def patch_atomic_write(monkeypatch: pytest.MonkeyPatch, wrapper) -> None:
    for name, module in list(sys.modules.items()):
        if module is not None and "instructions_hub" in name and hasattr(module, "atomic_write"):
            monkeypatch.setattr(module, "atomic_write", wrapper)


def manifest_of(home: Path, snapshot_id: str) -> dict:
    path = home / ".conductor" / "instructions" / "snapshots" / snapshot_id / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def only_snapshot_dir(home: Path) -> Path:
    root = home / ".conductor" / "instructions" / "snapshots"
    dirs = [item for item in root.iterdir() if item.is_dir()]
    assert len(dirs) == 1
    return dirs[0]


# --- unmanaged files: all-or-nothing, take-over --------------------------------


def test_unmanaged_file_refuses_the_whole_apply(home: Path) -> None:
    put(home / ".codex" / "AGENTS.md", "my own codex rules\n")
    before = tree(home)
    with pytest.raises(HubError) as caught:
        hub_apply.apply(home)
    assert "AGENTS.md" in str(caught.value)
    # Nothing was written anywhere, not even for the nine clean targets.
    assert tree(home) == before
    assert not (home / ".grok").exists()
    assert not (home / ".conductor" / "instructions" / "snapshots").exists()
    plan = hub_apply.plan_apply(home)
    assert plan["refused"] == 1
    row = next(item for item in plan["files"] if item["action"] == "refused")
    assert row["target"] == "codex" and row["reason"] == "unmanaged"
    assert tree(home) == before


def test_take_over_accepts_the_unmanaged_file_and_rollback_restores_it(home: Path) -> None:
    path = put(home / ".codex" / "AGENTS.md", "my own codex rules\n", 0o640)
    result = hub_apply.apply(home, targets=["codex"], take_over=["codex"])
    assert result["snapshot_id"]
    assert path.read_text().startswith(HEADER)
    assert path.stat().st_mode & 0o777 == 0o640
    hub_apply.rollback(home)
    assert path.read_text() == "my own codex rules\n"
    assert path.stat().st_mode & 0o777 == 0o640


def test_take_over_goes_through_target_aliases(home: Path) -> None:
    put(home / ".minimax" / "AGENTS.md", "mine\n")
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["mcode"])
    hub_apply.apply(home, targets=["mcode"], take_over=["minimax"])
    assert (home / ".minimax" / "AGENTS.md").read_text().startswith(HEADER)


def test_take_over_for_one_target_does_not_cover_another(home: Path) -> None:
    put(home / ".codex" / "AGENTS.md", "mine\n")
    put(home / ".grok" / "AGENTS.md", "also mine\n")
    before = tree(home)
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["codex", "grok"], take_over=["codex"])
    assert tree(home) == before


def test_identical_unmanaged_file_is_unchanged_not_refused(home: Path) -> None:
    hub_apply.apply(home, targets=["codex"])
    body = (home / ".codex" / "AGENTS.md").read_bytes()
    # A file that already equals the render needs no take-over.
    plan = hub_apply.plan_apply(home, targets=["codex"])
    assert plan["changes"] == 0 and plan["refused"] == 0
    assert (home / ".codex" / "AGENTS.md").read_bytes() == body


def test_non_utf8_file_is_refused_as_unreadable(home: Path) -> None:
    path = home / ".zcode" / "AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    plan = hub_apply.plan_apply(home, targets=["zcode"], take_over=["zcode"])
    row = plan["files"][0]
    assert row["action"] == "refused" and row["reason"].startswith("unreadable")
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["zcode"], take_over=["zcode"])
    assert path.read_bytes() == b"\xff\xfe\x00bad"


def test_failed_apply_releases_the_lock(home: Path) -> None:
    path = put(home / ".codex" / "AGENTS.md", "mine\n")
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["codex"])
    path.write_text(STALE, encoding="utf-8")
    assert hub_apply.apply(home, targets=["codex"])["snapshot_id"]


# --- symlinks ---------------------------------------------------------------------


def test_symlinked_file_is_refused_even_with_take_over(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = put(outside / "victim.txt", "outside secret\n")
    link = home / ".codex" / "AGENTS.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(victim)
    before = tree(home)
    plan = hub_apply.plan_apply(home, targets=["codex"], take_over=["codex"])
    row = plan["files"][0]
    assert row["action"] == "refused" and row["reason"] in {"symlink", "outside_home"}
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["codex"], take_over=["codex"])
    assert victim.read_text() == "outside secret\n"
    assert link.is_symlink() and link.resolve() == victim.resolve()
    assert tree(home) == before


def test_dangling_symlink_is_not_written_through(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / ".grok" / "AGENTS.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside / "missing.txt")
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["grok"], take_over=["grok"])
    assert not (outside / "missing.txt").exists()
    assert link.is_symlink()


def test_symlinked_ancestor_is_refused_even_with_take_over(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".agents").symlink_to(outside)
    before = tree(home)
    with pytest.raises(HubError):
        hub_apply.apply(home, take_over=["kimi", "dsh"])
    assert list(outside.iterdir()) == []
    assert (home / ".agents").is_symlink()
    assert tree(home) == before
    assert not (home / ".codex").exists()


def test_symlinked_conductor_dir_cannot_redirect_snapshots(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".conductor").symlink_to(outside)
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["codex"])
    assert list(outside.iterdir()) == []
    assert not (home / ".codex" / "AGENTS.md").exists()


def test_cli_symlink_refusal(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = put(outside / "v.txt", "keep\n")
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.md").symlink_to(victim)
    code, _ = run_cli(
        ["instructions", "apply", "--home", str(home), "--apply",
         "--target", "codex", "--take-over", "codex"]
    )
    assert code == 1
    assert victim.read_text() == "keep\n"


# --- operator HOME -----------------------------------------------------------------


def test_home_equal_to_operator_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = patch_operator_home(monkeypatch, tmp_path / "operator")
    for call in (hub_apply.plan_apply, hub_apply.apply):
        with pytest.raises(HubError):
            call(op, targets=["codex"])
    with pytest.raises(HubError):
        hub_apply.rollback(op)
    with pytest.raises(HubError):
        hub_apply.list_snapshots(op)
    assert list(op.iterdir()) == []


def test_operator_flag_only_accepts_the_operator_home(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = patch_operator_home(monkeypatch, tmp_path / "operator")
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["codex"], operator=True)
    assert not (home / ".codex").exists()
    plan = hub_apply.plan_apply(op, targets=["codex"], operator=True)
    assert plan["dry_run"] is True
    assert list(op.iterdir()) == []
    result = hub_apply.apply(op, targets=["codex"], operator=True)
    assert (op / ".codex" / "AGENTS.md").read_text().startswith(HEADER)
    assert manifest_of(op, result["snapshot_id"])["operator"] is True
    hub_apply.rollback(op, operator=True)
    assert not (op / ".codex" / "AGENTS.md").exists()
    assert [row["state"] for row in hub_apply.list_snapshots(op, operator=True)] == [
        "rolled_back"
    ]


def test_cli_operator_home_apply_needs_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = patch_operator_home(monkeypatch, tmp_path / "operator")
    base = ["instructions", "apply", "--operator-home", "--target", "codex"]
    code, plan = run_cli(base)
    assert code == 0 and plan["dry_run"] is True
    assert list(op.iterdir()) == []

    code, message = run_cli([*base, "--apply"])
    assert code == 1
    assert "--yes" in str(message)
    assert list(op.iterdir()) == []

    code, result = run_cli([*base, "--apply", "--yes"])
    assert code == 0 and result["snapshot_id"]
    assert (op / ".codex" / "AGENTS.md").is_file()

    code, message = run_cli(["instructions", "rollback", "--operator-home"])
    assert code == 1 and "--yes" in str(message)
    assert (op / ".codex" / "AGENTS.md").is_file()
    code, snaps = run_cli(["instructions", "snapshots", "--operator-home"])
    assert code == 0
    code, rolled = run_cli(["instructions", "rollback", "--operator-home", "--yes"])
    assert code == 0
    assert not (op / ".codex" / "AGENTS.md").exists()


def test_cli_home_flag_refuses_the_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = patch_operator_home(monkeypatch, tmp_path / "operator")
    for command in ("apply", "snapshots", "rollback"):
        code, _ = run_cli(["instructions", command, "--home", str(op)])
        assert code == 1, command
    code, _ = run_cli(["instructions", "apply", "--home", str(op), "--apply", "--yes"])
    assert code == 1
    assert list(op.iterdir()) == []


def test_cli_operator_home_read_only_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_operator_home(monkeypatch, tmp_path / "operator")
    for argv in (
        ["instructions", "status", "--operator-home"],
        ["instructions", "list", "--operator-home"],
        ["instructions", "diff", "--operator-home", "--target", "codex"],
        ["instructions", "render", "--operator-home", "--target", "codex"],
        ["instructions", "snapshots", "--operator-home"],
    ):
        code, _ = run_cli(argv)
        assert code == 0, argv


def test_cli_operator_home_flag_without_patch_target_is_not_a_real_home(
    home: Path,
) -> None:
    # --home pointing at a tmp dir never needs --yes.
    code, result = run_cli(
        ["instructions", "apply", "--home", str(home), "--apply", "--target", "codex"]
    )
    assert code == 0 and result["snapshot_id"]


# --- override env ------------------------------------------------------------------


def test_override_env_outside_home_is_refused(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    before = tree(home)
    with pytest.raises(ValueError):
        hub_apply.apply(home, environ={"GROK_HOME": str(outside)})
    assert list(outside.iterdir()) == []
    assert tree(home) == before


def test_override_env_inside_home_is_honoured(home: Path) -> None:
    result = hub_apply.apply(
        home, targets=["grok"], environ={"GROK_HOME": str(home / "custom-grok")}
    )
    assert result["snapshot_id"]
    assert (home / "custom-grok" / "AGENTS.md").read_text().startswith(HEADER)
    assert not (home / ".grok").exists()


def test_cli_override_env_outside_home_is_refused(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GROK_HOME", str(outside))
    code, _ = run_cli(
        ["instructions", "apply", "--home", str(home), "--apply", "--target", "grok"]
    )
    assert code == 1
    assert list(outside.iterdir()) == []


# --- compare-and-swap and locking -------------------------------------------------


def _seed_managed(home: Path, targets: dict[str, tuple[str, ...]]) -> dict[str, Path]:
    return {
        name: put(home.joinpath(*parts), STALE) for name, parts in targets.items()
    }


def test_file_modified_during_apply_aborts_and_restores(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _seed_managed(
        home,
        {
            "codex": (".codex", "AGENTS.md"),
            "grok": (".grok", "AGENTS.md"),
            "zcode": (".zcode", "AGENTS.md"),
            "opencode": (".config", "opencode", "AGENTS.md"),
        },
    )
    concurrent = hub_written("concurrent edit\n")
    calls: list[Path] = []
    real_write = hub_apply.atomic_write if hasattr(hub_apply, "atomic_write") else None
    from instructions_hub import core

    original = real_write or core.atomic_write

    def meddling(path, data, *args, **kwargs):
        calls.append(Path(path))
        if len(calls) == 1:
            # A second writer touches every file this apply has not reached yet.
            for other in files.values():
                if other.resolve() != Path(path).resolve():
                    other.write_text(concurrent, encoding="utf-8")
        return original(path, data, *args, **kwargs)

    patch_atomic_write(monkeypatch, meddling)
    with pytest.raises(HubError) as caught:
        hub_apply.apply(home, targets=list(files))
    assert calls, "apply never went through atomic_write; the CAS hook did not fire"
    assert "changed during apply" in str(caught.value)
    written = Path(calls[0]).resolve()
    for path in files.values():
        if path.resolve() == written:
            # Already written, so it has to be put back.
            assert path.read_text() == STALE
        else:
            # Never overwritten, so the other writer's edit survives.
            assert path.read_text() == concurrent
    snapshot = only_snapshot_dir(home)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert manifest["state"] == "aborted"
    # An aborted snapshot is not a rollback candidate.
    with pytest.raises(HubError):
        hub_apply.rollback(home)


def test_file_created_during_apply_is_not_overwritten(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from instructions_hub import core

    targets = {"codex": home / ".codex" / "AGENTS.md", "grok": home / ".grok" / "AGENTS.md"}
    original = core.atomic_write
    seen: list[Path] = []

    def meddling(path, data, *args, **kwargs):
        seen.append(Path(path))
        if len(seen) == 1:
            for other in targets.values():
                if other.resolve() != Path(path).resolve():
                    put(other, "user created this meanwhile\n")
        return original(path, data, *args, **kwargs)

    patch_atomic_write(monkeypatch, meddling)
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=list(targets))
    assert seen
    first = seen[0].resolve()
    for other in targets.values():
        if other.resolve() == first:
            assert not other.exists()
        else:
            assert other.read_text() == "user created this meanwhile\n"


def test_concurrent_apply_and_rollback_are_blocked_by_the_lock(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_apply.apply(home, targets=["grok"])
    put(home / ".codex" / "AGENTS.md", STALE)
    errors: list[str] = []

    def hook() -> None:
        monkeypatch.setattr(hub_apply, "hold_lock_hook", None)
        for attempt in (
            lambda: hub_apply.apply(home, targets=["zcode"]),
            lambda: hub_apply.rollback(home),
        ):
            try:
                attempt()
            except HubError as exc:
                errors.append(str(exc))

    monkeypatch.setattr(hub_apply, "hold_lock_hook", hook)
    hub_apply.apply(home, targets=["codex"])
    assert len(errors) == 2
    assert all("another instructions apply is running" in text for text in errors)
    # The blocked attempts wrote nothing.
    assert not (home / ".zcode").exists()
    assert (home / ".grok" / "AGENTS.md").exists()
    # The lock is free again afterwards.
    hub_apply.rollback(home)


def test_dry_run_does_not_take_the_lock(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results: list[object] = []

    def hook() -> None:
        monkeypatch.setattr(hub_apply, "hold_lock_hook", None)
        results.append(hub_apply.plan_apply(home, targets=["codex"]))

    monkeypatch.setattr(hub_apply, "hold_lock_hook", hook)
    hub_apply.apply(home, targets=["codex"])
    assert results and results[0]["dry_run"] is True


# --- rollback rules ------------------------------------------------------------------


def test_rollback_refused_when_a_file_changed_after_apply(home: Path) -> None:
    put(home / ".grok" / "AGENTS.md", STALE, 0o600)
    result = hub_apply.apply(home, targets=["grok", "codex"])
    grok = home / ".grok" / "AGENTS.md"
    codex = home / ".codex" / "AGENTS.md"
    edited = grok.read_text() + "\nuser edit\n"
    grok.write_text(edited, encoding="utf-8")
    applied_codex = codex.read_bytes()
    with pytest.raises(HubError):
        hub_apply.rollback(home)
    # Refusal is all-or-nothing: the untouched file was not restored either.
    assert grok.read_text() == edited
    assert codex.read_bytes() == applied_codex
    assert manifest_of(home, result["snapshot_id"])["state"] == "applied"

    rolled = hub_apply.rollback(home, force=True)
    assert grok.read_text() == STALE
    assert grok.stat().st_mode & 0o777 == 0o600
    assert not codex.exists()
    assert rolled["snapshot_id"] == result["snapshot_id"]
    assert manifest_of(home, result["snapshot_id"])["state"] == "rolled_back"


def test_rollback_skips_created_file_deleted_but_refuses_an_edited_one(home: Path) -> None:
    hub_apply.apply(home, targets=["codex", "zcode"])
    codex = home / ".codex" / "AGENTS.md"
    zcode = home / ".zcode" / "AGENTS.md"
    codex.unlink()
    rolled = hub_apply.rollback(home)
    # A created file already gone is in its pre-apply state: skipped, not blocking.
    assert str(codex.resolve()) in [str(Path(p).resolve()) for p in rolled["skipped"]]
    assert not zcode.exists() and not codex.exists()

    hub_apply.apply(home, targets=["codex", "zcode"])
    zcode.write_text(zcode.read_text() + "edit\n", encoding="utf-8")
    edited = zcode.read_text()
    with pytest.raises(HubError):
        hub_apply.rollback(home)
    assert zcode.read_text() == edited and codex.exists()
    hub_apply.rollback(home, force=True)
    assert not zcode.exists() and not codex.exists()


def test_rollback_skips_a_file_already_holding_its_pre_apply_bytes(home: Path) -> None:
    grok = put(home / ".grok" / "AGENTS.md", STALE)
    hub_apply.apply(home, targets=["grok"])
    grok.write_text(STALE, encoding="utf-8")
    rolled = hub_apply.rollback(home)
    assert [Path(p).resolve() for p in rolled["skipped"]] == [grok.resolve()]
    assert grok.read_text() == STALE


def test_rollback_with_nothing_to_roll_back_creates_no_lock(home: Path) -> None:
    with pytest.raises(HubError):
        hub_apply.rollback(home)
    assert not (home / ".conductor" / "instructions" / ".lock").exists()
    code, _ = run_cli(["instructions", "rollback", "--home", str(home)])
    assert code == 1
    assert not (home / ".conductor" / "instructions" / ".lock").exists()


def test_hub_written_file_edited_by_another_tool_is_refused_as_modified(home: Path) -> None:
    grok = home / ".grok" / "AGENTS.md"
    put(grok, hub_written("old\n"))
    hub_apply.apply(home, targets=["grok"])
    grok.write_text(grok.read_text() + "line appended by another tool\n", encoding="utf-8")
    before = tree(home)
    plan = hub_apply.plan_apply(home, targets=["grok", "codex"])
    row = next(r for r in plan["files"] if r["target"] == "grok")
    assert row["action"] == "refused" and row["reason"] == "modified"
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["grok", "codex"])
    assert tree(home) == before
    assert not (home / ".codex").exists()
    result = hub_apply.apply(home, targets=["grok", "codex"], take_over=["grok"])
    assert result["snapshot_id"]
    assert "line appended" not in grok.read_text()
    assert (home / ".codex" / "AGENTS.md").is_file()


def test_forged_digest_is_modified_not_managed(home: Path) -> None:
    put(home / ".grok" / "AGENTS.md", HEADER + "000000000000 -->\n\nforged\n")
    plan = hub_apply.plan_apply(home, targets=["grok"])
    assert plan["files"][0]["action"] == "refused"
    assert plan["files"][0]["reason"] == "modified"


def test_interrupt_during_apply_aborts_restores_and_propagates(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from instructions_hub import core

    files = _seed_managed(
        home,
        {
            "codex": (".codex", "AGENTS.md"),
            "grok": (".grok", "AGENTS.md"),
            "zcode": (".zcode", "AGENTS.md"),
        },
    )
    original = core.atomic_write
    calls: list[Path] = []

    def interrupting(path, data, *args, **kwargs):
        # Snapshot blobs also go through atomic_write; count target files only.
        if Path(path).resolve() in targets:
            calls.append(Path(path))
            if len(calls) == 2:
                raise KeyboardInterrupt
        return original(path, data, *args, **kwargs)

    targets = {p.resolve() for p in files.values()}
    patch_atomic_write(monkeypatch, interrupting)
    with pytest.raises(KeyboardInterrupt):
        hub_apply.apply(home, targets=list(files))
    assert len(calls) >= 2  # the restore of the first file also writes
    # The first file was written and then put back; the others never changed.
    for path in files.values():
        assert path.read_text() == STALE
    manifest = json.loads((only_snapshot_dir(home) / "manifest.json").read_text())
    assert manifest["state"] == "aborted"


def test_older_snapshot_cannot_roll_back_under_a_newer_one(home: Path) -> None:
    first = hub_apply.apply(home, profile="operator", targets=["codex"])
    second = hub_apply.apply(home, profile="worker", targets=["codex"])
    assert second["snapshot_id"] and first["snapshot_id"] != second["snapshot_id"]
    codex = home / ".codex" / "AGENTS.md"
    current = codex.read_bytes()
    with pytest.raises(HubError) as caught:
        hub_apply.rollback(home, first["snapshot_id"])
    assert second["snapshot_id"] in str(caught.value)
    assert codex.read_bytes() == current
    # Newest first, then the older one.
    hub_apply.rollback(home, second["snapshot_id"])
    hub_apply.rollback(home, first["snapshot_id"])
    assert not codex.exists()


def test_rollback_without_any_snapshot_is_an_error(home: Path) -> None:
    with pytest.raises(HubError):
        hub_apply.rollback(home)
    assert hub_apply.list_snapshots(home) == []


def test_rollback_does_not_remove_directories(home: Path) -> None:
    hub_apply.apply(home, targets=["zcode"])
    hub_apply.rollback(home)
    assert (home / ".zcode").is_dir()
    assert not (home / ".zcode" / "AGENTS.md").exists()


# --- size budget -----------------------------------------------------------------------


def _fragment(payload_bytes: int) -> str:
    head = "# Big\n\n"
    return head + "x" * (payload_bytes - len(head) - 1) + "\n"


def test_over_budget_file_is_refused_never_truncated(home: Path, make_library) -> None:
    from .conftest import HEADER_BYTES

    library = make_library(
        {"big": _fragment(8192 - HEADER_BYTES + 1)}, {"fat": {"fragments": ["big"]}}
    )
    before = tree(home)
    with pytest.raises(HubError) as caught:
        hub_apply.apply(home, profile="fat", library=library, targets=["codex"])
    assert "budget" in str(caught.value).lower()
    assert tree(home) == before
    # One oversized target in a full apply stops the others as well.
    with pytest.raises(HubError):
        hub_apply.apply(home, profile="fat", library=library)
    assert tree(home) == before


def test_exactly_at_budget_is_written_whole(home: Path, make_library) -> None:
    from .conftest import HEADER_BYTES

    library = make_library(
        {"big": _fragment(8192 - HEADER_BYTES)}, {"fat": {"fragments": ["big"]}}
    )
    hub_apply.apply(home, profile="fat", library=library, targets=["codex"])
    assert (home / ".codex" / "AGENTS.md").stat().st_size == 8192


def test_cli_over_budget_is_refused(home: Path, make_library) -> None:
    library = make_library({"big": "# Big\n\n" + "x" * 9000 + "\n"}, {"fat": {"fragments": ["big"]}})
    before = tree(home)
    code, message = run_cli(
        ["instructions", "apply", "--home", str(home), "--apply", "--profile", "fat",
         "--library", str(library), "--target", "codex"]
    )
    assert code == 1 and "budget" in str(message).lower()
    assert tree(home) == before


# --- format injection ------------------------------------------------------------------


def test_a_header_lookalike_after_the_first_line_is_not_managed(home: Path) -> None:
    put(home / ".codex" / "AGENTS.md", "my notes\n" + SPOOF)
    put(
        home / ".cursor" / "rules" / "conductor.mdc",
        "---\nalwaysApply: true\n---\n\nuser rule\n" + SPOOF,
    )
    put(home / ".config" / "opencode" / "AGENTS.md", "---\n" + SPOOF + "rest\n")
    put(home / ".grok" / "AGENTS.md", "\n" + SPOOF)
    plan = hub_apply.plan_apply(
        home, targets=["codex", "cursor-agent", "opencode", "grok"]
    )
    actions = {row["target"]: (row["action"], row.get("reason")) for row in plan["files"]}
    assert actions == {
        "codex": ("refused", "unmanaged"),
        "cursor-agent": ("refused", "unmanaged"),
        "opencode": ("refused", "unmanaged"),
        "grok": ("refused", "unmanaged"),
    }
    before = tree(home)
    with pytest.raises(HubError):
        hub_apply.apply(home, targets=["codex", "cursor-agent", "opencode", "grok"])
    assert tree(home) == before


def test_fragment_forging_a_managed_header_is_refused_everywhere(
    home: Path, make_library
) -> None:
    library = make_library(
        {"nasty": "# Nasty\n\n" + SPOOF + "more\n"}, {"inj": {"fragments": ["nasty"]}}
    )
    before = tree(home)
    # Either the render or the plan refuses; nothing may be written either way.
    with pytest.raises(HubError):
        hub_apply.apply(home, profile="inj", library=library)
    assert tree(home) == before
    with contextlib.suppress(HubError):
        plan = hub_apply.plan_apply(home, profile="inj", library=library)
        assert plan["refused"] == len(plan["files"])


def test_fragment_with_frontmatter_and_toml_text_stays_inert(
    home: Path, make_library
) -> None:
    nasty = (
        "# Nasty\n\n"
        "---\nalwaysApply: false\n---\n"
        "\n```toml\n[mcp_servers.evil]\ncommand = \"x\"\n```\n"
    )
    library = make_library({"nasty": nasty}, {"inj": {"fragments": ["nasty"]}})
    hub_apply.apply(home, profile="inj", library=library)
    cursor = (home / ".cursor" / "rules" / "conductor.mdc").read_text()
    # One real front matter block at the top, ahead of the real header.
    assert cursor.startswith("---\nalwaysApply: true\n---\n\n" + HEADER)
    assert cursor.count("alwaysApply: true") == 1
    for rel in (
        (".codex", "AGENTS.md"),
        (".grok", "AGENTS.md"),
        (".agents", "AGENTS.md"),
        (".zcode", "AGENTS.md"),
    ):
        text = home.joinpath(*rel).read_text()
        assert text.startswith(HEADER)
        assert text.count(HEADER) == 1
    again = hub_apply.apply(home, profile="inj", library=library)
    assert again["snapshot_id"] is None and again["changes"] == 0
    hub_apply.rollback(home)
    assert not (home / ".cursor" / "rules" / "conductor.mdc").exists()


def test_a_managed_document_quoted_in_a_user_file_is_not_managed(home: Path) -> None:
    # A user file that quotes a managed document in the middle is still theirs.
    hub_apply.apply(home, targets=["grok"])
    quoted = (home / ".grok" / "AGENTS.md").read_text()
    put(home / ".codex" / "AGENTS.md", "my text before\n\n" + quoted)
    plan = hub_apply.plan_apply(home, targets=["codex"])
    assert plan["files"][0]["action"] == "refused"
    assert plan["files"][0]["reason"] == "unmanaged"
