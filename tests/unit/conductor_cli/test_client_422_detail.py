"""A refused request says which field the server refused.

FastAPI answers a body it refuses with ``detail`` as a list of pydantic
validation errors: where the field is, what is wrong with it, and the type of
the error. The CLI shows that list instead of a bare status line, so an
operator learns what to change without opening the server log.

The echoed input is not shown: it is the very value the operator sent.
"""

from __future__ import annotations

import httpx
import pytest

from src.conductor_cli.client import APIError, APIClient


def _client(handler) -> APIClient:
    return APIClient(
        "http://127.0.0.1:8000", "", transport=httpx.MockTransport(handler)
    )


def test_a_validation_list_reaches_the_operator() -> None:
    detail = [
        {
            "type": "value_error",
            "loc": ["body", "goal"],
            "msg": "Value error, the dispatch goal is longer than 8000 bytes",
            "input": "x" * 9000,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": detail})

    client = _client(handler)
    try:
        with pytest.raises(APIError) as error:
            client.post("/api/v1/dispatch", {"goal": "x" * 9000})
    finally:
        client.close()

    assert error.value.status_code == 422
    assert error.value.detail == detail
    rendered = str(error.value)
    assert "body.goal" in rendered
    assert "the dispatch goal is longer than 8000 bytes" in rendered
    assert "HTTP 422" in rendered


def test_a_refused_body_does_not_echo_the_input_back() -> None:
    secret = "x" * 9000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "value_error",
                        "loc": ["body", "goal"],
                        "msg": "Value error, the dispatch goal is too long",
                        "input": secret,
                    }
                ]
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(APIError) as error:
            client.post("/api/v1/dispatch", {"goal": secret})
    finally:
        client.close()

    assert secret not in str(error.value)


def test_every_refused_field_is_shown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "type": "missing",
                        "loc": ["body", "goal"],
                        "msg": "Field required",
                    },
                    {
                        "type": "string_too_short",
                        "loc": ["body", "note"],
                        "msg": "String should have at least 1 character",
                    },
                ]
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(APIError) as error:
            client.post("/api/v1/dispatch", {})
    finally:
        client.close()

    rendered = str(error.value)
    assert "body.goal: Field required" in rendered
    assert "body.note: String should have at least 1 character" in rendered


def test_a_string_detail_is_shown_as_it_is() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "The goal is not decomposable"})

    client = _client(handler)
    try:
        with pytest.raises(APIError) as error:
            client.post("/api/v1/dispatch", {"goal": "x"})
    finally:
        client.close()

    assert str(error.value) == "The goal is not decomposable"
    assert error.value.detail is None


def test_a_refusal_without_a_detail_still_names_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={})

    client = _client(handler)
    try:
        with pytest.raises(APIError) as error:
            client.post("/api/v1/dispatch", {"goal": "x"})
    finally:
        client.close()

    assert str(error.value) == "HTTP 422 from Conductor"
