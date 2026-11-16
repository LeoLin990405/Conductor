"""Discover SuperAgent adapters and skip the ones that are not in the tree."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from prototypes.superagent.bridge import ContractBridge
from prototypes.superagent.contract import (
    EventSubscriber,
    FakeCreditGate,
    FakeMemoryRecall,
    FakePostgresStore,
    MemoryHit,
    RuntimeContext,
    ScriptedFakeChatModel,
)
from tests.prototypes.superagent.offline_guard import install, uninstall

# ``reference`` is in-process. ``reference_bridge`` boots a child process that
# reaches the doubles only over the bridge, which is the path ``n_pi`` and
# ``c_native`` take.
ADAPTER_NAMES = ("reference", "reference_bridge", "p_python", "n_pi", "c_native", "production")
_PROTECTED_PROBE_NAMES = frozenset(
    {
        "importlib",
        "json",
        "os",
        "prototypes",
        "src",
        "sys",
        "tests",
    }
)


def adapter_root() -> Path:
    """Directory that holds ``reference``, ``p_python``, ``n_pi``, and ``c_native``."""

    return Path(__file__).resolve().parents[3] / "prototypes" / "superagent"


def adapter_is_present(name: str, *, root: Path | None = None) -> bool:
    """True when this optional adapter has a module file on disk.

    Presence is the file, not a successful import. A package that exists but
    fails while importing is broken and must not be reported as absent.
    ``root`` points discovery at a temporary tree so a probe never creates or
    deletes ``prototypes/superagent/<name>``.
    """

    package = (root or adapter_root()) / name
    return (package / "adapter.py").is_file() or (
        package / "adapter" / "__init__.py"
    ).is_file()


def load_adapter_module(name: str, *, root: Path | None = None):
    """Import an adapter module, or None if that file is absent.

    The default root loads ``prototypes.superagent.<name>.adapter``. An
    explicit ``root`` loads ``<name>.adapter`` from that directory only.
    Import errors from a present adapter propagate. The real adapter package
    is not modified, and a missing optional adapter is the only skip.
    """

    package_root = (root or adapter_root()).resolve()
    if not adapter_is_present(name, root=package_root):
        return None
    if root is None:
        return importlib.import_module(f"prototypes.superagent.{name}.adapter")
    return _import_isolated_adapter(package_root, name)


def _import_isolated_adapter(package_root: Path, name: str):
    """Import ``<name>.adapter`` from a temporary directory.

    The probe name is a top-level module for the duration of the import.
    It is removed afterwards so it cannot shadow a later import. Saved
    modules under that same name are put back. Nothing under the real
    SuperAgent tree is created or deleted.
    """

    if name in _PROTECTED_PROBE_NAMES or "." in name or not name.isidentifier():
        raise ValueError(f"refusing to probe over {name}")
    inserted = str(package_root)
    saved = {
        key: sys.modules.pop(key)
        for key in list(sys.modules)
        if key == name or key.startswith(f"{name}.")
    }
    sys.path.insert(0, inserted)
    importlib.invalidate_caches()
    try:
        return importlib.import_module(f"{name}.adapter")
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(f"{name}."):
                sys.modules.pop(key, None)
        sys.modules.update(saved)
        try:
            sys.path.remove(inserted)
        except ValueError:
            pass
        importlib.invalidate_caches()


@pytest.fixture(scope="session")
def superagent_offline_bootstrap(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Directory whose ``sitecustomize`` keeps Python children offline."""

    path = tmp_path_factory.mktemp("superagent-offline")
    (path / "sitecustomize.py").write_text(
        "from tests.prototypes.superagent.offline_guard import install\n"
        "install()\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _offline_lifecycle(
    superagent_offline_bootstrap: Path, monkeypatch: pytest.MonkeyPatch
):
    """Block network and real databases for this test and its children.

    Adapter import, construction, ``start``, and ``turn`` all run after this
    fixture. Cold-start and any other Python child inherit the bootstrap
    directory on ``PYTHONPATH``, so ``site`` imports the guard before user code.
    """

    install()
    root = str(Path(__file__).resolve().parents[3])
    entries = [str(superagent_offline_bootstrap), root]
    current = os.environ.get("PYTHONPATH", "")
    if current:
        entries.append(current)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(entries))
    monkeypatch.setenv("SUPERAGENT_OFFLINE", "1")
    try:
        yield
    finally:
        uninstall()


@pytest.fixture(params=ADAPTER_NAMES)
def adapter_name(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def adapter_module(adapter_name: str, _offline_lifecycle: None):
    module = load_adapter_module(adapter_name)
    if module is None:
        pytest.skip(f"prototypes.superagent.{adapter_name}.adapter is not installed")
    # An adapter whose runtime is not set up on this machine (for example the
    # Node prototype before `npm ci`) says why, and its slot is skipped rather
    # than failed. It never hides a check: an available adapter runs them all.
    reason_fn = getattr(module, "unavailable_reason", None)
    reason = reason_fn() if callable(reason_fn) else None
    if reason:
        pytest.skip(f"prototypes.superagent.{adapter_name}: {reason}")
    if not hasattr(module, "make_adapter"):
        pytest.fail(
            f"prototypes.superagent.{adapter_name}.adapter has no make_adapter()"
        )
    return module


@pytest.fixture
def runtime_root():
    """Short per-test directory for bridge sockets and adapter scratch dirs.

    Unix socket paths are capped near 104 bytes on macOS, so this is not
    ``tmp_path``.
    """

    path = Path(tempfile.mkdtemp(prefix="superagent-")).resolve()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class AdapterFactory:
    """Build an adapter the way every contract test does.

    Each call gets its own empty ``scratch_dir`` and its own bridge serving
    exactly the doubles passed in. When the module declares
    ``runtime_argv(runtime)``, those commands are registered with ``gate``
    (if one is given) before ``make_adapter`` runs. Every adapter and bridge
    is closed at teardown; ``close`` must be idempotent.
    """

    def __init__(self, module, root: Path) -> None:
        self.module = module
        self.root = root
        self.runtimes: list[dict] = []
        self._bridges: list[ContractBridge] = []
        self._adapters: list[object] = []

    def __call__(self, *, model, store, credit_gate, memory, subscriber, gate=None):
        index = len(self.runtimes) + 1
        home = self.root / f"a{index}"
        scratch = home / "scratch"
        scratch.mkdir(parents=True)
        bridge = ContractBridge(
            str(home / "bridge.sock"),
            model=model,
            store=store,
            credit_gate=credit_gate,
            memory=memory,
            subscriber=subscriber,
        ).start()
        self._bridges.append(bridge)
        runtime = RuntimeContext(scratch_dir=scratch, bridge_path=bridge.path)
        declare = getattr(self.module, "runtime_argv", None)
        argv = [tuple(command) for command in declare(runtime)] if declare else []
        if gate is not None:
            for command in argv:
                gate.authorize(command)
        self.runtimes.append({"runtime": runtime, "argv": argv, "bridge": bridge})
        adapter = self.module.make_adapter(
            model=model,
            store=store,
            credit_gate=credit_gate,
            memory=memory,
            subscriber=subscriber,
            runtime=runtime,
        )
        self._adapters.append(adapter)
        self.runtimes[-1]["adapter"] = adapter
        return adapter

    def close_bridge(self, adapter) -> None:
        """Close the bridge built for ``adapter`` (after the adapter is closed)."""

        for entry in self.runtimes:
            if entry.get("adapter") is adapter:
                entry["bridge"].close()

    def teardown(self) -> None:
        for adapter in reversed(self._adapters):
            result = adapter.close()
            if inspect.isawaitable(result):
                asyncio.run(_await(result))
        for bridge in self._bridges:
            bridge.close()


async def _await(awaitable) -> None:
    await awaitable


async def close_adapter(adapter) -> None:
    """Close an adapter whose ``close`` is a plain method or a coroutine."""

    result = adapter.close()
    if inspect.isawaitable(result):
        await result


@pytest.fixture
def make_adapter(adapter_module, runtime_root: Path):
    factory = AdapterFactory(adapter_module, runtime_root)
    try:
        yield factory
    finally:
        factory.teardown()


def make_doubles(
    script: tuple | list | None = ("ok",),
    *,
    allowed: bool = True,
    allowed_checks: int | None = None,
    hits: list[MemoryHit] | None = None,
    trace: list[str] | None = None,
    store: FakePostgresStore | None = None,
    subscriber: EventSubscriber | None = None,
) -> dict:
    """Keyword arguments for ``make_adapter``, sharing one trace list when given."""

    subscriber = subscriber or EventSubscriber()
    model = ScriptedFakeChatModel(script=script, trace=trace, subscriber=subscriber)
    return {
        "model": model,
        "store": store or FakePostgresStore(),
        "credit_gate": FakeCreditGate(
            allowed=allowed, allowed_checks=allowed_checks, trace=trace
        ),
        "memory": FakeMemoryRecall(hits=hits, trace=trace),
        "subscriber": subscriber,
    }
