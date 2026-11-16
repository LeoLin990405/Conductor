"""Archived worker-session counts and Ticket detail rotation rows."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest
from rich.cells import cell_len

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_jobs import STRINGS, job_detail_rows  # noqa: E402
from ptc_cli.superagent.tui_rotation import count_rotations  # noqa: E402
from ptc_cli.superagent.tui_theme import resolve_theme  # noqa: E402


def _theme():
    return resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})


def _ticket(archived: object = None, *, legacy: bool = False) -> dict:
    ticket = {
        "ticket_id": "7f782add-aaaa-bbbb-cccc-123456789abc",
        "status": "running",
        "title": "Rotate the worker session",
        "worker_provider": "kimi",
        "worker_model": "k2",
    }
    if legacy:
        ticket["session_archive"] = archived
    else:
        ticket["worker_session"] = {"archived": archived}
    return ticket


def test_count_rotations_reads_current_and_legacy_archives() -> None:
    archived = [{"session_id": "session-a"}, {"session_id": "session-b"}]
    assert count_rotations(_ticket(archived)) == 2
    assert count_rotations(_ticket(archived, legacy=True)) == 2


def test_count_rotations_prefers_the_worker_session_archive() -> None:
    ticket = _ticket([{"session_id": "current"}])
    ticket["session_archive"] = [{"session_id": "legacy"}]
    assert count_rotations(ticket) == 1


def test_count_rotations_deduplicates_ids_and_ignores_placeholders_without_mutating() -> None:
    ticket = _ticket(
        [
            {"session_id": "session-a"},
            {"session_id": "session-a"},
            {"session_id": "session-b"},
            {"session_id": "rot-pending"},
        ]
    )
    before = deepcopy(ticket)
    assert count_rotations(ticket) == 2
    assert ticket == before


@pytest.mark.parametrize(
    "ticket",
    [
        None,
        [],
        {},
        {"worker_session": None},
        {"worker_session": []},
        {"worker_session": {"archived": "not-a-list"}},
        {"session_archive": {"session_id": "session-a"}},
        {
            "session_archive": [
                None,
                "not-a-dict",
                {},
                {"session_id": None},
                {"session_id": 123},
                {"session_id": ""},
                {"session_id": "   "},
                {"session_id": "rot-pending"},
            ]
        },
    ],
)
def test_count_rotations_treats_malformed_data_as_zero(ticket: object) -> None:
    assert count_rotations(ticket) == 0


def test_count_rotations_skips_malformed_entries() -> None:
    ticket = _ticket([None, {"session_id": "session-a"}, "bad", {"session_id": 123}])
    assert count_rotations(ticket) == 1


def test_rotation_row_is_bilingual_and_immediately_follows_worker() -> None:
    ticket = _ticket([{"session_id": "session-a"}, {"session_id": "session-b"}])
    assert STRINGS["en"]["rotated"].format(count=2) == "rotated 2 times"
    assert STRINGS["zh-CN"]["rotated"].format(count=2) == "已轮换 2 次"
    for locale, worker, rotated in (
        ("en", "worker", "rotated 2 times"),
        ("zh-CN", "执行者", "已轮换 2 次"),
    ):
        rows = job_detail_rows(ticket, _theme(), locale, 80)
        worker_index = next(index for index, row in enumerate(rows) if row.startswith(f"  {worker}:"))
        assert rows[worker_index + 1] == f"  {rotated}"


def test_rotation_row_is_absent_when_count_is_zero() -> None:
    for archived in (None, [], [{"session_id": "rot-pending"}, {"session_id": ""}]):
        ticket = _ticket(archived)
        for locale in ("en", "zh-CN"):
            rows = job_detail_rows(ticket, _theme(), locale, 80)
            assert all("rotated" not in row and "轮换" not in row for row in rows)


@pytest.mark.parametrize("width", [40, 80, 120])
def test_every_detail_row_fits_the_terminal(width: int) -> None:
    ticket = _ticket([{"session_id": f"session-{index}"} for index in range(120)])
    ticket["updated_at"] = "2026-11-11T12:34:56Z"
    for locale in ("en", "zh-CN"):
        rows = job_detail_rows(ticket, _theme(), locale, width)
        assert rows
        assert all(cell_len(row) <= width for row in rows)
