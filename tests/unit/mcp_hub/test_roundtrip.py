"""Import, in-sync diff, apply, and byte-exact rollback across the fleet."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from src.mcp_hub import (
    Registry,
    Server,
    apply_all,
    diff_all,
    import_existing,
    rollback,
    save_registry,
)
from src.mcp_hub.agents import agent_path
from src.mcp_hub.journal import ownership_from_journal, snapshot_ids, write_entry
from src.mcp_hub.store import load_registry

HAND = '{ "command" : "echo-hand-unique", "args" : ["stay"] }'
ODD_USER = '"userID" : "USER-ODD-SPACE"'
ODD_THEME = '"theme" : "SOLAR-ODD"'

TOML = """# keep-this-comment
model = "keep-model"

[mcp_servers.hand]
# hand-note
command = "echo-hand-unique"
args = ["stay"]

[profiles]
name = "keep-profile"
"""


DSH_PATCH = """# keep-dsh-comment
- id: keep-dsh-row
  config: {level: 'KEEP-DSH'}
- insert:
    - id: hand-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: hand
        transport: stdio
        command: echo-hand-unique
        args: ['stay']
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _json_with_hand(sibling: str) -> str:
    return f'{{\n  {sibling},\n  "mcpServers": {{\n    "hand": {HAND}\n  }}\n}}\n'


def _seed(home: Path) -> dict[str, bytes]:
    _write(
        home / ".claude.json",
        "{\n"
        f"  {ODD_USER},\n"
        '  "mcpServers": {\n'
        f'    "hand": {HAND}\n'
        "  },\n"
        f"  {ODD_THEME}\n"
        "}\n",
    )
    _write(home / ".cursor" / "mcp.json", _json_with_hand('"extra" : "KEEP-CURSOR"'))
    _write(home / ".kimi-code" / "mcp.json", _json_with_hand('"extra" : "KEEP-KIMI"'))
    _write(home / ".minimax" / "mcp.json", _json_with_hand('"extra" : "KEEP-MINIMAX"'))
    _write(
        home / ".minimax" / "config.yaml",
        "name: leave-this-yaml\nflag: true\n",
    )
    _write(home / ".codex" / "config.toml", TOML)
    _write(home / ".grok" / "config.toml", TOML)
    _write(home / ".stepcode" / "config.toml", TOML)
    _write(
        home / ".zcode" / "cli" / "config.json",
        "{\n"
        '  "theme" : "Z-ODD",\n'
        '  "mcp": {\n'
        '    "keep": true,\n'
        '    "servers": {\n'
        f'      "hand": {HAND}\n'
        "    }\n"
        "  }\n"
        "}\n",
    )
    _write(home / ".dsh" / "cordis.patch.yml", DSH_PATCH)
    _write(home / ".dsh" / ".credentials.yaml", "token: do-not-touch\n")
    _write(home / ".dsh" / "AGENTS.md", "# do not touch\n")
    sentinels = {
        "yaml": (home / ".minimax" / "config.yaml").read_bytes(),
        "creds": (home / ".dsh" / ".credentials.yaml").read_bytes(),
        "agents": (home / ".dsh" / "AGENTS.md").read_bytes(),
    }
    return sentinels


def _strip_hand(home: Path) -> None:
    """Release ``hand`` through a journal event. The sidecar is not consulted."""
    registry = load_registry(home)
    registry.servers.pop("hand", None)
    save_registry(home, registry)
    before, _hashes = ownership_from_journal(home)
    changed = {
        agent: names - {"hand"} for agent, names in before.items() if "hand" in names
    }
    if not changed:
        return
    taken = set(snapshot_ids(home))
    base = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    snapshot_id = base
    counter = 0
    while snapshot_id in taken:
        counter += 1
        snapshot_id = f"{base}-{counter}"
    for agent, names in changed.items():
        path = agent_path(home, agent)
        raw = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
        write_entry(
            home,
            snapshot_id,
            {
                "agent": agent,
                "target": path.relative_to(home).as_posix(),
                "pre_hash": None,
                "post_hash": hashlib.sha256(raw).hexdigest(),
                "owned": sorted(names),
                "state": "imported",
            },
        )


def _snapshot_bytes(home: Path) -> dict[str, bytes | None]:
    found: dict[str, bytes | None] = {}
    for agent, view in diff_all(home).items():
        path = Path(view["path"])
        found[agent] = path.read_bytes() if path.is_file() else None
    return found


def test_import_sync_apply_rollback(tmp_path: Path) -> None:
    sentinels = _seed(tmp_path)
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed",
                    command="uvx",
                    args=["pkg"],
                    env={"TOKEN": "${TOKEN}"},
                    targets="all",
                )
            }
        ),
    )
    apply_all(tmp_path, apply=True)
    claude = (tmp_path / ".claude.json").read_text(encoding="utf-8")
    assert ODD_USER in claude
    assert ODD_THEME in claude
    assert '"command" : "echo-hand-unique"' in claude
    codex = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "# hand-note" in codex
    assert 'model = "keep-model"' in codex
    assert 'name = "keep-profile"' in codex

    imported = import_existing(tmp_path)
    assert "hubbed" in imported
    assert "hand" in imported
    stored = (tmp_path / ".conductor" / "mcp" / "servers.toml").read_text(
        encoding="utf-8"
    )
    assert "supersecret" not in stored
    _strip_hand(tmp_path)

    in_sync = diff_all(tmp_path)
    assert all(item["unified_diff"] == "" for item in in_sync.values())

    original = _snapshot_bytes(tmp_path)
    claude_prefix = original["claude"].split(b'"mcpServers"', 1)[0]
    registry = load_registry(tmp_path)
    current = registry.servers["hubbed"]
    registry.servers["hubbed"] = Server(
        name="hubbed",
        transport=current.transport,
        command="uvx-changed",
        args=current.args,
        url=current.url,
        env=dict(current.env),
        targets=current.targets,
        overrides=dict(current.overrides),
        enabled=current.enabled,
    )
    save_registry(tmp_path, registry)
    first = diff_all(tmp_path)
    assert first["claude"]["unified_diff"]
    assert first["codex"]["unified_diff"]
    assert first["cursor"]["unified_diff"]
    assert first["kimi"]["unified_diff"]
    assert first["grok"]["unified_diff"]
    assert first["stepfun"]["unified_diff"]
    assert first["minimax"]["unified_diff"]
    assert first["dsh"]["unified_diff"]

    apply_all(tmp_path, apply=True)
    updated = (tmp_path / ".claude.json").read_bytes()
    assert updated.split(b'"mcpServers"', 1)[0] == claude_prefix
    text = updated.decode("utf-8")
    assert ODD_USER in text
    assert ODD_THEME in text
    assert '"command" : "echo-hand-unique"' in text
    assert "uvx-changed" in text
    dsh_now = (tmp_path / ".dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
    assert dsh_now.startswith(DSH_PATCH)
    assert "uvx-changed" in dsh_now
    codex_now = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "# hand-note" in codex_now
    assert "# keep-this-comment" in codex_now
    assert 'name = "keep-profile"' in codex_now
    assert (tmp_path / ".minimax" / "config.yaml").read_bytes() == sentinels["yaml"]
    assert (tmp_path / ".dsh" / ".credentials.yaml").read_bytes() == sentinels["creds"]
    assert (tmp_path / ".dsh" / "AGENTS.md").read_bytes() == sentinels["agents"]

    rollback(tmp_path, apply=True)
    restored = _snapshot_bytes(tmp_path)
    assert restored == original
    second = diff_all(tmp_path)
    assert second == first
    assert (tmp_path / ".minimax" / "config.yaml").read_bytes() == sentinels["yaml"]
    assert (tmp_path / ".dsh" / ".credentials.yaml").read_bytes() == sentinels["creds"]
    assert (tmp_path / ".dsh" / "AGENTS.md").read_bytes() == sentinels["agents"]
