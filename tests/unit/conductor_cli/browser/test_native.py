from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from pathlib import Path

import pytest

from src.conductor_cli.browser.envelope import failure, success
from src.conductor_cli.browser.service import BrowserService


class _FixtureSemantics(HTMLParser):
    """Turn local HTML fixture markup into the same safe projection as the driver."""

    TAG_ROLES = {"h1": "heading", "h2": "heading", "h3": "heading", "a": "link", "li": "listitem", "article": "article"}

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._skip = 0
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "textarea", "select", "option"}:
            self._skip += 1
            return
        if self._skip == 0 and tag in self.TAG_ROLES:
            self._current = {"role": self.TAG_ROLES[tag], "text": "", "accessible_name": "", "tag": tag}

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "textarea", "select", "option"} and self._skip:
            self._skip -= 1
        if self._current and tag == self._current["tag"]:
            self._current["text"] = self._current["text"].strip()
            self._current["accessible_name"] = self._current["text"]
            self.rows.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and self._current is not None:
            self._current["text"] += data


class FixtureDriver:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.url = ""
        self.title = ""
        self.fixture_by_host = {
            "docs.example.test": "<h1>Setup guide</h1><a>Install</a><script>secret-script</script><input value='secret-form'>",
            "catalog.example.test": "<h1>Catalog</h1><ul><li>Blue mug</li><li>Red cup</li></ul>",
            "redirect.example.test": "<h1>Outside page</h1>",
        }
        self.redirects: dict[str, str] = {}
        self.slow_goto = False
        self.eval_metadata: object = {"result_kind": "string", "result_size": 14}
        self.network_rows = 1

    async def goto(self, url: str):
        self.calls.append(("goto", url))
        if self.slow_goto:
            await asyncio.sleep(0.05)
        self.url = self.redirects.get(url, url).split("?", 1)[0].split("#", 1)[0]
        self.title = "Fixture title"
        return {"url": self.url, "title": self.title}

    async def snapshot(self):
        self.calls.append(("snapshot",))
        parsed = _FixtureSemantics(self.fixture_by_host[self.url.split("/", 3)[2].split(":")[0]])
        return {"url": self.url, "title": self.title, "frames": [{"status": "ok", "items": parsed.rows}]}

    async def evaluate_page(self, script: str):
        self.calls.append(("evaluate", script))
        return self.eval_metadata

    async def capture_network(self, url: str):
        self.calls.append(("network", url))
        return [
            {"method": "GET", "origin": "https://docs.example.test/path?token=hidden", "resource_type": "document", "status": 200}
            for _ in range(self.network_rows)
        ]


@pytest.mark.asyncio
async def test_registry_and_help_expose_json_schemas_without_starting_driver(tmp_path: Path) -> None:
    driver = FixtureDriver()
    service = BrowserService(driver, tmp_path / "profile")

    listed = await service.handle({"command": "commands"})
    help_result = await service.handle({"command": "help", "name": "eval"})

    assert listed["ok"] is True
    assert {row["name"] for row in listed["data"]["commands"]} == {"commands", "help", "site", "read", "eval", "network"}
    assert {row["name"] for row in listed["data"]["sites"]} == {"docs", "catalog"}
    assert help_result["data"]["schema"]["properties"]["enabled"] == {"const": True}
    assert driver.calls == []


@pytest.mark.asyncio
async def test_read_uses_safe_semantic_fixture_for_matching_adapter(tmp_path: Path) -> None:
    driver = FixtureDriver()
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle({"command": "read", "url": "https://docs.example.test/guide?session=hidden"})

    assert result["ok"] is True
    assert result["data"]["site"] == "docs"
    assert result["data"]["result"]["headings"] == ["Setup guide"]
    assert result["data"]["result"]["links"] == ["Install"]
    assert "secret-script" not in repr(result)
    assert "secret-form" not in repr(result)
    assert "session=hidden" not in repr(result)


@pytest.mark.asyncio
async def test_site_action_checks_redirected_actual_url(tmp_path: Path) -> None:
    driver = FixtureDriver()
    driver.redirects["https://docs.example.test/guide"] = "https://redirect.example.test/away"
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle({"command": "site", "site": "docs", "action": "read", "url": "https://docs.example.test/guide"})

    assert result["ok"] is False
    assert result["error"]["code"] == "site_mismatch"
    assert [call[0] for call in driver.calls] == ["goto", "snapshot"]


@pytest.mark.asyncio
async def test_automatic_read_selects_adapter_after_redirect(tmp_path: Path) -> None:
    driver = FixtureDriver()
    driver.redirects["https://unknown.example.test/redirect"] = "https://docs.example.test/guide"
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle({"command": "read", "url": "https://unknown.example.test/redirect"})

    assert result["ok"] is True
    assert result["data"]["site"] == "docs"


@pytest.mark.asyncio
async def test_catalog_adapter_has_its_own_actions_and_visible_item_projection(tmp_path: Path) -> None:
    service = BrowserService(FixtureDriver(), tmp_path / "profile")

    result = await service.handle({"command": "site", "site": "catalog", "action": "items", "url": "https://catalog.example.test/"})

    assert result["ok"] is True
    assert result["data"]["items"] == ["Blue mug", "Red cup"]


@pytest.mark.asyncio
async def test_invalid_site_action_is_rejected_before_navigation(tmp_path: Path) -> None:
    driver = FixtureDriver()
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle({"command": "site", "site": "catalog", "action": "delete", "url": "https://catalog.example.test/"})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_site_action"
    assert driver.calls == []


@pytest.mark.asyncio
async def test_eval_and_network_are_per_call_opt_in_and_redact_returned_data(tmp_path: Path) -> None:
    driver = FixtureDriver()
    service = BrowserService(driver, tmp_path / "profile")

    denied_eval = await service.handle({"command": "eval", "script": "document.cookie", "enabled": False})
    eval_result = await service.handle({"command": "eval", "script": "document.cookie", "enabled": True})
    denied_network = await service.handle({"command": "network", "url": "https://docs.example.test/", "enabled": False})
    network_result = await service.handle({"command": "network", "url": "https://docs.example.test/?token=secret", "enabled": True})

    assert denied_eval["error"]["code"] == "opt_in_required"
    assert denied_network["error"]["code"] == "opt_in_required"
    assert eval_result["data"] == {"executed": True, "result_kind": "string", "result_size": 14}
    assert network_result["data"]["captured"] == [{"method": "GET", "origin": "https://docs.example.test", "resource_type": "document", "status": 200}]
    assert "document.cookie" not in repr(eval_result)
    assert "token=secret" not in repr(network_result)
    assert "token=hidden" not in repr(network_result)
    assert len([call for call in driver.calls if call[0] == "evaluate"]) == 1
    assert len([call for call in driver.calls if call[0] == "network"]) == 1


@pytest.mark.asyncio
async def test_eval_metadata_is_validated_at_service_boundary(tmp_path: Path) -> None:
    driver = FixtureDriver()
    driver.eval_metadata = {"result_kind": "string", "result_size": 2**31, "value": "never-return-this"}
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle({"command": "eval", "script": "'private'", "enabled": True})

    assert result["data"] == {"executed": True, "result_kind": "string", "result_size": 0}
    assert "never-return-this" not in repr(result)


@pytest.mark.asyncio
async def test_network_capture_reports_truncation_after_safe_metadata_cap(tmp_path: Path) -> None:
    driver = FixtureDriver()
    driver.network_rows = 51
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle({"command": "network", "url": "https://docs.example.test/", "enabled": True})

    assert len(result["data"]["captured"]) == 50
    assert result["data"]["truncated"] is True


@pytest.mark.asyncio
async def test_read_timeout_is_bounded_and_returned_as_safe_envelope(tmp_path: Path) -> None:
    driver = FixtureDriver()
    driver.slow_goto = True
    service = BrowserService(driver, tmp_path / "profile", step_timeout=0.001)

    result = await service.handle({"command": "read", "url": "https://docs.example.test/guide"})

    assert result["ok"] is False
    assert result["error"]["message"] == "Browser operation failed or exceeded its time limit"


def test_envelope_has_fixed_keys_and_null_hint() -> None:
    assert success({"value": 1}) == {"ok": True, "data": {"value": 1}, "error": None}
    assert failure("bad", "safe") == {"ok": False, "data": None, "error": {"code": "bad", "message": "safe", "hint": None}}
