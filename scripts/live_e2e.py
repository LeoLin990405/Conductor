#!/usr/bin/env python3
"""Real-stack E2E: backend APIs, CN market data, and browser native turns.

Unlike the mocked Playwright suite, this drives the running frontend (:5173)
and backend (:8000), and every native turn is a real vendor CLI call.

Usage:
    make e2e-live
    make e2e-live ARGS="--quick"              # API/CN only, no native turns
    make e2e-live ARGS="--providers grok,kimi"
    python3 scripts/live_e2e.py --json

Requires the live stack from `make dev`/`make dev-web`, plus logged-in native
CLIs for the browser-turn phase.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
EXPECTED_NATIVE = ["codex", "deepseek-dsh", "grok", "kimi", "mimo", "minimax", "stepfun", "zcode"]
GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
SYMBOLS = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "-"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    duration: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, **data: Any) -> None:
        self.checks.append(Check(name, status, detail, data=data))

    @property
    def failed(self) -> bool:
        return any(c.status == "fail" for c in self.checks)


def http_json(url: str, timeout: float = 20.0) -> tuple[int | None, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, None


def api(base: str, path: str) -> tuple[int | None, Any]:
    return http_json(f"{base}{path}")


def check_health(report: Report, base: str) -> bool:
    started = time.monotonic()
    status, body = api(base, "/health")
    ok = status == 200 and isinstance(body, dict) and body.get("status") == "healthy"
    report.add("backend health", "ok" if ok else "fail", f"HTTP {status}", duration=round(time.monotonic() - started, 2))
    return ok


def check_native_inventory(report: Report, base: str) -> list[str]:
    started = time.monotonic()
    status, body = api(base, "/api/v1/native-agents")
    if status != 200 or not isinstance(body, dict):
        report.add("native inventory", "fail", f"HTTP {status}", duration=round(time.monotonic() - started, 2))
        return []
    runtimes = [r for r in body.get("runtimes", []) if isinstance(r, dict)]
    live = [r["provider"] for r in runtimes if r.get("live_verified") and r.get("provider")]
    missing = [p for p in EXPECTED_NATIVE if p not in live]
    if missing:
        report.add(
            "native inventory",
            "fail",
            f"{len(live)} live, missing: {', '.join(missing)}",
            duration=round(time.monotonic() - started, 2),
            data={"live": live, "missing": missing},
        )
    else:
        report.add(
            "native inventory",
            "ok",
            f"all {len(EXPECTED_NATIVE)} live: {', '.join(live)}",
            duration=round(time.monotonic() - started, 2),
            data={"live": live},
        )
    return live


def find_home_workspace(report: Report, base: str, explicit: str | None) -> str | None:
    if explicit:
        report.add("workspace", "ok", explicit)
        return explicit
    status, body = api(base, "/api/v1/workspaces?include_home=true")
    workspaces = body.get("workspaces", []) if isinstance(body, dict) else []
    if status == 200 and workspaces:
        ws = workspaces[0].get("workspace_id")
        report.add("workspace", "ok", ws)
        return ws
    # Older/local deployments may not list home; fall back to the saved id file
    # used by the existing live smoke scripts, if present.
    fallback = Path("/tmp/home.json")
    if fallback.is_file():
        try:
            ws = json.loads(fallback.read_text()).get("workspace_id")
            if ws:
                report.add("workspace", "warn", f"{ws} (fallback /tmp/home.json)")
                return ws
        except (OSError, ValueError):
            pass
    report.add("workspace", "fail", "no workspace found (pass --workspace)")
    return None


def check_cn_market(report: Report, base: str) -> None:
    started = time.monotonic()
    status, pulse = api(base, "/api/v1/cn/market/pulse")
    sources = (pulse or {}).get("sources", []) if isinstance(pulse, dict) else []
    ok_sources = [s.get("key") for s in sources if isinstance(s, dict) and s.get("status") == "ok"]
    if status != 200 or not isinstance(pulse, dict):
        report.add("cn market pulse", "fail", f"HTTP {status}", duration=round(time.monotonic() - started, 2))
    elif not sources:
        report.add("cn market pulse", "fail", "no data sources in response", duration=round(time.monotonic() - started, 2))
    elif not ok_sources:
        report.add("cn market pulse", "fail", f"all sources unavailable: {[s.get('key') for s in sources]}", duration=round(time.monotonic() - started, 2))
    else:
        report.add("cn market pulse", "ok", f"sources ok: {', '.join(ok_sources)}", duration=round(time.monotonic() - started, 2))

    started = time.monotonic()
    status, insight = api(base, "/api/v1/cn/insight")
    insights = (insight or {}).get("insights", []) if isinstance(insight, dict) else []
    if status == 200 and insights:
        report.add("cn market insight", "ok", f"{len(insights)} insight(s)", duration=round(time.monotonic() - started, 2))
    else:
        # The insight job calls live upstreams and can be between generations;
        # pulse is the required CN check, insight is useful evidence when present.
        report.add("cn market insight", "warn", f"HTTP {status}, insights={len(insights)}", duration=round(time.monotonic() - started, 2))

    started = time.monotonic()
    status, news = api(base, "/api/v1/cn/news")
    articles = (news or {}).get("articles", []) if isinstance(news, dict) else []
    if status == 200 and articles:
        report.add("cn news", "ok", f"{len(articles)} articles", duration=round(time.monotonic() - started, 2))
    else:
        report.add(
            "cn news",
            "warn",
            f"HTTP {status}, articles={len(articles)} (upstream may be temporarily unavailable)",
            duration=round(time.monotonic() - started, 2),
        )


def run_browser_turns(report: Report, workspace: str, providers: list[str], timeout: float) -> None:
    started = time.monotonic()
    env = dict(os.environ)
    env["CONDUCTOR_WORKSPACE_ID"] = workspace
    env["PROVIDERS"] = ",".join(providers)
    try:
        result = subprocess.run(
            ["node", "scripts/live-native-turn-smoke.mjs"],
            cwd=WEB,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("browser native turns", "fail", str(exc), duration=round(time.monotonic() - started, 2))
        return
    output = (result.stdout or "") + (result.stderr or "")
    lines = [line for line in output.splitlines() if "PROVIDER_TURN_OK" in line]
    errors = [line for line in output.splitlines() if line.startswith("PAGE_ERRORS")]
    good = result.returncode == 0 and len(lines) == len(providers)
    detail = f"{len(lines)}/{len(providers)} providers" + (f"; {errors[-1]}" if errors else "")
    if not good:
        detail += "\n" + "\n".join(output.splitlines()[-20:])
    report.add("browser native turns", "ok" if good else "fail", detail, duration=round(time.monotonic() - started, 2))


def run_native_hitl(report: Report, workspace: str, provider: str, timeout: float = 420) -> None:
    started = time.monotonic()
    env = dict(os.environ)
    env["CONDUCTOR_WORKSPACE_ID"] = workspace
    env["HITL_PROVIDER"] = provider
    try:
        result = subprocess.run(
            ["node", "scripts/live-native-hitl-smoke.mjs"],
            cwd=WEB,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("native HITL", "fail", str(exc), duration=round(time.monotonic() - started, 2))
        return
    output = (result.stdout or "") + (result.stderr or "")
    passed = result.returncode == 0 and "NATIVE_HITL_OK" in output
    detail = f"{provider}: approval -> command -> resumed turn" if passed else "\n".join(output.splitlines()[-20:])
    report.add("native HITL", "ok" if passed else "fail", detail, duration=round(time.monotonic() - started, 2))


def run_page_smoke(report: Report, workspace: str) -> None:
    started = time.monotonic()
    env = dict(os.environ)
    env["CONDUCTOR_WORKSPACE_ID"] = workspace
    try:
        result = subprocess.run(
            ["node", "scripts/live-page-smoke.mjs"],
            cwd=WEB,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("browser key pages", "fail", str(exc), duration=round(time.monotonic() - started, 2))
        return
    output = (result.stdout or "") + (result.stderr or "")
    passed = result.returncode == 0 and "LIVE_PAGE_SMOKE_OK" in output
    detail = "desktop+mobile routes passed" if passed else "\n".join(output.splitlines()[-20:])
    report.add("browser key pages", "ok" if passed else "fail", detail, duration=round(time.monotonic() - started, 2))


def render(report: Report, color: bool = True) -> str:
    c = {k: (v if color else "") for k, v in {"ok": GREEN, "warn": YELLOW, "fail": RED, "skip": DIM}.items()}
    lines = ["Conductor live E2E"]
    for check in report.checks:
        symbol = SYMBOLS.get(check.status, "?")
        suffix = f"  {check.duration:.1f}s" if check.duration else ""
        lines.append(f"  {c.get(check.status, '')}{symbol}{RESET if color else ''} {check.name:<20} {check.detail}{suffix}")
    ok = sum(c.status == "ok" for c in report.checks)
    warn = sum(c.status == "warn" for c in report.checks)
    fail = sum(c.status == "fail" for c in report.checks)
    lines.append("")
    lines.append(f"Summary: {ok} ok, {warn} warn, {fail} fail")
    lines.append("Result: LIVE E2E PASS" if not report.failed else "Result: LIVE E2E FAIL")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Conductor real-stack E2E")
    parser.add_argument("--base", default=os.environ.get("CONDUCTOR_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--workspace", default=os.environ.get("CONDUCTOR_WORKSPACE_ID"))
    parser.add_argument("--providers", default=",".join(EXPECTED_NATIVE))
    parser.add_argument("--hitl-provider", default=os.environ.get("HITL_PROVIDER", "codex"))
    parser.add_argument("--quick", action="store_true", help="skip browser native turns, HITL and key-page smoke")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800, help="native-turn subprocess timeout (seconds)")
    args = parser.parse_args()

    report = Report()
    if not check_health(report, args.base):
        print(render(report, color=sys.stdout.isatty()))
        return 1
    live = check_native_inventory(report, args.base)
    workspace = find_home_workspace(report, args.base, args.workspace)
    check_cn_market(report, args.base)
    if not args.quick and workspace and live:
        providers = [p.strip() for p in args.providers.split(",") if p.strip()]
        run_browser_turns(report, workspace, providers, args.timeout)
        run_native_hitl(report, workspace, args.hitl_provider)
        run_page_smoke(report, workspace)

    if args.json:
        print(json.dumps({"ready": not report.failed, "checks": [asdict(c) for c in report.checks]}, indent=2, ensure_ascii=False))
    else:
        print(render(report, color=sys.stdout.isatty()))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
