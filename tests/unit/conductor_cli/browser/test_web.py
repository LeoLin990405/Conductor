from __future__ import annotations

import json
from typing import Any

import pytest

from src.conductor_cli import cli
from src.conductor_cli.browser import web
from src.conductor_cli.client import CLIError


def envelope(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def test_default_read_uses_managed_browser_and_returns_route(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(web, "browser_command", lambda agent, request, **kwargs: calls.append((agent, request, kwargs)) or envelope({"site": None, "result": {"url": request["url"], "title": "Page", "frames": []}}))
    args = cli.build_parser().parse_args(["web", "read", "https://example.test/page", "--agent", "researcher"])

    result = args.handler(args, None)

    assert calls == [("researcher", {"command": "read", "url": "https://example.test/page"}, {"start": True, "timeout": 25.0})]
    assert result == {"route": "browser", "reason": "default_read", "site": None, "result": {"url": "https://example.test/page", "title": "Page", "frames": []}}


@pytest.mark.parametrize(("url", "args_api"), [("https://example.test/data.json", True), ("https://example.test/v1/items", True)])
def test_explicit_json_uses_unauthenticated_direct_fetch(monkeypatch: pytest.MonkeyPatch, url: str, args_api: bool) -> None:
    seen: list[tuple] = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        encoding = "utf-8"

        def __init__(self, request_url: str):
            self.url = request_url

        def iter_bytes(self):
            yield b'{"items":[1]}'

    class FakeClient:
        def __init__(self, **kwargs):
            seen.append(("init", kwargs))
            self.cookies = type("Cookies", (), {"clear": lambda _self: None})()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, method, request_url, **kwargs):
            seen.append((method, request_url, kwargs))
            class Context:
                def __enter__(self):
                    return FakeResponse(request_url)
                def __exit__(self, *_args):
                    return None
            return Context()

    monkeypatch.setattr(web.httpx, "Client", FakeClient)
    monkeypatch.setattr(web, "browser_command", lambda *_args, **_kwargs: pytest.fail("JSON route must not use browser"))
    args = cli.build_parser().parse_args(["web", "read", url, "--agent", "researcher", "--api"] if "/v1/" in url else ["web", "read", url, "--agent", "researcher"])

    result = args.handler(args, None)

    assert seen[0][1]["trust_env"] is False
    assert "Authorization" not in seen[1][2]["headers"]
    assert result["route"] == "fetch"
    assert result["data"] == {"items": [1]}


def test_browser_failure_falls_back_to_bounded_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "browser_command", lambda *_args, **_kwargs: {"ok": False, "data": None, "error": {"code": "browser_error", "message": "Browser operation failed"}})
    monkeypatch.setattr(web, "_raw_fetch", lambda url, **kwargs: {"route": "fetch", "url": url, "data": "fallback"})
    args = cli.build_parser().parse_args(["web", "read", "https://example.test", "--agent", "researcher"])

    result = args.handler(args, None)

    assert result == {"route": "fetch", "url": "https://example.test/", "data": "fallback", "reason": "browser_failure"}


def test_explicit_binary_file_is_returned_without_lossy_text_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cookies:
        def clear(self):
            return None

    class Response:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        encoding = "utf-8"
        url = "https://example.test/manual.pdf"

        def iter_bytes(self):
            yield b"\x00\xffpdf"

    class Context:
        def __enter__(self):
            return Response()
        def __exit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            self.cookies = Cookies()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def stream(self, *_args, **_kwargs):
            return Context()

    monkeypatch.setattr(web.httpx, "Client", Client)
    monkeypatch.setattr(web, "browser_command", lambda *_args, **_kwargs: pytest.fail("PDF route must not use browser"))
    args = cli.build_parser().parse_args(["web", "read", "https://example.test/manual.pdf", "--agent", "researcher"])

    result = args.handler(args, None)

    assert result["data"] == {"encoding": "base64", "content": "AP9wZGY="}


def test_binary_content_type_is_encoded_even_without_file_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cookies:
        def clear(self):
            return None

    class Response:
        status_code = 200
        headers = {"content-type": "application/pdf"}
        encoding = "utf-8"
        url = "https://example.test/download"

        def iter_bytes(self):
            yield b"\x00\xffpdf"

    class Context:
        def __enter__(self):
            return Response()
        def __exit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            self.cookies = Cookies()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def stream(self, *_args, **_kwargs):
            return Context()

    monkeypatch.setattr(web.httpx, "Client", Client)

    result = web._raw_fetch("https://example.test/download")

    assert result["data"] == {"encoding": "base64", "content": "AP9wZGY="}


def test_invalid_browser_response_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web, "browser_command", lambda *_args, **_kwargs: {"ok": False, "data": None, "error": {"code": "invalid_request", "message": "Invalid request"}})
    monkeypatch.setattr(web, "_raw_fetch", lambda *_args, **_kwargs: pytest.fail("invalid input must not fetch"))
    args = cli.build_parser().parse_args(["web", "read", "https://example.test", "--agent", "researcher"])

    with pytest.raises(CLIError, match="Invalid request"):
        args.handler(args, None)


def test_search_extracts_and_follows_result_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_browser(agent: str, request: dict[str, Any], **_kwargs):
        calls.append((agent, request))
        if request["command"] == "read" and "duckduckgo.com" in request["url"]:
            return envelope({"site": None, "result": {"url": request["url"], "title": "Search", "frames": [{"items": [
                {"role": "link", "ref": "r1", "accessible_name": "One", "children": []},
                {"role": "link", "ref": "r2", "accessible_name": "Two", "children": []},
            ]}]}})
        if request["command"] == "run":
            ref = request["steps"][0]["ref"]
            url = "https://one.test/" if ref == "r1" else "https://two.test/"
            return {"steps": [{"status": "ok", "result": {"href": url, "text": f"Result {ref}"}}]}
        return envelope({"site": None, "result": {"url": request["url"], "title": request["url"], "frames": []}})

    monkeypatch.setattr(web, "browser_command", fake_browser)
    args = cli.build_parser().parse_args(["web", "search", "example query", "--agent", "researcher", "--limit", "2"])

    result = args.handler(args, None)

    assert [call[1].get("url") for call in calls if call[1]["command"] == "read"] == [
        "https://html.duckduckgo.com/html/?q=example+query",
        "https://one.test/",
        "https://two.test/",
    ]
    assert len(result["results"]) == 2
    assert result["results"][0]["read"]["result"]["title"] == "https://one.test/"


def test_search_fetches_results_only_after_browser_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_browser(agent: str, request: dict[str, Any], **_kwargs):
        del agent
        calls.append(request["command"] + ":" + request.get("url", ""))
        if "duckduckgo.com" in request.get("url", ""):
            return {"ok": False, "data": None, "error": {"code": "browser_timeout", "message": "Browser operation failed"}}
        return envelope({"site": None, "result": {"url": request["url"], "title": "Page", "frames": []}})

    markup = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Fstory%3Fsecret%3D1">Story</a>'
    monkeypatch.setattr(web, "browser_command", fake_browser)
    monkeypatch.setattr(web, "_raw_fetch", lambda url, **_kwargs: {"route": "fetch", "reason": "explicit_raw", "status": 200, "url": "https://html.duckduckgo.com/html/", "content_type": "text/html", "data": markup})
    args = cli.build_parser().parse_args(["web", "search", "story", "--agent", "researcher", "--limit", "1"])

    result = args.handler(args, None)

    assert result["route"] == "fetch"
    assert result["reason"] == "browser_failure"
    assert result["results"][0]["url"] == "https://example.test/story"
    assert "secret" not in json.dumps(result)


def test_search_keeps_result_when_page_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_browser(agent: str, request: dict[str, Any], **_kwargs):
        del agent
        if request["command"] == "read" and "duckduckgo.com" in request["url"]:
            return envelope({"site": None, "result": {"url": request["url"], "title": "Search", "frames": [{"items": [
                {"role": "link", "ref": "r1", "accessible_name": "One", "children": []},
            ]}]}})
        if request["command"] == "run":
            return {"steps": [{"status": "ok", "result": {"href": "https://one.test/", "text": "Result"}}]}
        return {"ok": False, "data": None, "error": {"code": "site_mismatch", "message": "Page read failed", "hint": None}}

    monkeypatch.setattr(web, "browser_command", fake_browser)
    args = cli.build_parser().parse_args(["web", "search", "story", "--agent", "researcher", "--limit", "1"])

    result = args.handler(args, None)

    assert result["status"] == "partial"
    assert len(result["results"]) == 1
    assert result["results"][0]["url"] == "https://one.test/"
    assert result["results"][0]["read"]["ok"] is False


def test_local_web_cli_wraps_result_and_never_constructs_api_client(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "APIClient", lambda **_kwargs: pytest.fail("web command must not construct APIClient"))
    monkeypatch.setattr(web, "browser_command", lambda *_args, **_kwargs: envelope({"url": "https://example.test/", "title": "Page"}))

    assert cli.main(["web", "open", "https://example.test", "--agent", "researcher"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["error"] is None
    assert result["data"]["route"] == "browser"


def test_html_fallback_omits_hidden_and_form_values_but_keeps_later_text() -> None:
    parser = web._TextExtractor()
    parser.feed(
        '<div hidden>hidden secret</div><div aria-hidden="true">aria secret</div>'
        '<div style="display: none">style secret</div><textarea>typed secret</textarea>'
        '<select><option>selected secret</option></select><input value="input secret">'
        '<div>Visible text</div><p>Later visible text</p>'
    )
    result = " ".join("".join(parser.parts).split())

    assert result == "Visible text Later visible text"


def test_invalid_local_arguments_return_json_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["web", "read", "--agent", "researcher"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("subcommand", ["commands", "help"])
def test_browser_discovery_is_local_and_does_not_start_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], subcommand: str
) -> None:
    from src.conductor_cli import browser

    monkeypatch.setattr(cli, "APIClient", lambda **_kwargs: pytest.fail("browser discovery must not construct APIClient"))
    monkeypatch.setattr(browser.commands, "browser_command", lambda *_args, **_kwargs: pytest.fail("browser discovery must not start owner"))
    argv = ["browser", subcommand, "--agent", "researcher"]
    if subcommand == "help":
        argv.insert(2, "read")

    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    if subcommand == "help":
        assert result["data"]["name"] == "read"
    else:
        assert result["data"]["commands"]
