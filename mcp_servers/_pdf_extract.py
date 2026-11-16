"""Bounded subprocess protocol for untrusted announcement PDFs.

Linux RLIMIT_AS and RLIMIT_CPU are required. macOS aliases RLIMIT_AS to its
advisory RSS limit, so it cannot supply this boundary and fails closed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

MAX_INPUT_BYTES = 60_000_000
MAX_RESULT_BYTES = 1_500_000
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
CPU_LIMIT_SECONDS = 10
WALL_TIMEOUT_SECONDS = 20.0


class PdfExtractionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _run_worker(command: list[str], *, timeout: float, max_output: int) -> dict:
    """Read a bounded pipe and always kill/reap a child that fails its budget."""
    selector = selectors.DefaultSelector()
    process = None
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        deadline = time.monotonic() + timeout
        output = bytearray()
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PdfExtractionError("timeout")
            events = selector.select(remaining)
            if not events:
                raise PdfExtractionError("timeout")
            chunk = os.read(process.stdout.fileno(), min(65536, max_output - len(output) + 1))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > max_output:
                raise PdfExtractionError("output_limit")
        try:
            return_code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise PdfExtractionError("timeout") from exc
        if return_code != 0:
            resource_signals = {-signal.SIGKILL, -getattr(signal, "SIGXCPU", signal.SIGKILL)}
            raise PdfExtractionError("resource_limit" if return_code in resource_signals else "parse_failed")
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError) as exc:
            raise PdfExtractionError("parse_failed") from exc
        if not isinstance(result, dict):
            raise PdfExtractionError("parse_failed")
        if result.get("error"):
            code = result["error"]
            raise PdfExtractionError(code if code in {"resource_limit", "unsupported", "parse_failed"} else "parse_failed")
        if not isinstance(result.get("data"), list) or not isinstance(result.get("total_pages"), int):
            raise PdfExtractionError("parse_failed")
        return result
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            process.stdout.close()


def extract_pdf_isolated(content: bytes, max_pages: int, max_chars: int) -> dict:
    if sys.platform != "linux":
        raise PdfExtractionError("unsupported")
    if len(content) > MAX_INPUT_BYTES:
        raise PdfExtractionError("resource_limit")
    pages = max(1, min(int(max_pages), 60))
    chars = max(1000, min(int(max_chars), 200_000))
    with tempfile.TemporaryDirectory(prefix="conductor-pdf-") as temporary:
        path = Path(temporary) / "announcement.pdf"
        path.write_bytes(content)
        return _run_worker(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(path),
             str(pages), str(chars), str(MEMORY_LIMIT_BYTES), str(CPU_LIMIT_SECONDS)],
            timeout=WALL_TIMEOUT_SECONDS, max_output=MAX_RESULT_BYTES,
        )


def _extract_pages(path: str, max_pages: int, max_chars: int) -> dict:
    # Imported only in the limited child; the MCP process never parses PDFs.
    from pypdf import PdfReader

    reader = PdfReader(path)
    out, used = [], 0
    total_pages = len(reader.pages)
    for index in range(min(total_pages, max_pages)):
        text = (reader.pages[index].extract_text() or "").strip()[:max_chars - used]
        out.append({"page": index + 1, "text": text})
        used += len(text)
        if used >= max_chars:
            break
    return {"data": out, "chars": used, "total_pages": total_pages}


def _worker_main(argv: list[str]) -> dict:
    if sys.platform != "linux":
        return {"error": "unsupported"}
    try:
        import resource

        path, pages, chars, memory, cpu = argv
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_AS, (int(memory), int(memory)))
        resource.setrlimit(resource.RLIMIT_CPU, (int(cpu), int(cpu)))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    except (ImportError, AttributeError, ValueError, OSError):
        return {"error": "unsupported"}
    try:
        return _extract_pages(path, int(pages), int(chars))
    except MemoryError:
        return {"error": "resource_limit"}
    except Exception:
        return {"error": "parse_failed"}


if __name__ == "__main__":
    result = _worker_main(sys.argv[1:])
    try:
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except MemoryError:
        sys.stdout.buffer.write(b'{"error":"resource_limit"}')
