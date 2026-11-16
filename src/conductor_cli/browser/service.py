"""Bounded command validation and execution for an owned browser."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .adapters import AdapterError, DEFAULT_ADAPTERS, SiteAdapter
from .driver import BrowserDriver
from .envelope import failure, success

MAX_STEPS = 30
MAX_TEXT = 4096
MAX_INPUT_BYTES = 256_000
MAX_OUTPUT_BYTES = 256_000
MAX_STEP_OUTPUT_BYTES = 32_000
MAX_WAIT_SECONDS = 10.0
DEFAULT_STEP_TIMEOUT = 15.0
DEFAULT_BATCH_TIMEOUT = 60.0
MAX_URL_LENGTH = 8192
SAFE_FIELDS = {"text", "href", "title", "tag", "role", "accessible_name"}
SAFE_ROLES = {
    "alert", "button", "checkbox", "combobox", "generic", "heading", "link",
    "listitem", "main", "navigation", "option", "paragraph", "radio",
    "searchbox", "switch", "tab", "textbox",
}
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.I)
MAX_EVAL_BYTES = 8_000
MAX_NETWORK_ROWS = 50

_NATIVE_SCHEMAS: dict[str, dict[str, Any]] = {
    "commands": {"type": "object", "properties": {"command": {"const": "commands"}}, "required": ["command"], "additionalProperties": False},
    "help": {"type": "object", "properties": {"command": {"const": "help"}, "name": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
    "site": {"type": "object", "properties": {"command": {"const": "site"}, "site": {"type": "string"}, "action": {"type": "string"}, "url": {"type": "string", "format": "uri"}, "args": {"type": "object", "additionalProperties": False}}, "required": ["command", "site", "action", "url"], "additionalProperties": False},
    "read": {"type": "object", "properties": {"command": {"const": "read"}, "url": {"type": "string", "format": "uri"}, "site": {"type": "string"}}, "required": ["command", "url"], "additionalProperties": False},
    "eval": {"type": "object", "properties": {"command": {"const": "eval"}, "script": {"type": "string", "maxLength": MAX_EVAL_BYTES}, "enabled": {"const": True}}, "required": ["command", "script", "enabled"], "additionalProperties": False},
    "network": {"type": "object", "properties": {"command": {"const": "network"}, "url": {"type": "string", "format": "uri"}, "enabled": {"const": True}}, "required": ["command", "url", "enabled"], "additionalProperties": False},
}
_NATIVE_SUMMARIES = {
    "commands": "List native browser commands and installed site adapters.",
    "help": "Return the JSON Schema for one command, or all native command schemas.",
    "site": "Run one registered site adapter action on a matching managed page.",
    "read": "Navigate to a URL and return a matching adapter result or a safe semantic snapshot.",
    "eval": "Evaluate page JavaScript only when explicitly enabled; the result value is never returned.",
    "network": "Capture metadata for an explicitly enabled navigation; no URL paths or sensitive payloads are returned.",
}


def discovery_response(command: str, name: str | None = None, *, adapters: tuple[SiteAdapter, ...] = DEFAULT_ADAPTERS) -> dict[str, Any]:
    """Build command discovery locally without starting or contacting an owner."""
    if command == "commands":
        return success({
            "commands": [{"name": key, "summary": _NATIVE_SUMMARIES[key], "schema": value} for key, value in _NATIVE_SCHEMAS.items()],
            "sites": [{"name": adapter.name, "summary": adapter.summary, "domains": list(adapter.domains), "actions": [{"name": action.name, "summary": action.summary, "schema": action.schema} for action in adapter.actions]} for adapter in adapters],
        })
    if command == "help":
        if name is None:
            return success({"commands": [{"name": key, "summary": _NATIVE_SUMMARIES[key], "schema": value} for key, value in _NATIVE_SCHEMAS.items()]})
        if not isinstance(name, str) or name not in _NATIVE_SCHEMAS:
            return failure("unknown_command", "Unknown native browser command", "Run `conductor browser commands` to list commands.")
        return success({"name": name, "summary": _NATIVE_SUMMARIES[name], "schema": _NATIVE_SCHEMAS[name]})
    return failure("unknown_command", "Unknown native browser command")


class BrowserCommandError(ValueError):
    """A safe, user-displayable browser command error."""


def clean_domain(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or ":" in value or "/" in value or "*" in value:
        raise BrowserCommandError("Allowed domains must be exact DNS names")
    raw = value[:-1] if value.endswith(".") else value
    if not raw or raw.endswith("."):
        raise BrowserCommandError("Allowed domains must be exact DNS names")
    try:
        domain = raw.encode("idna").decode("ascii").lower()
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    except UnicodeError as exc:
        raise BrowserCommandError("Allowed domains must be valid IDNA DNS names") from exc
    else:
        raise BrowserCommandError("Allowed domains must be exact DNS names, not IP addresses")
    if not DOMAIN_PATTERN.fullmatch(domain):
        raise BrowserCommandError("Allowed domains must be exact DNS names")
    return domain


def validate_import_state(path: Any, domains: Any) -> tuple[Path, list[str]]:
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise BrowserCommandError("A storage-state file path is required")
    if not isinstance(domains, list) or not domains or len(domains) > 50:
        raise BrowserCommandError("Provide one or more exact --domain values")
    normalized = [clean_domain(domain) for domain in domains]
    if len(set(normalized)) != len(normalized):
        raise BrowserCommandError("Allowed domains must be unique")
    return Path(path).expanduser(), normalized


def _safe_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        raise BrowserCommandError("URL is missing or too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BrowserCommandError("URL contains invalid control characters")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        _ = parsed.port
    except ValueError as exc:
        raise BrowserCommandError("Use a valid HTTP or HTTPS URL without user information") from exc
    return value


def _validate_target(step: dict[str, Any], op: str) -> dict[str, Any]:
    target = step.get("target")
    if not isinstance(target, dict) or set(target) - {"role", "name", "frame"}:
        raise BrowserCommandError(f"{op} requires either ref or a semantic target")
    role, name, frame = target.get("role"), target.get("name"), target.get("frame")
    if not isinstance(role, str) or role not in SAFE_ROLES or not isinstance(name, str) or not name or len(name) > 256:
        raise BrowserCommandError("Target requires a supported role and exact accessible name")
    if frame is not None and (not isinstance(frame, str) or not 1 <= len(frame) <= 64):
        raise BrowserCommandError("Target frame must be a frame key from the latest snapshot")
    return {"role": role, "name": name, **({"frame": frame} if frame is not None else {})}


def _validate_target_or_ref(step: dict[str, Any], op: str) -> dict[str, Any]:
    if ("ref" in step) == ("target" in step):
        raise BrowserCommandError(f"{op} requires exactly one of ref or target")
    if "ref" in step:
        ref = step["ref"]
        if not isinstance(ref, str) or not ref or len(ref) > 160:
            raise BrowserCommandError(f"{op} requires a valid snapshot ref")
        return {"ref": ref}
    return {"target": _validate_target(step, op)}


def _validate_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict) or not isinstance(step.get("op"), str):
        raise BrowserCommandError("Each step must be an object with an op")
    op = step["op"]
    allowed_fields: dict[str, set[str]] = {
        "goto": {"op", "url"},
        "click": {"op", "ref", "target"},
        "fill": {"op", "ref", "target", "text"},
        "wait": {"op", "seconds"},
        "snapshot": {"op"},
        "extract": {"op", "ref", "target", "fields"},
    }
    if op not in allowed_fields or set(step) - allowed_fields[op]:
        raise BrowserCommandError("Unknown browser operation or step field")
    normalized = dict(step)
    if op == "goto":
        normalized["url"] = _safe_url(step.get("url"))
    elif op in {"click", "fill", "extract"}:
        normalized.update(_validate_target_or_ref(step, op))
        if op == "fill":
            text = step.get("text")
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise BrowserCommandError("fill text must be a string of at most 4096 characters")
        if op == "extract":
            fields = step.get("fields", ["text"])
            if not isinstance(fields, list) or not fields or len(fields) > len(SAFE_FIELDS) or any(not isinstance(field, str) or field not in SAFE_FIELDS for field in fields):
                raise BrowserCommandError("extract fields must use the documented safe field names")
            normalized["fields"] = list(dict.fromkeys(fields))
    elif op == "wait":
        seconds = step.get("seconds")
        if type(seconds) not in {int, float} or not math.isfinite(seconds) or not 0 <= seconds <= MAX_WAIT_SECONDS:
            raise BrowserCommandError("wait seconds must be between 0 and 10")
        normalized["seconds"] = float(seconds)
    return normalized


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise BrowserCommandError("run requires between 1 and 30 steps")
    return [_validate_step(step) for step in steps]


def parse_flow(text: str, *, format: str | None = None) -> list[dict[str, Any]]:
    if not isinstance(text, str):
        raise BrowserCommandError("Flow input must be text")
    if format not in {None, "json", "yaml"}:
        raise BrowserCommandError("Flow format must be JSON or YAML")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise BrowserCommandError("Flow file is too large")
    try:
        if format == "json" or (format is None and text.lstrip().startswith(("{", "["))):
            payload = json.loads(text)
        else:
            payload = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError, UnicodeError) as exc:
        raise BrowserCommandError("Flow must be valid JSON or YAML") from exc
    if isinstance(payload, dict):
        if set(payload) != {"steps"}:
            raise BrowserCommandError("Flow object may contain only a steps field")
        steps = payload["steps"]
    else:
        steps = payload
    return validate_steps(steps)


def _json_encoded(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise BrowserCommandError("Browser result was not safe JSON data") from None


def _bounded(value: Any) -> Any:
    if len(_json_encoded(value).encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise BrowserCommandError("Browser result exceeded the output limit")
    return value


def _origin_only(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        port = parsed.port
        host = parsed.hostname.lower()
        return f"{parsed.scheme}://{host}{f':{port}' if port is not None else ''}"
    except ValueError:
        return ""


def _bound_step(value: Any) -> Any:
    if len(_json_encoded(value).encode("utf-8")) > MAX_STEP_OUTPUT_BYTES:
        raise BrowserCommandError("Step result exceeded its output limit")
    return value


class BrowserService:
    def __init__(
        self,
        driver: BrowserDriver,
        profile: Path,
        *,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        batch_timeout: float = DEFAULT_BATCH_TIMEOUT,
        adapters: tuple[SiteAdapter, ...] = DEFAULT_ADAPTERS,
    ) -> None:
        self.driver = driver
        self.profile = profile
        self.step_timeout = step_timeout
        self.batch_timeout = batch_timeout
        self.adapters = adapters

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or not isinstance(request.get("command"), str):
            raise BrowserCommandError("Invalid browser request")
        command = request["command"]
        if command in _NATIVE_SCHEMAS:
            response = await self._handle_native(request)
            try:
                return _bounded(response)
            except BrowserCommandError:
                return failure("result_too_large", "Browser result exceeded the output limit")
        allowed = {
            "snapshot": {"command"},
            "run": {"command", "steps"},
            "import-login": {"command", "path", "domains"},
            "close": {"command"},
            "ping": {"command"},
        }
        if command not in allowed or set(request) != allowed[command]:
            raise BrowserCommandError("Invalid browser request fields")
        if command == "snapshot":
            try:
                async with asyncio.timeout(self.step_timeout):
                    return _bounded(await self.driver.snapshot())
            except Exception:
                raise BrowserCommandError("Snapshot failed or exceeded its time limit") from None
        if command == "run":
            steps = validate_steps(request["steps"])
            results: list[dict[str, Any]] = []

            async def execute_steps() -> None:
                for index, step in enumerate(steps):
                    started = time.monotonic()
                    try:
                        async with asyncio.timeout(self.step_timeout):
                            result = await self._execute_step(step)
                        result = _bound_step(result)
                        row = {"index": index, "op": step["op"], "status": "ok", "elapsed_ms": round((time.monotonic() - started) * 1000), "result": result}
                        if len(_json_encoded({"steps": [*results, row]}).encode("utf-8")) > MAX_OUTPUT_BYTES:
                            raise BrowserCommandError("Batch result exceeded its output limit")
                        results.append(row)
                    except Exception:
                        results.append({"index": index, "op": step["op"], "status": "error", "elapsed_ms": round((time.monotonic() - started) * 1000), "error": "Step failed or exceeded its time limit"})
                        results.extend({"index": later, "op": steps[later]["op"], "status": "skipped"} for later in range(index + 1, len(steps)))
                        return

            try:
                async with asyncio.timeout(self.batch_timeout):
                    await execute_steps()
            except TimeoutError:
                if len(results) < len(steps):
                    index = len(results)
                    results.append({"index": index, "op": steps[index]["op"], "status": "error", "error": "Batch exceeded its time limit"})
                    results.extend({"index": later, "op": steps[later]["op"], "status": "skipped"} for later in range(index + 1, len(steps)))
            return _bounded({"steps": results})
        if command == "import-login":
            path, domains = validate_import_state(request.get("path"), request.get("domains"))
            try:
                async with asyncio.timeout(self.step_timeout):
                    return _bounded(await self.driver.import_storage_state(path, domains))
            except Exception:
                raise BrowserCommandError("Login state import failed or exceeded its time limit") from None
        if command == "close":
            await self.driver.close()
            return {"status": "closing"}
        return {"status": "ready"}

    async def _handle_native(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request["command"]
        schema = _NATIVE_SCHEMAS[command]
        if set(request) - set(schema["properties"]):
            return failure("invalid_request", "Unknown browser request fields")
        if command == "commands":
            return discovery_response(command, adapters=self.adapters)
        if command == "help":
            return discovery_response(command, request.get("name"), adapters=self.adapters)
        try:
            if command == "eval":
                return await self._native_eval(request)
            if command == "network":
                return await self._native_network(request)
            if command == "site":
                return await self._native_site(request)
            if command == "read":
                return await self._native_read(request)
        except BrowserCommandError as exc:
            return failure("browser_error", str(exc))
        except (AdapterError, TimeoutError):
            return failure("browser_error", "Browser operation failed or exceeded its time limit")
        except Exception:
            return failure("browser_error", "Browser operation failed")
        return failure("unknown_command", "Unknown native browser command")

    async def _native_eval(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) != {"command", "script", "enabled"}:
            return failure("invalid_request", "eval requires script and enabled fields")
        if request["enabled"] is not True:
            return failure("opt_in_required", "Page evaluation is disabled for this call", "Pass enabled=true for this call to allow evaluation.")
        script = request["script"]
        if not isinstance(script, str) or not script.strip() or len(script.encode("utf-8")) > MAX_EVAL_BYTES:
            return failure("invalid_request", "script must be non-empty and at most 8000 UTF-8 bytes")
        method = getattr(self.driver, "evaluate_page", None)
        if method is None:
            return failure("unsupported", "Page evaluation is unavailable in this browser driver")
        async with asyncio.timeout(self.step_timeout):
            metadata = await method(script)
        if not isinstance(metadata, dict):
            metadata = {}
        allowed_kinds = {"undefined", "object", "boolean", "number", "string", "function", "symbol", "null", "array"}
        kind = metadata.get("result_kind")
        size = metadata.get("result_size")
        # Drivers return metadata only. Never put the evaluated value in the envelope.
        return success({
            "executed": True,
            "result_kind": kind if isinstance(kind, str) and kind in allowed_kinds else "unknown",
            "result_size": size if type(size) is int and 0 <= size <= 1_000_000 else 0,
        })

    async def _native_network(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) != {"command", "url", "enabled"}:
            return failure("invalid_request", "network requires url and enabled fields")
        if request["enabled"] is not True:
            return failure("opt_in_required", "Network capture is disabled for this call", "Pass enabled=true for this call to capture one navigation.")
        try:
            url = _safe_url(request["url"])
        except BrowserCommandError as exc:
            return failure("invalid_request", str(exc))
        method = getattr(self.driver, "capture_network", None)
        if method is None:
            return failure("unsupported", "Network capture is unavailable in this browser driver")
        async with asyncio.timeout(self.step_timeout):
            rows = await method(url)
        safe_rows = []
        for row in rows[:MAX_NETWORK_ROWS] if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            safe_rows.append({
                "method": str(row.get("method", ""))[:12],
                "origin": _origin_only(row.get("origin", "")),
                "resource_type": str(row.get("resource_type", ""))[:24],
                "status": row.get("status") if type(row.get("status")) is int else None,
            })
        return success({"captured": safe_rows, "truncated": len(rows) > MAX_NETWORK_ROWS if isinstance(rows, list) else False})

    def _adapter(self, name: str | None) -> SiteAdapter | None:
        if name is None:
            return None
        for adapter in self.adapters:
            if adapter.name == name:
                return adapter
        raise AdapterError("Unknown site adapter")

    async def _native_site(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) - {"command", "site", "action", "url", "args"} or not {"site", "action", "url"}.issubset(request):
            return failure("invalid_request", "site requires site, action, and url; args is optional")
        if not isinstance(request["site"], str) or not isinstance(request["action"], str):
            return failure("invalid_request", "site and action must be strings")
        args = request.get("args", {})
        if not isinstance(args, dict):
            return failure("invalid_request", "args must be an object")
        try:
            url = _safe_url(request["url"])
        except BrowserCommandError as exc:
            return failure("invalid_request", str(exc))
        try:
            adapter = self._adapter(request["site"])
        except AdapterError:
            return failure("unknown_site", "Unknown site adapter", "Run `conductor browser commands` to list installed sites.")
        if not adapter.matches(url):
            return failure("site_mismatch", "URL does not match the selected site adapter")
        try:
            adapter.validate(request["action"], args)
        except AdapterError as exc:
            return failure("invalid_site_action", str(exc))
        async with asyncio.timeout(self.step_timeout):
            await self.driver.goto(url)
            snapshot = _bounded(await self.driver.snapshot())
        if not adapter.matches(str(snapshot.get("url", ""))):
            return failure("site_mismatch", "Navigation left the selected site's allowed domains")
        return success(adapter.execute(request["action"], args, snapshot))

    async def _native_read(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) - {"command", "url", "site"} or "url" not in request:
            return failure("invalid_request", "read requires url and accepts an optional site")
        try:
            url = _safe_url(request["url"])
        except BrowserCommandError as exc:
            return failure("invalid_request", str(exc))
        requested_name = request.get("site")
        if requested_name is not None and not isinstance(requested_name, str):
            return failure("invalid_request", "site must be a registered adapter name")
        try:
            requested_adapter = self._adapter(requested_name)
        except AdapterError:
            return failure("unknown_site", "Unknown site adapter", "Run `conductor browser commands` to list installed sites.")
        if requested_adapter is not None and not requested_adapter.matches(url):
            return failure("site_mismatch", "URL does not match the selected site adapter")
        if requested_adapter is not None:
            try:
                requested_adapter.validate("read", {})
            except AdapterError as exc:
                return failure("invalid_site_action", str(exc))
        async with asyncio.timeout(self.step_timeout):
            await self.driver.goto(url)
            snapshot = _bounded(await self.driver.snapshot())
        adapter = requested_adapter or next((candidate for candidate in self.adapters if candidate.matches(str(snapshot.get("url", "")))), None)
        if adapter is not None:
            if not adapter.matches(str(snapshot.get("url", ""))):
                return failure("site_mismatch", "Navigation left the selected site's allowed domains")
            return success({"site": adapter.name, "result": adapter.execute("read", {}, snapshot)})
        return success({"site": None, "result": snapshot})

    async def _execute_step(self, step: dict[str, Any]) -> dict[str, Any]:
        op = step["op"]
        if op == "goto":
            return await self.driver.goto(step["url"])
        if op == "wait":
            return await self.driver.wait(step["seconds"])
        if op == "snapshot":
            return await self.driver.snapshot()
        if op in {"click", "fill"}:
            return await self._action(step, op)
        return await self._extract(step)

    async def _action(self, step: dict[str, Any], operation: str) -> dict[str, Any]:
        target = step.get("target")
        if target is not None:
            if operation == "click":
                return await self.driver.click_target(target["role"], target["name"], target.get("frame"))
            return await self.driver.fill_target(target["role"], target["name"], step["text"], target.get("frame"))
        if operation == "click":
            return await self.driver.click(step["ref"])
        return await self.driver.fill(step["ref"], step["text"])

    async def _extract(self, step: dict[str, Any]) -> dict[str, Any]:
        target = step.get("target")
        if target is not None:
            return await self.driver.extract_target(target["role"], target["name"], step["fields"], target.get("frame"))
        return await self.driver.extract(step["ref"], step["fields"])
