"""R9b L10: the run-lane activity stream reader lives in services.runs.

The Ticket activity stream (and the chat run handlers) consume
``stream_from_log``; it moved out of ``handlers/chat`` so the services tier
owns it. The old handler-tier modules must be gone (no shim).
"""

import importlib
import importlib.util

import pytest


def test_new_paths_importable():
    reader = importlib.import_module("src.server.services.runs.stream_reader")
    tuning = importlib.import_module("src.server.services.runs.xread_tuning")
    assert callable(reader.stream_from_log)
    assert callable(tuning.xread_wait_timeout_s)


@pytest.mark.parametrize(
    "old",
    [
        "src.server.handlers.chat.run_stream_reader",
        "src.server.handlers.chat.xread_tuning",
    ],
)
def test_old_handler_paths_gone(old):
    assert importlib.util.find_spec(old) is None


def test_importers_share_the_moved_object():
    from src.server.handlers.chat import native_run, ptc_run, superagent_run
    from src.server.services.runs.stream_reader import stream_from_log

    assert native_run.stream_from_log is stream_from_log
    assert ptc_run.stream_from_log is stream_from_log
    assert superagent_run.stream_from_log is stream_from_log
