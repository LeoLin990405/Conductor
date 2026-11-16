"""L5 in the CLI: short-id prefixes, retry/close/followup/follow, endless follow."""

from __future__ import annotations

from typing import Any

import pytest

from src.conductor_cli.cli import build_parser
from src.conductor_cli.client import APIError, CLIError, ServerError
from src.conductor_cli.repl import Repl, follow_ticket
from src.conductor_cli.ticket_ids import resolve_ticket_ref

FULL = "abcdef12-3456-4789-8abc-def012345678"
OTHER = "abcdef12-9999-4999-8999-999999999999"


class Client:
    """Records calls; ``resolve`` is the server's answer (or an exception)."""

    def __init__(self, *, resolve: Any = None, listing: list[dict] | None = None) -> None:
        self.calls: list[tuple] = []
        self.resolve = resolve
        self.listing = listing or []
        self.details: list[Any] = []

    def get(self, path: str, *, params: dict | None = None, timeout: float | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if path == "/api/v1/autonomy/tickets/resolve":
            if isinstance(self.resolve, Exception):
                raise self.resolve
            return self.resolve
        if path == "/api/v1/autonomy/tickets":
            offset = int((params or {}).get("offset") or 0)
            return {"tickets": self.listing[offset : offset + 200]}
        if path.startswith("/api/v1/autonomy/tickets/"):
            item = self.details.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        raise AssertionError(path)

    def post(self, path: str, json: Any = None, *, timeout: float | None = None) -> Any:
        self.calls.append(("POST", path, json))
        return {"ok": True}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path, kwargs.get("params")))
        return {"ok": True}


def _matches(*ids: str, more: bool = False) -> dict:
    rows = [{"ticket_id": i, "title": f"title {i[-4:]}", "status": "running"} for i in ids]
    return {"matches": rows, "ticket_id": ids[0] if len(ids) == 1 else None, "more": more}


# -- resolver ----------------------------------------------------------------


def test_a_full_uuid_needs_no_lookup():
    client = Client()
    assert resolve_ticket_ref(client, FULL.upper()) == FULL
    assert client.calls == []


def test_a_unique_prefix_resolves_through_the_server():
    client = Client(resolve=_matches(FULL))
    assert resolve_ticket_ref(client, "ABCDEF12") == FULL
    assert client.calls == [("GET", "/api/v1/autonomy/tickets/resolve", {"prefix": "abcdef12"})]


def test_an_ambiguous_prefix_lists_the_candidates():
    client = Client(resolve=_matches(FULL, OTHER))
    with pytest.raises(APIError) as caught:
        resolve_ticket_ref(client, "abcdef12")
    assert caught.value.status_code == 409
    message = str(caught.value)
    assert "ambiguous" in message
    assert "abcdef12 [running] title 5678" in message
    assert "abcdef12 [running] title 9999" in message
    assert caught.value.detail == {"candidates": [FULL, OTHER]}


def test_a_capped_candidate_list_still_says_there_are_more():
    client = Client(resolve=_matches(FULL, more=True))
    with pytest.raises(APIError) as caught:
        resolve_ticket_ref(client, "ab")
    assert caught.value.status_code == 409
    assert "others" in str(caught.value)


@pytest.mark.parametrize("ref", ["abc999", "retry", "t1", "ticket-1"])
def test_no_match_is_a_404(ref):
    client = Client(resolve=_matches())
    with pytest.raises(APIError) as caught:
        resolve_ticket_ref(client, ref)
    assert caught.value.status_code == 404


def test_an_empty_ref_is_refused():
    with pytest.raises(CLIError):
        resolve_ticket_ref(Client(), "  ")


@pytest.mark.parametrize("status", [404, 405, 422])
def test_an_older_server_falls_back_to_scanning_the_listing(status):
    listing = [{"ticket_id": f"00000000-0000-4000-8000-{n:012d}"} for n in range(250)]
    listing.append({"ticket_id": FULL, "status": "failed", "title": "x"})
    client = Client(resolve=APIError("HTTP 422", status), listing=listing)
    assert resolve_ticket_ref(client, "abcd") == FULL
    pages = [c for c in client.calls if c[1] == "/api/v1/autonomy/tickets"]
    assert [c[2]["offset"] for c in pages] == [0, 200]


def test_a_server_failure_is_not_mistaken_for_an_old_server():
    client = Client(resolve=APIError("forbidden", 403))
    with pytest.raises(APIError):
        resolve_ticket_ref(client, "abcd")


# -- CLI verbs -----------------------------------------------------------------


def _run(argv: list[str], client: Client) -> Any:
    args = build_parser().parse_args(argv)
    return args.handler(args, client)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["tickets", "show", "abcdef12"], ("GET", f"/api/v1/autonomy/tickets/{FULL}", None)),
        (["tickets", "cancel", "abcdef12"], ("POST", f"/api/v1/autonomy/tickets/{FULL}/stop", None)),
        (["tickets", "retry", "abcdef12"], ("POST", f"/api/v1/autonomy/tickets/{FULL}/retry", None)),
        (
            ["tickets", "close", "abcdef12", "--reason", "done"],
            ("POST", f"/api/v1/autonomy/tickets/{FULL}/close", {"reason": "done"}),
        ),
        (
            ["tickets", "reply", "abcdef12", "use main"],
            ("POST", f"/api/v1/autonomy/tickets/{FULL}/reply", {"text": "use main"}),
        ),
        (
            ["tickets", "followup", "abcdef12", "add a test"],
            ("POST", f"/api/v1/autonomy/tickets/{FULL}/followup", {"text": "add a test"}),
        ),
        (
            ["tickets", "confirm", "abcdef12", "--text", "ok"],
            ("POST", f"/api/v1/autonomy/tickets/{FULL}/clarification/confirm", {"text": "ok"}),
        ),
    ],
)
def test_ticket_verbs_accept_a_unique_prefix(argv, expected):
    client = Client(resolve=_matches(FULL))
    _run(argv, client)
    assert client.calls[0][1] == "/api/v1/autonomy/tickets/resolve"
    assert client.calls[-1] == expected


def test_switch_accepts_a_unique_prefix():
    client = Client(resolve=_matches(FULL))
    _run(["tickets", "switch", "abcdef12", "--provider", "kimi"], client)
    assert client.calls[-1][1] == f"/api/v1/autonomy/tickets/{FULL}/switch"


def test_an_ambiguous_prefix_sends_nothing():
    client = Client(resolve=_matches(FULL, OTHER))
    with pytest.raises(APIError):
        _run(["tickets", "cancel", "abcdef12"], client)
    assert all(call[0] == "GET" for call in client.calls)


def test_tickets_follow_resolves_and_follows_to_the_end(monkeypatch):
    client = Client(resolve=_matches(FULL))
    client.details = [
        {"ticket_id": FULL, "status": "running"},
        {"ticket_id": FULL, "status": "completed", "summary": "done"},
    ]
    monkeypatch.setattr("src.conductor_cli.repl.time.sleep", lambda _s: None)
    result = _run(["tickets", "follow", "abcdef12", "--poll", "0.01"], client)
    assert result == {"ticket_id": FULL, "status": "completed", "summary": "done"}


# -- follow has no deadline ------------------------------------------------------


def test_follow_outlives_fifteen_minutes_and_a_dropped_connection():
    running = {"ticket_id": FULL, "status": "running"}
    client = Client()
    # Well past 15 minutes of polling at 2 s, with an outage in the middle.
    client.details = (
        [running] * 300
        + [ServerError("Could not reach Conductor"), ServerError("HTTP 502 from Conductor", 502)]
        + [APIError("too many requests", 429)]
        + [running] * 300
        + [{"ticket_id": FULL, "status": "completed", "summary": "done"}]
    )
    slept: list[float] = []
    emitted: list[str] = []
    final = follow_ticket(
        client,
        FULL,
        poll=2.0,
        sleep=slept.append,
        emit=lambda text="", style=None: emitted.append(text),
    )
    assert final["status"] == "completed"
    assert sum(slept) > 15 * 60
    # Backoff grows and stays bounded.
    assert slept[300:303] == [2.0, 4.0, 8.0]
    assert max(slept) <= 30.0
    assert sum("retrying" in line for line in emitted) == 1
    assert "reconnected" in emitted


def test_follow_backoff_is_capped():
    client = Client()
    client.details = [ServerError("down")] * 12 + [{"ticket_id": FULL, "status": "completed"}]
    slept: list[float] = []
    follow_ticket(client, FULL, poll=2.0, sleep=slept.append, emit=lambda *a, **k: None)
    assert slept[-1] == 30.0
    assert all(s <= 30.0 for s in slept)


def test_follow_stops_on_a_ticket_that_is_gone():
    client = Client()
    client.details = [APIError("Ticket not found", 404)]
    with pytest.raises(APIError):
        follow_ticket(client, FULL, sleep=lambda _s: None, emit=lambda *a, **k: None)


# -- REPL ------------------------------------------------------------------------


def test_repl_resolves_a_prefix_missing_from_its_listing_on_the_server():
    client = Client(resolve=_matches(FULL))
    repl = Repl(client, emit=lambda *a, **k: None, read_line=lambda: None, sleep=lambda _s: None)
    repl.recent_jobs = [{"ticket_id": OTHER}]
    assert repl._resolve_ticket("abcdef12-3") == FULL
    assert ("GET", "/api/v1/autonomy/tickets/resolve", {"prefix": "abcdef12-3"}) in client.calls
