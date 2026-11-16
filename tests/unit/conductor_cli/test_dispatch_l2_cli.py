"""L2 CLI: ``dispatch abort``, ``dispatch deliver`` and ``--workers``."""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import cli, dispatch

SUMMARY = {"id": "d1", "state": "COMPLETE", "lines": []}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        return dict(SUMMARY)

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        return dict(SUMMARY)

    def close(self) -> None:
        pass


def _run(*words: str, **flags):
    client = FakeClient()
    namespace = argparse.Namespace(
        args=list(words),
        note=flags.get("note"),
        force=flags.get("force", False),
        json=False,
        workers=flags.get("workers"),
    )
    return client, dispatch._dispatch(namespace, client)


def test_abort_posts_note_and_force() -> None:
    client, result = _run("abort", "d1", note="stop", force=True)
    assert client.calls == [
        ("POST", "/api/v1/dispatch/d1/abort", {"note": "stop", "force": True})
    ]
    assert "rendered" in result


def test_deliver_posts_to_the_deliver_route() -> None:
    client, _ = _run("deliver", "d1")
    assert client.calls == [("POST", "/api/v1/dispatch/d1/deliver", {})]


def test_workers_go_with_a_goal_as_a_list() -> None:
    client, _ = _run("start", "ship", "it", workers="kimi, GLM,kimi")
    assert client.calls == [
        ("POST", "/api/v1/dispatch", {"goal": "ship it", "workers": ["kimi", "glm"]})
    ]


def test_workers_are_refused_with_a_verb_and_when_blank() -> None:
    with pytest.raises(dispatch.DispatchUsageError, match="--workers goes with"):
        _run("status", "d1", workers="kimi")
    with pytest.raises(dispatch.DispatchUsageError, match="at least one worker"):
        _run("start", "ship it", workers=" , ")


def test_the_parser_accepts_the_new_flag_and_verbs() -> None:
    parsed = cli.build_parser().parse_args(["dispatch", "start", "ship it", "--workers", "kimi"])
    assert parsed.workers == "kimi"
    assert "abort" in dispatch.VERBS and "deliver" in dispatch.VERBS
