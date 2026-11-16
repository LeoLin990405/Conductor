"""N87: ``conductor dispatch recover <id> <line> --rereview``.

A line parked ``replan_exhausted`` had no operator path (``unblock`` said
``line_not_wedged``, ``recover`` offered only ``--split``/``--human``). The
CLI now sends ``mode: rereview``: the operator fixed the branch, so G2 and
the review run again on its current head.
"""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import cli, dispatch


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        return {"id": "d1", "state": "REVIEW", "lines": []}

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        return {"id": "d1", "state": "REVIEW", "lines": [{"id": "line-0", "status": "RUNNING"}]}


def _run(*words: str, **flags) -> tuple[FakeClient, object]:
    client = FakeClient()
    namespace = argparse.Namespace(args=list(words), **flags)
    return client, dispatch._dispatch(namespace, client)


def test_rereview_posts_the_rereview_mode_with_the_note() -> None:
    client, result = _run("recover", "d1", "line-0", rereview=True, note="fixed by hand")
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch/d1/lines/line-0/recover",
            {"mode": "rereview", "note": "fixed by hand"},
        )
    ]
    assert any("G2 and review run again" in text for text in result["rendered"])


def test_rereview_needs_a_line_id() -> None:
    with pytest.raises(dispatch.DispatchUsageError, match="--rereview takes"):
        _run("recover", "d1", rereview=True)


@pytest.mark.parametrize(
    ("words", "flags", "message"),
    [
        (("recover", "d1", "line-0"), {"rereview": True, "split": True}, "not both"),
        (("unblock", "d1", "line-0"), {"rereview": True}, "--rereview goes with recover"),
    ],
)
def test_rereview_usage_errors(words, flags, message) -> None:
    with pytest.raises(dispatch.DispatchUsageError, match=message):
        _run(*words, **flags)


def test_the_parser_reads_the_flag_and_the_usage_names_it() -> None:
    args = cli.build_parser().parse_args(
        ["dispatch", "recover", "d1", "line-0", "--rereview"]
    )
    assert args.rereview is True
    assert "--rereview" in dispatch.USAGE
