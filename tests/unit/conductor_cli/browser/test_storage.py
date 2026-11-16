from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from conductor_cli.browser.service import BrowserCommandError
from conductor_cli.browser.storage import _normalize_state, read_storage_state

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def isolated_root():
    with tempfile.TemporaryDirectory(prefix=".browser-storage-test-", dir=REPO_ROOT) as root:
        yield Path(root)


def _write_state(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_filters_unselected_sites_and_preserves_cookie_scope(isolated_root: Path):
    state_file = _write_state(
        isolated_root / "state.json",
        {
            "cookies": [
                {"name": "session", "value": "keep-value", "domain": ".Example.Test", "path": "/"},
                {"name": "host", "value": "host-value", "domain": "example.test"},
                {"name": "tracker", "value": {"not": "read"}, "domain": ".tracker.test"},
                {"name": "sub", "value": "sub-value", "domain": "sub.example.test"},
            ],
            "origins": [
                {
                    "origin": "https://EXAMPLE.test/",
                    "localStorage": [{"name": "theme", "value": "dark"}],
                },
                {"origin": "https://tracker.test", "localStorage": "excluded malformed contents"},
                {"origin": "https://sub.example.test", "localStorage": [{"name": "token", "value": "excluded"}]},
            ],
        },
    )

    result = read_storage_state(state_file, ["example.test"])

    assert [cookie["name"] for cookie in result.cookies] == ["session", "host"]
    assert result.cookies[0]["domain"] == ".Example.Test"
    assert result.cookies[1]["domain"] == "example.test"
    assert result.local_storage == {"https://example.test": {"theme": "dark"}}
    assert result.domains == ["example.test"]
    assert "tracker" not in repr(result)
    assert "sub-value" not in repr(result)


def test_unselected_rows_are_skipped_before_value_validation():
    payload = {
        "cookies": [{"name": None, "value": {"token": "private"}, "domain": ".other.test"}],
        "origins": [{"origin": "http://other.test/path", "localStorage": {"private": "payload"}}],
    }

    result = _normalize_state(json.dumps(payload).encode(), ["example.test"])

    assert result.cookies == []
    assert result.local_storage == {}
    assert result.domains == []


def test_allowed_cookie_and_storage_values_are_imported(isolated_root: Path):
    state_file = _write_state(
        isolated_root / "state.json",
        {
            "cookies": [{"name": "sid", "value": "secret-cookie", "domain": ".example.test", "secure": True}],
            "origins": [{"origin": "https://example.test", "localStorage": [{"name": "sid", "value": "secret-local"}]}],
        },
    )

    result = read_storage_state(state_file, ["example.test"])

    assert result.cookies[0]["value"] == "secret-cookie"
    assert result.local_storage["https://example.test"]["sid"] == "secret-local"
    assert result.domains == ["example.test"]


def test_storage_state_repr_omits_allowed_secret_values():
    payload = {
        "cookies": [{"name": "sid", "value": "private-cookie-value", "domain": ".example.test"}],
        "origins": [{"origin": "https://example.test", "localStorage": [{"name": "sid", "value": "private-local-value"}]}],
    }

    result = _normalize_state(json.dumps(payload).encode(), ["example.test"])

    assert "example.test" in repr(result)
    assert "private-cookie-value" not in repr(result)
    assert "private-local-value" not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"cookies": [], "origins": [], "indexedDB": []},
        {"cookies": [{"name": "x", "value": "secret", "domain": ".example.test", "extra": True}], "origins": []},
        {"cookies": [], "origins": [{"origin": "http://example.test", "localStorage": []}]},
        {"cookies": [], "origins": [{"origin": "https://example.test/path", "localStorage": []}]},
    ],
)
def test_rejects_unsupported_or_malformed_allowed_state(payload: object):
    with pytest.raises(BrowserCommandError) as error:
        _normalize_state(json.dumps(payload).encode(), ["example.test"])
    assert "secret" not in str(error.value)


def test_selected_cookie_value_error_is_sanitized():
    payload = {"cookies": [{"name": "sid", "value": "private payload", "domain": "example.test"}], "origins": []}
    payload["cookies"][0]["value"] = {"unexpected": "private payload"}  # type: ignore[index]

    with pytest.raises(BrowserCommandError) as error:
        _normalize_state(json.dumps(payload).encode(), ["example.test"])

    assert "private payload" not in str(error.value)


def test_malformed_same_site_type_is_sanitized():
    payload = {"cookies": [{"name": "sid", "value": "private", "domain": "example.test", "sameSite": []}], "origins": []}

    with pytest.raises(BrowserCommandError) as error:
        _normalize_state(json.dumps(payload).encode(), ["example.test"])

    assert "private" not in str(error.value)


def test_deep_json_is_reported_as_safe_validation_error():
    deeply_nested = b"[" * 10000 + b"0" + b"]" * 10000

    with pytest.raises(BrowserCommandError) as error:
        _normalize_state(deeply_nested, ["example.test"])
    assert "RecursionError" not in str(error.value)


def test_checked_state_file_rejects_symlink_and_permissive_mode(isolated_root: Path):
    source = _write_state(isolated_root / "state.json", {"cookies": [], "origins": []})
    link = isolated_root / "linked.json"
    link.symlink_to(source)

    with pytest.raises(BrowserCommandError):
        read_storage_state(link, ["example.test"])

    source.chmod(0o644)
    with pytest.raises(BrowserCommandError):
        read_storage_state(source, ["example.test"])
