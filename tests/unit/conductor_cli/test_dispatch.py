"""``conductor dispatch``: verb routing, and what each verb sends."""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import cli, dispatch


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        return {"ok": True}

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        return {"ok": True}


def _run(*words: str) -> FakeClient:
    client = FakeClient()
    dispatch._dispatch(argparse.Namespace(args=list(words)), client)
    return client


def test_start_starts_a_dispatch() -> None:
    client = _run("start", "ship", "the", "thing")
    assert client.calls == [("POST", "/api/v1/dispatch", {"goal": "ship the thing"})]


def test_status_reads_one_dispatch() -> None:
    client = _run("status", "d1")
    assert client.calls == [("GET", "/api/v1/dispatch/d1", None)]


def test_active_reads_the_active_list() -> None:
    client = _run("active")
    assert client.calls == [("GET", "/api/v1/dispatch/status/active", None)]


def test_approve_posts_the_decision() -> None:
    client = _run("approve", "d1")
    assert client.calls == [("POST", "/api/v1/dispatch/d1/approve", {})]


def test_reject_posts_the_decision() -> None:
    client = _run("reject", "d1")
    assert client.calls == [("POST", "/api/v1/dispatch/d1/reject", {})]


def test_reassign_posts_the_new_model() -> None:
    client = _run("reassign", "d1", "line-0", "kimi")
    assert client.calls == [
        ("POST", "/api/v1/dispatch/d1/lines/line-0/reassign", {"model": "kimi"})
    ]


def test_no_arguments_is_a_usage_error() -> None:
    with pytest.raises(ValueError, match="conductor dispatch"):
        _run()


@pytest.mark.parametrize(
    "words",
    [("status", "d1", "d2"), ("approve",), ("reject", ""), ("reassign", "d1")],
)
def test_a_verb_with_the_wrong_arity_is_a_usage_error(words: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        _run(*words)


def test_the_dispatch_group_is_registered_on_the_shared_parser() -> None:
    args = cli.build_parser().parse_args(["dispatch", "status", "d1"])
    assert args.handler is dispatch._dispatch
    assert args.args == ["status", "d1"]

    goal = cli.build_parser().parse_args(["dispatch", "start", "make it async"])
    assert goal.args == ["start", "make it async"]
