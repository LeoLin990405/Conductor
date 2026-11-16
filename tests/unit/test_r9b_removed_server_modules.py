"""R9b L13 guard: owner decision 7 removed five unreachable server modules."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

REMOVED_L13 = [
    "src.server.handlers.chat.agent_change",
    "src.server.handlers.thread_maintenance",
    "src.server.services.refactor.native_memory",
    "src.server.services.share_redaction",
    "src.server.ssh_gateway",
]


@pytest.mark.parametrize("dotted", REMOVED_L13)
def test_decision_7_module_is_gone(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None


def test_compose_has_no_ssh_gateway_service() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "ssh-gateway" not in compose["services"]
    assert "src.server.ssh_gateway" not in (REPO_ROOT / "docker-compose.yml").read_text(
        encoding="utf-8"
    )


def test_env_example_has_no_ssh_gateway_block() -> None:
    assert "SSH_GATEWAY_" not in (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
