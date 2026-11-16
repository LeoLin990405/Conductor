"""List stale worktrees, daemon registrations, and dead pid files.

The default is a report. ``--apply`` removes only merged, clean worktrees,
stale registration rows, and pid files whose process is already gone.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .ops_runtime import prepare
from .ops_support import OpsError, RealSystem, Settings


def _git(system: RealSystem, cwd: Path, *args: str) -> tuple[int, str, str]:
    result = system.run(["git", "-C", str(cwd), *args])
    return result.code, result.stdout, result.stderr


def _classify_worktree(
    system: RealSystem, path: Path, base_ref: str
) -> dict[str, object]:
    code, _out, err = _git(system, path, "rev-parse", "--verify", "HEAD")
    if code != 0:
        return {
            "path": str(path),
            "class": "broken",
            "dirty": False,
            "reason": "missing HEAD",
        }
    status_code, status, _status_err = _git(system, path, "status", "--porcelain")
    if status_code != 0:
        return {
            "path": str(path),
            "class": "broken",
            "dirty": False,
            "reason": "git status failed",
        }
    dirty = bool(status.strip())
    merged_code, _merged, _merged_err = _git(
        system, path, "merge-base", "--is-ancestor", "HEAD", base_ref
    )
    merged = merged_code == 0
    if not merged:
        kind = "unmerged"
    elif dirty:
        kind = "uncommitted"
    else:
        kind = "merged_clean"
    return {
        "path": str(path),
        "class": kind,
        "dirty": dirty,
        "reason": "" if merged or dirty else err.strip(),
    }


def _worktree_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        git_entry = child / ".git"
        if git_entry.exists() or git_entry.is_symlink():
            found.append(child)
    return found


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _registrations(
    settings: Settings, system: RealSystem, now: datetime
) -> list[dict[str, object]]:
    if not settings.registry_file.is_file():
        return []
    try:
        payload = json.loads(settings.registry_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [
            {"id": "", "provider": "", "stale": True, "reason": "unreadable registry"}
        ]
    rows = payload.get("daemons") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    stale = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("pid")
        heartbeat = _parse_time(str(row.get("heartbeat") or ""))
        dead = isinstance(pid, int) and not system.alive(pid)
        old = (
            heartbeat is None
            or (now - heartbeat).total_seconds() > settings.stale_daemon_seconds
        )
        if dead or old:
            stale.append(
                {
                    "id": row.get("id"),
                    "provider": row.get("provider"),
                    "pid": pid,
                    "stale": True,
                    "reason": "dead process" if dead else "stale heartbeat",
                }
            )
    return stale


def _dead_pids(settings: Settings, system: RealSystem) -> list[dict[str, object]]:
    if not settings.run_dir.is_dir():
        return []
    dead = []
    for path in sorted(settings.run_dir.glob("*.pid")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            dead.append({"path": str(path), "pid": None, "reason": "unreadable"})
            continue
        if not text.isdigit():
            dead.append({"path": str(path), "pid": text, "reason": "invalid"})
            continue
        pid = int(text)
        if not system.alive(pid):
            dead.append({"path": str(path), "pid": pid, "reason": "not running"})
    return dead


def clean_report(
    system: RealSystem,
    settings: Settings,
    *,
    root: Path | None = None,
    base_ref: str = "main",
    now: datetime | None = None,
) -> dict[str, object]:
    root = root or settings.worktree_root
    worktrees = [
        _classify_worktree(system, path, base_ref)
        for path in _worktree_dirs(root)
        if path.resolve() != settings.repo.resolve()
    ]
    moment = now or datetime.now(UTC)
    return {
        "root": str(root),
        "base_ref": base_ref,
        "worktrees": worktrees,
        "stale_daemons": _registrations(settings, system, moment),
        "dead_processes": _dead_pids(settings, system),
    }


def _remove_worktree(system: RealSystem, path: Path, base_ref: str) -> None:
    current = _classify_worktree(system, path, base_ref)
    if current["class"] != "merged_clean" or current["dirty"]:
        raise OpsError(f"refusing to remove {path}: unmerged or uncommitted work")
    code, common, _err = _git(system, path, "rev-parse", "--git-common-dir")
    if code != 0:
        raise OpsError(f"refusing to remove {path}: not a git worktree")
    common_path = Path(common.strip())
    if not common_path.is_absolute():
        common_path = (path / common_path).resolve()
    repo = common_path.parent if common_path.name == ".git" else common_path
    result = system.run(["git", "-C", str(repo), "worktree", "remove", str(path)])
    if result.code != 0:
        raise OpsError(f"git worktree remove failed for {path.name}")


def apply_clean(
    system: RealSystem,
    settings: Settings,
    report: dict[str, object],
    *,
    base_ref: str,
) -> dict[str, object]:
    removed: list[str] = []
    skipped: list[str] = []
    worktrees = report.get("worktrees")
    if isinstance(worktrees, list):
        for item in worktrees:
            if not isinstance(item, dict):
                continue
            path = Path(str(item.get("path")))
            if item.get("class") == "merged_clean" and not item.get("dirty"):
                _remove_worktree(system, path, base_ref)
                removed.append(str(path))
            else:
                skipped.append(str(path))
    stale = report.get("stale_daemons")
    if isinstance(stale, list) and settings.registry_file.is_file():
        try:
            payload = json.loads(settings.registry_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"daemons": []}
        stale_ids = {
            str(item.get("id"))
            for item in stale
            if isinstance(item, dict) and item.get("id")
        }
        rows = payload.get("daemons") if isinstance(payload, dict) else []
        if isinstance(rows, list):
            payload["daemons"] = [
                row
                for row in rows
                if not (isinstance(row, dict) and str(row.get("id")) in stale_ids)
            ]
            settings.registry_file.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
    dead = report.get("dead_processes")
    if isinstance(dead, list):
        for item in dead:
            if not isinstance(item, dict):
                continue
            path = Path(str(item.get("path")))
            pid = item.get("pid")
            if isinstance(pid, int) and system.alive(pid):
                skipped.append(str(path))
                continue
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return {"removed": removed, "skipped": skipped}


def format_clean(report: dict[str, object]) -> str:
    lines = [f"worktree root: {report.get('root')} (base {report.get('base_ref')})"]
    grouped = {"merged_clean": [], "unmerged": [], "uncommitted": [], "broken": []}
    worktrees = report.get("worktrees")
    if isinstance(worktrees, list):
        for item in worktrees:
            if isinstance(item, dict):
                grouped.setdefault(str(item.get("class")), []).append(
                    str(item.get("path"))
                )
    for name in ("merged_clean", "unmerged", "uncommitted", "broken"):
        lines.append(f"{name}:")
        rows = grouped.get(name) or []
        if not rows:
            lines.append("  (none)")
        for row in rows:
            lines.append(f"  {row}")
    lines.append("stale_daemons:")
    stale = report.get("stale_daemons")
    if not isinstance(stale, list) or not stale:
        lines.append("  (none)")
    else:
        for item in stale:
            if isinstance(item, dict):
                lines.append(
                    f"  {item.get('provider')}/{item.get('id')} ({item.get('reason')})"
                )
    lines.append("dead_processes:")
    dead = report.get("dead_processes")
    if not isinstance(dead, list) or not dead:
        lines.append("  (none)")
    else:
        for item in dead:
            if isinstance(item, dict):
                lines.append(f"  pid {item.get('pid')} {item.get('path')}")
    lines.append("clean: list only (pass --apply to remove merged clean worktrees)")
    return "\n".join(lines) + "\n"


def register_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    clean = commands.add_parser(
        "clean",
        help="list stale worktrees, daemon registrations, and dead pid files",
    )
    clean.add_argument(
        "--apply", action="store_true", help="remove only merged clean worktrees"
    )
    clean.add_argument("--json", action="store_true")
    clean.add_argument("--root", help="worktree root to scan")
    clean.add_argument(
        "--base", default="main", help="ref a merged worktree must be contained in"
    )
    clean.set_defaults(handler=_handler)


def _handler(args: argparse.Namespace) -> int:
    system, settings, _fleet = prepare()
    root = Path(args.root).expanduser() if args.root else None
    report = clean_report(system, settings, root=root, base_ref=args.base)
    if args.apply:
        report["applied"] = apply_clean(system, settings, report, base_ref=args.base)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_clean(report), end="")
        applied = report.get("applied")
        if isinstance(applied, dict):
            print(f"removed: {len(applied.get('removed') or [])}")
    return 0
