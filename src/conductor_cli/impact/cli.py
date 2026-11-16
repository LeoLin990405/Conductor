"""`conductor impact` command."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ..client import CLIError, json_dumps_safe, sanitize_terminal

from .analyze import impact_report, load_catalog
from .index import ImpactIndex
from .model import ImpactError
from .review import review_range


def run_impact(args: argparse.Namespace) -> dict[str, Any]:
    try:
        root = find_root(getattr(args, "root", None))
        index_path = getattr(args, "index", None)
        index = ImpactIndex(root, Path(index_path).expanduser().resolve() if index_path else None)
        if getattr(args, "review", None):
            if getattr(args, "targets", None):
                raise ImpactError("--review does not take file or symbol arguments")
            return review_range(root, args.review, index=index, depth=args.depth)
        targets = list(getattr(args, "targets", []) or [])
        if not targets:
            raise ImpactError("pass at least one file or symbol, or --review BASE..HEAD")
        stats = index.update()
        catalog = load_catalog(index)
        return impact_report(
            catalog,
            root,
            targets,
            depth=args.depth,
            index_stats=stats,
        )
    except ImpactError as exc:
        raise CLIError(str(exc)) from exc


def find_root(explicit: Path | None) -> Path:
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise ImpactError(f"repository root is not a directory: {root}")
        return root
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def render_result(result: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        # The index and review payloads carry repository paths; keep JSON valid
        # and terminal-safe (C1 escapes) on the way out.
        print(json_dumps_safe(result))
        return
    text = _render_review(result) if result.get("mode") == "review" else _render_impact(result)
    # Paths and symbol names are repository content: strip terminal controls
    # and carriage returns (which would overwrite the rendered line).
    print(sanitize_terminal(text.replace("\r", "")))


def _render_impact(result: dict[str, Any]) -> str:
    lines = [f"confidence: {float(result.get('confidence', 0)):.2f}"]
    note = result.get("note")
    if note:
        lines.append(str(note))
    lines.append("targets:")
    lines.extend(_target_lines(result.get("targets") or []))
    lines.append("affected functions:")
    for item in result.get("affected_functions") or []:
        lines.append(
            f"  {float(item['confidence']):.2f}  {item['path']}:{item['line']}  "
            f"{item['qualified_name']}  {item['reason']}"
        )
    if not result.get("affected_functions"):
        lines.append("  (none)")
    lines.append("affected files:")
    for item in result.get("affected_files") or []:
        lines.append(f"  {float(item['confidence']):.2f}  {item['path']}  {item['reason']}")
    if not result.get("affected_files"):
        lines.append("  (none)")
    lines.append("affected tests:")
    for item in result.get("affected_tests") or []:
        lines.append(f"  {float(item['confidence']):.2f}  {item['path']}  {item['reason']}")
    if not result.get("affected_tests"):
        lines.append("  (none)")
    lines.append("ambiguous:")
    lines.extend(_ambiguous_lines(result.get("ambiguous") or []))
    return "\n".join(lines)


def _render_review(result: dict[str, Any]) -> str:
    lines = [
        f"review {result.get('range', '')}",
        f"confidence: {float(result.get('confidence', 0)):.2f}",
        "changed symbols:",
    ]
    for item in result.get("changed_symbols") or []:
        suffix = " removed" if item.get("removed") else ""
        lines.append(f"  {item['path']}:{item['line']}  {item['qualified_name']}{suffix}")
    if not result.get("changed_symbols"):
        lines.append("  (none)")
    lines.append("flags:")
    flags = result.get("flags") or []
    if not flags:
        lines.append("  (none)")
    for flag in flags:
        lines.append(
            f"  {flag['path']}:{flag['line']}  {flag['symbol']}  "
            f"confidence {float(flag['confidence']):.2f}"
        )
        for caller in flag.get("untouched_callers") or []:
            name = caller.get("qualified_name") or caller.get("path")
            lines.append(
                f"    untouched caller {caller['path']}:{caller['line']}  {name}  "
                f"{float(caller['confidence']):.2f}"
            )
    lines.append("ambiguous:")
    lines.extend(_ambiguous_lines(result.get("ambiguous") or []))
    return "\n".join(lines)


def _target_lines(targets: list[dict[str, Any]]) -> list[str]:
    if not targets:
        return ["  (none)"]
    return [
        f"  {item.get('qualified_name', item.get('input', ''))}  {item.get('path', '')}:{item.get('line', '')}"
        for item in targets
    ]


def _ambiguous_lines(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["  (none)"]
    lines: list[str] = []
    for item in items:
        label = item.get("input") or item.get("name") or "name"
        where = ""
        if item.get("path"):
            where = f" at {item['path']}:{item.get('line', '')}"
        lines.append(f"  {label}{where}")
        for candidate in item.get("candidates") or []:
            lines.append(
                f"    {candidate['path']}:{candidate['line']}  {candidate['qualified_name']}"
            )
    return lines
