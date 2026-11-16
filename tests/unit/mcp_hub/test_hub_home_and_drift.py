"""``--home`` on every ``conductor mcp`` command, dry-run import, and drift.

Every test runs with HOME pointed at a tmp dir (``isolated_home``) and a
second tmp dir passed as ``--home``, so a command that ignored ``--home``
would show up in the process HOME instead.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.mcp_hub import Registry, Server, import_existing, save_registry
from src.mcp_hub.agents import SPECS, agent_path
from src.mcp_hub.drift import drift_report
from src.mcp_hub.model import AGENT_IDS

FAKE_SECRET = "sk-" "FAKEFAKEFAKEFAKEFAKEFAKE0000"


@pytest.fixture
def homes(isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    rehearsal = tmp_path / "rehearsal"
    rehearsal.mkdir()
    return isolated_home, rehearsal


def run(argv: list[str]) -> tuple[int, object, object]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)

    def load(text: str) -> object:
        text = text.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return code, load(out.getvalue()), load(err.getvalue())


def _tree(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = path.read_bytes() if path.is_file() else None
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_cursor(home: Path) -> None:
    _write(
        home / ".cursor" / "mcp.json",
        '{\n  "mcpServers": {\n    "hand": {"command": "echo", "args": ["x"]}\n  }\n}\n',
    )


def test_every_mcp_subcommand_accepts_home() -> None:
    import argparse

    from src.conductor_cli import mcp_cli

    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    mcp_cli.register_parser(subs)
    mcp = subs.choices["mcp"]
    commands = next(
        action
        for action in mcp._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for name, child in commands.choices.items():
        flags = {flag for action in child._actions for flag in action.option_strings}
        assert "--home" in flags, name


def test_import_dry_run_writes_nothing_anywhere(homes) -> None:
    process_home, rehearsal = homes
    _seed_cursor(rehearsal)
    before = _tree(rehearsal)
    code, out, err = run(["mcp", "import", "--home", str(rehearsal), "--json"])
    assert code == 0, err
    assert out["data"]["dry_run"] is True
    assert "hand" in out["data"]["imported"]
    # Old code wrote an ``imported`` journal event (and created
    # .conductor/mcp) even on a dry run.
    assert _tree(rehearsal) == before
    assert not (rehearsal / ".conductor").exists()
    assert list(process_home.iterdir()) == []


def test_import_write_false_is_side_effect_free(isolated_home: Path) -> None:
    _seed_cursor(isolated_home)
    before = _tree(isolated_home)
    imported = import_existing(isolated_home, write=False)
    assert "hand" in imported
    assert _tree(isolated_home) == before


def test_apply_with_home_writes_only_there(homes) -> None:
    process_home, rehearsal = homes
    _seed_cursor(rehearsal)
    assert run(["mcp", "import", "--home", str(rehearsal), "--apply", "--json"])[0] == 0
    code, out, err = run(
        [
            "mcp",
            "add",
            "--name",
            "fresh",
            "--command",
            "echo",
            "--target",
            "cursor",
            "--home",
            str(rehearsal),
            "--apply",
            "--json",
        ]
    )
    assert code == 0, err
    before = _tree(rehearsal)
    code, out, err = run(["mcp", "apply", "--home", str(rehearsal), "--json"])
    assert code == 0, err
    assert "fresh" in out["data"]["cursor"]["unified_diff"]
    assert _tree(rehearsal) == before, "apply without --apply must not write"
    code, out, err = run(["mcp", "apply", "--home", str(rehearsal), "--apply", "--json"])
    assert code == 0, err
    snapshot = out["data"]["snapshot_id"]
    assert snapshot
    servers = json.loads((rehearsal / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"hand", "fresh"}
    code, out, err = run(
        ["mcp", "rollback", snapshot, "--home", str(rehearsal), "--apply", "--json"]
    )
    assert code == 0, err
    servers = json.loads((rehearsal / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"hand"}
    assert list(process_home.iterdir()) == []


def test_home_must_be_an_existing_directory(homes, tmp_path: Path) -> None:
    code, out, err = run(["mcp", "diff", "--home", str(tmp_path / "nope"), "--json"])
    assert code == 2
    assert err["errors"][0]["code"] == "usage"


def test_new_and_fixed_agent_paths() -> None:
    home = Path("/h")
    expected = {
        "claude-desktop": "Library/Application Support/Claude/claude_desktop_config.json",
        "gemini": ".gemini/settings.json",
        "antigravity": ".gemini/config/mcp_config.json",
        "qwen": ".qwen/settings.json",
        "agents": ".agents/mcp.json",
        "dsh": ".dsh/cordis.patch.yml",
        # mcode reads <MINIMAX_DATA_DIR>/mcp.json, default ~/.minimax.
        "minimax": ".minimax/mcp.json",
    }
    for agent, rel in expected.items():
        assert agent in AGENT_IDS
        assert agent_path(home, agent) == home / rel
    assert set(SPECS) == set(AGENT_IDS)


def test_generic_import_maps_native_remote_spellings(isolated_home: Path) -> None:
    _write(
        isolated_home / ".gemini" / "settings.json",
        json.dumps(
            {
                "mcpServers": {
                    "gh": {"httpUrl": "https://example.test/mcp"},
                    "slow": {"command": "slowcmd", "timeout": 30000},
                }
            }
        ),
    )
    _write(
        isolated_home / ".gemini" / "config" / "mcp_config.json",
        json.dumps(
            {
                "mcpServers": {
                    "legacy": {"serverUrl": "https://legacy.test/mcp"},
                    "off": {"command": "offcmd", "disabled": True},
                }
            }
        ),
    )
    _write(
        isolated_home / ".qwen" / "settings.json",
        json.dumps({"mcpServers": {"events": {"url": "https://sse.test/sse"}}}),
    )
    imported = import_existing(isolated_home, write=False)
    assert imported["gh"]["transport"] == "http"
    assert imported["gh"]["url"] == "https://example.test/mcp"
    assert imported["legacy"]["transport"] == "http"
    assert imported["legacy"]["url"] == "https://legacy.test/mcp"
    assert imported["events"]["transport"] == "sse"
    assert imported["off"]["enabled"] is False or (
        imported["off"]["overrides"].get("antigravity", {}).get("enabled") is False
    )


def test_drift_report_lists_missing_different_extra(isolated_home: Path) -> None:
    home = isolated_home
    _write(
        home / ".cursor" / "mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": "old-cmd", "args": []},
                    "hand": {"command": "echo", "args": []},
                }
            }
        ),
    )
    save_registry(
        home,
        Registry(
            servers={
                "shared": Server(name="shared", command="new-cmd", targets=("cursor",)),
                "wanted": Server(name="wanted", command="w", targets=("cursor",)),
            }
        ),
    )
    before = _tree(home)
    report = drift_report(home)
    assert _tree(home) == before, "drift must not write"
    cursor = report["agents"]["cursor"]
    assert cursor["missing"] == ["wanted"]
    assert cursor["different"] == [{"key": "shared", "fields": ["command"]}]
    assert cursor["extra"] == [{"key": "hand", "owned": False}]
    assert report["totals"]["missing"] == 1
    assert any(line.startswith("cursor: missing=1") for line in report["lines"])


def test_drift_flags_draft_dirs_and_secret_literals_by_path_only(
    homes,
) -> None:
    _process_home, rehearsal = homes
    draft = "/Users/someone/.zcode/workspace/draft-0000aaaa/opencat-mcp"
    _write(
        rehearsal / ".qwen" / "settings.json",
        json.dumps(
            {
                "mcpServers": {
                    "opencat": {
                        "command": "/usr/bin/uv",
                        "args": ["--directory", draft, "run", "server.py"],
                        "env": {"API_TOKEN": FAKE_SECRET, "HOST": "${HOST}"},
                    },
                    "tmpcmd": {"command": "/tmp/build/server"},
                }
            }
        ),
    )
    _write(
        rehearsal / ".config" / "opencode" / "opencode.json",
        json.dumps({"provider": {"x": {"options": {"apiKey": "abcdefghij123456"}}}}),
    )
    before = _tree(rehearsal)
    code, out, err = run(["mcp", "drift", "--home", str(rehearsal), "--json"])
    assert code == 0, err
    assert _tree(rehearsal) == before
    data = out["data"]
    qwen = data["agents"]["qwen"]
    assert {"key": "opencat", "path": "args[1]"} in qwen["fragile"]
    assert {"key": "tmpcmd", "path": "command"} in qwen["fragile"]
    assert "mcpServers.opencat.env.API_TOKEN" in qwen["secrets"]
    assert not any("HOST" in item for item in qwen["secrets"])
    assert data["agents"]["opencode"]["secrets"] == ["provider.x.options.apiKey"]
    rendered = json.dumps(out)
    assert FAKE_SECRET not in rendered
    assert "abcdefghij123456" not in rendered
    assert "draft-0000aaaa" not in rendered


def test_drift_reports_unreadable_config_as_error(isolated_home: Path) -> None:
    _write(isolated_home / ".cursor" / "mcp.json", "{not json")
    report = drift_report(isolated_home)
    assert "error" in report["agents"]["cursor"]
    assert report["totals"]["errors"] == 1


def test_dsh_long_file_key_is_skipped_not_fatal(isolated_home: Path) -> None:
    from src.mcp_hub import apply_all

    long_key = "k" * 40
    save_registry(
        isolated_home,
        Registry(
            servers={
                "longname": Server(
                    name="longname",
                    command="echo",
                    targets=("dsh",),
                    overrides={"dsh": {"name_in_file": long_key}},
                ),
                "ok": Server(name="ok", command="echo", targets=("dsh",)),
            }
        ),
    )
    result = apply_all(isolated_home, apply=True)
    skipped = result["diffs"]["dsh"]["skipped"]
    assert any(item["server"] == "longname" for item in skipped)
    text = (isolated_home / ".dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "serverName" in text and long_key not in text


def test_antigravity_disabled_entry_survives_import_and_apply(isolated_home: Path) -> None:
    from src.mcp_hub import apply_all, diff_all

    path = isolated_home / ".gemini" / "config" / "mcp_config.json"
    _write(path, '{"mcpServers": {"a": {"command": "npx", "disabled": true}}}\n')
    original = path.read_bytes()
    import_existing(isolated_home)
    view = diff_all(isolated_home)["antigravity"]
    # Old code: released=[{key: a, action: delete}] and the entry vanished.
    assert view["unified_diff"] == ""
    assert all(item["action"] != "delete" for item in view["released"])
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == original


def test_fresh_import_of_hand_entries_has_no_diff(isolated_home: Path) -> None:
    from src.mcp_hub import diff_all

    _write(
        isolated_home / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json",
        '{"mcpServers": {"bare": {"command": "rea"}}}\n',
    )
    _write(
        isolated_home / ".gemini" / "settings.json",
        json.dumps(
            {
                "mcpServers": {
                    "old": {"httpUrl": "https://h.test/mcp"},
                    "events": {"url": "https://s.test/sse"},
                    "bare": {"command": "rea"},
                }
            }
        ),
    )
    _write(
        isolated_home / ".gemini" / "config" / "mcp_config.json",
        json.dumps({"mcpServers": {"legacy": {"serverUrl": "https://l.test/mcp"}}}),
    )
    _write(isolated_home / ".agents" / "mcp.json", '{"mcpServers": {"bare": {"command": "rea"}}}')
    import_existing(isolated_home)
    diffs = diff_all(isolated_home)
    for agent in ("claude-desktop", "gemini", "antigravity", "agents"):
        assert diffs[agent]["unified_diff"] == "", agent
    report = drift_report(isolated_home)
    for agent in ("claude-desktop", "gemini", "antigravity", "agents"):
        assert report["agents"][agent]["different"] == [], agent


def test_drift_secret_heuristics_skip_variable_names_and_references(
    isolated_home: Path,
) -> None:
    _write(
        isolated_home / ".codex" / "config.toml",
        '[mcp_servers.gh]\nurl = "https://api.test/mcp"\n'
        'bearer_token_env_var = "GITHUB_TOKEN"\n'
        '[mcp_servers.gh.http_headers]\nAuthorization = "Bearer ${TOK}"\n'
        '[mcp_servers.bad.env]\nAPI_KEY = "abcdefghijklmnop"\n',
    )
    secrets = drift_report(isolated_home)["agents"]["codex"]["secrets"]
    assert secrets == ["mcp_servers.bad.env.API_KEY"]


def test_drift_does_not_call_catalog_keys_extra(isolated_home: Path) -> None:
    _write(
        isolated_home / ".cursor" / "mcp.json",
        '{"mcpServers": {"off": {"command": "x"}, "remote": {"url": "https://r.test"}}}',
    )
    save_registry(
        isolated_home,
        Registry(
            servers={
                "off": Server(name="off", command="x", targets=("cursor",), enabled=False),
            }
        ),
    )
    extra = drift_report(isolated_home)["agents"]["cursor"]["extra"]
    assert extra == [{"key": "remote", "owned": False}]


def test_drift_fragile_flag_inside_option_value(isolated_home: Path) -> None:
    _write(
        isolated_home / ".cursor" / "mcp.json",
        '{"mcpServers": {"t": {"command": "srv", "args": ["--dir=/tmp/x"], "cwd": "/x/draft-9/y"}}}',
    )
    fragile = drift_report(isolated_home)["agents"]["cursor"]["fragile"]
    assert {"key": "t", "path": "args[0]"} in fragile
    assert {"key": "t", "path": "cwd"} in fragile
