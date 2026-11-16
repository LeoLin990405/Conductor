"""Shell and Python must classify the GLM CLI the same way and agree on argv.

``scripts/dispatch_launch.sh``'s ``glm_grammar`` and
``ZCodeCliAdapter._is_clean_room_cli`` both implement the same two-step rule:
the invocation name ``zcode-cli`` wins (before any link is resolved), then the
resolved target's basename ``zcode-cli`` wins; anything else is the official
grammar. The fakes below have a *fixed* capability - one accepts only the
clean-room grammar, the other only the official one - so a wrong classification
makes the executed adapter argv or the launcher fail instead of silently
passing.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "dispatch_launch.sh"

RECORD_BODY = """python3 - "$FAKE_RECORD" "$0" "$@" <<'PY'
import json, sys
record, path = sys.argv[1], sys.argv[2]
with open(record, "w") as handle:
    json.dump({"path": path, "argv": sys.argv[3:]}, handle)
PY
exit 0
"""

# Capability is part of the fake's own file, not of how it was invoked.
CLEAN_ROOM_ONLY = """#!/bin/bash
for arg in "$@"; do
    case "$arg" in
        --surface)
            echo "unknown flag: $arg" >&2
            exit 23
            ;;
    esac
done
""" + RECORD_BODY

OFFICIAL_ONLY = """#!/bin/bash
for arg in "$@"; do
    case "$arg" in
        --model|--max-turns|--provider|-p)
            echo "unknown flag: $arg" >&2
            exit 23
            ;;
    esac
done
""" + RECORD_BODY


def _write(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _layout(root: Path) -> tuple[Path, Path, Path]:
    """bin/zcode (official wrapper), bin/zcode-cli -> lib/cli.js, bin/glm-custom."""
    lib = root / "lib"
    lib.mkdir()
    _write(lib, "cli.js", CLEAN_ROOM_ONLY)  # fixed clean-room capability
    _write(lib, "zcode-cli", CLEAN_ROOM_ONLY)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _write(bin_dir, "zcode", OFFICIAL_ONLY)  # fixed official capability
    (bin_dir / "zcode-cli").symlink_to("../lib/cli.js")
    (bin_dir / "glm-custom").symlink_to("../lib/zcode-cli")
    return bin_dir, lib, bin_dir / "zcode"


def _env(root: Path, bin_dir: Path, override: str | None) -> dict[str, str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(root),
        "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(root / "logs"),
        "FAKE_RECORD": str(root / "record.json"),
    }
    if override is not None:
        env["NATIVE_CLI_ZCODE_BIN"] = override
    return env


def _launch(root: Path, bin_dir: Path, override: str | None) -> dict:
    result = subprocess.run(
        ["bash", str(SCRIPT), "glm", "the task"],
        cwd=str(root),
        env=_env(root, bin_dir, override),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads((root / "record.json").read_text(encoding="utf-8"))


def _adapter_run(root: Path, bin_dir: Path, override: str | None) -> dict:
    """Build the real adapter invocation and execute it against the fixed fakes."""
    from src.llms.native_cli.zcode import ZCodeCliAdapter

    env = _env(root, bin_dir, override)
    saved = os.environ.copy()
    try:
        os.environ.update(env)
        invocation = ZCodeCliAdapter(cwd=str(root)).build_invocation(
            "the task", cwd=str(root)
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)
    run_env = dict(env)
    run_env.update(invocation.env or {})
    result = subprocess.run(
        list(invocation.argv),
        cwd=invocation.cwd or str(root),
        env=run_env,
        input=invocation.stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr, invocation.argv)
    return json.loads((root / "record.json").read_text(encoding="utf-8"))


def _python_flag(override: str | None, bin_dir: Path) -> bool:
    from src.llms.native_cli.zcode import _is_clean_room_cli

    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    try:
        return _is_clean_room_cli(override or "zcode")
    finally:
        os.environ["PATH"] = saved_path


def _grammar(argv: list[str]) -> str:
    return "official" if "--surface" in argv else "clean-room"


def test_python_classifies_the_shared_paths(tmp_path: Path) -> None:
    bin_dir, lib, official = _layout(tmp_path)
    from src.llms.native_cli.zcode import _is_clean_room_cli

    assert _is_clean_room_cli(str(lib / "cli.js")) is False
    assert _is_clean_room_cli(str(bin_dir / "zcode-cli")) is True  # fast path
    assert _is_clean_room_cli(str(bin_dir / "glm-custom")) is True  # resolved target
    assert _is_clean_room_cli(str(official)) is False


def test_a_bare_override_name_is_found_on_path(tmp_path: Path) -> None:
    """rev20 blocker: NATIVE_CLI_ZCODE_BIN=glm-custom must resolve through PATH."""
    bin_dir, _, _ = _layout(tmp_path)
    assert _python_flag("glm-custom", bin_dir) is True


def test_shell_and_adapter_agree_on_every_override(tmp_path: Path) -> None:
    bin_dir, lib, official = _layout(tmp_path)
    cases = [
        ("glm-custom", "glm-custom", True),
        ("bin/zcode-cli", str(bin_dir / "zcode-cli"), True),
        ("bin/glm-custom", str(bin_dir / "glm-custom"), True),
        ("bin/zcode", str(official), False),
    ]
    for label, override, clean in cases:
        python_clean = _python_flag(override, bin_dir)
        assert python_clean is clean, label
        shell = _launch(tmp_path, bin_dir, override)
        shell_clean = _grammar(shell["argv"]) == "clean-room"
        assert shell_clean is clean, (label, shell)
        adapter = _adapter_run(tmp_path, bin_dir, override)
        adapter_clean = _grammar(adapter["argv"]) == "clean-room"
        assert adapter_clean is clean, (label, adapter)
        assert shell_clean is adapter_clean, (label, shell, adapter)
        if clean:
            assert "--model" in shell["argv"] and "--surface" not in shell["argv"]
        else:
            assert "--surface" in shell["argv"] and "--model" not in shell["argv"]


def test_no_override_uses_official_zcode_even_with_zcode_cli_on_path(
    tmp_path: Path,
) -> None:
    bin_dir, _, official = _layout(tmp_path)
    shell = _launch(tmp_path, bin_dir, None)
    assert Path(shell["path"]).resolve() == official.resolve()
    assert _grammar(shell["argv"]) == "official"
    adapter = _adapter_run(tmp_path, bin_dir, None)
    assert Path(adapter["path"]).resolve() == official.resolve()
    assert _grammar(adapter["argv"]) == "official"
