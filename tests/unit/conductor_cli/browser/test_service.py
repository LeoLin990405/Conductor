from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.conductor_cli.browser.service import (
    BrowserCommandError,
    BrowserService,
    parse_flow,
    validate_steps,
)


class FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None

    async def _call(self, *args):
        self.calls.append(args)
        if self.fail_with:
            raise self.fail_with
        return {"ok": True}

    async def start(self, profile: Path):
        return await self._call("start", profile)

    async def goto(self, url: str):
        return await self._call("goto", url)

    async def snapshot(self):
        return await self._call("snapshot")

    async def click(self, ref: str):
        return await self._call("click", ref)

    async def click_target(self, role: str, name: str, frame: str | None = None):
        return await self._call("click_target", role, name, frame)

    async def fill(self, ref: str, text: str):
        return await self._call("fill", ref, text)

    async def fill_target(self, role: str, name: str, value: str, frame: str | None = None):
        return await self._call("fill_target", role, name, value, frame)

    async def wait(self, seconds: float):
        return await self._call("wait", seconds)

    async def extract(self, ref: str, fields: list[str]):
        return await self._call("extract", ref, fields)

    async def extract_target(self, role: str, name: str, fields: list[str], frame: str | None = None):
        return await self._call("extract_target", role, name, fields, frame)

    async def import_storage_state(self, path: Path, domains: list[str]):
        return await self._call("import", path, domains)

    async def close(self):
        return await self._call("close")


@pytest.mark.parametrize(
    "steps",
    [
        [{"op": "click", "ref": "d1.frame.node", "unexpected": True}],
        [{"op": "goto", "url": "file:///etc/passwd"}],
        [{"op": "goto", "url": "https://user:password@example.test"}],
        [{"op": "wait", "seconds": float("inf")}],
        [{"op": "fill", "ref": "r1", "text": "x" * 4097}],
        [{"op": "extract", "ref": "r1", "fields": ["text", "value"]}],
        [{"op": "evaluate", "script": "alert(1)"}],
    ],
)
def test_invalid_batch_is_rejected_before_any_driver_call(tmp_path: Path, steps: list[dict]) -> None:
    driver = FakeDriver()
    service = BrowserService(driver, tmp_path / "profile")

    with pytest.raises(BrowserCommandError):
        import asyncio

        asyncio.run(service.handle({"command": "run", "steps": steps}))

    assert driver.calls == []


def test_step_count_is_bounded_before_execution() -> None:
    with pytest.raises(BrowserCommandError, match="between 1 and 30"):
        validate_steps([{"op": "snapshot"}] * 31)


@pytest.mark.parametrize(
    ("flow", "format"),
    [
        ('{"steps":[{"op":"goto","url":"https://example.test"}]}', "json"),
        ("steps:\n  - op: goto\n    url: https://example.test\n", "yaml"),
    ("- op: snapshot\n", "yaml"),
    ("[{op: snapshot}]", "json"),
],
)
def test_flow_parser_accepts_valid_json_and_yaml_and_rejects_invalid_json(flow: str, format: str) -> None:
    if format == "json" and flow.startswith("["):
        with pytest.raises(BrowserCommandError, match="valid JSON or YAML"):
            parse_flow(flow, format=format)
    else:
        assert parse_flow(flow, format=format)


@pytest.mark.asyncio
async def test_run_executes_only_validated_steps_in_order(tmp_path: Path) -> None:
    driver = FakeDriver()
    service = BrowserService(driver, tmp_path / "profile")
    steps = [
        {"op": "goto", "url": "https://example.test/path"},
        {"op": "snapshot"},
        {"op": "click", "target": {"role": "button", "name": "Continue"}},
        {"op": "fill", "target": {"role": "textbox", "name": "Search"}, "text": "safe fake value"},
        {"op": "wait", "seconds": 0},
        {"op": "extract", "target": {"role": "heading", "name": "Results"}, "fields": ["text", "href"]},
    ]

    result = await service.handle({"command": "run", "steps": steps})

    assert [call[0] for call in driver.calls] == ["goto", "snapshot", "click_target", "fill_target", "wait", "extract_target"]
    assert [step["index"] for step in result["steps"]] == list(range(6))
    assert all(step["status"] == "ok" for step in result["steps"])


@pytest.mark.asyncio
async def test_driver_error_is_sanitized_and_stops_later_steps(tmp_path: Path) -> None:
    driver = FakeDriver()
    driver.fail_with = RuntimeError("protocol leaked cookie=super-secret-value")
    service = BrowserService(driver, tmp_path / "profile")

    result = await service.handle(
        {"command": "run", "steps": [{"op": "snapshot"}, {"op": "goto", "url": "https://example.test"}]}
    )

    assert len(driver.calls) == 1
    assert result["steps"][0]["status"] == "error"
    assert "super-secret-value" not in repr(result)
    assert "protocol leaked" not in repr(result)


@pytest.mark.asyncio
async def test_login_import_requires_explicit_path_and_exact_dns_domains(tmp_path: Path) -> None:
    driver = FakeDriver()
    service = BrowserService(driver, tmp_path / "profile")

    with pytest.raises(BrowserCommandError):
        await service.handle({"command": "import-login", "path": str(tmp_path / "state.json"), "domains": ["*.example.test"]})
    assert driver.calls == []

    result = await service.handle(
        {"command": "import-login", "path": str(tmp_path / "selected-state.json"), "domains": ["Example.Test."]}
    )
    assert driver.calls == [("import", tmp_path / "selected-state.json", ["example.test"])]
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_login_domain_normalizes_idna_hostname_before_import(tmp_path: Path) -> None:
    driver = FakeDriver()
    service = BrowserService(driver, tmp_path / "profile")

    await service.handle(
        {"command": "import-login", "path": str(tmp_path / "selected-state.json"), "domains": ["bücher.test"]}
    )

    assert driver.calls == [
        ("import", tmp_path / "selected-state.json", ["xn--bcher-kva.test"])
    ]


@pytest.mark.parametrize("domain", ["example.test/path", "https://example.test", "example.test:443", "*.example.test", "127.0.0.1"])
def test_login_domain_rejects_non_exact_or_non_dns_values(domain: str) -> None:
    with pytest.raises(BrowserCommandError):
        import asyncio

        asyncio.run(
            BrowserService(FakeDriver(), Path("/fake/profile")).handle(
                {"command": "import-login", "path": "/fake/selected.json", "domains": [domain]}
            )
        )


@pytest.mark.asyncio
async def test_request_rejects_unknown_fields_before_driver_call(tmp_path: Path) -> None:
    driver = FakeDriver()
    service = BrowserService(driver, tmp_path / "profile")

    with pytest.raises(BrowserCommandError, match="fields"):
        await service.handle({"command": "snapshot", "include_values": True})
    assert driver.calls == []


@pytest.mark.asyncio
async def test_step_timeout_marks_remaining_steps_skipped(tmp_path: Path) -> None:
    class SlowDriver(FakeDriver):
        async def wait(self, seconds: float):
            self.calls.append(("wait", seconds))
            await asyncio.sleep(0.1)
            return {"ok": True}

    driver = SlowDriver()
    service = BrowserService(driver, tmp_path / "profile", step_timeout=0.01, batch_timeout=1)
    result = await service.handle(
        {"command": "run", "steps": [{"op": "wait", "seconds": 0}, {"op": "snapshot"}]}
    )

    assert [step["status"] for step in result["steps"]] == ["error", "skipped"]
    assert "0.1" not in repr(result)
    assert len(driver.calls) == 1


@pytest.mark.asyncio
async def test_cyclic_or_non_json_driver_result_is_sanitized(tmp_path: Path) -> None:
    class CyclicDriver(FakeDriver):
        async def snapshot(self):
            self.calls.append(("snapshot",))
            cyclic: list[object] = []
            cyclic.append(cyclic)
            return {"data": cyclic}

    driver = CyclicDriver()
    service = BrowserService(driver, tmp_path / "profile")

    with pytest.raises(BrowserCommandError, match="Snapshot failed") as error:
        await service.handle({"command": "snapshot"})
    assert "RecursionError" not in str(error.value)


@pytest.mark.asyncio
async def test_import_driver_failure_does_not_echo_sensitive_exception(tmp_path: Path) -> None:
    driver = FakeDriver()
    driver.fail_with = RuntimeError("state cookie value: very-secret-cookie")
    service = BrowserService(driver, tmp_path / "profile")

    with pytest.raises(BrowserCommandError) as error:
        await service.handle(
            {"command": "import-login", "path": str(tmp_path / "state.json"), "domains": ["example.test"]}
        )
    assert "very-secret-cookie" not in str(error.value)
