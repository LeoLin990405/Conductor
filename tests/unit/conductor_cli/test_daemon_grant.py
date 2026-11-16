"""A daemon grant is accepted only when this device's trusted key signed it."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.conductor_cli.daemon_grant import (
    GrantExpired,
    GrantReplayed,
    GrantSignatureError,
    GrantUnsigned,
    NonceStore,
    SigningKey,
    TrustedKeys,
    sign_grant,
    verify_grant,
)

LEASE = "77777777-7777-4777-8777-777777777777"
TICKET = "66666666-6666-4666-8666-666666666666"
DAEMON = "55555555-5555-4555-8555-555555555555"
NOW = 1_700_000_000


def _grant(key: SigningKey, *, exp: int = NOW + 60, nonce: str | None = None, **overrides):
    fields = {
        "key": key,
        "lease_id": LEASE,
        "ticket_id": TICKET,
        "daemon_id": DAEMON,
        "policy_version": "abc123",
        "expires_at": exp,
        "issued_at": NOW,
        "nonce": nonce,
    }
    fields.update(overrides)
    return sign_grant(**fields)


def test_a_grant_signed_by_a_trusted_key_verifies():
    key = SigningKey.generate("server")
    grant = _grant(key)

    verified = verify_grant(grant, TrustedKeys.trust(key), now=NOW)

    assert verified.kid == "server"
    assert verified.lease_id == LEASE
    assert verified.ticket_id == TICKET
    assert verified.daemon_id == DAEMON
    assert verified.policy_version == "abc123"
    assert verified.expires_at == NOW + 60
    assert "seed" not in repr(key)


def test_an_expired_grant_is_refused():
    key = SigningKey.generate("server")
    grant = _grant(key, exp=NOW + 10)

    with pytest.raises(GrantExpired):
        verify_grant(grant, TrustedKeys.trust(key), now=NOW + 10)


def test_a_grant_signed_by_the_wrong_key_is_refused():
    trusted = SigningKey.generate("server")
    other = SigningKey.generate("server")
    grant = _grant(other)

    with pytest.raises(GrantSignatureError):
        verify_grant(grant, TrustedKeys.trust(trusted), now=NOW)


def test_a_grant_whose_key_id_is_not_trusted_is_refused():
    trusted = SigningKey.generate("server")
    other = SigningKey.generate("other")

    with pytest.raises(GrantSignatureError):
        verify_grant(_grant(other), TrustedKeys.trust(trusted), now=NOW)


def test_a_rotated_key_verifies_while_the_previous_key_stays_trusted():
    old = SigningKey.generate("2026-old")
    new = SigningKey.generate("2026-new")
    keys = TrustedKeys.trust(old, new)

    verified = verify_grant(_grant(new, nonce="rotated-key-nonce-1"), keys, now=NOW)
    still_trusted = verify_grant(
        _grant(old, nonce="rotated-key-nonce-2"), keys, now=NOW
    )

    assert verified.kid == "2026-new"
    assert still_trusted.kid == "2026-old"


def test_a_replayed_grant_is_refused_the_second_time(tmp_path: Path):
    key = SigningKey.generate("server")
    grant = _grant(key)
    store = NonceStore(tmp_path / "nonces.json")
    verified = verify_grant(grant, TrustedKeys.trust(key), now=NOW)

    store.consume(verified.nonce, verified.expires_at, now=NOW)

    assert store.seen(verified.nonce, now=NOW)
    with pytest.raises(GrantReplayed):
        store.consume(verified.nonce, verified.expires_at, now=NOW)


def test_an_unsigned_grant_is_refused():
    key = SigningKey.generate("server")
    grant = _grant(key)
    grant["sig"] = ""

    with pytest.raises(GrantUnsigned):
        verify_grant(grant, TrustedKeys.trust(key), now=NOW)
    with pytest.raises(GrantUnsigned):
        verify_grant(None, TrustedKeys.trust(key), now=NOW)


def test_a_changed_field_breaks_the_signature():
    key = SigningKey.generate("server")
    grant = _grant(key)
    grant["policy_version"] = "ffff"

    with pytest.raises(GrantSignatureError):
        verify_grant(grant, TrustedKeys.trust(key), now=NOW)


def test_a_trusted_key_file_can_hold_more_than_one_key(tmp_path: Path):
    old = SigningKey.generate("2026-old")
    new = SigningKey.generate("2026-new")
    path = tmp_path / "daemon_trusted_keys.json"
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "keys": [
                    {"kid": old.kid, "public_key": old.public_b64()},
                    {"kid": new.kid, "public_key": new.public_b64()},
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = TrustedKeys.load(path)

    assert set(loaded.keys) == {"2026-old", "2026-new"}
    verify_grant(_grant(new), loaded, now=NOW)


def test_a_key_file_another_user_could_write_is_refused(tmp_path: Path):
    path = tmp_path / "loose.json"
    path.write_text('{"format": 1, "keys": []}\n', encoding="utf-8")
    path.chmod(0o666)

    with pytest.raises(Exception, match="not writable by others"):
        TrustedKeys.load(path)


def test_the_shipped_example_loads():
    example = (
        Path(__file__).resolve().parents[3]
        / "config"
        / "daemon_trusted_keys.example.json"
    )

    loaded = TrustedKeys.load(example)

    assert tuple(loaded.keys) == ("example",)


def test_an_expired_nonce_does_not_block_a_later_grant(tmp_path: Path):
    store = NonceStore(tmp_path / "nonces.json")
    store.consume("a-nonce-that-is-long-enough", NOW + 5, now=NOW)

    store.consume("a-nonce-that-is-long-enough", NOW + 50, now=NOW + 5)

    assert store.seen("a-nonce-that-is-long-enough", now=NOW + 5)


def test_a_corrupt_nonce_file_fails_closed(tmp_path: Path):
    path = tmp_path / "nonces.json"
    path.write_text("{not json", encoding="utf-8")
    store = NonceStore(path)

    from src.conductor_cli.daemon_grant import NonceStoreError

    with pytest.raises(NonceStoreError):
        store.seen("a-nonce-that-is-long-enough", now=NOW)


def test_an_expired_grant_is_not_recorded_as_accepted(tmp_path: Path):
    """A grant that expires while the poll runs must fail, not vanish."""
    store = NonceStore(tmp_path / "nonces.json")

    with pytest.raises(GrantExpired):
        store.consume("a-nonce-that-is-long-enough", NOW, now=NOW)


def test_two_processes_do_not_lose_a_nonce_update(tmp_path: Path):
    """Two daemon processes share the replay file; the update must serialise."""
    path = tmp_path / "nonces.json"
    first = NonceStore(path)
    second = NonceStore(path)
    entered = threading.Event()
    release = threading.Event()
    loads: list[str] = []
    original_load = NonceStore._load

    def slow_load(self, now):
        loads.append("load")
        entered.set()
        release.wait(timeout=2)
        return original_load(self, now)

    def consume(store, nonce):
        store.consume(nonce, NOW + 60, now=NOW)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(NonceStore, "_load", slow_load)
        first_thread = threading.Thread(
            target=consume, args=(first, "a-nonce-that-is-long-enough")
        )
        first_thread.start()
        assert entered.wait(timeout=2)
        second_thread = threading.Thread(
            target=consume, args=(second, "b-nonce-that-is-long-enough")
        )
        second_thread.start()
        time.sleep(0.15)
        # The per-instance thread lock alone would let the second process
        # enter _load before the first write; the interprocess lock keeps it
        # out until the first consume commits.
        assert loads == ["load"]
        release.set()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)
        assert loads == ["load", "load"]
        assert not first_thread.is_alive() and not second_thread.is_alive()

    check = NonceStore(path)
    assert check.seen("a-nonce-that-is-long-enough", now=NOW) is True
    assert check.seen("b-nonce-that-is-long-enough", now=NOW) is True


def test_a_repointed_parent_symlink_cannot_redirect_the_replay_record(tmp_path: Path):
    """The record location is pinned; re-pointing an ancestor changes nothing."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "state"
    link.symlink_to(real, target_is_directory=True)
    store = NonceStore(link / "nonces.json")
    nonce = "a-nonce-that-is-long-enough"
    store.consume(nonce, NOW + 60, now=NOW)

    link.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(GrantReplayed):
        store.consume(nonce, NOW + 60, now=NOW)
    assert store.seen(nonce, now=NOW) is True
    assert not (elsewhere / "nonces.json").exists()
