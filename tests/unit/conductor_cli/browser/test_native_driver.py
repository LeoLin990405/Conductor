from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from src.conductor_cli.browser.playwright_driver import PlaywrightDriver


class FakeRequest:
    method = "GET"
    resource_type = "document"


class FakeResponse:
    request = FakeRequest()
    url = "https://example.test/path?token=private"
    status = 200

    @property
    def headers(self):
        raise AssertionError("capture must not inspect response headers")

    async def body(self):
        raise AssertionError("capture must not inspect response bodies")


class FakePage:
    def __init__(self, *, response_count: int = 1, failure: BaseException | None = None, evaluation: Any = None) -> None:
        self.url = "https://example.test/"
        self.handlers: dict[str, list[Any]] = {}
        self.removed: list[tuple[str, Any]] = []
        self.response_count = response_count
        self.failure = failure
        self.evaluation = evaluation or {"result_kind": "string", "result_size": 5, "value": "private-page-data"}
        self.evaluated_script = ""
        self.evaluated_args: tuple[Any, ...] = ()

    def is_closed(self) -> bool:
        return False

    def on(self, event: str, callback: Any) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        self.removed.append((event, callback))
        self.handlers[event].remove(callback)

    async def goto(self, _url: str, **_kwargs) -> None:
        if self.failure is not None:
            raise self.failure
        for _ in range(self.response_count):
            for callback in tuple(self.handlers.get("response", [])):
                callback(FakeResponse())

    async def evaluate(self, script: str, *_args):
        self.evaluated_script = script
        self.evaluated_args = _args
        return self.evaluation


def _driver(page: FakePage) -> PlaywrightDriver:
    driver = PlaywrightDriver()
    driver._context = SimpleNamespace(pages=[page])
    driver._page = page
    return driver


@pytest.mark.asyncio
async def test_network_capture_removes_listener_and_returns_metadata_only() -> None:
    page = FakePage()
    driver = _driver(page)

    rows = await driver.capture_network("https://example.test/start?credential=private")

    assert rows == [{"method": "GET", "origin": "https://example.test", "resource_type": "document", "status": 200}]
    assert len(page.removed) == 1
    assert page.handlers["response"] == []
    assert "token=private" not in repr(rows)
    assert not hasattr(rows[0], "headers")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("navigation failed"), asyncio.CancelledError()])
async def test_network_capture_removes_listener_after_failure_or_cancellation(failure: BaseException) -> None:
    page = FakePage(failure=failure)
    driver = _driver(page)

    with pytest.raises(type(failure)):
        await driver.capture_network("https://example.test/start")

    assert len(page.removed) == 1
    assert page.handlers["response"] == []


@pytest.mark.asyncio
async def test_network_capture_keeps_one_sentinel_row_to_report_truncation() -> None:
    page = FakePage(response_count=60)
    driver = _driver(page)

    rows = await driver.capture_network("https://example.test/start")

    assert len(rows) == 51
    assert len(page.removed) == 1


@pytest.mark.asyncio
async def test_evaluate_page_returns_validated_metadata_and_never_the_value() -> None:
    page = FakePage(evaluation={"result_kind": "string", "result_size": "bad", "value": "private-page-data"})
    driver = _driver(page)

    result = await driver.evaluate_page("document.cookie")

    assert result == {"result_kind": "string", "result_size": 0}
    assert "private-page-data" not in repr(result)
    assert page.evaluated_args == ("document.cookie",)
    assert "return {result_kind: kind, result_size: Math.min(size, 1000000)}" in page.evaluated_script
