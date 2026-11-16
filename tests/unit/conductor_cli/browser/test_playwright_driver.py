from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.conductor_cli.browser.playwright_driver import PlaywrightDriver
from src.conductor_cli.browser.storage import StorageState


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode or 0
        return self.returncode


class FakeLocator:
    def __init__(self, handle: "FakeElement") -> None:
        self.handle = handle
        self.role_call: tuple[str, dict[str, Any]] | None = None

    async def count(self) -> int:
        return 1

    async def element_handle(self):
        return self.handle


class FakeElement:
    def __init__(self, frame: "FakeFrame", node_id: str, summary: dict[str, Any]) -> None:
        self.frame = frame
        self.node_id = node_id
        self.summary = summary
        self.disposed = False
        self.clicked = 0

    async def evaluate(self, script: str, *_args):
        if "isConnected" in script:
            return True
        if "node_id" in script:
            return {
                "node_id": self.node_id,
                "parent_node_id": self.summary.get("parent_node_id"),
                "depth": self.summary.get("depth", 0),
                "summary": {key: value for key, value in self.summary.items() if key not in {"parent_node_id", "depth"}},
            }
        return True

    async def owner_frame(self):
        return self.frame

    async def click(self, **_kwargs):
        self.clicked += 1

    async def dispose(self):
        self.disposed = True


class FakeProperty:
    def __init__(self, element: FakeElement | None) -> None:
        self.element = element
        self.disposed = False

    def as_element(self):
        return self.element

    async def dispose(self):
        self.disposed = True


class FakeElementsHandle:
    def __init__(self, elements: list[FakeElement], *, include_non_element: bool = False) -> None:
        self.properties = {str(index): FakeProperty(item) for index, item in enumerate(elements)}
        self.unused_property: FakeProperty | None = None
        if include_non_element:
            self.unused_property = FakeProperty(None)
            self.properties[str(len(elements))] = self.unused_property
        self.disposed = False

    async def get_properties(self):
        return self.properties

    async def dispose(self):
        self.disposed = True


class FakeFrame:
    def __init__(self, url: str, token: str, elements: list[FakeElement] | None = None) -> None:
        self.url = url
        self.token = token
        self.elements = elements or []
        self.locator_calls: list[tuple[str, dict[str, Any]]] = []
        self.last_elements_handle: FakeElementsHandle | None = None

    async def evaluate(self, script: str, *_args):
        if "__conductor_agent_browser_doc" in script:
            return self.token
        return None

    async def evaluate_handle(self, _script: str):
        self.last_elements_handle = FakeElementsHandle(self.elements, include_non_element=True)
        return self.last_elements_handle

    def get_by_role(self, role: str, **kwargs):
        self.locator_calls.append((role, kwargs))
        return FakeLocator(self.elements[0])


class FakePage:
    def __init__(self, frames: list[FakeFrame]) -> None:
        self.frames = frames
        self.main_frame = frames[0]
        self.url = frames[0].url

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "A safe title"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.handlers: dict[str, Any] = {}

    def on(self, name: str, callback) -> None:
        self.handlers[name] = callback

    async def close(self) -> None:
        pass


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]
        self.endpoints: list[str] = []

    async def close(self) -> None:
        pass


class FakeChromium:
    def __init__(self, executable_path: Path, browser: FakeBrowser, *, fail_connect: bool = False) -> None:
        self.executable_path = str(executable_path)
        self.browser = browser
        self.fail_connect = fail_connect

    async def connect_over_cdp(self, endpoint: str, **_kwargs):
        self.browser.endpoints.append(endpoint)
        if self.fail_connect:
            raise RuntimeError("fake CDP failure")
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self):
        return self.playwright


def _driver_harness(tmp_path: Path, *, fail_connect: bool = False):
    profile = tmp_path / "agent-profile"
    profile.mkdir(mode=0o700)
    executable = tmp_path / "fake-chromium"
    executable.write_text("fake binary")
    main_frame = FakeFrame("https://top.example.test/", "doc-top")
    child_frame = FakeFrame("https://pay.example.net/", "doc-child")
    main_element = FakeElement(main_frame, "top-node", {"role": "button", "accessible_name": "Continue", "text": "Continue", "tag": "button"})
    child_element = FakeElement(child_frame, "child-node", {"role": "button", "accessible_name": "Pay", "text": "Pay", "tag": "button"})
    main_frame.elements = [main_element]
    child_frame.elements = [child_element]
    page = FakePage([main_frame, child_frame])
    context = FakeContext(page)
    browser = FakeBrowser(context)
    playwright = FakePlaywright(FakeChromium(executable, browser, fail_connect=fail_connect))
    process = FakeProcess()
    captured: dict[str, Any] = {}

    def popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        active_port = profile / "chromium" / "DevToolsActivePort"
        active_port.parent.mkdir(mode=0o700, exist_ok=True)
        active_port.write_text("39221\n/devtools/browser/managed-id\n", encoding="ascii")
        active_port.chmod(0o600)
        return process

    driver = PlaywrightDriver(
        playwright_factory=lambda: FakeStarter(playwright),
        popen_factory=popen,
        startup_timeout=0.1,
    )
    return driver, profile, playwright, browser, process, captured, main_element, child_element, page


@pytest.mark.asyncio
async def test_start_connects_only_to_managed_loopback_endpoint_and_close_cleans_child(tmp_path: Path) -> None:
    driver, profile, playwright, browser, process, captured, *_ = _driver_harness(tmp_path)

    await driver.start(profile)
    assert browser.endpoints == ["ws://127.0.0.1:39221/devtools/browser/managed-id"]
    assert captured["args"][0].endswith("fake-chromium")
    assert f"--user-data-dir={profile / 'chromium'}" in captured["args"]
    assert "--remote-debugging-address=127.0.0.1" in captured["args"]
    assert "--remote-debugging-port=0" in captured["args"]
    assert captured["kwargs"]["env"]["HOME"]
    assert "LANGSMITH_API_KEY" not in captured["kwargs"]["env"]

    await driver.close()

    assert process.terminated
    assert playwright.stopped


@pytest.mark.asyncio
async def test_failed_cdp_connect_terminates_managed_child_and_stops_playwright(tmp_path: Path) -> None:
    driver, profile, playwright, browser, process, *_ = _driver_harness(tmp_path, fail_connect=True)

    with pytest.raises(RuntimeError, match="fake CDP failure"):
        await driver.start(profile)

    assert browser.endpoints == ["ws://127.0.0.1:39221/devtools/browser/managed-id"]
    assert process.terminated
    assert playwright.stopped


@pytest.mark.asyncio
async def test_snapshot_inspects_cross_origin_frames_and_refs_resolve_in_their_frame(tmp_path: Path) -> None:
    driver, _profile, _playwright, _browser, _process, _captured, main_element, child_element, _page = _driver_harness(tmp_path)
    # Inject a persistent context/page to exercise frame APIs without launching a browser.
    main_frame, cross_frame = main_element.frame, child_element.frame
    same_frame = FakeFrame("https://top.example.test/checkout", "doc-same-origin-child")
    same_element = FakeElement(same_frame, "same-node", {"role": "heading", "accessible_name": "Checkout", "text": "Checkout", "tag": "h1"})
    same_frame.elements = [same_element]
    nested = FakeElement(main_frame, "nested-node", {"role": "link", "accessible_name": "Details", "text": "Details", "tag": "a", "parent_node_id": "top-node", "depth": 1})
    main_frame.elements.append(nested)
    page = FakePage([main_frame, same_frame, cross_frame])
    driver._context = FakeContext(page)
    driver._page = page

    snapshot = await driver.snapshot()

    assert [row["origin"] for row in snapshot["frames"]] == [
        "https://top.example.test",
        "https://top.example.test",
        "https://pay.example.net",
    ]
    assert snapshot["frames"][0]["items"][0]["children"][0]["text"] == "Details"
    top_ref = snapshot["frames"][0]["items"][0]["ref"]
    same_origin_ref = snapshot["frames"][1]["items"][0]["ref"]
    child_ref = snapshot["frames"][2]["items"][0]["ref"]
    assert top_ref != child_ref
    assert same_origin_ref != top_ref
    for frame in (main_frame, same_frame, cross_frame):
        assert frame.last_elements_handle is not None
        assert frame.last_elements_handle.disposed
        assert frame.last_elements_handle.unused_property is not None
        assert frame.last_elements_handle.unused_property.disposed
        assert all(not prop.disposed for key, prop in frame.last_elements_handle.properties.items() if prop.element is not None)
    await driver.click(child_ref)
    assert child_element.clicked == 1
    assert main_element.clicked == 0
    assert await driver._refs.resolve(top_ref) is main_element
    await driver.close()
    assert all(
        prop.disposed
        for frame in (main_frame, same_frame, cross_frame)
        for prop in frame.last_elements_handle.properties.values()
        if prop.element is not None
    )


@pytest.mark.asyncio
async def test_storage_import_applies_cookies_before_origin_scoped_local_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple] = []

    class Session:
        async def send(self, command: str, params=None):
            events.append(("cdp", command, params))

        async def detach(self):
            events.append(("detach",))

    class ImportPage:
        async def route(self, pattern: str, handler):
            events.append(("route", pattern, handler))

        async def add_init_script(self, script: str):
            events.append(("init_script", script))

        async def goto(self, origin: str, **kwargs):
            events.append(("goto", origin, kwargs))

        async def close(self):
            events.append(("page_close",))

    class ImportContext:
        async def add_cookies(self, cookies):
            events.append(("cookies", cookies))

        async def new_page(self):
            events.append(("new_page",))
            return ImportPage()

        async def new_cdp_session(self, page):
            events.append(("new_cdp_session", page))
            return Session()

    secret = "private-local-storage-value"
    state = StorageState(
        cookies=[{"name": "session", "value": "private-cookie-value", "domain": "example.test", "path": "/"}],
        local_storage={"https://example.test": {"session": secret}},
        domains=["example.test"],
    )
    monkeypatch.setattr("src.conductor_cli.browser.playwright_driver.read_storage_state", lambda path, domains: state)
    driver = PlaywrightDriver()
    driver._context = ImportContext()

    result = await driver.import_storage_state(Path("/worktree/tmp/selected-state.json"), ["example.test"])

    labels = [event[0] for event in events]
    assert labels.index("cookies") < labels.index("new_page") < labels.index("init_script") < labels.index("goto")
    assert labels.index("cdp") < labels.index("goto") < labels.index("detach") < labels.index("page_close")
    init_script = next(event[1] for event in events if event[0] == "init_script")
    assert "https://example.test" in init_script
    assert secret in init_script
    assert result == {"status": "imported", "cookies": 1, "local_storage_items": 1, "domains": ["example.test"]}
    assert secret not in repr(result)
    assert "private-cookie-value" not in repr(result)
