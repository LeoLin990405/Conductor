"""N85: ``conductor dispatch <unknown-word>`` never starts a dispatch.

On 2026-11-13 ``dispatch show <id>`` and ``dispatch list`` were taken as bare
goals and started nine real dispatches. A run now starts only with
``conductor dispatch start "<goal>"``; any other unknown first word is refused
with a "did you mean" hint and nothing is posted.
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
        return {"ok": True}

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        return {"id": "d1", "state": "PLANNING"}


def _run(*words: str, **flags) -> FakeClient:
    client = FakeClient()
    dispatch._dispatch(argparse.Namespace(args=list(words), **flags), client)
    return client


@pytest.mark.parametrize(
    ("words", "hint"),
    [
        (("show", "5e00816c"), "dispatch status <id>"),
        (("list",), "dispatch active"),
        (("ls",), "dispatch active"),
        (("get", "d1"), "dispatch status <id>"),
        (("info", "d1"), "dispatch status <id>"),
        (("help",), "dispatch --help"),
        (("stauts", "d1"), "dispatch status"),
        (("reasign", "d1", "line-0", "kimi"), "dispatch reassign"),
    ],
)
def test_an_unknown_verb_is_refused_with_a_hint_and_posts_nothing(words, hint) -> None:
    client = FakeClient()
    with pytest.raises(dispatch.DispatchUsageError) as caught:
        dispatch._dispatch(argparse.Namespace(args=list(words)), client)
    text = str(caught.value)
    assert "unknown dispatch verb" in text
    assert hint in text
    assert 'conductor dispatch start "<goal>"' in text
    assert "Nothing was started" in text
    assert client.calls == []


@pytest.mark.parametrize(
    "words",
    [
        ("ship the thing",),  # a quoted multi-word goal: the old bare form
        ("make", "it", "async"),  # an unquoted multi-word goal
        ("refactor",),
    ],
)
def test_the_old_bare_goal_form_no_longer_starts_a_run(words) -> None:
    client = FakeClient()
    with pytest.raises(dispatch.DispatchUsageError, match="unknown dispatch verb"):
        dispatch._dispatch(argparse.Namespace(args=list(words)), client)
    assert client.calls == []


def test_start_posts_the_goal() -> None:
    client = _run("start", "Add a --json flag to the status command")
    assert client.calls == [
        ("POST", "/api/v1/dispatch", {"goal": "Add a --json flag to the status command"})
    ]


def test_start_joins_unquoted_words_and_keeps_workspace_and_workers() -> None:
    client = _run("start", "ship", "it", workspace="conductor", workers="kimi,glm")
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch",
            {"goal": "ship it", "workspace": "conductor", "workers": ["kimi", "glm"]},
        )
    ]


@pytest.mark.parametrize(
    ("words", "match"),
    [
        (("start",), "needs a goal"),
        (("start", "  "), "needs a goal"),
        (("start", "refactor"), "single word"),
        (("start", "list"), "verb-like"),
        (("start", "show", "5e00816c"), "verb-like"),
        (("start", "status d1"), "verb-like"),
    ],
)
def test_start_refuses_one_word_and_verb_like_goals(words, match) -> None:
    client = FakeClient()
    with pytest.raises(dispatch.DispatchUsageError, match=match):
        dispatch._dispatch(argparse.Namespace(args=list(words)), client)
    assert client.calls == []


def test_workspace_and_workers_go_only_with_start() -> None:
    with pytest.raises(dispatch.DispatchUsageError, match="goes with dispatch start"):
        _run("status", "d1", workspace="conductor")
    with pytest.raises(dispatch.DispatchUsageError, match="goes with dispatch start"):
        _run("active", workers="kimi")


def test_main_refuses_an_unknown_verb_with_a_nonzero_exit(monkeypatch, capsys) -> None:
    posted: list[object] = []

    class Client(FakeClient):
        def post(self, path: str, json=None):
            posted.append((path, json))
            return {}

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "APIClient", lambda *a, **k: Client(), raising=False)
    code = cli.main(["dispatch", "show", "5e00816c"])
    assert code != 0
    assert posted == []
    err = capsys.readouterr().err
    assert "did you mean" in err


def test_help_and_usage_name_the_start_form() -> None:
    assert 'conductor dispatch start "<goal>"' in dispatch.USAGE
    assert "start" in dispatch.VERBS
    assert "dispatch <goal>" not in dispatch.USAGE
