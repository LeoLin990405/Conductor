"""``conductor workspace`` verbs and ``dispatch --workspace`` request bodies."""

from __future__ import annotations

import argparse

import pytest

from src.conductor_cli import cli, dispatch
from src.conductor_cli.client import APIError, CLIError

ROW = {
    "workspace_id": "11111111-1111-1111-1111-111111111111",
    "name": "conductor",
    "path": "/src/conductor",
    "git_remote": "git@example.com:conductor.git",
    "sandbox_policy": "workspace-write",
    "default_worker": "codex",
}


class FakeClient:
    def __init__(self, *, get=None, post=None, delete=None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._get = get if get is not None else dict(ROW)
        self._post = post if post is not None else dict(ROW)
        self._delete = delete if delete is not None else {"deleted": True}

    def get(self, path):
        self.calls.append(("GET", path, None))
        return self._get

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return self._post

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        return self._delete


def _parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["workspace", *argv])


def test_workspace_verbs_parse() -> None:
    for argv, handler in (
        (("add", "/src/conductor"), dispatch._workspace_add),
        (("list",), dispatch._workspace_list),
        (("show", "conductor"), dispatch._workspace_show),
        (("remove", "conductor"), dispatch._workspace_remove),
    ):
        assert _parse(*argv).handler is handler


def test_add_derives_the_name_and_posts_the_fields() -> None:
    client = FakeClient()
    args = _parse("add", "/src/conductor", "--default-worker", "codex")
    dispatch._workspace_add(args, client)
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch-workspaces",
            {
                "name": "conductor",
                "path": "/src/conductor",
                "sandbox_policy": "",
                "default_worker": "codex",
            },
        )
    ]


def test_add_passes_an_explicit_remote_and_fetches_when_asked() -> None:
    client = FakeClient(post={"workspace_id": ROW["workspace_id"]})
    args = _parse(
        "add",
        "/src/conductor",
        "--remote",
        "git@example.com:other.git",
        "--fetch",
    )
    result = dispatch._workspace_add(args, client)
    assert client.calls[0][2]["git_remote"] == "git@example.com:other.git"
    assert client.calls[1] == (
        "POST",
        f"/api/v1/dispatch-workspaces/{ROW['workspace_id']}/fetch",
        None,
    )
    assert result["fetch"] == {"workspace_id": ROW["workspace_id"]}


def test_list_accepts_a_bare_list_from_an_older_server() -> None:
    client = FakeClient(get=[ROW])
    assert dispatch._workspace_list(_parse("list"), client) == {"workspaces": [ROW]}


def test_show_gets_one_workspace() -> None:
    client = FakeClient()
    assert dispatch._workspace_show(_parse("show", "conductor"), client) == ROW
    assert client.calls == [("GET", "/api/v1/dispatch-workspaces/conductor", None)]


def test_remove_deletes_the_registry_row() -> None:
    client = FakeClient()
    assert dispatch._workspace_remove(_parse("remove", "conductor"), client) == {
        "deleted": True
    }
    assert client.calls == [("DELETE", "/api/v1/dispatch-workspaces/conductor", None)]


def test_dispatch_start_sends_the_workspace() -> None:
    client = FakeClient(post={"id": "d1", "state": "SUBMITTED", "lines": []})
    namespace = argparse.Namespace(
        args=["start", "ship", "it"],
        note=None,
        force=False,
        workspace="conductor",
        json=True,
    )
    dispatch._dispatch(namespace, client)
    assert client.calls == [
        (
            "POST",
            "/api/v1/dispatch",
            {"goal": "ship it", "workspace": "conductor"},
        )
    ]


def test_dispatch_workspace_404_is_surfaced_not_faked() -> None:
    """The API's workspace-not-found 404 must not become a local fake run."""

    class MissingWorkspace(FakeClient):
        def post(self, path, json=None):
            self.calls.append(("POST", path, json))
            raise APIError("Workspace not found", 404)

    client = MissingWorkspace()
    namespace = argparse.Namespace(
        args=["start", "ship", "it"],
        note=None,
        force=False,
        workspace="missing",
        json=True,
    )

    with pytest.raises(CLIError, match="missing.*Workspace not found"):
        dispatch._dispatch(namespace, client)

    assert len(client.calls) == 1


def test_workspace_is_refused_with_an_operator_verb() -> None:
    namespace = argparse.Namespace(
        args=["status", "d1"],
        note=None,
        force=False,
        workspace="conductor",
        json=False,
    )
    try:
        dispatch._dispatch(namespace, FakeClient())
    except dispatch.DispatchUsageError as exc:
        assert "--workspace" in str(exc)
    else:  # pragma: no cover - the branch itself is the failure
        raise AssertionError("--workspace with a verb was accepted")
