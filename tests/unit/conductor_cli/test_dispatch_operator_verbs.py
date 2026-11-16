"""``conductor dispatch`` operator verbs: cancel, unblock, adjudicate, --note, --force."""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import cli, dispatch

SUMMARY = {"id": "d1", "state": "MONITORING", "lines": []}


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


def _run(*words: str, note=None, force=False, as_json=False):
    client = FakeClient()
    namespace = argparse.Namespace(args=list(words), note=note, force=force, json=as_json)
    result = dispatch._dispatch(namespace, client)
    return client, result


def _parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["dispatch", *argv])


# what each verb posts ---------------------------------------------------------


def test_reject_with_note_and_force() -> None:
    client, result = _run("reject", "d1", note="wedged", force=True)
    assert client.calls == [
        ("POST", "/api/v1/dispatch/d1/reject", {"note": "wedged", "force": True})
    ]
    assert "rendered" in result


def test_plain_reject_and_approve_still_post_an_empty_body() -> None:
    assert _run("reject", "d1")[0].calls == [("POST", "/api/v1/dispatch/d1/reject", {})]
    assert _run("approve", "d1")[0].calls == [("POST", "/api/v1/dispatch/d1/approve", {})]


def test_approve_passes_the_note() -> None:
    client, _ = _run("approve", "d1", note="lgtm")
    assert client.calls == [("POST", "/api/v1/dispatch/d1/approve", {"note": "lgtm"})]


def test_cancel_posts_note_and_force() -> None:
    client, result = _run("cancel", "d1", note="stuck", force=True)
    assert client.calls == [
        ("POST", "/api/v1/dispatch/d1/cancel", {"note": "stuck", "force": True})
    ]
    assert result["rendered"][0].startswith("d1  MONITORING")


def test_cancel_json_returns_the_server_object() -> None:
    _, result = _run("cancel", "d1", as_json=True)
    assert result == SUMMARY


def test_unblock_posts_the_note() -> None:
    client, result = _run("unblock", "d1", "line-0", note="parked before the fix")
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch/d1/lines/line-0/unblock",
            {"note": "parked before the fix"},
        )
    ]
    assert "rendered" in result


@pytest.mark.parametrize(
    ("given", "sent"),
    [("in-scope", "in-scope"), ("out-of-scope", "out-of-scope"), ("IN_SCOPE", "in-scope")],
)
def test_adjudicate_posts_the_ruling(given: str, sent: str) -> None:
    client, _ = _run("adjudicate", "d1", "line-0", given, note="the spec allows it")
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch/d1/lines/line-0/adjudicate",
            {"ruling": sent, "note": "the spec allows it"},
        )
    ]


def test_adjudicate_without_a_note_posts_the_ruling_only() -> None:
    client, _ = _run("adjudicate", "d1", "line-0", "out-of-scope")
    assert client.calls[0][2] == {"ruling": "out-of-scope"}


# usage errors -------------------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        ("cancel",),
        ("cancel", "d1", "d2"),
        ("unblock", "d1"),
        ("unblock", "d1", "line-0", "x"),
        ("adjudicate", "d1", "line-0"),
        ("adjudicate", "d1", "line-0", "in-scope", "x"),
    ],
)
def test_wrong_arity_is_a_usage_error(words: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        _run(*words)


def test_a_bad_ruling_is_a_usage_error_before_any_request() -> None:
    client = FakeClient()
    namespace = argparse.Namespace(args=["adjudicate", "d1", "line-0", "maybe"])
    with pytest.raises(dispatch.DispatchUsageError, match="in-scope or out-of-scope"):
        dispatch._dispatch(namespace, client)
    assert client.calls == []


@pytest.mark.parametrize(
    "words",
    [
        ("unblock", "d1", "line-0"),
        ("adjudicate", "d1", "line-0", "in-scope"),
        ("approve", "d1"),
        ("reassign", "d1", "line-0", "kimi"),
        ("status", "d1"),
        ("start", "ship", "it"),
    ],
)
def test_force_outside_reject_and_cancel_is_a_usage_error(words) -> None:
    with pytest.raises(dispatch.DispatchUsageError, match="--force"):
        _run(*words, force=True)


@pytest.mark.parametrize(
    "words", [("status", "d1"), ("active",), ("reassign", "d1", "l", "m"), ("start", "ship", "it")]
)
def test_note_outside_the_decisions_is_a_usage_error(words) -> None:
    with pytest.raises(dispatch.DispatchUsageError, match="--note"):
        _run(*words, note="x")


# the shared parser -------------------------------------------------------------


def test_the_parser_reads_note_and_force_after_the_words() -> None:
    args = _parse("cancel", "d1", "--note", "stuck", "--force")
    assert args.args == ["cancel", "d1"]
    assert args.note == "stuck"
    assert args.force is True


def test_the_parser_defaults_leave_note_unset() -> None:
    args = _parse("unblock", "d1", "line-0")
    assert args.note is None
    assert args.force is False


def test_main_runs_adjudicate_with_json(monkeypatch, capsys) -> None:
    client = FakeClient()
    monkeypatch.setattr(cli, "APIClient", lambda base_url=None: client)
    code = cli.main(["dispatch", "adjudicate", "d1", "line-0", "in-scope", "--note", "n", "--json"])
    assert code in (0, None)
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch/d1/lines/line-0/adjudicate",
            {"ruling": "in-scope", "note": "n"},
        )
    ]


# render hints ------------------------------------------------------------------


def _rendered(*rows: dict) -> str:
    return "\n".join(dispatch.render_status({"id": "d1", "state": "MONITORING", "lines": list(rows)}))


@pytest.mark.parametrize("reason", ["reviewer_conflict_scope", "reviewer_conflict_security"])
def test_a_scope_conflict_hints_adjudicate(reason: str) -> None:
    text = _rendered(
        {"id": "line-0", "status": "BLOCKED", "assigned_model": "kimi", "failure_reason": reason}
    )
    assert "conductor dispatch adjudicate d1 line-0 in-scope|out-of-scope" in text
    assert "reassign d1 line-0" not in text


@pytest.mark.parametrize("reason", ["no_new_commit", "uncommitted_changes"])
def test_a_blocked_worker_hints_reassign_and_unblock(reason: str) -> None:
    text = _rendered(
        {"id": "line-1", "status": "BLOCKED", "assigned_model": "glm", "failure_reason": reason}
    )
    assert "conductor dispatch reassign d1 line-1 <model>" in text
    assert "conductor dispatch unblock d1 line-1" in text


def test_other_lines_get_no_hint() -> None:
    text = _rendered(
        {"id": "line-2", "status": "RUNNING", "assigned_model": "glm", "failure_reason": "no_new_commit"},
        {"id": "line-3", "status": "BLOCKED", "assigned_model": "glm", "failure_reason": "tests_failed"},
    )
    assert "adjudicate" not in text
    assert "reassign d1" not in text
    assert "unblock" not in text


def test_a_review_unavailable_park_hints_unblock() -> None:
    from src.conductor_cli.dispatch import render_status

    summary = {
        "id": "d1",
        "goal": "g",
        "state": "REVIEW",
        "lines": [{"id": "l", "status": "BLOCKED", "failure_reason": "review_unavailable"}],
        "gates": {},
        "outcomes": {},
    }
    text = "\n".join(render_status(summary))
    assert "conductor dispatch unblock d1 l" in text
