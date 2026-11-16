"""Autonomy dispatch keys must satisfy the message door's UUID contract."""

from __future__ import annotations

from uuid import UUID

from src.server.services.projects.dispatch import stable_request_key


def test_logical_autonomy_key_becomes_a_stable_uuid():
    first = stable_request_key("autonomy:router:ea917b09-780f-4edd-a60e-313cd41d0f20")
    second = stable_request_key("autonomy:router:ea917b09-780f-4edd-a60e-313cd41d0f20")
    assert first == second
    UUID(first)


def test_a_uuid_key_is_kept_verbatim():
    value = "ea917b09-780f-4edd-a60e-313cd41d0f20"
    assert stable_request_key(value) == value
