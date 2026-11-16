from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.server.models.autonomy import DaemonReport


def test_daemon_report_body_is_limited_by_utf8_bytes() -> None:
    accepted = "界" * (256 * 1024 // 3)
    report = DaemonReport(lease_id=uuid4(), state="ready", body=accepted)
    assert report.body == accepted

    with pytest.raises(ValidationError, match="262144 UTF-8 bytes"):
        DaemonReport(
            lease_id=uuid4(), state="ready", body="界" * (256 * 1024 // 3 + 1)
        )


def test_daemon_report_rejects_invalid_utf8_surrogate() -> None:
    with pytest.raises(ValidationError, match="valid UTF-8"):
        DaemonReport(lease_id=uuid4(), state="ready", body="bad\ud800text")


def test_daemon_report_allows_null_body() -> None:
    report = DaemonReport(lease_id=uuid4(), state="ready", body=None)
    assert report.body is None
