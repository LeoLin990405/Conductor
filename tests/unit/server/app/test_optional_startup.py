"""Optional startup workers: a missing module is one info line, not a warning."""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

# Importing src.server.app executes setup and builds the FastAPI app.
_HELPER_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "server"
    / "app"
    / "optional_startup.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "optional_startup_under_test", _HELPER_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
optional_startup = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = optional_startup
_SPEC.loader.exec_module(optional_startup)

pytestmark = pytest.mark.asyncio

_LOGGER = optional_startup.__name__
_MISSING = "optional_startup_missing_dependency"
_INSTALLED = "optional_startup_fake_worker"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == _LOGGER]


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType(_INSTALLED)
    monkeypatch.setitem(sys.modules, _INSTALLED, module)
    return module


@pytest.mark.parametrize("label", ["Weixin bot", "WeChat subscription worker"])
async def test_import_error_is_one_info(
    label: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await optional_startup.start_optional(
            _MISSING,
            "start_worker",
            label=label,
        )

    assert result is None
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].exc_info is None
    assert records[0].getMessage() == (
        f"{label} was skipped because the optional dependency is not installed"
    )
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("async_start", [False, True], ids=["sync", "async"])
async def test_start_runtime_error_is_one_warning(
    async_start: bool,
    installed: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if async_start:

        async def start_configured_manager() -> None:
            raise RuntimeError("boom")

    else:

        def start_configured_manager() -> None:
            raise RuntimeError("boom")

    installed.start_configured_manager = start_configured_manager
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await optional_startup.start_optional(
            _INSTALLED,
            "start_configured_manager",
            label="Weixin bot",
        )

    assert result is None
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert records[0].getMessage() == "Weixin bot did not start: RuntimeError"
    assert not any("not installed" in record.getMessage() for record in records)


async def test_start_importerror_after_import_is_a_warning(
    installed: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def start_worker() -> None:
        raise ImportError("optional extra missing at call time")

    installed.start_worker = start_worker
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await optional_startup.start_optional(
            _INSTALLED,
            "start_worker",
            label="WeChat subscription worker",
        )

    assert result is None
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage() == (
        "WeChat subscription worker did not start: ImportError"
    )


@pytest.mark.parametrize("async_start", [False, True], ids=["sync", "async"])
async def test_start_success_returns_module(
    async_start: bool,
    installed: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    if async_start:

        async def start_worker() -> None:
            calls.append("start")

    else:

        def start_worker() -> None:
            calls.append("start")

    installed.start_worker = start_worker
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await optional_startup.start_optional(
            _INSTALLED,
            "start_worker",
            label="WeChat subscription worker",
        )

    assert result is installed
    assert calls == ["start"]
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]


async def test_stop_none_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        await optional_startup.stop_optional(
            None,
            "stop_worker",
            label="WeChat subscription worker",
        )

    assert _records(caplog) == []
    assert caplog.records == []


async def test_stop_failure_is_one_warning(
    installed: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def stop_worker() -> None:
        raise RuntimeError("boom")

    installed.stop_worker = stop_worker
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        await optional_startup.stop_optional(
            installed,
            "stop_worker",
            label="WeChat subscription worker",
        )

    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
    assert records[0].getMessage() == (
        "WeChat subscription worker shutdown failed: RuntimeError"
    )


async def test_stop_async_success_is_awaited_once(
    installed: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    async def shutdown_managers() -> None:
        calls.append("stop")

    installed.shutdown_managers = shutdown_managers
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        await optional_startup.stop_optional(
            installed,
            "shutdown_managers",
            label="Weixin bot",
        )

    assert calls == ["stop"]
    assert not [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
