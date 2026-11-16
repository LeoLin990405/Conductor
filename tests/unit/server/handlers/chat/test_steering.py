"""Which steers came through a chat gateway.

A gateway resubmits what its run missed itself, so its steers are returned on
a stop; a steer from a surface conductor draws itself becomes the next turn.
Web sends carry ``platform`` too, so "platform set" is not the test.
"""

from __future__ import annotations

import pytest

from src.server.handlers.chat.steering import is_chat_app, steer_payload


@pytest.mark.parametrize(
    "platform, chat_app",
    [
        ("web", False),
        ("web:REPORT-1", False),
        # A legacy alias: no surface draws market_view any more, but a client
        # still sending it keeps the delivery it always had.
        ("market_view", False),
        ("market_view:REPORT-1", False),
        ("custom_view:REPORT-1", True),
        ("telegram", True),
        ("slack", True),
        (None, False),
        ("", False),
    ],
)
def test_only_a_gateway_surface_is_a_chat_app(platform, chat_app):
    assert is_chat_app(platform) is chat_app


@pytest.mark.parametrize(
    "platform, chat_app",
    [
        ("web", False),
        ("market_view:REPORT-1", False),
        ("custom_view:REPORT-1", True),
        ("telegram", True),
        (None, False),
    ],
)
def test_the_payload_stamps_chat_app_from_its_platform(platform, chat_app):
    payload = steer_payload("also cover the CLI", "u-1", platform=platform)

    assert payload["chat_app"] is chat_app
    assert payload["platform"] == (platform or None)
