"""HOME isolation and a keychain tripwire for the collab MCP server tests.

N80's rule: HOME is a temporary directory and the real HOME is asserted
unchanged afterwards (the collab tripwire), and no test reads the real
keychain -- every token here is generated and every keychain is a fake.
"""

from __future__ import annotations

import secrets
import subprocess

import pytest

from tests.unit._isolated_home import assert_untouched, isolate


@pytest.fixture(autouse=True)
def isolated_collab_mcp_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    yield home
    assert_untouched(original, before)


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    real_run = subprocess.run

    def guarded(argv, *args, **kwargs):
        first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
        if str(first).rsplit("/", 1)[-1] == "security":
            raise AssertionError("a test reached the real keychain")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture
def token() -> str:
    return "tok_" + secrets.token_hex(20)


class FakeKeychain:
    def __init__(self, secret: str | None) -> None:
        self.secret = secret
        self.reads: list[str] = []

    def read(self, ref) -> str:
        from src.collab_mcp.client import KeychainError

        self.reads.append(str(ref))
        if self.secret is None:
            raise KeychainError(f"no token found at {ref}")
        return self.secret
