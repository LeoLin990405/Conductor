"""Collab app tokens never reach a sandbox or worker (N80 security review M1).

``COLLAB_APP_TOKEN_<APP_ID>`` rows hold a collaboration app's identity
token. The vault module drops them from every sandbox-bound read: the
snapshot every publish path starts from (provisioning, the settle loop, the
push fan-out) and the named ``${vault:NAME}`` lookups MCP servers and the
egress relay resolve. The redactor still reads them, so a leaked value is
still scrubbed. No database is touched: ``get_db_connection`` is a fake that
answers the rows below, and every token value is a fake.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from src.server.database import user_vault_secrets as vault_db
from src.server.services.collab import apps as app_svc
from tests.unit._isolated_home import assert_untouched, isolate

TOKEN_NAME = "COLLAB_APP_TOKEN_APP_A"
TOKEN_VALUE = "fake-collab-app-token-value-0001"
OTHER = {"GITHUB_TOKEN": "fake-github-value-0002", "OPENAI_KEY": "fake-openai-value-0003"}
ROWS = {**OTHER, TOKEN_NAME: TOKEN_VALUE, "COLLAB_APP_TOKEN_APP_B": "fake-collab-app-token-0004"}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    yield home
    assert_untouched(original, before)


class _Cursor:
    def __init__(self, db: "_FakeDb") -> None:
        self.db = db
        self._rows: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self.db.executed.append((sql, params))
        rows = self.db.rows
        if "string_agg" in sql and "LEFT JOIN" in sql:
            fp = self.db.fingerprint
            self._rows = [{"fingerprint": fp, "name": n, "plaintext": v} for n, v in rows.items()]
            return
        if "string_agg" in sql:
            self._rows = [{"fingerprint": self.db.fingerprint}]
            return
        if "name = ANY" in sql:
            wanted = set(params[-1])
            self._rows = [{"name": n, "plaintext": v} for n, v in rows.items() if n in wanted]
            return
        self._rows = [{"name": n, "plaintext": v} for n, v in rows.items()]

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self) -> None:
        self.rows = dict(ROWS)
        self.fingerprint = "fp-1"
        self.executed: list[tuple[str, Any]] = []

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor(self)


@pytest.fixture
def db(monkeypatch) -> _FakeDb:
    fake = _FakeDb()

    @asynccontextmanager
    async def connection(conn: Any = None):
        yield fake

    monkeypatch.setattr(vault_db, "get_db_connection", connection)
    monkeypatch.setattr(vault_db, "encryption_configured", lambda: True)
    monkeypatch.setattr(vault_db, "_get_encryption_key", lambda: "fake-key")
    return fake


def _no_collab_token(secrets: dict[str, str]) -> None:
    assert not any(name.startswith("COLLAB_APP_TOKEN_") for name in secrets)
    assert TOKEN_VALUE not in secrets.values()


def test_app_token_names_are_withheld():
    assert vault_db.is_sandbox_withheld(app_svc.vault_secret_name("app-a"))
    assert vault_db.is_sandbox_withheld(app_svc.vault_secret_name("my-app-2"))
    assert not vault_db.is_sandbox_withheld("GITHUB_TOKEN")
    assert not vault_db.is_sandbox_withheld("MY_COLLAB_APP_TOKEN_X")


@pytest.mark.asyncio
async def test_snapshot_drops_app_tokens_and_keeps_other_secrets(db):
    secrets, fingerprint = await vault_db.get_user_vault_snapshot("u1")
    assert secrets == OTHER
    assert fingerprint == "fp-1"


@pytest.mark.asyncio
async def test_snapshot_without_encryption_drops_app_tokens(db, monkeypatch):
    monkeypatch.setattr(vault_db, "encryption_configured", lambda: False)
    secrets, fingerprint = await vault_db.get_user_vault_snapshot("u1")
    assert secrets == OTHER
    assert fingerprint == ""


@pytest.mark.asyncio
async def test_named_lookup_never_resolves_an_app_token(db):
    assert await vault_db.get_user_secrets_decrypted("u1", [TOKEN_NAME]) == {}
    # Nothing was read for a lookup that names only withheld secrets.
    assert db.executed == []
    mixed = await vault_db.get_user_secrets_decrypted("u1", [TOKEN_NAME, "GITHUB_TOKEN"])
    assert mixed == {"GITHUB_TOKEN": OTHER["GITHUB_TOKEN"]}
    assert TOKEN_NAME not in db.executed[-1][1][-1]


@pytest.mark.asyncio
async def test_redaction_still_reads_the_app_token(db):
    """The redactor's whole-vault read keeps the token so a leak is scrubbed."""
    everything = await vault_db.get_user_secrets_decrypted("u1")
    assert everything[TOKEN_NAME] == TOKEN_VALUE


# ---------------------------------------------------------------------------
# Every sandbox publish path in the computer manager
# ---------------------------------------------------------------------------


@pytest.fixture
def published(monkeypatch) -> list[dict[str, str]]:
    from ptc_agent.core.sandbox import assets

    calls: list[dict[str, str]] = []

    async def publish(sandbox: Any, secrets: Any) -> bool:
        calls.append(dict(secrets))
        return True

    monkeypatch.setattr(assets, "publish_vault_secrets", publish)
    return calls


def _mixin():
    from src.server.services.computer_manager._mcp import McpSecretsMixin

    return McpSecretsMixin()


@pytest.mark.asyncio
async def test_provisioning_snapshot_withholds_app_tokens(db):
    owner, secrets, _fp = await _mixin()._vault_snapshot("ws-1", "u1")
    assert owner == "u1"
    assert secrets == OTHER


@pytest.mark.asyncio
async def test_settle_republish_withholds_app_tokens(db, published):
    db.fingerprint = "fp-2"  # the vault moved after the first decrypt
    left = await _mixin()._settle_vault("u1", object(), dict(OTHER), "fp-1")
    assert published, "the settle loop republished"
    for secrets in published:
        _no_collab_token(secrets)
    assert left == OTHER


@pytest.mark.asyncio
async def test_push_fan_out_withholds_app_tokens(db, published, monkeypatch):
    from src.server.services.computer_manager import _mcp

    sandbox = SimpleNamespace(vault_secrets={}, runtime=None)
    mixin = _mixin()
    mixin._live_session_computer = lambda workspace_id: "computer-1"
    mixin.get_session_if_ready = lambda workspace_id, expected_sandbox_id=None: SimpleNamespace(
        sandbox=sandbox
    )

    async def identity(workspace_id: str) -> dict[str, str]:
        return {"sandbox_id": "sb-1"}

    monkeypatch.setattr(_mcp, "db_get_workspace_identity", identity)
    reached = await mixin.push_user_vault("u1", ["ws-1"])
    assert reached == 1
    assert published == [OTHER]
    assert sandbox.vault_secrets == OTHER
