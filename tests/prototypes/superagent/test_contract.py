"""Behavior every SuperAgent prototype adapter has to share.

Missing implementations are skipped by ``conftest.py``. The reference fake
is always collected. Cold-start and LOC JSON land in ``SUPERAGENT_RESULT_DIR``
when that variable is set, and in a temporary directory otherwise.
"""

from __future__ import annotations

import builtins
import copy
import fcntl
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from prototypes.superagent.contract import (
    CREDIT_STOP_TEXT,
    FAKE_TOOL_HANDLERS,
    SHELL_OR_FILE_TOOLS,
    FakePostgresStore,
    MemoryHit,
    ScriptedChunk,
    is_shell_or_file_tool,
    scripted_tool_call_text,
    scripted_tool_result,
)
from src.tools.autonomy.tools import AUTONOMY_ROUTER_TOOLS, AUTONOMY_WATCHDOG_TOOLS
from tests.prototypes.superagent.cheaters import (
    CacheHistoryCheater,
    ExtraBuiltinsCheater,
    NativeBuiltinCheater,
    NoToolListCheater,
    OutOfRoleDispatchCheater,
    PreflightOnlyCreditCheater,
    StaleStoreCheater,
    StoreBypassCheater,
    UnionToolsCheater,
    cheat_factory,
)
from tests.prototypes.superagent.conftest import (
    ADAPTER_NAMES,
    adapter_is_present,
    adapter_root,
    close_adapter,
    load_adapter_module,
    make_doubles,
)
from tests.prototypes.superagent.offline_guard import assert_offline

# Dependency installs and build output sit inside an adapter package after
# boot. They are not that adapter's implementation.
_LOC_EXCLUDED_DIRS = frozenset(
    {
        "__pycache__",
        ".next",
        ".turbo",
        ".venv",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "generated",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "third_party",
        "vendor",
        "venv",
    }
)

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_SUFFIXES = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".rs",
    ".go",
}

_COLD_START_SCRIPT = r"""
import asyncio
import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from tests.prototypes.superagent.offline_guard import assert_offline, install

install()
assert_offline()
name, dest = sys.argv[1], sys.argv[2]
t0 = time.perf_counter()
module = importlib.import_module(f"prototypes.superagent.{name}.adapter")
imported = time.perf_counter()
from prototypes.superagent.bridge import ContractBridge
from prototypes.superagent.contract import (
    EventSubscriber,
    FakeCreditGate,
    FakeMemoryRecall,
    FakePostgresStore,
    RuntimeContext,
    ScriptedFakeChatModel,
)

home = Path(tempfile.mkdtemp(prefix="superagent-cold-")).resolve()
(home / "scratch").mkdir()
doubles = dict(
    model=ScriptedFakeChatModel(script=["ok"]),
    store=FakePostgresStore(),
    credit_gate=FakeCreditGate(),
    memory=FakeMemoryRecall(),
    subscriber=EventSubscriber(),
)
bridge = ContractBridge(str(home / "bridge.sock"), **doubles).start()
runtime = RuntimeContext(scratch_dir=home / "scratch", bridge_path=bridge.path)
bridge_ready = time.perf_counter()
adapter = module.make_adapter(**doubles, runtime=runtime)
constructed = time.perf_counter()
asyncio.run(adapter.start("cold-session", role="router", user_id="cold-user"))
done = time.perf_counter()
role = adapter.session("cold-session").role
closed = adapter.close()
if inspect.isawaitable(closed):
    asyncio.run(closed)
bridge.close()
shutil.rmtree(home, ignore_errors=True)
# The harness's bridge setup is not the adapter's cold start.
harness_ms = (bridge_ready - imported) * 1000
Path(dest).write_text(
    json.dumps(
        {
            "adapter": name,
            "schema_version": 1,
            "provider_name": adapter.provider_name,
            "role": role,
            "session_id": "cold-session",
            "import_ms": (imported - t0) * 1000,
            "construct_ms": (constructed - bridge_ready) * 1000,
            "start_ms": (done - constructed) * 1000,
            "cold_start_ms": (done - t0) * 1000 - harness_ms,
            "pid": os.getpid(),
        }
    ),
    encoding="utf-8",
)
"""


def test_reference_discovery_skips_only_absent_adapters(tmp_path: Path) -> None:
    """Broken and missing adapters are probed in a temporary tree.

    The real ``n_pi`` package is left alone. Once that implementation exists,
    deleting it here would make the rest of the suite skip a present adapter.
    """

    real_packages = {
        name: _tree_signature(adapter_root() / name) for name in ADAPTER_NAMES
    }
    assert load_adapter_module("not_a_real_adapter") is None
    assert load_adapter_module("reference") is not None

    probe = tmp_path / "adapters"
    absent = probe / "absent_pkg"
    absent.mkdir(parents=True)
    (absent / "__init__.py").write_text("", encoding="utf-8")
    assert adapter_is_present("absent_pkg", root=probe) is False
    assert load_adapter_module("absent_pkg", root=probe) is None

    present = probe / "present_pkg" / "adapter"
    present.mkdir(parents=True)
    (present / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    loaded = load_adapter_module("present_pkg", root=probe)
    assert loaded is not None
    assert loaded.VALUE == 1

    broken = probe / "broken_pkg"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("", encoding="utf-8")
    (broken / "adapter.py").write_text(
        "from broken_pkg.missing_runtime import start\n",
        encoding="utf-8",
    )
    with pytest.raises(ModuleNotFoundError, match="missing_runtime"):
        load_adapter_module("broken_pkg", root=probe)

    assert {
        name: _tree_signature(adapter_root() / name) for name in ADAPTER_NAMES
    } == real_packages


def test_role_tool_set_matches_autonomy_lists(make_adapter) -> None:
    adapter = make_adapter(**make_doubles())
    router = [tool.name for tool in AUTONOMY_ROUTER_TOOLS]
    watchdog = [tool.name for tool in AUTONOMY_WATCHDOG_TOOLS]
    assert adapter.tool_names("router") == router
    assert adapter.tool_names("supervisor") == router
    assert adapter.tool_names("watchdog") == watchdog
    assert not any(is_shell_or_file_tool(name) for name in [*router, *watchdog])
    with pytest.raises(ValueError):
        adapter.tool_names("worker")


async def check_restart_reads_the_store(make_adapter) -> None:
    """A restarted adapter on a deep copy of the store has the whole session.

    The first adapter is closed before the restart, and the copy shares no
    objects with the original, so neither a long-lived process nor process
    memory can carry state across. A row written into the copy by "another
    process" must reach the restarted model request: history comes from the
    store, not from a cache.
    """

    router = [tool.name for tool in AUTONOMY_ROUTER_TOOLS]
    store = FakePostgresStore()
    first_deps = make_doubles(
        script=["saved"], store=store, hits=[MemoryHit(text="kept-memory")]
    )
    first = make_adapter(**first_deps)
    await first.start("s1", role="router", user_id="u1")
    commits_before_turn = store.commits
    await first.turn("s1", "remember-the-whale")

    row = store.get_session("s1")
    assert row is not None, "restart: no session row in the store"
    assert (row["role"], row["user_id"], row["status"]) == ("router", "u1", "ready"), (
        f"restart: stored session row is {row!r}"
    )
    transcript_rows = store.transcript_rows("s1")
    assert [(r["kind"], r["text"]) for r in transcript_rows] == [
        ("user", "remember-the-whale"),
        ("memory", "kept-memory"),
        ("assistant", "saved"),
        ("done", ""),
    ], f"restart: stored transcript rows are {transcript_rows!r}"
    event_rows = store.event_rows("s1")
    assert [(r["kind"], r["text"]) for r in event_rows] == [
        ("token", "saved"),
        ("done", ""),
    ], f"restart: stored event rows are {event_rows!r}"
    seqs = [r["seq"] for r in transcript_rows]
    assert seqs == sorted(set(seqs)), "restart: transcript seqs are not unique"
    assert row["next_seq"] == max(seqs) + 1, "restart: next_seq did not advance"
    assert store.commits == commits_before_turn + 1, (
        "restart: a turn must commit in exactly one store transaction"
    )
    await close_adapter(first)
    # Nothing the first adapter started can reach the old store after this.
    close_bridge = getattr(make_adapter, "close_bridge", None)
    if close_bridge is not None:
        close_bridge(first)
    original_snapshot = (
        store.get_session("s1"),
        store.transcript_rows("s1"),
        store.event_rows("s1"),
        store.commits,
    )
    copied = copy.deepcopy(store)
    with copied.transaction() as txn:
        current = txn.get_session("s1")
        txn.append_transcript(
            {
                "session_id": "s1",
                "seq": current["next_seq"],
                "kind": "user",
                "text": "written-by-another-process",
            }
        )
        current["next_seq"] += 1
        txn.upsert_session(current)

    second_deps = make_doubles(script=["still-here"], store=copied)
    restarted = make_adapter(**second_deps)
    await restarted.start("s1", role="watchdog", user_id="someone-else")
    session = restarted.session("s1")
    assert (session.role, session.user_id, session.status) == (
        "router",
        "u1",
        "ready",
    ), f"restart: restarted session is {session!r}"
    text = restarted.transcript("s1").text()
    for expected in ("remember-the-whale", "saved", "written-by-another-process"):
        assert expected in text, f"restart: transcript is missing {expected!r}"

    await restarted.turn("s1", "next")
    calls = second_deps["model"].calls
    assert calls, "restart: the restarted adapter never called the model"
    contents = calls[0].texts()
    for expected in ("remember-the-whale", "saved", "written-by-another-process"):
        assert expected in contents, (
            f"restart: model request is missing {expected!r}; history must come "
            "from the store"
        )
    assert calls[0].tools is not None and sorted(calls[0].tools) == sorted(router), (
        "restart: the stored role must pick the offered tools"
    )
    assert [r["text"] for r in copied.event_rows("s1") if r["kind"] == "token"] == [
        "saved",
        "still-here",
    ], "restart: the restarted turn's events are not in the store it was given"
    assert copied.get_session("s1")["status"] == "ready"
    assert (
        store.get_session("s1"),
        store.transcript_rows("s1"),
        store.event_rows("s1"),
        store.commits,
    ) == original_snapshot, "restart: the restarted adapter wrote to the old store"
    assert restarted.tool_names(session.role) == router
    await close_adapter(restarted)


@pytest.mark.asyncio
async def test_restart_survival(make_adapter) -> None:
    await check_restart_reads_the_store(make_adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cheater", [StoreBypassCheater, CacheHistoryCheater, StaleStoreCheater]
)
async def test_restart_check_catches_cheaters(cheater) -> None:
    with pytest.raises(AssertionError, match="^restart: "):
        await check_restart_reads_the_store(cheat_factory(cheater))


async def check_offered_tools(make_adapter) -> None:
    """Every model request carries exactly the role's tool definitions.

    The scripted model records what it was offered. ``tool_names`` is the
    adapter's own claim and is not what this compares.
    """

    router = [tool.name for tool in AUTONOMY_ROUTER_TOOLS]
    watchdog = [tool.name for tool in AUTONOMY_WATCHDOG_TOOLS]
    for role, expected in (
        ("router", router),
        ("supervisor", router),
        ("watchdog", watchdog),
    ):
        deps = make_doubles(
            script=[
                ScriptedChunk(tool_name=expected[0], tool_args={"probe": role}),
                "after",
            ]
        )
        adapter = make_adapter(**deps)
        await adapter.start(f"s-{role}", role=role, user_id="u1")
        await adapter.turn(f"s-{role}", "which tools")
        calls = deps["model"].calls
        assert len(calls) == 2, f"offered tools: expected two rounds, saw {len(calls)}"
        for call in calls:
            assert call.tools is not None, (
                f"offered tools: the {role} request carried no tool list"
            )
            assert len(set(call.tools)) == len(call.tools), (
                f"offered tools: duplicates in {call.tools!r}"
            )
            assert sorted(call.tools) == sorted(expected), (
                f"offered tools: {role} offered {call.tools!r}, expected {expected!r}"
            )
            assert not any(is_shell_or_file_tool(name) for name in call.tools), (
                f"offered tools: shell or file tool offered in {call.tools!r}"
            )
        await close_adapter(adapter)


@pytest.mark.asyncio
async def test_offered_tools_are_exactly_the_role_list(make_adapter) -> None:
    await check_offered_tools(make_adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cheater", [ExtraBuiltinsCheater, UnionToolsCheater, NoToolListCheater]
)
async def test_offered_tools_check_catches_cheaters(cheater) -> None:
    with pytest.raises(AssertionError, match="^offered tools: "):
        await check_offered_tools(cheat_factory(cheater))


def test_shell_or_file_names_cover_cli_builtins() -> None:
    for name in (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "NotebookEdit",
        "WebFetch",
        "WebSearch",
        "Task",
    ):
        assert name in SHELL_OR_FILE_TOOLS
        assert is_shell_or_file_tool(name.upper())
        assert is_shell_or_file_tool(name.lower())
        assert is_shell_or_file_tool(f"mcp__builtin__{name}")
    assert is_shell_or_file_tool("mcp__fs__write_file")
    assert not is_shell_or_file_tool("autonomy_read")
    assert not is_shell_or_file_tool("mcp__conductor__autonomy_route")


@pytest.mark.asyncio
async def test_transcript_records_the_turn(make_adapter) -> None:
    deps = make_doubles(script=["world"], hits=[MemoryHit(text="memo-line")])
    adapter = make_adapter(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    await adapter.turn("s1", "hello")
    transcript = adapter.transcript("s1")
    assert transcript.session_id == "s1"
    assert transcript.kinds() == ["user", "memory", "assistant", "done"]
    assert transcript.entries[0].text == "hello"
    assert transcript.entries[1].text == "memo-line"
    assert transcript.entries[2].text == "world"
    seqs = [entry.seq for entry in transcript.entries]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


@pytest.mark.asyncio
async def test_ordered_streaming(make_adapter) -> None:
    deps = make_doubles(script=["one", "two", "three"])
    adapter = make_adapter(**deps)
    await adapter.start("s1", role="watchdog", user_id="u1")
    before = len(deps["subscriber"].events)
    events = await adapter.turn("s1", "count")
    subscriber = deps["subscriber"]
    assert events == subscriber.events[before:]
    assert subscriber.out_of_order == []
    seqs = [event.seq for event in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    assert [event.text for event in events if event.kind == "token"] == [
        "one",
        "two",
        "three",
    ]
    assert events[-1].kind == "done"
    assert deps["model"].prefix_lengths == [0, 1, 2]


@pytest.mark.asyncio
async def test_credit_gate_stops_before_the_model(make_adapter) -> None:
    trace: list[str] = []
    deps = make_doubles(
        script=["should-not-run"],
        allowed=False,
        trace=trace,
        hits=[MemoryHit(text="not-injected")],
    )
    adapter = make_adapter(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    events = await adapter.turn("s1", "spend")
    assert trace == ["credit"]
    assert deps["model"].calls == []
    assert deps["memory"].calls == []
    assert [event.kind for event in events] == ["stop"]
    assert events[0].text == CREDIT_STOP_TEXT
    assert adapter.session("s1").status == "stopped"
    transcript = adapter.transcript("s1")
    assert transcript.kinds() == ["user", "stop"]
    assert CREDIT_STOP_TEXT in transcript.text()
    assert "should-not-run" not in transcript.text()


async def check_credit_stops_mid_turn(make_adapter) -> None:
    """The gate is asked again before the round that follows a tool call."""

    trace: list[str] = []
    arguments = {"ticket_id": "t-credit"}
    deps = make_doubles(
        script=[
            ScriptedChunk(tool_name="autonomy_route", tool_args=arguments),
            "after-credit-ran-out",
        ],
        allowed_checks=1,
        trace=trace,
    )
    adapter = make_adapter(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    events = await adapter.turn("s1", "spend then stop")
    assert [event.kind for event in events] == ["tool_call", "tool_result", "stop"], (
        f"credit mid-turn: events were {[event.kind for event in events]!r}"
    )
    assert events[-1].text == CREDIT_STOP_TEXT
    assert len(deps["model"].calls) == 1, (
        "credit mid-turn: the model was called again after the gate said stop"
    )
    assert trace == ["credit", "memory", "model", "credit"], (
        f"credit mid-turn: trace was {trace!r}"
    )
    store = deps["store"]
    assert store.get_session("s1")["status"] == "stopped"
    assert [r["kind"] for r in store.transcript_rows("s1")] == [
        "user",
        "memory",
        "tool_call",
        "tool_result",
        "stop",
    ]
    assert [r["text"] for r in store.transcript_rows("s1") if r["kind"] == "tool_result"] == [
        scripted_tool_result("autonomy_route", arguments)
    ]
    assert "after-credit-ran-out" not in adapter.transcript("s1").text()
    assert adapter.session("s1").status == "stopped"
    await close_adapter(adapter)


@pytest.mark.asyncio
async def test_credit_gate_stops_mid_turn(make_adapter) -> None:
    await check_credit_stops_mid_turn(make_adapter)


@pytest.mark.asyncio
async def test_credit_mid_turn_check_catches_cheater() -> None:
    with pytest.raises(AssertionError, match="^credit mid-turn: "):
        await check_credit_stops_mid_turn(cheat_factory(PreflightOnlyCreditCheater))


@pytest.mark.asyncio
async def test_memory_is_injected_before_the_model_call(make_adapter) -> None:
    trace: list[str] = []
    deps = make_doubles(
        script=["ack"],
        trace=trace,
        hits=[MemoryHit(text="injected-memory-token", memory_id="m1")],
    )
    adapter = make_adapter(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    await adapter.turn("s1", "where is it")
    assert trace == ["credit", "memory", "model"]
    assert deps["memory"].calls[0]["query"] == "where is it"
    messages = deps["model"].calls[0].message_texts()
    memory_at = next(
        index
        for index, pieces in enumerate(messages)
        if any("injected-memory-token" in piece for piece in pieces)
    )
    user_at = max(
        index for index, pieces in enumerate(messages) if "where is it" in pieces
    )
    assert memory_at < user_at


@pytest.mark.asyncio
async def test_scripted_role_tools_run_and_continue(make_adapter) -> None:
    roles = (
        ("router", AUTONOMY_ROUTER_TOOLS),
        ("watchdog", AUTONOMY_WATCHDOG_TOOLS),
    )
    for role, tools in roles:
        for tool in tools:
            arguments = {"probe": tool.name, "ticket_id": f"ticket-{tool.name}"}
            call_text = scripted_tool_call_text(tool.name, arguments)
            result = scripted_tool_result(tool.name, arguments)
            session_id = f"s-{role}-{tool.name}"
            deps = make_doubles(
                script=[
                    ScriptedChunk(tool_name=tool.name, tool_args=arguments),
                    f"after-{tool.name}",
                ]
            )
            adapter = make_adapter(**deps)
            await adapter.start(session_id, role=role, user_id="u1")
            events = await adapter.turn(session_id, f"use {tool.name}")
            assert [event.text for event in events if event.kind == "tool_call"] == [
                call_text
            ]
            assert [event.text for event in events if event.kind == "tool_result"] == [
                result
            ]
            assert [event.text for event in events if event.kind == "token"] == [
                f"after-{tool.name}"
            ]
            assert [event.kind for event in events if event.kind == "tool_refused"] == []
            assert len(deps["model"].calls) == 2
            assert [chunk.tool_name for chunk in deps["model"].rounds[0]] == [tool.name]
            assert [chunk.text for chunk in deps["model"].rounds[1]] == [
                f"after-{tool.name}"
            ]
            first = "\n".join(deps["model"].calls[0].texts())
            second = "\n".join(deps["model"].calls[1].texts())
            assert result not in first
            assert call_text in second
            assert result in second
            assert deps["model"].prefix_lengths == [0, 2]
            stored = adapter.transcript(session_id).text()
            assert call_text in stored
            assert result in stored

            restarted_deps = make_doubles(script=["restarted"], store=deps["store"])
            restarted = make_adapter(**restarted_deps)
            await restarted.start(session_id, role="supervisor", user_id="other")
            await restarted.turn(session_id, "next")
            assert restarted.session(session_id).role == role
            assert restarted.session(session_id).user_id == "u1"
            continued = restarted_deps["model"].calls[0].texts()
            assert call_text in continued
            assert result in continued
            assert [
                event.text
                for event in restarted.events(session_id)
                if event.kind == "tool_result"
            ] == [result]

    await check_out_of_role_tool_is_not_dispatched(make_adapter)


async def check_out_of_role_tool_is_not_dispatched(make_adapter) -> None:
    """A Router tool requested in a watchdog session is refused, not run."""

    denied_args = {"probe": "nope"}
    denied = make_doubles(
        script=[
            ScriptedChunk(tool_name="autonomy_route", tool_args=denied_args),
            "continued",
        ]
    )
    dispatched, restore_handlers = _spy_tool_handlers()
    try:
        watchdog = make_adapter(**denied)
        await watchdog.start("s-deny", role="watchdog", user_id="u1")
        denied_events = await watchdog.turn("s-deny", "route anyway")
    finally:
        restore_handlers()
    assert dispatched == [], f"denied tool: dispatched {dispatched!r}"
    assert [event.text for event in denied_events if event.kind == "tool_refused"] == [
        "autonomy_route"
    ], "denied tool: autonomy_route was not refused"
    assert [event.kind for event in denied_events if event.kind == "tool_result"] == [], (
        "denied tool: a refused tool produced a result"
    )
    assert [event.text for event in denied_events if event.kind == "token"] == [
        "continued"
    ]
    assert len(denied["model"].calls) == 2
    assert (
        scripted_tool_result("autonomy_route", denied_args)
        not in watchdog.transcript("s-deny").text()
    )
    await close_adapter(watchdog)


@pytest.mark.asyncio
async def test_out_of_role_check_catches_cheater() -> None:
    with pytest.raises(AssertionError, match="^denied tool: "):
        await check_out_of_role_tool_is_not_dispatched(
            cheat_factory(OutOfRoleDispatchCheater)
        )


# Every shell and file name, plus spellings a lowercase-only check misses.
_FORBIDDEN_SCRIPT = [
    *sorted(SHELL_OR_FILE_TOOLS),
    "BASH",
    "Write_File",
    "mcp__fs__write_file",
]


async def check_no_shell_or_file_actions(
    make_adapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model shell and file tools are refused and never dispatched.

    Runtime boots are exact registered commands: the factory registers what
    the adapter declares in ``runtime_argv`` and nothing else. ``node`` and
    ``npm`` are not exempt, and a sentinel string is not how a command is
    judged. File writes during ``start`` and ``turn`` fail closed for every
    path outside the adapter's scratch directory.
    """

    sentinel = tmp_path / "should-not-be-written.txt"
    gate = _RuntimeGate()
    _install_runtime_gate(monkeypatch, gate)
    dispatched, restore_handlers = _spy_tool_handlers()
    script = [
        ScriptedChunk(
            tool_name=name,
            tool_args={"command": f"touch {sentinel}", "path": str(sentinel)},
        )
        for name in _FORBIDDEN_SCRIPT
    ]
    script.append(ScriptedChunk(text="talked"))
    deps = make_doubles(script=script)
    try:
        adapter = make_adapter(**deps, gate=gate)
        runtimes = getattr(make_adapter, "runtimes", None)
        # Only this adapter's own scratch dir, not its siblings or the socket.
        scratch = runtimes[-1]["runtime"].scratch_dir if runtimes else None
        before = _tree_signature(tmp_path)
        restore_files = _block_file_actions(allow_under=scratch)
        try:
            await adapter.start("s1", role="router", user_id="u1")
            events = await adapter.turn("s1", "do not touch disk")
        finally:
            restore_files()
    finally:
        restore_handlers()
    assert _tree_signature(tmp_path) == before, "no shell: a file changed"
    assert not sentinel.exists(), "no shell: the sentinel was written"
    assert gate.refused == [], f"no shell: unregistered launches {gate.refused!r}"
    assert set(gate.launches) <= gate.allowed
    assert not any(is_shell_or_file_tool(name) for name in dispatched), (
        f"no shell: dispatched {dispatched!r}"
    )
    refused = [event.text for event in events if event.kind == "tool_refused"]
    assert refused == _FORBIDDEN_SCRIPT, f"no shell: refused {refused!r}"
    assert [event.kind for event in events if event.kind == "tool_result"] == [], (
        "no shell: a forbidden tool produced a result"
    )
    assert [event.text for event in events if event.kind == "token"] == ["talked"]
    assert "bash" in adapter.transcript("s1").text()
    joined = " ".join(" ".join(launch) for launch in gate.launches)
    assert str(sentinel) not in joined, "no shell: the sentinel reached a command"
    for role in ("router", "watchdog"):
        assert not any(is_shell_or_file_tool(n) for n in adapter.tool_names(role))
    for call in deps["model"].calls:
        assert call.tools is not None
        assert not any(is_shell_or_file_tool(name) for name in call.tools), (
            f"no shell: offered {call.tools!r}"
        )
    await close_adapter(adapter)


@pytest.mark.asyncio
async def test_no_shell_or_file_actions(
    make_adapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await check_no_shell_or_file_actions(make_adapter, tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_no_shell_check_catches_native_builtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AssertionError, match="^no shell: "):
        await check_no_shell_or_file_actions(
            cheat_factory(NativeBuiltinCheater), tmp_path, monkeypatch
        )


@pytest.mark.asyncio
async def test_out_of_process_runtime_boots_only_its_declared_argv(
    runtime_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge reference launches exactly what it declared, writes only scratch.

    This is the path a Node service or a native CLI takes: a registered boot
    command, a scratch directory, and the doubles reached over the bridge.
    """

    from tests.prototypes.superagent.conftest import AdapterFactory

    module = load_adapter_module("reference_bridge")
    factory = AdapterFactory(module, runtime_root)
    try:
        gate = _RuntimeGate()
        _install_runtime_gate(monkeypatch, gate)
        deps = make_doubles(script=["over-the-bridge"])
        adapter = factory(**deps, gate=gate)
        scratch = factory.runtimes[0]["runtime"].scratch_dir
        restore_files = _block_file_actions(allow_under=scratch)
        try:
            await adapter.start("s1", role="router", user_id="u1")
            events = await adapter.turn("s1", "hello")
        finally:
            restore_files()
        declared = factory.runtimes[0]["argv"]
        assert gate.launches == declared
        assert len(declared) == 1
        assert (scratch / "boot.json").is_file()
        assert (scratch / "runtime.pid").is_file()
        assert [event.text for event in events if event.kind == "token"] == [
            "over-the-bridge"
        ]
        bridge = factory.runtimes[0]["bridge"]
        assert {"hello", "model.open", "model.next", "seq.allocate", "store.commit"} <= (
            set(bridge.calls)
        )
        assert deps["store"].transcript_rows("s1")
        # Shutdown: the runtime exits on EOF of its control channel.
        process = adapter._process
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        adapter._process = None
        process.stdout.close()
    finally:
        factory.teardown()

    # Without registration the same boot is refused.
    factory = AdapterFactory(module, runtime_root / "u")
    (runtime_root / "u").mkdir()
    try:
        gate = _RuntimeGate()
        _install_runtime_gate(monkeypatch, gate)
        adapter = factory(**make_doubles())
        with pytest.raises(AssertionError, match="uncontrolled runtime launch"):
            await adapter.start("s1", role="router", user_id="u1")
        assert gate.launches == []
    finally:
        factory.teardown()


def test_reference_runtime_launch_is_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a pre-registered argv may start. Nothing is inferred from the text.

    The registered boot is a real process. ``node``, ``npm``, and a shell
    that mentions the sentinel are not registered, so they never run.
    """

    sentinel = tmp_path / "should-not-be-written.txt"
    gate = _RuntimeGate()
    boot = (sys.executable, "-c", "print('runtime-ok')")
    popen_boot = (sys.executable, "-c", "print('popen-ok')")
    gate.authorize(boot)
    gate.authorize(popen_boot)
    _install_runtime_gate(monkeypatch, gate)

    completed = subprocess.run(list(boot), capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    assert "runtime-ok" in completed.stdout
    process = subprocess.Popen(
        list(popen_boot),
        stdout=subprocess.PIPE,
        text=True,
    )
    output, _ = process.communicate()
    assert process.returncode == 0
    assert "popen-ok" in output
    assert gate.launches == [boot, popen_boot]

    for argv in (
        ["npm", "install"],
        ["node", "server.js"],
        ["npx", "serve"],
        ["touch", str(sentinel)],
        ["bash", "-c", f"touch {sentinel}"],
        ["sh", "-c", f"echo > {sentinel}"],
        [sys.executable, "-c", "print('not-authorized')"],
    ):
        with pytest.raises(AssertionError, match="uncontrolled runtime launch"):
            subprocess.run(argv, check=False)
    with pytest.raises(AssertionError, match="uncontrolled runtime launch"):
        os.system(f"touch {sentinel}")
    assert not sentinel.exists()
    assert gate.launches == [boot, popen_boot]


@pytest.mark.asyncio
async def test_no_real_provider(make_adapter) -> None:
    """Import, construction, start, turn, and child processes stay offline."""

    assert_offline()
    deps = make_doubles(script=["local-only"])
    assert_offline()
    adapter = make_adapter(**deps)
    assert adapter.provider_name == "scripted"
    assert deps["model"].provider_name == "scripted"
    assert_offline()
    await adapter.start("s1", role="router", user_id="u1")
    assert_offline()
    events = await adapter.turn("s1", "stay local")
    assert_offline()
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.prototypes.superagent.offline_guard import assert_offline\n"
            "assert_offline()\n"
            "print('offline-ok')\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr or child.stdout
    assert "offline-ok" in child.stdout
    assert len(deps["model"].calls) == 1
    assert [event.text for event in events if event.kind == "token"] == ["local-only"]


def test_cold_start_and_loc_json(
    adapter_name: str, adapter_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert adapter_name in ADAPTER_NAMES
    assert hasattr(adapter_module, "make_adapter")
    dest = _result_dir(tmp_path, monkeypatch)
    measured = _measure_cold_start(adapter_name)
    assert measured["provider_name"] == "scripted"
    assert measured["role"] == "router"
    assert measured["session_id"] == "cold-session"
    assert math.isfinite(measured["cold_start_ms"])
    assert measured["cold_start_ms"] >= 0
    assert measured["import_ms"] >= 0
    assert measured["start_ms"] >= 0
    package = _ROOT / "prototypes" / "superagent" / adapter_name
    before = _tree_signature(package)
    loc = _package_loc(package)
    assert _tree_signature(package) == before
    assert loc["loc"] > 0
    counted = [item["path"] for item in loc["files"]]
    assert any(path.endswith("/adapter.py") for path in counted)
    assert all(
        part not in _LOC_EXCLUDED_DIRS
        for path in counted
        for part in Path(path).parts
    )
    _merge_json(dest, "cold-start.json", adapter_name, measured)
    _merge_json(dest, "loc.json", adapter_name, loc)
    cold = json.loads((dest / "cold-start.json").read_text(encoding="utf-8"))
    lines = json.loads((dest / "loc.json").read_text(encoding="utf-8"))
    assert cold[adapter_name]["cold_start_ms"] == measured["cold_start_ms"]
    assert lines[adapter_name]["package"] == f"prototypes/superagent/{adapter_name}"
    assert lines[adapter_name]["loc"] == loc["loc"]


def _result_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    configured = os.environ.get("SUPERAGENT_RESULT_DIR")
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setenv("SUPERAGENT_RESULT_DIR", str(tmp_path))
    return tmp_path


def _measure_cold_start(adapter_name: str) -> dict:
    if adapter_name not in ADAPTER_NAMES:
        raise AssertionError(adapter_name)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LANGSMITH_TRACING"] = "false"
    env["LANGCHAIN_TRACING_V2"] = "false"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cold.json"
        completed = subprocess.run(
            [sys.executable, "-c", _COLD_START_SCRIPT, adapter_name, str(out)],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr or completed.stdout)
        return json.loads(out.read_text(encoding="utf-8"))


def _is_implementation_source(path: Path, package: Path) -> bool:
    if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
        return False
    relative = path.relative_to(package)
    if any(part in _LOC_EXCLUDED_DIRS for part in relative.parts):
        return False
    name = path.name
    if ".generated." in name or name.endswith(".min.js") or name.endswith(".bundle.js"):
        return False
    return True


def test_reference_loc_exclusions_use_a_temporary_tree(tmp_path: Path) -> None:
    """Dependency and generated files are excluded without writing into an adapter.

    A real ``dist/app.js`` or ``generated/output.py`` stays where it is. The
    exclusion rules are checked on a fixture tree, and the real package is
    only read.
    """

    real = _ROOT / "prototypes" / "superagent" / "reference"
    before = _tree_signature(real)
    package = tmp_path / "sample_adapter"
    _write(package / "adapter.py", "value = 1\n\n")
    _write(package / "kept.py", "value = 2\n")
    _write(package / "README.md", "not source\n")
    _write(package / "pkg.generated.py", "generated = True\n")
    _write(package / "app.min.js", "var a=1\n")
    _write(package / "app.bundle.js", "var a=1\n")
    for excluded in sorted(_LOC_EXCLUDED_DIRS):
        _write(package / excluded / "skip.py", "skip = True\n\n")
    _write(package / "node_modules" / "leftpad" / "index.js", "module.exports = 1\n")
    _write(package / "dist" / "app.js", "generated = True\n\n")
    _write(package / "generated" / "output.py", "generated = True\n\n")

    loc = _package_loc(package, display_root=tmp_path)
    counted = [item["path"] for item in loc["files"]]
    assert counted == ["sample_adapter/adapter.py", "sample_adapter/kept.py"]
    assert loc["loc"] == 2
    assert _tree_signature(real) == before
    assert _package_loc(real)["loc"] > 0


def _package_loc(package: Path, *, display_root: Path | None = None) -> dict:
    package = package.resolve()
    anchor = (display_root or _ROOT).resolve()
    files = []
    for path in sorted(package.rglob("*")):
        if not _is_implementation_source(path, package):
            continue
        loc, raw = _count_lines(path)
        files.append(
            {
                "path": path.relative_to(anchor).as_posix(),
                "loc": loc,
                "raw_lines": raw,
            }
        )
    return {
        "adapter": package.name,
        "schema_version": 1,
        "package": package.relative_to(anchor).as_posix(),
        "loc": sum(item["loc"] for item in files),
        "raw_lines": sum(item["raw_lines"] for item in files),
        "files": files,
    }


def _count_lines(path: Path) -> tuple[int, int]:
    raw = 0
    loc = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        raw += 1
        if line.strip():
            loc += 1
    return loc, raw


class _RuntimeGate:
    """Exact-argv allowlist for an adapter runtime boot.

    Unregistered commands raise before a process is created. There is no
    special case for ``node`` or ``npm``, and the command text is not scanned
    for a sentinel path.
    """

    def __init__(self) -> None:
        self.allowed: set[tuple[str, ...]] = set()
        self.launches: list[tuple[str, ...]] = []
        self.refused: list[tuple[str, ...]] = []

    def authorize(self, argv: tuple[str, ...] | list[str]) -> None:
        self.allowed.add(tuple(argv))

    def launch(self, argv: tuple[str, ...]) -> None:
        if argv not in self.allowed:
            # Kept even if an adapter swallows the error.
            self.refused.append(argv)
            raise AssertionError(f"uncontrolled runtime launch: {argv!r}")
        self.launches.append(argv)


def _install_runtime_gate(
    monkeypatch: pytest.MonkeyPatch, gate: _RuntimeGate
) -> None:
    real_run = subprocess.run
    real_popen = subprocess.Popen

    def guarded_run(*args, **kwargs):
        argv = _command_argv(args, kwargs)
        gate.launch(argv)
        # ``run`` looks up ``Popen`` on the module. Keep that call on the real
        # constructor so one authorized command is a single launch.
        current = subprocess.Popen
        subprocess.Popen = real_popen
        try:
            return real_run(*args, **kwargs)
        finally:
            subprocess.Popen = current

    def guarded_popen(*args, **kwargs):
        argv = _command_argv(args, kwargs)
        gate.launch(argv)
        return real_popen(*args, **kwargs)

    def guarded_system(command, *_args, **_kwargs):
        raise AssertionError(f"uncontrolled runtime launch: {command!r}")

    def guarded_popen_os(*_args, **_kwargs):
        raise AssertionError("uncontrolled runtime launch")

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(
        subprocess, "call", lambda *a, **k: guarded_run(*a, **k).returncode
    )
    monkeypatch.setattr(
        subprocess,
        "check_call",
        lambda *a, **k: guarded_run(*a, **k).check_returncode(),
    )
    monkeypatch.setattr(
        subprocess, "check_output", lambda *a, **k: guarded_run(*a, **k).stdout
    )
    monkeypatch.setattr(os, "system", guarded_system)
    monkeypatch.setattr(os, "popen", guarded_popen_os)
    for name in (
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    ):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, guarded_popen_os)


def _command_argv(args: tuple, kwargs: dict) -> tuple[str, ...]:
    command = kwargs.get("args")
    if command is None and args:
        command = args[0]
    if isinstance(command, (str, os.PathLike)):
        return ("sh", "-c", os.fsdecode(command))
    if isinstance(command, (list, tuple)):
        return tuple(os.fsdecode(part) for part in command)
    raise AssertionError("uncontrolled runtime launch")


def _spy_tool_handlers():
    """Record fake-handler calls and restore the shared map afterwards.

    Shell and file names are not in the map. A dispatch of one still shows
    up here if an adapter looks the name up after this spy is installed.
    """

    dispatched: list[str] = []
    originals = dict(FAKE_TOOL_HANDLERS)

    def wrap(name: str, handler):
        def wrapped(arguments):
            dispatched.append(name)
            if name in SHELL_OR_FILE_TOOLS:
                raise AssertionError(f"forbidden model action dispatched: {name}")
            return handler(arguments)

        return wrapped

    for name, handler in originals.items():
        FAKE_TOOL_HANDLERS[name] = wrap(name, handler)

    def restore() -> None:
        FAKE_TOOL_HANDLERS.clear()
        FAKE_TOOL_HANDLERS.update(originals)

    return dispatched, restore


def _block_file_actions(*, allow_under: Path | None = None):
    """Reject every file mutation outside ``allow_under`` for the guarded region.

    ``allow_under`` is the harness's runtime directory, which holds each
    adapter's scratch dir. Every other path fails closed, and a write through
    a bare file descriptor is refused. Reads stay available. This patches the
    test interpreter only; a child process is checked by the sentinel and the
    tree signature instead.
    """

    originals: list[tuple[object, str, object]] = []
    real_open = builtins.open
    real_os_open = os.open
    root = allow_under.resolve() if allow_under is not None else None

    def inside(target: object) -> bool:
        if root is None or not isinstance(target, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(target)).resolve().is_relative_to(root)

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            if not inside(file):
                raise AssertionError(f"file action: {file!r}")
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        mutating = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        if flags & mutating and not inside(path):
            raise AssertionError(f"file action: {path!r}")
        return real_os_open(path, flags, *args, **kwargs)

    def deny_outside(raw, positions: tuple[int, ...] = (0,)):
        """Allow the call only when every path argument is inside the root."""

        def guarded(*args, **kwargs):
            targets = [args[i] for i in positions if i < len(args)]
            if not targets or not all(inside(target) for target in targets):
                raise AssertionError(f"file action: {targets!r}")
            return raw(*args, **kwargs)

        return guarded

    def install(obj: object, name: str, replacement: object) -> None:
        originals.append((obj, name, getattr(obj, name)))
        setattr(obj, name, replacement)

    install(builtins, "open", guarded_open)
    install(os, "open", guarded_os_open)
    for name in ("remove", "unlink", "mkdir", "makedirs"):
        install(os, name, deny_outside(getattr(os, name)))
    for name in ("rename", "replace"):
        install(os, name, deny_outside(getattr(os, name), (0, 1)))
    for name in ("write_text", "write_bytes", "touch", "mkdir", "unlink", "rmdir"):
        install(Path, name, deny_outside(getattr(Path, name)))
    for name in ("rename", "replace"):
        install(Path, name, deny_outside(getattr(Path, name), (0, 1)))
    for name in ("copy", "copy2", "copyfile"):
        # The source may be anywhere; the destination must be scratch.
        install(shutil, name, deny_outside(getattr(shutil, name), (1,)))
    install(shutil, "move", deny_outside(shutil.move, (0, 1)))
    install(shutil, "rmtree", deny_outside(shutil.rmtree))

    def restore() -> None:
        while originals:
            obj, name, raw = originals.pop()
            setattr(obj, name, raw)

    return restore


def _tree_signature(path: Path) -> tuple | None:
    if not path.exists():
        return None
    rows = []
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path).as_posix()
        stat = item.lstat()
        rows.append((rel, stat.st_mode, stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _merge_json(directory: Path, filename: str, adapter: str, record: dict) -> None:
    path = directory / filename
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read()
            body = json.loads(raw) if raw.strip() else {}
            if not isinstance(body, dict):
                body = {}
            body[adapter] = record
            handle.seek(0)
            handle.truncate()
            json.dump(body, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _bridge_for(runtime_root: Path, deps: dict, name: str = "b.sock"):
    from prototypes.superagent.bridge import ContractBridge

    return ContractBridge(str(runtime_root / name), **deps).start()


def test_bridge_requires_a_versioned_hello(runtime_root: Path) -> None:
    from prototypes.superagent.bridge import (
        PROTOCOL_VERSION,
        BridgeClient,
        BridgeError,
    )

    bridge = _bridge_for(runtime_root, make_doubles())
    try:
        silent = BridgeClient(bridge.path, hello=False)
        with pytest.raises(BridgeError, match="first bridge call must be hello"):
            silent.call("subscriber.count")
        with pytest.raises(BridgeError, match="protocol"):
            silent.call("hello", protocol=PROTOCOL_VERSION + 1)
        reply = silent.call("hello", protocol=PROTOCOL_VERSION)
        assert reply["protocol"] == PROTOCOL_VERSION
        assert {"tools.call", "seq.allocate", "subscriber.count"} <= set(
            reply["methods"]
        )
        assert silent.call("subscriber.count") == 0
        silent.close()
    finally:
        bridge.close()


def test_bridge_tools_call_enforces_the_stored_role(runtime_root: Path) -> None:
    """A runtime that asks the bridge to run a Router tool for a watchdog is refused."""

    from prototypes.superagent.bridge import BridgeClient, BridgeError

    deps = make_doubles()
    with deps["store"].transaction() as txn:
        txn.upsert_session(
            {
                "session_id": "s-w",
                "user_id": "u1",
                "role": "watchdog",
                "status": "ready",
                "next_seq": 1,
            }
        )
    bridge = _bridge_for(runtime_root, deps)
    dispatched, restore = _spy_tool_handlers()
    try:
        client = BridgeClient(bridge.path)
        with pytest.raises(BridgeError, match="not offered to the watchdog role"):
            client.call(
                "tools.call", session_id="s-w", name="autonomy_route", arguments={}
            )
        with pytest.raises(BridgeError, match="not started"):
            client.call(
                "tools.call", session_id="nope", name="autonomy_watchdog", arguments={}
            )
        assert dispatched == []
        done = client.call(
            "tools.call",
            session_id="s-w",
            name="autonomy_watchdog",
            arguments={"ticket_id": "t1"},
        )
        assert dispatched == ["autonomy_watchdog"]
        assert done == {
            "call": scripted_tool_call_text("autonomy_watchdog", {"ticket_id": "t1"}),
            "result": scripted_tool_result("autonomy_watchdog", {"ticket_id": "t1"}),
        }
        client.close()
    finally:
        restore()
        bridge.close()


def test_bridge_shares_seq_and_event_count_across_connections(
    runtime_root: Path,
) -> None:
    """A model shim and an MCP server get disjoint seqs and see each other's events."""

    from prototypes.superagent.bridge import BridgeClient
    from prototypes.superagent.contract import StreamEvent

    deps = make_doubles()
    with deps["store"].transaction() as txn:
        txn.upsert_session(
            {
                "session_id": "s1",
                "user_id": "u1",
                "role": "router",
                "status": "ready",
                "next_seq": 7,
            }
        )
    bridge = _bridge_for(runtime_root, deps)
    try:
        shim, mcp = BridgeClient(bridge.path), BridgeClient(bridge.path)
        assert shim.call("seq.allocate", session_id="s1")["first"] == 7
        assert mcp.call("seq.allocate", session_id="s1", count=2)["first"] == 8
        assert shim.call("seq.allocate", session_id="s1")["first"] == 10
        mcp.call("subscriber.publish", seq=8, kind="tool_call", text="c")
        assert shim.call("subscriber.count") == 1
        assert deps["subscriber"].events == [StreamEvent(8, "tool_call", "c")]
        shim.close()
        mcp.close()
    finally:
        bridge.close()


def test_message_texts_reads_strings_and_content_blocks() -> None:
    from prototypes.superagent.contract import ModelCall, message_texts

    call_text = scripted_tool_call_text("autonomy_route", {"ticket_id": "t1"})
    anthropic = [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "routing"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "autonomy_route",
                    "input": {"ticket_id": "t1"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": "routed"}],
                }
            ],
        },
    ]
    openai = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "autonomy_route",
                        "arguments": '{"ticket_id": "t1"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "routed"},
    ]
    expected = ["hello", call_text, "routed"]
    assert ModelCall(messages=anthropic).texts() == ["hello", "routing", *expected[1:]]
    assert ModelCall(messages=openai).texts() == expected
    assert message_texts({"role": "user", "content": "plain"}) == ["plain"]
    with pytest.raises(ValueError, match="image"):
        message_texts({"role": "user", "content": [{"type": "image"}]})


def test_file_guard_allows_only_the_given_scratch(runtime_root: Path) -> None:
    own = runtime_root / "a1" / "scratch"
    sibling = runtime_root / "a2" / "scratch"
    own.mkdir(parents=True)
    sibling.mkdir(parents=True)
    restore = _block_file_actions(allow_under=own)
    try:
        (own / "ok.txt").write_text("ok", encoding="utf-8")
        with open(own / "ok2.txt", "w", encoding="utf-8") as handle:
            handle.write("ok")
        for target in (sibling / "leak.txt", runtime_root / "a1" / "bridge.txt"):
            with pytest.raises(AssertionError, match="file action"):
                target.write_text("leak", encoding="utf-8")
            with pytest.raises(AssertionError, match="file action"):
                open(target, "w", encoding="utf-8")
        with pytest.raises(AssertionError, match="file action"):
            os.rename(own / "ok.txt", sibling / "moved.txt")
    finally:
        restore()
    assert sorted(path.name for path in own.iterdir()) == ["ok.txt", "ok2.txt"]
    assert list(sibling.iterdir()) == []
