"""``conductor dispatch recover`` and the N76 parked-line display."""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import cli, dispatch
from src.conductor_cli.client import APIError

PARKED = {
    "id": "d1",
    "state": "REVIEW",
    "lines": [
        {"id": "line-0", "status": "BLOCKED", "failure_reason": "no_new_commit"},
        {"id": "line-1", "status": "PASSED"},
        {"id": "line-2", "status": "BLOCKED", "failure_reason": "handoff_incomplete"},
    ],
    "liveness": {
        "mode": "shadow",
        "lines": {
            "line-0": {
                "status": "BLOCKED",
                "owner": "recovery_timer",
                "next_step": "reassign",
                "next_at": "2026-11-20T10:01:00+00:00",
                "budget_left": 4,
                "needs_human": False,
            },
            "line-2": {
                "status": "BLOCKED",
                "owner": "needs_human",
                "next_step": "human",
                "next_at": None,
                "budget_left": 0,
                "needs_human": True,
                "human_reason": "handoff_incomplete",
            },
        },
        "needs_human": ["line-2"],
    },
}


class FakeClient:
    def __init__(self, refuse: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.refuse = refuse or {}

    def get(self, path: str, *, params=None):
        self.calls.append(("GET", path, params))
        return dict(PARKED)

    def post(self, path: str, json=None):
        self.calls.append(("POST", path, json))
        for line_id, code in self.refuse.items():
            if f"/lines/{line_id}/" in path:
                raise APIError(code, 409)
        return dict(PARKED)

    def close(self) -> None:
        pass


def _run(*words: str, note=None, split=False, human=False, as_json=False, client=None):
    client = client or FakeClient()
    namespace = argparse.Namespace(
        args=list(words), note=note, force=False, split=split, human=human, json=as_json
    )
    return client, dispatch._dispatch(namespace, client)


def _parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["dispatch", *argv])


def test_recover_one_line_posts_next() -> None:
    client, result = _run("recover", "d1", "line-0", note="go")
    assert client.calls == [
        ("POST", "/api/v1/dispatch/d1/lines/line-0/recover", {"mode": "next", "note": "go"})
    ]
    assert result["rendered"][0] == "recover line-0: next requested; the drive runs it next"


@pytest.mark.parametrize(("flag", "mode"), [("split", "split"), ("human", "human")])
def test_recover_flags_choose_the_mode(flag: str, mode: str) -> None:
    client, _ = _run("recover", "d1", "line-0", **{flag: True})
    assert client.calls == [
        ("POST", "/api/v1/dispatch/d1/lines/line-0/recover", {"mode": mode})
    ]


def test_the_parser_accepts_split_and_human() -> None:
    parsed = _parse("recover", "d1", "line-0", "--split")
    assert parsed.split is True and parsed.human is False
    parsed = _parse("recover", "d1", "line-0", "--human", "--note", "mine")
    assert parsed.human is True and parsed.note == "mine"


def test_recover_without_a_line_runs_every_parked_line_and_reports_refusals() -> None:
    client = FakeClient(refuse={"line-2": "needs_human"})
    _, result = _run("recover", "d1", client=client)
    posted = [path for method, path, _ in client.calls if method == "POST"]
    assert posted == [
        "/api/v1/dispatch/d1/lines/line-0/recover",
        "/api/v1/dispatch/d1/lines/line-2/recover",
    ]
    text = "\n".join(result["rendered"])
    assert "recover d1: 2 parked line(s)" in text
    assert "line-0: requested" in text
    assert "line-2: refused" in text and "needs_human" in text
    assert result["recover"][1]["result"] == "refused"


def test_recover_json_keeps_the_server_object() -> None:
    _, result = _run("recover", "d1", "line-0", as_json=True)
    assert result == PARKED


@pytest.mark.parametrize(
    ("words", "kwargs", "message"),
    [
        (("recover",), {}, "takes <dispatch-id>"),
        (("recover", "d1", "a", "b"), {}, "takes <dispatch-id>"),
        (("recover", "d1"), {"split": True}, "--split takes"),
        (("recover", "d1", "line-0"), {"split": True, "human": True}, "not both"),
        (("unblock", "d1", "line-0"), {"split": True}, "--split goes with recover"),
        (("cancel", "d1"), {"human": True}, "--human goes with recover"),
    ],
)
def test_recover_usage_errors(words, kwargs, message) -> None:
    with pytest.raises(dispatch.DispatchUsageError, match=message):
        _run(*words, **kwargs)


def _with_mode(mode: str) -> dict:
    summary = dict(PARKED)
    summary["liveness"] = {**PARKED["liveness"], "mode": mode}
    return summary


def test_shadow_says_the_step_runs_only_on_request() -> None:
    text = "\n".join(dispatch.render_status(_with_mode("shadow")))
    assert (
        "→ next: reassign (liveness shadow: runs only on request); "
        "manual: conductor dispatch recover d1 line-0"
    ) in text


def test_status_shows_owner_next_step_next_at_and_budget() -> None:
    text = "\n".join(dispatch.render_status(_with_mode("enforce")))
    assert "owner recovery_timer · next_step reassign · next_at 2026-11-20T10:01:00+00:00" in text
    assert "budget_left 4" in text
    assert "→ next: reassign at 2026-11-20T10:01:00+00:00; manual: conductor dispatch recover d1 line-0" in text
    # The parked message names the next step and recover, not a list of verbs.
    assert "conductor dispatch reassign d1 line-0" not in text
    assert "conductor dispatch unblock d1 line-0" not in text


def test_status_lists_the_lines_a_person_owns() -> None:
    lines = dispatch.render_status(PARKED)
    assert "needs a person: line-2" in lines
    text = "\n".join(lines)
    assert "needs a person (handoff_incomplete)" in text
    assert "manual: conductor dispatch recover d1 line-2 --split" in text


def test_a_scope_conflict_still_names_adjudicate() -> None:
    summary = {
        "id": "d1",
        "state": "REVIEW",
        "lines": [{"id": "l", "status": "BLOCKED", "failure_reason": "reviewer_conflict_scope"}],
        "liveness": {
            "lines": {"l": {"owner": "needs_human", "next_step": "human", "needs_human": True}},
            "needs_human": ["l"],
        },
    }
    text = "\n".join(dispatch.render_status(summary))
    assert "conductor dispatch adjudicate d1 l in-scope|out-of-scope" in text


def test_a_server_without_liveness_keeps_the_old_hints() -> None:
    summary = {
        "id": "d1",
        "state": "REVIEW",
        "lines": [{"id": "l", "status": "BLOCKED", "failure_reason": "no_new_commit"}],
    }
    text = "\n".join(dispatch.render_status(summary))
    assert "conductor dispatch reassign d1 l <model>" in text
    assert "owner" not in text


def test_recover_is_in_the_usage_text() -> None:
    assert "recover <id> [<line-id>] [--split|--human|--rereview]" in dispatch.USAGE
    assert "recover" in dispatch.VERBS
