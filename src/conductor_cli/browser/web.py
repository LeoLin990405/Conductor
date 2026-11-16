"""Browser-first web commands for the local Conductor CLI."""

from __future__ import annotations

import argparse
import base64
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from ..client import CLIError
from .transport import browser_command, validate_agent

MAX_RESPONSE_BYTES = 1_000_000
MAX_SEARCH_RESULTS = 10
_RAW_SUFFIXES = {".csv", ".docx", ".jpeg", ".jpg", ".mp3", ".mp4", ".pdf", ".png", ".txt", ".webp", ".xlsx", ".xml", ".zip"}
_BINARY_SUFFIXES = {".docx", ".jpeg", ".jpg", ".mp3", ".mp4", ".pdf", ".png", ".webp", ".xlsx", ".zip"}
_FALLBACK_CODES = {"browser_error", "browser_timeout", "unavailable", "not_ready", "timeout"}


def register_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    web = commands.add_parser("web", help="open, read, and search the web in an isolated agent browser")
    actions = web.add_subparsers(dest="web_command", required=True)

    open_parser = actions.add_parser("open", help="open a URL in the managed agent browser")
    open_parser.add_argument("url")
    open_parser.add_argument("--agent", required=True)
    open_parser.set_defaults(handler=handle_open)

    read_parser = actions.add_parser("read", help="read a page through the managed agent browser")
    read_parser.add_argument("url")
    read_parser.add_argument("--agent", required=True)
    read_parser.add_argument("--site", help="use a named registered site adapter")
    read_parser.add_argument("--raw", action="store_true", help="fetch the response body directly")
    read_parser.add_argument("--api", action="store_true", help="fetch and parse a JSON/API response directly")
    read_parser.set_defaults(handler=handle_read)

    search_parser = actions.add_parser("search", help="search the web in the managed agent browser")
    search_parser.add_argument("query")
    search_parser.add_argument("--agent", required=True)
    search_parser.add_argument("--limit", type=int, default=5)
    search_parser.set_defaults(handler=handle_search)


def _clean_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CLIError("Use a valid HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError):
        raise CLIError("Use a valid HTTP or HTTPS URL") from None


def _display_url(value: str) -> str:
    parsed = urlsplit(_clean_url(value))
    host = parsed.hostname or ""
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _sanitize_url_fields(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"url", "href"} and isinstance(item, str):
                try:
                    sanitized[key] = _display_url(item)
                except CLIError:
                    sanitized[key] = ""
            else:
                sanitized[key] = _sanitize_url_fields(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_url_fields(item) for item in value]
    return value


def _envelope_data(value: Any) -> Any:
    if not isinstance(value, dict) or "ok" not in value:
        return value
    if value.get("ok") is True:
        return value.get("data")
    error = value.get("error")
    if not isinstance(error, dict):
        raise CLIError("Browser command failed")
    code = error.get("code") if isinstance(error.get("code"), str) else "browser_error"
    message = error.get("message") if isinstance(error.get("message"), str) else "Browser command failed"
    exc = CLIError(message[:200])
    exc.code = code  # type: ignore[attr-defined]
    raise exc


def _fallback_allowed(exc: CLIError) -> bool:
    message = str(exc).lower()
    return (
        getattr(exc, "code", None) in _FALLBACK_CODES
        or "time limit" in message
        or "timed out" in message
        or "managed browser is unavailable" in message
        or "browser owner exited during startup" in message
        or "did not become ready" in message
    )


def _browser_read(agent: str, url: str, *, site: str | None = None, start: bool = False) -> Any:
    request: dict[str, Any] = {"command": "read", "url": url}
    if site:
        request["site"] = site
    return _envelope_data(browser_command(agent, request, start=start, timeout=25.0))


class _TextExtractor(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _SUPPRESSED = {"script", "style", "noscript", "svg", "textarea", "select", "option"}
    _BLOCKS = {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        style = re.sub(r"\s+", "", values.get("style", "").lower())
        hidden = bool(self.stack and self.stack[-1][1]) or tag in self._SUPPRESSED or "hidden" in values or values.get("aria-hidden", "").lower() == "true" or "display:none" in style or "visibility:hidden" in style
        if tag not in self._VOID:
            self.stack.append((tag, hidden))
        if tag in self._BLOCKS and not hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        hidden = bool(self.stack and self.stack[-1][1])
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                hidden = self.stack[index][1]
                del self.stack[index:]
                break
        if tag in self._BLOCKS and not hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not (self.stack and self.stack[-1][1]):
            self.parts.append(data)


def _raw_fetch(url: str, *, parse_json: bool = False, raw_bytes: bool = False, preserve_markup: bool = False) -> dict[str, Any]:
    current = url
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10.0)) as client:
            response_data: bytes | None = None
            status_code = 0
            media_type = ""
            final_url = current
            for _ in range(4):
                with client.stream("GET", current, headers={"Accept": "application/json, text/html;q=0.9, */*;q=0.5"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                        current = _clean_url(urljoin(current, response.headers["location"]))
                        client.cookies.clear()
                        continue
                    status_code = response.status_code
                    final_url = str(response.url)
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    body_buffer = bytearray()
                    for chunk in response.iter_bytes():
                        body_buffer.extend(chunk)
                        if len(body_buffer) > MAX_RESPONSE_BYTES:
                            raise CLIError("Response exceeded the 1 MB limit")
                    response_data = bytes(body_buffer)
                    encoding = response.encoding or "utf-8"
                    client.cookies.clear()
                    break
            if response_data is None:
                raise CLIError("Too many web redirects")
            if not 200 <= status_code < 300:
                raise CLIError("Direct web fetch returned an unsuccessful status")
            try:
                text = response_data.decode(encoding, errors="replace")
            except LookupError:
                text = response_data.decode("utf-8", errors="replace")
            data: Any = text
            path = urlsplit(current).path.lower()
            binary_type = media_type.startswith(("image/", "audio/", "video/")) or media_type in {"application/octet-stream", "application/zip", "application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
            if binary_type or (raw_bytes and any(path.endswith(suffix) for suffix in _BINARY_SUFFIXES)):
                data = {"encoding": "base64", "content": base64.b64encode(response_data).decode("ascii")}
            elif not raw_bytes and (parse_json or media_type in {"application/json", "text/json"} or media_type.endswith("+json") or path.endswith(".json")):
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, RecursionError):
                    if parse_json:
                        raise CLIError("Response was not valid JSON") from None
            elif not preserve_markup and ("html" in media_type or "<html" in text[:512].lower()):
                parser = _TextExtractor()
                parser.feed(text)
                data = re.sub(r"[ \t]+", " ", "".join(parser.parts))
                data = re.sub(r"\n{3,}", "\n\n", data).strip()[:MAX_RESPONSE_BYTES]
            return {
                "route": "fetch",
                "reason": "explicit_api" if parse_json else "explicit_raw",
                "status": status_code,
                "url": _display_url(final_url),
                "content_type": media_type,
                "data": data,
            }
    except CLIError:
        raise
    except (httpx.HTTPError, OSError, ValueError):
        raise CLIError("Direct web fetch failed") from None


def _with_route(data: Any, route: str, reason: str) -> dict[str, Any]:
    if isinstance(data, dict):
        return {"route": route, "reason": reason, **data}
    return {"route": route, "reason": reason, "data": data}


def _read(url: str, agent: str, *, site: str | None = None) -> dict[str, Any]:
    validate_agent(agent)
    normalized = _clean_url(url)
    path = urlsplit(normalized).path.lower()
    if path.endswith(".json") or any(path.endswith(suffix) for suffix in _RAW_SUFFIXES):
        return _raw_fetch(normalized, parse_json=path.endswith(".json"), raw_bytes=any(path.endswith(suffix) for suffix in _BINARY_SUFFIXES))
    try:
        result = _sanitize_url_fields(_browser_read(agent, normalized, site=site, start=True))
        return _with_route(result, "browser", "default_read")
    except CLIError as exc:
        if not _fallback_allowed(exc):
            raise
        result = _raw_fetch(normalized)
        result["reason"] = "browser_failure"
        return result


def handle_open(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    return _read(args.url, args.agent)


def handle_read(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    if args.raw and args.api:
        raise CLIError("Choose either --raw or --api")
    validate_agent(args.agent)
    url = _clean_url(args.url)
    if args.raw or args.api:
        return _raw_fetch(url, parse_json=args.api, raw_bytes=args.raw)
    return _read(url, args.agent, site=args.site)


def _walk_snapshot(snapshot: Any):
    if not isinstance(snapshot, dict):
        return
    payload = snapshot.get("snapshot", snapshot)
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    frames = payload.get("frames", []) if isinstance(payload, dict) else []
    for frame in frames if isinstance(frames, list) else []:
        stack = list(frame.get("items", [])) if isinstance(frame, dict) else []
        while stack:
            node = stack.pop(0)
            if not isinstance(node, dict):
                continue
            yield node
            children = node.get("children", [])
            if isinstance(children, list):
                stack[0:0] = children


def _extract_link(agent: str, ref: str) -> dict[str, Any] | None:
    result = browser_command(agent, {"command": "run", "steps": [{"op": "extract", "ref": ref, "fields": ["href", "text"]}]}, timeout=20.0)
    result = _envelope_data(result)
    steps = result.get("steps", []) if isinstance(result, dict) else []
    if not steps or steps[0].get("status") != "ok":
        return None
    extracted = steps[0].get("result", {})
    if isinstance(extracted, dict):
        return extracted
    return None


class _SearchLinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if "result__a" in classes and values.get("href"):
            self.current = {"href": values["href"], "text": ""}

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            self.results.append({key: value[:1024] for key, value in self.current.items()})
            self.current = None


def _search_links_from_markup(markup: str, limit: int) -> list[dict[str, Any]]:
    parser = _SearchLinkExtractor()
    parser.feed(markup)
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parser.results:
        try:
            href = urljoin("https://html.duckduckgo.com/", item["href"])
            parsed = urlsplit(href)
            if parsed.hostname and parsed.hostname.lower().endswith("duckduckgo.com"):
                href = parse_qs(parsed.query).get("uddg", [None])[0] or ""
            href = _clean_url(href)
        except CLIError:
            continue
        if "duckduckgo.com" in (urlsplit(href).hostname or "").lower() or href in seen:
            continue
        seen.add(href)
        links.append({"url": href, "title": item["text"].strip()[:240]})
        if len(links) >= limit:
            break
    return links


def _search_links(agent: str, query: str, limit: int) -> tuple[Any, list[dict[str, Any]], str, str]:
    search_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    try:
        search_page = _sanitize_url_fields(_browser_read(agent, search_url, start=True))
    except CLIError as exc:
        if not _fallback_allowed(exc):
            raise
        fallback = _raw_fetch(search_url, raw_bytes=True, preserve_markup=True)
        markup = fallback.get("data")
        links = _search_links_from_markup(markup, limit) if isinstance(markup, str) else []
        page_meta = {key: value for key, value in fallback.items() if key != "data"}
        return page_meta, links, "fetch", "browser_failure"
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in _walk_snapshot(search_page):
        if node.get("role") != "link" or not isinstance(node.get("ref"), str):
            continue
        extracted = _extract_link(agent, node["ref"])
        if not extracted or not isinstance(extracted.get("href"), str):
            continue
        href = extracted["href"]
        try:
            parsed = urlsplit(href)
            if parsed.hostname and parsed.hostname.lower().endswith("duckduckgo.com"):
                target = parse_qs(parsed.query).get("uddg", [None])[0]
                href = target or href
            href = _clean_url(href)
        except CLIError:
            continue
        if href not in seen and "duckduckgo.com" not in (urlsplit(href).hostname or "").lower():
            seen.add(href)
            links.append({"url": href, "title": str(extracted.get("text", node.get("accessible_name", "")))[:240]})
            if len(links) >= limit:
                break
    return search_page, links, "browser", "browser_search"


def handle_search(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    del client
    validate_agent(args.agent)
    query = args.query.strip()
    if not query or len(query) > 512:
        raise CLIError("Search query must contain 1 to 512 characters")
    if not 1 <= args.limit <= MAX_SEARCH_RESULTS:
        raise CLIError("--limit must be between 1 and 10")
    try:
        search_page, links, route, reason = _search_links(args.agent, query, args.limit)
    except CLIError as exc:
        if not _fallback_allowed(exc):
            raise
        raise CLIError("Browser search failed") from None
    results = []
    for link in links:
        try:
            page = _read(link["url"], args.agent)
            results.append({**link, "url": _display_url(link["url"]), "read": page})
        except CLIError as exc:
            results.append({
                **link,
                "url": _display_url(link["url"]),
                "read": _cli_failure(exc),
            })
    return {
        "route": route,
        "reason": reason,
        "status": "partial" if any(result["read"].get("ok") is False for result in results) else "complete",
        "query": query,
        "search_page": search_page,
        "results": results,
    }


def _cli_failure(exc: CLIError) -> dict[str, Any]:
    from .envelope import failure

    return failure(
        getattr(exc, "code", "read_failed"),
        str(exc)[:200],
        getattr(exc, "hint", "The result was found, but its page could not be read."),
    )
