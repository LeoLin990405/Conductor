#!/usr/bin/env python3
"""Inspect, install and de-duplicate the local Conductor desktop build.

One version number lives in ``desktop/package.json``. This tool compares it
with the built bundles and the installed app, copies a built bundle into
``~/Applications`` atomically, and removes the duplicate app that
electron-builder leaves under ``desktop/dist`` so Spotlight only finds the
installed copy.

Examples:
    python3 scripts/desktop_release.py status
    python3 scripts/desktop_release.py status --json
    python3 scripts/desktop_release.py install
    python3 scripts/desktop_release.py install --keep-build-app
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
BUNDLE_ID = "cn.conductor.desktop.oss"
INSTALL_TARGET = Path.home() / "Applications" / "Conductor.app"
BUILD_ROOTS = [DESKTOP / "dist"]
SEARCH_ROOTS = [Path.home() / "Applications", Path("/Applications"), DESKTOP / "dist"]

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


@dataclass
class Bundle:
    path: str
    version: str | None
    bundle_id: str | None
    signature: str | None = None
    note: str | None = None


@dataclass
class Status:
    repo_version: str
    installed: Bundle | None
    built: list[Bundle] = field(default_factory=list)
    duplicates: list[Bundle] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.problems


def read_plist(bundle: Path) -> dict:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as fh:
            return plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def is_app_bundle(path: Path) -> bool:
    return path.is_dir() and path.suffix == ".app" and (path / "Contents" / "Info.plist").is_file()


def signing_state(path: Path) -> str | None:
    """Classify the signature without pretending an ad-hoc signature is a release one."""
    codesign = shutil.which("codesign")
    if not codesign:
        return None
    try:
        result = subprocess.run([codesign, "-dv", "--verbose=2", str(path)], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stderr or "") + (result.stdout or "")
    if result.returncode != 0:
        return "unsigned"
    if "Authority=Developer ID" in output:
        return "developer-id"
    if "Signature=adhoc" in output or "adhoc" in output:
        return "ad-hoc"
    return "signed"


def bundle(path: Path) -> Bundle:
    info = read_plist(path)
    bid = info.get("CFBundleIdentifier")
    return Bundle(
        path=str(path),
        version=info.get("CFBundleShortVersionString"),
        bundle_id=bid if isinstance(bid, str) else None,
        signature=signing_state(path) if bid == BUNDLE_ID else None,
    )


def repo_version() -> str:
    data = json.loads((DESKTOP / "package.json").read_text())
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit("desktop/package.json has no version")
    return version


def find_apps(roots: list[Path], max_depth: int = 4) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if not root.is_dir():
            continue
        base_depth = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            path = Path(dirpath)
            if len(path.parts) - base_depth > max_depth:
                dirnames[:] = []
                continue
            # Do not descend into an app bundle: a nested Helper.app is not a
            # second Conductor.
            if path.name.endswith(".app"):
                dirnames[:] = []
                if is_app_bundle(path):
                    info = read_plist(path)
                    if info.get("CFBundleIdentifier") == BUNDLE_ID:
                        found.append(path)
                continue
    return found


def build_apps() -> list[Path]:
    return find_apps(BUILD_ROOTS, max_depth=5)


def installed_app() -> Path | None:
    return INSTALL_TARGET if is_app_bundle(INSTALL_TARGET) else None


def collect_status() -> Status:
    version = repo_version()
    installed_path = installed_app()
    installed = bundle(installed_path) if installed_path else None
    built = [bundle(path) for path in build_apps()]
    all_apps = [bundle(path) for path in find_apps(SEARCH_ROOTS)]
    duplicates = [item for item in all_apps if installed and item.path != installed.path]

    problems: list[str] = []
    warnings: list[str] = []
    if not installed:
        problems.append(f"Conductor is not installed at {INSTALL_TARGET}")
    elif installed.bundle_id != BUNDLE_ID:
        problems.append(f"installed bundle id is {installed.bundle_id!r}, expected {BUNDLE_ID}")
    elif installed.version != version:
        problems.append(f"installed {installed.version or '?'} != repo {version}; run scripts/desktop_release.py install")

    for item in built:
        if item.version != version:
            warnings.append(f"stale build artifact: {item.path} ({item.version or '?'})")
    if duplicates:
        warnings.append(f"{len(duplicates)} duplicate app bundle(s) besides the installed copy")
    if installed and installed.signature in {"unsigned", "ad-hoc"}:
        warnings.append(f"installed app is {installed.signature}; not notarized (expected for the OSS build)")
    return Status(version, installed, built, duplicates, problems, warnings)


def render(status: Status, color: bool = True) -> str:
    c = {k: (v if color else "") for k, v in {"ok": GREEN, "warn": YELLOW, "fail": RED, "dim": DIM}.items()}
    lines = ["Conductor desktop", f"repo: {DESKTOP}", f"version: {status.repo_version}", ""]
    if status.installed:
        item = status.installed
        lines.append(f"  {c['ok']}✓ installed      {item.path}  ({item.version or '?'}, {item.signature or 'signature unknown'})")
    else:
        lines.append(f"  {c['fail']}✗ installed      {INSTALL_TARGET}  (missing)")
    for item in status.built:
        lines.append(f"  {c['dim']}- built          {item.path}  ({item.version or '?'})")
    for item in status.duplicates:
        lines.append(f"  {c['warn']}! duplicate      {item.path}  ({item.version or '?'})")
    for warning in status.warnings:
        lines.append(f"  {c['warn']}! {warning}")
    for problem in status.problems:
        lines.append(f"  {c['fail']}✗ {problem}")
    lines.append("")
    lines.append("Result: READY" if status.ready else "Result: NOT READY")
    return "\n".join(lines)


def locate_build(arch: str | None) -> Path:
    machine = arch or platform.machine()
    wanted = {"arm64": "mac-arm64", "aarch64": "mac-arm64", "x86_64": "mac", "amd64": "mac"}.get(machine, machine)
    candidates = [DESKTOP / "dist" / "oss" / wanted / "Conductor.app"]
    for candidate in candidates:
        if is_app_bundle(candidate):
            return candidate
    all_built = build_apps()
    if not all_built:
        raise SystemExit("no built Conductor.app under desktop/dist; run `make desktop-dist` first")
    if len(all_built) == 1:
        return all_built[0]
    raise SystemExit(f"multiple built bundles; pass --arch. Found: {', '.join(str(p) for p in all_built)}")


def running_from(path: Path) -> bool:
    try:
        result = subprocess.run(["pgrep", "-f", str(path)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(result.stdout.strip())


def install(args: argparse.Namespace) -> int:
    source = locate_build(args.arch).resolve()
    info = read_plist(source)
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        print(f"refusing to install: {source} is not {BUNDLE_ID}", file=sys.stderr)
        return 1
    target = Path(args.target).expanduser().resolve() if args.target else INSTALL_TARGET
    for running_path in (source, target):
        if running_from(running_path):
            print(f"refusing to install: a process is running from {running_path}", file=sys.stderr)
            return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".installing")
    backup = target.with_name(target.name + ".previous")
    for stale in (temp, backup):
        if stale.exists():
            shutil.rmtree(stale)
    ditto = shutil.which("ditto")

    def copy(src: Path, dst: Path) -> None:
        if ditto:
            result = subprocess.run([ditto, str(src), str(dst)], capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "ditto failed")
        else:
            shutil.copytree(src, dst, symlinks=True)

    try:
        copy(source, temp)
        if target.exists():
            target.rename(backup)
        temp.rename(target)
    except Exception as exc:  # noqa: BLE001 - surface a release-tool error, not a traceback
        print(f"install failed: {exc}", file=sys.stderr)
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        if backup.exists() and not target.exists():
            backup.rename(target)
        return 1

    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)

    removed_builds = []
    if not args.keep_build_app:
        for path in build_apps():
            if path.resolve() == target:
                continue
            # Keep the DMG/ZIP as release artifacts; remove only the duplicate
            # .app bundle that Launch Services would index as a second Conductor.
            shutil.rmtree(path, ignore_errors=True)
            removed_builds.append(str(path))

    print(f"installed {source} -> {target}")
    print(f"version {info.get('CFBundleShortVersionString') or '?'} / {info.get('CFBundleIdentifier')}")
    if removed_builds:
        print("removed duplicate build app bundles:")
        for path in removed_builds:
            print(f"  {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Conductor desktop release/install helper")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="compare repo/build/installed versions and find duplicates")
    status.add_argument("--json", action="store_true")
    status.add_argument("--strict", action="store_true", help="treat warnings as failures")
    inst = sub.add_parser("install", help="install the built app into ~/Applications and remove duplicate build apps")
    inst.add_argument("--arch", choices=["arm64", "x86_64"], help="built architecture to install")
    inst.add_argument("--target", help=f"install target (default: {INSTALL_TARGET})")
    inst.add_argument("--keep-build-app", action="store_true", help="leave desktop/dist/**/Conductor.app in place")
    args = parser.parse_args()

    if args.command == "status":
        status_obj = collect_status()
        if args.json:
            print(json.dumps(asdict(status_obj) | {"ready": status_obj.ready}, indent=2, ensure_ascii=False))
        else:
            print(render(status_obj, color=sys.stdout.isatty()))
        failed = not status_obj.ready or (args.strict and status_obj.warnings)
        return 1 if failed else 0
    return install(args)


if __name__ == "__main__":
    sys.exit(main())
