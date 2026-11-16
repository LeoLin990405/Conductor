#!/usr/bin/env python3
"""Bridge CodexBar's local usage snapshot into Conductor.

CodexBar is a macOS menu-bar app: its credentials, browser cookies and provider
sessions live on the host, and the Linux Conductor backend cannot run its CLI.
This tiny host-side bridge shells out to ``codexbar usage --format json``,
removes account-identifying fields, and serves the result on loopback (or
writes it once).

Run it on the host:

    python3 scripts/codexbar_bridge.py --serve --port 8765

Then point Conductor at ``CODEXBAR_BRIDGE_URL`` (the compose file documents
``http://host.docker.internal:8765/usage`` for Docker Desktop). No CodexBar
credential reaches Conductor: only provider, plan label, quota windows, reset
times and a sanitized error summary are forwarded.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_CODEXBAR = "codexbar"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ACCOUNT_ID = re.compile(r"\b(?:user|acct|account)[_:-]?[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_SENSITIVE_KEYS = {
    "account",
    "accountEmail",
    "accountID",
    "accountOrganization",
    "identity",
    "cookie",
    "cookies",
    "token",
    "accessToken",
    "refreshToken",
    "password",
    "secret",
}


def _clean_text(value: str, limit: int = 240) -> str:
    value = _EMAIL.sub("<email>", value)
    value = _ACCOUNT_ID.sub("<account>", value)
    value = " ".join(value.split())
    return value[:limit]


def _scrub(value: Any) -> Any:
    """Drop identifying fields and scrub strings at any nesting depth."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _SENSITIVE_KEYS:
                continue
            out[key] = _scrub(item)
        return out
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return _clean_text(value)
    return value


def _window(raw: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {"id": label, "label": label}
    for key in ("usedPercent", "resetsAt", "resetDescription", "windowMinutes"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    return out


def sanitize(payload: Any) -> dict[str, Any]:
    """Turn the CodexBar CLI array into Conductor's small, non-identifying shape.

    Every provider gets a row with an explicit ``status``. CodexBar reports a
    provider it could not read as an error entry ("No available fetch strategy
    for <provider>." and friends) while the other providers are still usable,
    so an unreadable provider becomes an ``unknown`` row instead of vanishing
    and taking the quota and balance states of its neighbours with it.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    providers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return {"generated_at": generated_at, "providers": [], "errors": [], "error": "unexpected payload"}

    for item in payload:
        if not isinstance(item, dict):
            continue
        provider_id = str(item.get("provider") or "")
        usage = item.get("usage")
        if not isinstance(usage, dict):
            message = ""
            err = item.get("error")
            if isinstance(err, dict):
                message = _clean_text(str(err.get("message") or err.get("kind") or ""))
            errors.append({"provider": provider_id, "message": message})
            if provider_id:
                providers.append({
                    "provider": provider_id,
                    "source": _clean_text(str(item.get("source") or "")),
                    "status": "unknown",
                    "updated_at": None,
                    "windows": [],
                    "error": message,
                })
            continue

        labels = item.get("rateWindowLabels")
        labels = labels if isinstance(labels, dict) else {}
        windows = []
        for key in ("primary", "secondary", "tertiary"):
            label = str(labels.get(key) or key)
            window = _window(usage.get(key), label)
            if window:
                windows.append(window)
        for extra in usage.get("extraRateWindows") or []:
            if not isinstance(extra, dict):
                continue
            window = _window(extra.get("window"), str(extra.get("title") or extra.get("id") or "window"))
            if window:
                windows.append(window)

        entry: dict[str, Any] = {
            "provider": provider_id,
            "source": _clean_text(str(item.get("source") or "")),
            "status": "ok",
            "login_method": _clean_text(str(usage.get("loginMethod") or "")),
            "updated_at": usage.get("updatedAt"),
            "windows": windows,
        }
        # Preserve balance-style detail rows, but never their identity fields.
        details = usage.get("details")
        if isinstance(details, list):
            safe_details = []
            for block in details[:4]:
                if not isinstance(block, dict):
                    continue
                rows = []
                for row in (block.get("rows") or [])[:8]:
                    if not isinstance(row, dict):
                        continue
                    rows.append({
                        "label": _clean_text(str(row.get("label") or "")),
                        "value": _clean_text(str(row.get("value") or "")),
                        "secondary_value": _clean_text(str(row.get("secondaryValue") or "")) or None,
                    })
                if rows:
                    safe_details.append({"title": _clean_text(str(block.get("title") or "")), "rows": rows})
            if safe_details:
                entry["details"] = safe_details
        pace = item.get("pace")
        if isinstance(pace, dict):
            safe_pace = {}
            for key in ("primary", "secondary", "tertiary"):
                stage = pace.get(key)
                if isinstance(stage, dict) and stage.get("summary"):
                    safe_pace[key] = _clean_text(str(stage["summary"]))
            if safe_pace:
                entry["pace"] = safe_pace
        providers.append(entry)

    providers.sort(key=lambda entry: entry["provider"])
    return {
        "generated_at": generated_at,
        "providers": providers,
        "errors": errors,
    }


def widget_snapshot_path() -> Path | None:
    """The newest CodexBar widget snapshot, written by the app on every refresh.

    This is the fast path: the CLI walks every enabled provider and can take
    minutes, while the widget snapshot is already on disk and contains the same
    primary/secondary quota windows for the providers the user enabled.
    """
    roots = sorted(Path.home().glob("Library/Group Containers/*.steipete.codexbar"))
    candidates = [
        root / "widget-snapshot.json"
        for root in roots
        if (root / "widget-snapshot.json").is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.stat().st_mtime)


def sanitize_widget_snapshot(raw: Any, source_path: Path) -> dict[str, Any]:
    """Turn CodexBar's widget snapshot into the same shape as the CLI payload."""
    if not isinstance(raw, dict):
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "providers": [], "errors": [], "error": "invalid widget snapshot"}
    entries = raw.get("entries")
    providers: list[dict[str, Any]] = []
    for item in entries or []:
        if not isinstance(item, dict):
            continue
        provider_id = _clean_text(str(item.get("provider") or ""))
        if not provider_id:
            continue
        windows = []
        labels = {}
        for row in item.get("usageRows") or []:
            if isinstance(row, dict) and row.get("id"):
                labels[str(row["id"])] = _clean_text(str(row.get("title") or row["id"]))
        for key in ("primary", "secondary", "tertiary"):
            raw_window = item.get(key)
            if not isinstance(raw_window, dict):
                continue
            window = _window(raw_window, labels.get(key, key))
            if window:
                windows.append(window)
        row_labels = [str(row.get("title") or "") for row in item.get("usageRows") or [] if isinstance(row, dict) and row.get("title")]
        entry: dict[str, Any] = {
            "provider": provider_id,
            "source": "codexbar-widget",
            "status": "ok" if windows else "unknown",
            "login_method": "",
            "updated_at": item.get("updatedAt"),
            "windows": windows,
        }
        if row_labels:
            entry["detail_labels"] = [_clean_text(label) for label in row_labels]
        if item.get("balanceText"):
            entry["balance_text"] = _clean_text(str(item["balanceText"]))
        providers.append(entry)
    generated = raw.get("generatedAt")
    if not generated:
        try:
            generated = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            generated = datetime.now(timezone.utc).isoformat()
    providers.sort(key=lambda item: item["provider"])
    return {
        "generated_at": generated,
        "providers": providers,
        "errors": [],
        "source": "widget-snapshot",
    }


def run_codexbar(codexbar: str, provider: str, timeout: float = 90) -> dict[str, Any]:
    """Read the CLI's usage payload, keeping it even when the CLI exits non-zero.

    CodexBar exits 1 as soon as one provider fails to load, but its JSON
    payload still carries every provider it could read. Discarding that
    payload over the exit code would turn one unreadable provider into an
    empty snapshot for all of them.
    """
    cmd = [codexbar, "usage", "--provider", provider, "--format", "json", "--no-color"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "providers": [], "errors": [], "error": f"{codexbar} not found"}
    except OSError as exc:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "providers": [], "errors": [], "error": f"{codexbar} is not runnable: {type(exc).__name__}"}
    except subprocess.TimeoutExpired:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "providers": [], "errors": [], "error": "codexbar timed out"}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "providers": [],
            "errors": [],
            "error": _clean_text(proc.stderr or proc.stdout or f"codexbar exited {proc.returncode}"),
        }
    return sanitize(payload)


class Handler(BaseHTTPRequestHandler):
    server_version = "ConductorCodexBarBridge/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?", 1)[0] not in ("/usage", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.server.snapshot(), ensure_ascii=False).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


class BridgeServer(ThreadingHTTPServer):
    """Threading server with a short cache so repeated UI polls do not fork CLI runs."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], codexbar: str, provider: str, ttl: float, source: str = "auto"):
        super().__init__(address, Handler)
        self.codexbar = codexbar
        self.provider = provider
        self.ttl = ttl
        self.source = source
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if self._cached and now - self._cached[0] < self.ttl:
                return self._cached[1]
            path = widget_snapshot_path() if self.source in ("auto", "widget") else None
            if path is not None and self.source != "cli" and path.stat().st_mtime > now - 900:
                try:
                    data = sanitize_widget_snapshot(json.loads(path.read_text(encoding="utf-8")), path)
                except Exception as exc:  # noqa: BLE001 - fall through to the CLI
                    data = {"generated_at": datetime.now(timezone.utc).isoformat(), "providers": [], "errors": [], "error": f"widget snapshot unreadable: {type(exc).__name__}"}
            else:
                data = run_codexbar(self.codexbar, self.provider)
            self._cached = (now, data)
            return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=("auto", "widget", "cli"),
        default="auto",
        help="auto reads the fresh widget snapshot first, then falls back to the CLI",
    )
    parser.add_argument("--codexbar", default=DEFAULT_CODEXBAR, help="CodexBar CLI executable")
    parser.add_argument("--provider", default="all", help="Provider id or 'all'")
    parser.add_argument("--serve", action="store_true", help="Serve /usage instead of printing once")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ttl", type=float, default=60.0, help="Snapshot cache seconds")
    parser.add_argument("--output", help="Write one snapshot to this file (default: stdout)")
    args = parser.parse_args(argv)

    if args.serve:
        server = BridgeServer((args.host, args.port), args.codexbar, args.provider, args.ttl, args.source)
        print(f"CodexBar bridge listening on http://{args.host}:{args.port}/usage", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
        return 0

    path = widget_snapshot_path() if args.source in ("auto", "widget") else None
    if path is not None and args.source != "cli" and path.stat().st_mtime > time.time() - 900:
        data = sanitize_widget_snapshot(json.loads(path.read_text(encoding="utf-8")), path)
    else:
        data = run_codexbar(args.codexbar, args.provider)
    rendered = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(rendered + "\n", encoding="utf-8")
        tmp.replace(target)
        print(target, file=sys.stderr)
    else:
        print(rendered)
    return 0 if not data.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
