"""Instructions hub: the files a target loads, and the size budget per file."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.instructions_hub.core import HubError, operator_home
from src.instructions_hub.fragments import load_fragments
from src.instructions_hub.render import render
from src.instructions_hub.targets import SIZE_BUDGET, TARGETS, known_targets, target_path

from .conftest import FRONT_MATTER_BYTES, HEADER_BYTES


def _filler(payload_bytes: int) -> str:
    """A fragment whose rendered payload is exactly ``payload_bytes`` long."""
    head = "# Big\n\n"
    body = head + "x" * (payload_bytes - len(head) - 1)
    assert len(body) + 1 == payload_bytes
    return body + "\n"


def test_list_fragments_and_ten_targets() -> None:
    assert len(known_targets()) == 10
    safety = load_fragments()["worker-safety"]
    assert "Do not restart, kill, or tear down" in safety
    assert "Do not run uv or pip in the main clone" in safety
    assert "make no further commits" in safety
    assert "Fugu" not in safety


def test_claude_stub_names_the_body_it_loads(home: Path) -> None:
    rendered = render("operator", "claude", home=home)
    assert [planned.role for planned in rendered.files] == ["stub", "rendered"]
    stub, body = rendered.files
    assert stub.body == "@~/.conductor/instructions/rendered/operator.md\n"
    assert stub.path == home / ".claude" / "CLAUDE.md"
    assert body.path == home / ".conductor" / "instructions" / "rendered" / "operator.md"
    assert "managed_by=conductor" in body.body
    assert stub.digest == body.digest


def test_the_worker_profile_names_its_own_body(home: Path) -> None:
    rendered = render("worker", "claude", home=home)
    assert rendered.files[0].body == "@~/.conductor/instructions/rendered/worker.md\n"
    assert rendered.files[1].path.name == "worker.md"


def test_the_summary_names_the_file_that_carries_the_content(home: Path) -> None:
    # The digest and the size describe one file: the one the instructions
    # live in. For Claude that is the body the stub names, not the stub.
    claude = render("operator", "claude", home=home)
    assert claude.files[0].role == "stub"
    assert claude.content.role == "rendered"
    assert claude.body == claude.content.body
    assert claude.digest == claude.content.digest
    assert claude.bytes == claude.content.bytes
    zcode = render("operator", "zcode", home=home)
    assert zcode.content.role == "managed"
    assert zcode.body == zcode.files[0].body


def test_kimi_and_dsh_share_one_agents_file(home: Path) -> None:
    kimi = render("operator", "kimi", home=home)
    dsh = render("operator", "dsh", home=home)
    assert [planned.role for planned in kimi.files] == ["shared", "cli"]
    assert [planned.role for planned in dsh.files] == ["shared", "cli"]
    assert kimi.files[0].path == home / ".agents" / "AGENTS.md"
    assert dsh.files[0].path == home / ".agents" / "AGENTS.md"
    assert kimi.files[0].body == dsh.files[0].body
    assert kimi.files[0].digest == dsh.files[0].digest
    assert kimi.files[1].path == home / ".kimi-code" / "AGENTS.md"
    assert dsh.files[1].path == home / ".dsh" / "AGENTS.md"


def test_operator_dsh_still_plans_its_cli_file(home: Path) -> None:
    # The operator profile has no dsh extras, but DSH still loads .dsh/AGENTS.md.
    rendered = render("operator", "dsh", home=home)
    assert [planned.role for planned in rendered.files] == ["shared", "cli"]
    assert rendered.files[1].path == home / ".dsh" / "AGENTS.md"
    assert "managed_by=conductor" in rendered.files[1].body


def test_dsh_loads_its_own_cli_file(home: Path) -> None:
    rendered = render("worker", "dsh", home=home)
    assert [planned.role for planned in rendered.files] == ["shared", "cli"]
    shared, cli = rendered.files
    assert cli.path == home / ".dsh" / "AGENTS.md"
    assert "Course notes workflow" in cli.body
    assert "Course notes workflow" not in shared.body
    assert "Worker safety" in shared.body


def test_dsh_cli_file_follows_dsh_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = home / "agents-lib"
    cli_home = home / "dsh-home"
    monkeypatch.setenv("DSH_AGENTS_HOME", str(agents))
    monkeypatch.setenv("DSH_HOME", str(cli_home))
    rendered = render("worker", "dsh", home=home)
    shared, cli = rendered.files
    assert shared.path == agents / "AGENTS.md"
    assert cli.path == cli_home / "AGENTS.md"


def test_kimi_tracks_shared_and_kimi_code_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # KIMI_CODE_HOME moves only the CLI file. The shared .agents path stays.
    kimi_home = home / "kimi-home"
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    rendered = render("operator", "kimi", home=home)
    shared, cli = rendered.files
    assert shared.path == home / ".agents" / "AGENTS.md"
    assert cli.path == kimi_home / "AGENTS.md"


def test_a_dotted_override_names_a_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A suffix is not a file. KIMI_CODE_HOME=<home>/kimi.v1 and
    # DSH_HOME=<home>/dsh.v1 are directories, and the CLI reads
    # <override>/AGENTS.md from them.
    monkeypatch.setenv("KIMI_CODE_HOME", str(home / "kimi.v1"))
    monkeypatch.setenv("DSH_HOME", str(home / "dsh.v1"))
    kimi = render("operator", "kimi", home=home)
    dsh = render("worker", "dsh", home=home)
    assert kimi.files[1].path == (home / "kimi.v1" / "AGENTS.md").resolve()
    assert dsh.files[1].path == (home / "dsh.v1" / "AGENTS.md").resolve()
    # The shared file is untouched by either override.
    assert kimi.files[0].path == home / ".agents" / "AGENTS.md"
    assert dsh.files[0].path == home / ".agents" / "AGENTS.md"


def test_every_file_of_every_target_stays_under_the_budget(home: Path) -> None:
    for profile in ("operator", "worker"):
        for target in TARGETS:
            rendered = render(profile, target.name, home=home)
            assert rendered.files
            for planned in rendered.files:
                assert planned.bytes == len(planned.body.encode("utf-8"))
                assert planned.bytes <= SIZE_BUDGET


def test_cursor_keeps_its_front_matter(home: Path) -> None:
    rendered = render("operator", "cursor-agent", home=home)
    body = rendered.files[0]
    assert body.body.startswith("---\nalwaysApply: true\n---\n")
    assert "managed_by=conductor source=" in body.body
    assert body.bytes == len(body.body.encode("utf-8"))


def test_the_budget_boundary_is_exact(home: Path, make_library) -> None:
    # 8192 bytes passes, 8193 does not. The file on disk is what counts.
    at_budget = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES)},
        {"op": {"fragments": ["big"]}},
        name="at-budget",
    )
    over_budget = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES + 1)},
        {"op": {"fragments": ["big"]}},
        name="over-budget",
    )
    assert render("op", "zcode", home=home, library=at_budget).files[0].bytes == SIZE_BUDGET
    with pytest.raises(HubError, match=f"budget {SIZE_BUDGET}"):
        render("op", "zcode", home=home, library=over_budget)


def test_budget_counts_the_wrapper_of_the_final_file(home: Path, make_library) -> None:
    # The managed document lands exactly on the budget. Cursor's front matter
    # is part of the file it reads, so that file is over it.
    library = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES)},
        {"op": {"fragments": ["big"]}},
    )
    plain = render("op", "zcode", home=home, library=library)
    assert plain.files[0].bytes == SIZE_BUDGET
    with pytest.raises(HubError, match=r"\.cursor/rules/conductor\.mdc"):
        render("op", "cursor-agent", home=home, library=library)


def test_budget_counts_the_cli_specific_text(home: Path, make_library) -> None:
    library = make_library(
        {"small": "# Small\n", "huge": _filler(SIZE_BUDGET)},
        {"worker": {"fragments": ["small"], "extra": {"dsh": ["huge"]}}},
    )
    # The shared file is fine; only the CLI file that holds the DSH text is
    # over the budget, and it is refused on its own.
    kimi = render("worker", "kimi", home=home, library=library)
    assert kimi.files[0].bytes < SIZE_BUDGET
    with pytest.raises(HubError, match=r"\.dsh/AGENTS\.md"):
        render("worker", "dsh", home=home, library=library)


def test_the_budget_boundary_is_exact_for_the_cli_specific_file(
    home: Path, make_library
) -> None:
    # The file that holds the CLI's own text is a final file of its own, so
    # it gets the same exact boundary: 8192 passes, 8193 fails.
    def _library(payload_bytes: int, name: str) -> Path:
        return make_library(
            {"small": "# Small\n", "own": _filler(payload_bytes)},
            {"worker": {"fragments": ["small"], "extra": {"dsh": ["own"]}}},
            name=name,
        )

    at_budget = _library(SIZE_BUDGET - HEADER_BYTES, "at-budget")
    over_budget = _library(SIZE_BUDGET - HEADER_BYTES + 1, "over-budget")
    planned = render("worker", "dsh", home=home, library=at_budget).files
    assert planned[1].role == "cli"
    assert planned[1].bytes == SIZE_BUDGET
    with pytest.raises(HubError, match=r"\.dsh/AGENTS\.md"):
        render("worker", "dsh", home=home, library=over_budget)


def test_the_budget_boundary_is_exact_for_the_body_a_stub_names(
    home: Path, make_library
) -> None:
    # The body the Claude stub points at is a final file of its own, and the
    # stub is a second one. Both are checked; the body carries the size.
    def _library(payload_bytes: int, name: str) -> Path:
        return make_library(
            {"big": _filler(payload_bytes)},
            {"op": {"fragments": ["big"]}},
            name=name,
        )

    at_budget = _library(SIZE_BUDGET - HEADER_BYTES, "at-budget")
    over_budget = _library(SIZE_BUDGET - HEADER_BYTES + 1, "over-budget")
    planned = render("op", "claude", home=home, library=at_budget).files
    stub, body = planned
    assert stub.role == "stub" and body.role == "rendered"
    assert body.bytes == SIZE_BUDGET
    assert stub.bytes == len(stub.body.encode("utf-8"))
    with pytest.raises(HubError, match=r"rendered/.*\.md"):
        render("op", "claude", home=home, library=over_budget)


def test_the_budget_boundary_is_exact_for_a_cursor_file(
    home: Path, make_library
) -> None:
    # The front matter Cursor reads is part of the file, so the file lands on
    # the budget only when the managed document is that much smaller.
    library = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES - FRONT_MATTER_BYTES)},
        {"op": {"fragments": ["big"]}},
    )
    planned = render("op", "cursor-agent", home=home, library=library).files
    assert planned[0].bytes == SIZE_BUDGET
    assert planned[0].body.startswith("---\nalwaysApply: true\n---\n")

    over = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES - FRONT_MATTER_BYTES + 1)},
        {"op": {"fragments": ["big"]}},
        name="over-budget",
    )
    with pytest.raises(HubError, match=r"\.cursor/rules/conductor\.mdc"):
        render("op", "cursor-agent", home=home, library=over)


def test_size_budget_names_the_largest_fragment(home: Path, make_library) -> None:
    library = make_library(
        {"fleet-memory": _filler(SIZE_BUDGET)},
        {"op": {"fragments": ["fleet-memory"]}},
    )
    with pytest.raises(HubError, match="largest fragment is fleet-memory"):
        render("op", "zcode", home=home, library=library)


def test_fragment_cannot_break_mdc_frontmatter(home: Path, make_library) -> None:
    library = make_library(
        {"notes": "---\nalwaysApply: false\n---\n"},
        {"op": {"fragments": ["notes"]}},
    )
    with pytest.raises(HubError, match="front matter"):
        render("op", "cursor-agent", home=home, library=library)


def test_refuses_the_operator_home() -> None:
    from src.instructions_hub.store import status

    real = operator_home()
    with pytest.raises(HubError, match="refuses"):
        status(real)


def test_override_env_cannot_escape_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROK_HOME", str(operator_home() / ".grok"))
    with pytest.raises(ValueError, match="inside the isolated HOME"):
        target_path(next(t for t in TARGETS if t.name == "grok"), home)


def test_the_measured_wrappers_are_the_ones_the_budget_uses() -> None:
    assert FRONT_MATTER_BYTES == len("---\nalwaysApply: true\n---\n\n")
    assert HEADER_BYTES == len("<!-- managed_by=conductor source=") + 12 + len(" -->\n\n")
