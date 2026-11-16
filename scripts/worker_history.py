#!/usr/bin/env python3
"""Read-only history helper for a Worker (N29 v1, PRD section 4.5).

    worker_history.py list
    worker_history.py read --run <run_id> [--offset N] [--limit B]
    worker_history.py read --handoff <generation> [--offset N] [--limit B]
    worker_history.py search --query <literal> [--max K]
    worker_history.py budget

The runs directory is pinned by ``CONDUCTOR_HISTORY_RUNS_ROOT``, which the
launcher derives from the manifest it was given; without it the helper refuses
(``runs_root_unset``). The only manifest it reads is the one named by
``CONDUCTOR_HISTORY_MANIFEST``, and only when that path lies directly in
``<runs_root>/recovery-sessions/history/`` (else ``manifest_outside_runs_root``);
there is no option to point it anywhere else. The only files it reads are the
logs and handoff notes that manifest lists, plus the budget file and the
current run log named by the environment, and only when the path is a regular
file under the pinned runs root, reached without a symlink and without ``..``.
Files are opened ``O_RDONLY | O_NOFOLLOW``; nothing is created, changed or
removed.

This is scoping, not a security boundary (PRD section 5). The helper runs as
the same OS user as the Worker, and the Worker sets its environment, so a
Worker that wants to can read those files directly. What the scoping buys is a
default: the history a Worker pulls into its context is its own line's,
redacted, and not other lines' logs or stray secrets.

Every content segment passes ``redact_credentials`` and is preceded by
``HISTORY_DATA_MARKER``; stdout is capped at 64 KiB. Any refusal exits
non-zero and prints only ``error: <reason_code>`` to stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

# Running this file by path puts scripts/ on sys.path; the package is the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@contextlib.contextmanager
def _quiet_stdout() -> Iterator[None]:
    """Keep import-time log lines of the ``src`` packages off our stdout.

    Importing ``src.llms`` configures logging that prints to stdout; this
    command's stdout must carry only history data.
    """
    sys.stdout.flush()
    saved_fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved_stdout = sys.stdout
    try:
        os.dup2(devnull, 1)
        with open(os.devnull, "w", encoding="utf-8") as sink:
            sys.stdout = sink
            try:
                yield
            finally:
                sys.stdout = saved_stdout
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
        os.close(devnull)


with _quiet_stdout():
    from src.llms.native_cli.failure import redact_credentials
    from src.tools.auto_dispatch import context_budget as cb

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BUDGET_UNAVAILABLE = 3

READ_LIMIT_DEFAULT = 8192
READ_LIMIT_MAX = 16384
SEARCH_MAX_DEFAULT = 20
SEARCH_MAX = 20
SEARCH_CONTEXT_BYTES = 200
STDOUT_CAP_BYTES = 64 * 1024

#: How far a page edge or a search context may move to reach whitespace, so
#: a credential is not cut in two (half a token is not recognised as one).
_SNAP_BYTES = 512
#: How far back a page looks for an unterminated private-key header.
_PEM_LOOKBACK_BYTES = 16384
_SCAN_CHUNK_BYTES = 1 << 20
_WHITESPACE = frozenset(b" \t\r\n\f\v")
_PEM_BEGIN = b"-----BEGIN "
_PEM_END = b"-----END "
_TRUNCATED = "\n[history output truncated at 64 KiB]\n"


class Refusal(Exception):
    """Fail closed with one reason code."""

    def __init__(self, code: str, exit_code: int = EXIT_REFUSED) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: ARG002 - reason code only
        raise Refusal("usage")


# ---------------------------------------------------------------------------
# output


class _Output:
    """Accumulates stdout, never past STDOUT_CAP_BYTES."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._size = 0
        self.truncated = False

    def segment(self, header: str, content: str | None = None, *, redact: bool = True) -> None:
        """One marked segment; ``redact=False`` only for text this script computed."""
        text = f"{cb.HISTORY_DATA_MARKER} {header}\n"
        if content is not None:
            text += content
            if not content.endswith("\n"):
                text += "\n"
        self._write(str(redact_credentials(text)) if redact else text)

    def _write(self, text: str) -> None:
        if self.truncated:
            return
        data = text.encode("utf-8", errors="replace")
        tail = _TRUNCATED.encode("utf-8")
        room = STDOUT_CAP_BYTES - self._size
        if len(data) <= room - len(tail):
            self._parts.append(data)
            self._size += len(data)
            return
        keep = max(0, room - len(tail))
        # Drop a partial UTF-8 sequence at the cut.
        piece = data[:keep].decode("utf-8", errors="ignore").encode("utf-8")
        self._parts.append(piece + tail)
        self._size += len(piece) + len(tail)
        self.truncated = True

    def flush(self) -> None:
        sys.stdout.buffer.write(b"".join(self._parts))
        sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# paths


#: The runs dir the helper is pinned to; set by dispatch_launch.sh.
ENV_HISTORY_RUNS_ROOT = getattr(cb, "ENV_HISTORY_RUNS_ROOT", "CONDUCTOR_HISTORY_RUNS_ROOT")


def _runs_dir(env: Mapping[str, str]) -> tuple[Path, Path]:
    """(manifest path, pinned runs root) from the environment; no IO on the manifest.

    The runs root comes from ``CONDUCTOR_HISTORY_RUNS_ROOT``, never from the
    manifest path, and the manifest must lie directly in its history dir.
    """
    raw_root = env.get(ENV_HISTORY_RUNS_ROOT, "")
    if not raw_root:
        raise Refusal("runs_root_unset")
    runs_root = Path(raw_root)
    if not runs_root.is_absolute() or ".." in runs_root.parts:
        raise Refusal("runs_root_invalid")
    raw = env.get(cb.ENV_HISTORY_MANIFEST, "")
    if not raw:
        raise Refusal("manifest_unset")
    manifest = Path(raw)
    if not manifest.is_absolute() or ".." in manifest.parts:
        raise Refusal("manifest_path_invalid")
    for base in (runs_root, runs_root.resolve()):
        try:
            rel = manifest.relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) == 3 and rel.parts[:2] == (cb.RECOVERY_DIR, cb.HISTORY_SUBDIR):
            return manifest, runs_root
    raise Refusal("manifest_outside_runs_root")


def _relative_parts(path: Path, runs_dir: Path) -> tuple[str, ...]:
    if ".." in path.parts:
        raise Refusal("path_outside_runs_dir")
    if not path.is_absolute():
        path = runs_dir / path
    for base in (runs_dir, runs_dir.resolve()):
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if rel.parts:
            return rel.parts
    raise Refusal("path_outside_runs_dir")


def _checked(path: Path | str, runs_dir: Path, *, missing_code: str) -> Path | None:
    """The real path when ``path`` is a regular file under runs_dir.

    Every component below runs_dir is lstat'ed: a symlink anywhere refuses.
    Returns None when the file (or a parent) does not exist and
    ``missing_code`` is empty; otherwise a missing file refuses with it.
    """
    root = runs_dir.resolve()
    parts = _relative_parts(Path(path), runs_dir)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if missing_code:
                raise Refusal(missing_code) from None
            return None
        except OSError:
            raise Refusal("path_unreadable") from None
        if stat.S_ISLNK(info.st_mode):
            raise Refusal("path_symlink")
        last = index == len(parts) - 1
        if last and not stat.S_ISREG(info.st_mode):
            raise Refusal("not_regular_file")
        if not last and not stat.S_ISDIR(info.st_mode):
            raise Refusal("path_outside_runs_dir")
    resolved = current.resolve()
    if resolved != current or not resolved.is_relative_to(root):
        raise Refusal("path_outside_runs_dir")
    return current


def _open_ro(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise Refusal("path_unreadable") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refusal("not_regular_file")
    return fd


# ---------------------------------------------------------------------------
# manifest


def _load(env: Mapping[str, str]) -> tuple[Path, list[cb.ManifestEntry]]:
    manifest, runs_dir = _runs_dir(env)
    try:
        _checked(manifest, runs_dir, missing_code="manifest_missing")
    except Refusal as refusal:
        code = {
            "path_symlink": "manifest_symlink",
            "not_regular_file": "manifest_malformed",
        }.get(refusal.code, refusal.code)
        raise Refusal(code) from None
    try:
        entries = cb.read_manifest(manifest)
    except cb.ManifestError as exc:
        raise Refusal(exc.code or "manifest_malformed") from None
    except (OSError, ValueError, TypeError, KeyError):
        raise Refusal("manifest_malformed") from None
    return runs_dir, list(entries)


def _size(path: str | None, runs_dir: Path) -> int | None:
    if not path:
        return None
    real = _checked(path, runs_dir, missing_code="")
    if real is None:
        return None
    return os.lstat(real).st_size


# ---------------------------------------------------------------------------
# segments


def _is_space(byte: int) -> bool:
    return byte in _WHITESPACE


def _pem_carry(fd: int, start: int) -> bytes:
    """The private-key header line still open at ``start``, if any."""
    if start <= 0:
        return b""
    lo = max(0, start - _PEM_LOOKBACK_BYTES)
    before = os.pread(fd, start - lo, lo)
    begin = before.rfind(_PEM_BEGIN)
    if begin < 0 or before.find(_PEM_END, begin) >= 0:
        return b""
    close = before.find(b"-----", begin + len(_PEM_BEGIN))
    if close < 0:
        return before[begin:] + b"\n"
    return before[begin : close + 5] + b"\n"


def _text(fd: int, start: int, end: int) -> str:
    data = _pem_carry(fd, start) + os.pread(fd, max(0, end - start), start)
    return str(redact_credentials(data.decode("utf-8", errors="replace")))


def _snap_start(fd: int, start: int, size: int) -> int:
    """Move a start that falls inside a word forward to the next whitespace."""
    if start <= 0 or start >= size:
        return start
    window = os.pread(fd, _SNAP_BYTES + 1, start - 1)
    if len(window) < 2 or _is_space(window[0]) or _is_space(window[1]):
        return start
    for index in range(1, len(window)):
        if _is_space(window[index]):
            return start - 1 + index + 1
    return start


def _snap_end(fd: int, start: int, end: int, size: int) -> int:
    """Move an end that falls inside a word back to the last whitespace."""
    if end >= size or end <= start:
        return end
    lo = max(start, end - _SNAP_BYTES)
    window = os.pread(fd, end - lo + 1, lo)
    if _is_space(window[-1]) or _is_space(window[-2]):
        return end
    for index in range(len(window) - 2, -1, -1):
        if _is_space(window[index]):
            cut = lo + index + 1
            return cut if cut > start else end
    return end


def _widen(fd: int, start: int, end: int, size: int) -> tuple[int, int]:
    """Widen a search context outwards to whitespace (bounded)."""
    if start > 0:
        lo = max(0, start - _SNAP_BYTES)
        before = os.pread(fd, start - lo, lo)
        for index in range(len(before) - 1, -1, -1):
            if _is_space(before[index]):
                start = lo + index + 1
                break
        else:
            start = lo
    if end < size:
        after = os.pread(fd, min(_SNAP_BYTES, size - end), end)
        for index, byte in enumerate(after):
            if _is_space(byte):
                end += index
                break
        else:
            end += len(after)
    return start, end


# ---------------------------------------------------------------------------
# commands


def cmd_list(env: Mapping[str, str], out: _Output) -> int:
    runs_dir, entries = _load(env)
    groups: dict[tuple[int, str], list[cb.ManifestEntry]] = {}
    for entry in entries:
        groups.setdefault((entry.session_generation, entry.lineage_current), []).append(entry)
    out.segment(f"manifest runs={len(entries)} generations={len(groups)}")
    lines: list[str] = []
    for (generation, lineage), runs in sorted(groups.items(), key=lambda item: item[0]):
        sizes = [_size(run.log_path, runs_dir) for run in runs]
        statuses = sorted({run.handoff_request for run in runs})
        providers = sorted({run.provider for run in runs})
        notes = sum(1 for run in runs if run.handoff_path)
        lines.append(
            f"generation={generation} lineage_current={lineage} "
            f"provider={','.join(providers)} runs={len(runs)} "
            f"bytes={sum(size or 0 for size in sizes)} "
            f"handoff_request={','.join(statuses)} handoff_notes={notes}"
        )
        for run, size in zip(runs, sizes, strict=True):
            exit_code = "none" if run.exit_code is None else str(run.exit_code)
            lines.append(
                f"  run={run.run_id} bytes={'missing' if size is None else size} "
                f"exit_code={exit_code} handoff_request={run.handoff_request} "
                f"estimate_source={run.estimate_source}"
            )
    out.segment("generations", "\n".join(lines))
    return EXIT_OK


def _find_target(args: argparse.Namespace, entries: Sequence[cb.ManifestEntry]) -> tuple[str, str]:
    if args.run is not None:
        for entry in reversed(entries):
            if entry.run_id == args.run:
                return f"run={entry.run_id}", entry.log_path
        raise Refusal("unknown_run")
    for entry in reversed(entries):
        if entry.session_generation == args.handoff and entry.handoff_path:
            return f"handoff generation={args.handoff}", entry.handoff_path
    raise Refusal("unknown_handoff")


def cmd_read(args: argparse.Namespace, env: Mapping[str, str], out: _Output) -> int:
    if args.offset < 0 or args.limit < 1:
        raise Refusal("usage")
    limit = min(args.limit, READ_LIMIT_MAX)
    runs_dir, entries = _load(env)
    label, raw_path = _find_target(args, entries)
    real = _checked(raw_path, runs_dir, missing_code="file_missing")
    assert real is not None
    fd = _open_ro(real)
    try:
        size = os.fstat(fd).st_size
        start = _snap_start(fd, min(args.offset, size), size)
        end = _snap_end(fd, start, min(start + limit, size), size)
        content = _text(fd, start, end) if end > start else ""
    finally:
        os.close(fd)
    next_offset = "eof" if end >= size else str(end)
    out.segment(
        f"{label} offset={start} end={end} size={size} next_offset={next_offset}",
        content,
    )
    return EXIT_OK


def _hits(fd: int, size: int, needle: bytes, limit: int) -> list[int]:
    found: list[int] = []
    pos = 0
    overlap = len(needle) - 1
    while pos < size and len(found) < limit:
        chunk = os.pread(fd, _SCAN_CHUNK_BYTES + overlap, pos)
        if not chunk:
            break
        at = 0
        while len(found) < limit:
            index = chunk.find(needle, at)
            if index < 0:
                break
            if not found or pos + index >= found[-1] + len(needle):
                found.append(pos + index)
            at = index + max(1, len(needle))
        pos += _SCAN_CHUNK_BYTES
    return found


def cmd_search(args: argparse.Namespace, env: Mapping[str, str], out: _Output) -> int:
    if not args.query or args.max < 1:
        raise Refusal("usage")
    limit = min(args.max, SEARCH_MAX)
    needle = args.query.encode("utf-8", errors="surrogateescape")
    runs_dir, entries = _load(env)
    targets: list[tuple[str, str]] = []
    for entry in entries:
        targets.append((f"run={entry.run_id} kind=log", entry.log_path))
    for entry in entries:
        if entry.handoff_path:
            targets.append(
                (f"generation={entry.session_generation} kind=handoff", entry.handoff_path)
            )
    # Check every path first: one unsafe entry refuses the whole search.
    checked: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for label, raw_path in targets:
        real = _checked(raw_path, runs_dir, missing_code="")
        if real is not None and real not in seen:
            seen.add(real)
            checked.append((label, real))
    count = 0
    segments: list[tuple[str, str]] = []
    for label, real in checked:
        if count >= limit:
            break
        fd = _open_ro(real)
        try:
            size = os.fstat(fd).st_size
            for position in _hits(fd, size, needle, limit - count):
                start = max(0, position - SEARCH_CONTEXT_BYTES)
                end = min(size, position + len(needle) + SEARCH_CONTEXT_BYTES)
                start, end = _widen(fd, start, end, size)
                count += 1
                segments.append(
                    (f"hit={count} {label} offset={position}", _text(fd, start, end))
                )
        finally:
            os.close(fd)
    out.segment(f"search hits={count} max={limit}")
    for header, content in segments:
        out.segment(header, content)
    return EXIT_OK


def cmd_budget(env: Mapping[str, str], out: _Output) -> int:
    manifest, runs_dir = _runs_dir(env)
    # The manifest need not exist yet (the first run appends it on exit),
    # but a symlink in its place still refuses.
    try:
        _checked(manifest, runs_dir, missing_code="")
    except Refusal as refusal:
        if refusal.code == "path_symlink":
            raise Refusal("manifest_symlink") from None
        raise
    baseline = None
    budget_raw = env.get(cb.ENV_BUDGET_FILE, "")
    if budget_raw:
        real = _checked(budget_raw, runs_dir, missing_code="")
        if real is not None:
            fd = _open_ro(real)
            try:
                data = os.pread(fd, cb.BUDGET_BLOCK_MAX_BYTES + 1, 0)
            finally:
                os.close(fd)
            if len(data) <= cb.BUDGET_BLOCK_MAX_BYTES:
                baseline = cb.parse_budget_block(data.decode("utf-8", errors="replace"))
    run_log_bytes = 0
    run_log_raw = env.get(cb.ENV_RUN_LOG, "")
    if run_log_raw:
        size = _size(run_log_raw, runs_dir)
        run_log_bytes = size or 0
    if baseline is None:
        # Numbers and fixed codes only: the redaction would take the value
        # after "tokens_left:" for a credential.
        out.segment("budget", "tokens_left: unknown\nreason: budget_unavailable\n", redact=False)
        raise Refusal("budget_unavailable", EXIT_BUDGET_UNAVAILABLE)
    window = baseline.window_tokens
    used = baseline.used_tokens + run_log_bytes // cb.BYTES_PER_TOKEN
    left = max(0, window - used)
    ratio = used / window if window > 0 else 1.0
    lines = [
        f"tokens_left: {left}",
        "tokens_left_is_estimate: true",
        f"source: {baseline.source if baseline.source in cb.ESTIMATE_SOURCES else 'unknown'}",
        f"window_tokens: {window}",
        f"window_assumed: {str(baseline.window_assumed).lower()}",
        f"used_tokens: {used}",
        f"run_log_bytes: {run_log_bytes}",
        f"ratio: {ratio:.4f}",
        f"soft_ratio: {baseline.soft_ratio}",
        f"soft_ratio_reached: {str(ratio >= baseline.soft_ratio).lower()}",
    ]
    out.segment("budget", "\n".join(lines), redact=False)
    return EXIT_OK


# ---------------------------------------------------------------------------
# entry point


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="worker_history.py", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("list")
    read = sub.add_parser("read")
    target = read.add_mutually_exclusive_group(required=True)
    target.add_argument("--run")
    target.add_argument("--handoff", type=int)
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=READ_LIMIT_DEFAULT)
    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--max", type=int, default=SEARCH_MAX_DEFAULT)
    sub.add_parser("budget")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    out = _Output()
    try:
        args = _parser().parse_args(argv)
        if args.command == "list":
            code = cmd_list(env, out)
        elif args.command == "read":
            code = cmd_read(args, env, out)
        elif args.command == "search":
            code = cmd_search(args, env, out)
        else:
            code = cmd_budget(env, out)
    except Refusal as refusal:
        if refusal.exit_code == EXIT_BUDGET_UNAVAILABLE:
            out.flush()
        sys.stderr.write(f"error: {refusal.code}\n")
        return refusal.exit_code
    except Exception:  # noqa: BLE001 - fail closed without echoing anything
        sys.stderr.write("error: internal\n")
        return EXIT_REFUSED
    out.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
