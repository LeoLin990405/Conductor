"""Static PTC usage audit on a small Conductor-shaped fixture.

Covers reachability, transitive chains, ancestor package initializers,
namespace packages, deferred imports, web route parameters, unreached modules
and importers, syntax errors, and the ``--write`` / ``--check`` contract. No
fixture module is ever imported.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.ptc_usage_audit import (
    GEN_END,
    GEN_START,
    audit,
    client_routes,
    main,
    render_markdown,
    render_summary,
)

T = "src/ptc_agent/"

FILES = {
    "src/__init__.py": "",
    # --- target package -------------------------------------------------
    "src/ptc_agent/__init__.py": "from ptc_agent.config import load\n",
    "src/ptc_agent/config/__init__.py": "from .load import load\n",
    "src/ptc_agent/config/load.py": "def load():\n    return 1\n",
    "src/ptc_agent/agent/__init__.py": "",
    "src/ptc_agent/agent/turn.py": "from ..utils.kind import kind\n",
    "src/ptc_agent/agent/bad.py": "def broken(\n",
    "src/ptc_agent/agent/tools/__init__.py": "",
    "src/ptc_agent/agent/tools/todo.py": "from ptc_agent.far.mid import step\n",
    "src/ptc_agent/far/__init__.py": "",
    "src/ptc_agent/far/mid.py": "from ptc_agent.far.end import done\n",
    "src/ptc_agent/far/end.py": "def done():\n    return 1\n",
    "src/ptc_agent/utils/__init__.py": "",
    "src/ptc_agent/utils/kind.py": "def kind():\n    return 2\n",
    "src/ptc_agent/utils/roles.py": "ROLES = ()\n",
    # namespace package (no __init__.py) with a module and an attribute import
    "src/ptc_agent/nsp/leaf.py": "VALUE = 3\n",
    "src/ptc_agent/lazy/__init__.py": "",
    "src/ptc_agent/lazy/heavy.py": "HEAVY = 1\n",
    "src/ptc_agent/typing_only/__init__.py": "",
    "src/ptc_agent/typing_only/types.py": "class T: ...\n",
    "src/ptc_agent/orphan/__init__.py": "",
    "src/ptc_agent/orphan/mod.py": "def unused():\n    return 0\n",
    "src/ptc_agent/sealed/__init__.py": "",
    "src/ptc_agent/sealed/vault.py": "SECRET = 1\n",
    # dynamic import by string, and files shipped into a sandbox by path
    "src/ptc_agent/plug/__init__.py": (
        "from importlib import import_module\n"
        "_P = {'x': 'ptc_agent.plug.impl:Impl'}\n"
        "def get(k):\n"
        "    m, _, n = _P[k].partition(':')\n"
        "    return getattr(import_module(m), n)\n"
    ),
    "src/ptc_agent/plug/impl.py": "class Impl: ...\n",
    "src/ptc_agent/plug/ship.py": (
        "from pathlib import Path\n"
        "SRC = Path(__file__).with_name('runtime')\n"
        "ONE = Path(__file__).parent / 'runtime' / 'main.py'\n"
        "OTHER = Path('elsewhere') / '__init__.py'\n"
    ),
    "src/ptc_agent/plug/runtime/main.py": "print(1)\n",
    # --- entry groups ---------------------------------------------------
    # dispatch: types.py imports nothing from the target; its ancestor
    # package src/tools/__init__.py does.
    "src/tools/__init__.py": "from ptc_agent.agent.tools.todo import step\n",
    "src/tools/auto_dispatch/__init__.py": "",
    "src/tools/auto_dispatch/types.py": "X = 1\n",
    "src/server/__init__.py": "",
    "src/server/app/__init__.py": "",
    "src/server/services/__init__.py": "",
    "src/server/services/dispatch.py": (
        "def ready():\n    from ptc_agent.lazy.heavy import HEAVY\n    return HEAVY\n"
    ),
    "src/server/services/autonomy/__init__.py": "",
    "src/server/services/autonomy/router.py": (
        "from typing import TYPE_CHECKING\n"
        "from ptc_agent.agent.turn import kind\n"
        "if TYPE_CHECKING:\n"
        "    from ptc_agent.typing_only.types import T\n"
    ),
    "src/conductor_cli/__init__.py": "",
    "src/conductor_cli/ops_cli.py": "def main():\n    return 0\n",
    "src/conductor_cli/cli.py": "import ptc_agent.utils\n",
    "src/conductor_cli/plugins.py": "from ptc_agent.plug import get, ship\n",
    "src/llms/native_cli/__init__.py": "",
    "src/llms/native_cli/spec.py": "import ptc_agent.nsp.leaf\nfrom ptc_agent.nsp import leaf as again\n",
    "src/server/services/native_runtime/__init__.py": "",
    "src/server/services/native_runtime/usage.py": "from ptc_agent.nsp import ATTR\n",
    # web API
    "src/server/app/constants.py": 'PREFIX = "/api/v1/providers"\n',
    "src/server/app/providers.py": (
        "from fastapi import APIRouter\n"
        "from src.server.app.constants import PREFIX\n"
        "from ptc_agent.utils.roles import ROLES\n"
        "router = APIRouter(prefix=PREFIX)\n"
        '@router.get("/{provider}/models")\n'
        "def models(provider: str):\n"
        "    return ROLES\n"
        '@router.get("")\n'
        "def providers():\n"
        "    return []\n"
    ),
    "src/server/app/icons.py": (
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/api/v1/mcp")\n'
        'ICON_ROUTE = "/api/v1/mcp/server-icons/"\n'
        '@router.get("/server-icons/{handle}")\n'
        "def icon(handle: str):\n"
        "    return handle\n"
        "def server_icon_url(handle):\n"
        '    return f"{ICON_ROUTE}{handle}"\n'
    ),
    "src/server/app/hidden.py": (
        "from fastapi import APIRouter\n"
        "from ptc_agent.orphan.mod import unused\n"
        "router = APIRouter()\n"
        '@router.get("/api/v1/hidden")\n'
        "def hidden():\n"
        "    return unused()\n"
    ),
    "src/server/services/broken.py": "def oops(\nfrom ptc_agent.sealed.vault import SECRET\n",
    # TUI fork
    "libs/ptc-cli/ptc_cli/__init__.py": "",
    "libs/ptc-cli/ptc_cli/slash.py": "from ptc_agent.far.end import done\nfrom ptc_cli import helper\n",
    "libs/ptc-cli/ptc_cli/helper.py": "",
    "libs/ptc-cli/tests/test_x.py": "from ptc_agent.sealed.vault import SECRET\n",
    # web client
    "web/src/api/providers.ts": (
        "// api.get('/api/v1/commented-out')\n"
        "export const list = (provider: string, lazy: boolean) => {\n"
        "  const qs = lazy ? '?lazy=1' : '';\n"
        "  api.get<Models>(`/api/v1/providers/${provider}/models${qs}`);\n"
        "  return api.get('/api/v1/providers');\n"
        "};\n"
        "export const gone = () => fetch('/api/v1/nowhere');\n"
    ),
    "web/src/lib/brandArt.ts": "export const art = (s) => ({ src: `${baseURL}${s.icon_url}` });\n",
    "web/src/components/BrandMark.tsx": (
        "import { art } from '@/lib/brandArt';\n"
        "export const Mark = (p) => <img src={p.art.src} />;\n"
    ),
    "web/src/api/providers.test.ts": "api.get('/api/v1/hidden');\n",
    "scripts/cond": '#!/bin/sh\nexec "$interp" -m src.conductor_cli.ops_cli "$@"\n',
    "pyproject.toml": '[project]\nname = "x"\n[project.scripts]\nconductor = "conductor_cli.cli:main"\n',
}


def _build(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    for rel, text in {**FILES, **(extra or {})}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _group(report, name):
    return next(group for group in report.groups if group.group == name)


def _row(report, name):
    return next(row for row in report.rows if row.name == name)


def test_audit_never_imports_the_fixture(tmp_path: Path):
    root = _build(tmp_path)
    before = set(sys.modules)
    audit(root)
    assert not {
        name
        for name in set(sys.modules) - before
        if name.split(".")[0] in {"ptc_agent", "ptc_cli"}
    }


def test_entry_starts_with_its_ancestor_package_inits(tmp_path: Path):
    report = audit(_build(tmp_path))
    dispatch = _group(report, "dispatch")
    # types.py imports nothing, but loading it runs src/tools/__init__.py.
    assert T + "agent/tools/todo.py" in dispatch.reached
    assert dict(dispatch.per_entry)["src/tools/auto_dispatch/types.py"] > 0
    # the only direct import is the function-local one in services/dispatch.py
    assert [rel for rel, _ in dispatch.direct] == [T + "lazy/heavy.py"]
    assert "src/tools/__init__.py" in dict(dispatch.gateways)


def test_transitive_chain_is_followed_and_shown(tmp_path: Path):
    report = audit(_build(tmp_path))
    dispatch = _group(report, "dispatch")
    assert T + "far/end.py" in dispatch.reached  # todo -> far.mid -> far.end
    chain = next(c for c in _row(report, "far").chains if c.group == "dispatch")
    assert chain.hops[-1] in {T + "far/__init__.py", T + "far/mid.py", T + "far/end.py"}
    assert "src/tools/__init__.py" in chain.hops


def test_relative_imports_and_package_inits(tmp_path: Path):
    report = audit(_build(tmp_path))
    router = _group(report, "router_ticket")
    # router -> agent.turn -> (relative) ..utils.kind, and ptc_agent/__init__ -> config
    for rel in (
        "agent/turn.py",
        "utils/kind.py",
        "utils/__init__.py",
        "__init__.py",
        "config/load.py",
    ):
        assert T + rel in router.reached, rel
    # TYPE_CHECKING imports are still direct dependencies
    assert [rel for rel, _ in router.direct] == [
        T + "agent/turn.py",
        T + "typing_only/types.py",
    ]
    # named-only closure skips the package initializers
    assert set(router.named) == {
        T + "agent/turn.py",
        T + "utils/kind.py",
        T + "typing_only/types.py",
    }
    assert T + "config/load.py" in router.own


def test_namespace_package_keeps_ancestor_inits(tmp_path: Path):
    report = audit(_build(tmp_path))
    native = _group(report, "native_worker")
    assert T + "nsp/leaf.py" in native.reached
    # ``from ptc_agent.nsp import ATTR`` names no module, yet ptc_agent/__init__ runs.
    usage = dict(native.per_entry)["src/server/services/native_runtime/usage.py"]
    assert usage >= 3  # ptc_agent/__init__, config/__init__, config/load
    assert _row(report, "nsp").reached_files == 1


def test_bare_package_import_loads_the_package(tmp_path: Path):
    report = audit(_build(tmp_path))
    cli = _group(report, "cli")
    assert T + "utils/__init__.py" in cli.reached
    assert T + "__init__.py" in cli.reached
    assert "src/conductor_cli/ops_cli.py" in cli.entries
    assert "src/conductor_cli/cli.py" in cli.entries


def test_deferred_imports_are_reached_but_not_loaded(tmp_path: Path):
    report = audit(_build(tmp_path))
    dispatch = _group(report, "dispatch")
    assert T + "lazy/heavy.py" in dispatch.reached
    assert T + "lazy/heavy.py" not in dispatch.loaded
    router = _group(report, "router_ticket")
    assert T + "typing_only/types.py" in router.reached
    assert T + "typing_only/types.py" not in router.loaded
    assert T + "agent/turn.py" in router.loaded


def test_unreached_modules_and_importers(tmp_path: Path):
    report = audit(_build(tmp_path))
    assert T + "orphan/mod.py" in report.unreached_modules
    assert T + "sealed/vault.py" in report.unreached_modules
    assert (
        "src/server/app/hidden.py",
        (T + "orphan/mod.py",),
    ) in report.unreached_importers
    # a route requested only from a test file does not select the module
    assert "src/server/app/hidden.py" not in _group(report, "web_api").entries


def test_syntax_errors_are_skipped_and_reported(tmp_path: Path):
    report = audit(_build(tmp_path))
    failed = {item.path: item for item in report.syntax_errors}
    assert set(failed) == {T + "agent/bad.py", "src/server/services/broken.py"}
    assert failed[T + "agent/bad.py"].lineno == 1
    # broken.py's import is not followed
    assert T + "sealed/vault.py" not in report.reached_any
    assert "syntax errors skipped: 2" in render_summary(report)


def test_web_routes_keep_path_params_and_drop_query_suffixes():
    text = (
        "const qs = lazy ? '?lazy=1' : '';\n"
        "api.get(`/api/v1/providers/${provider}/models${qs}`);\n"
        "api.post(`/api/v1/x/${id ?? ''}/y`);\n"
        "/* fetch('/api/v1/comment') */\n"
    )
    assert client_routes(text) == {"/api/v1/providers/{}/models", "/api/v1/x/{}/y"}


def test_web_route_mapping_selects_servers(tmp_path: Path):
    report = audit(_build(tmp_path))
    mapping = {(m.request, m.server) for m in report.route_matches}
    assert ("/api/v1/providers/{}/models", "src/server/app/providers.py") in mapping
    assert ("/api/v1/providers", "src/server/app/providers.py") in mapping
    icon = next(
        m for m in report.route_matches if m.request == "/api/v1/mcp/server-icons/{}"
    )
    assert icon.server == "src/server/app/icons.py"
    assert set(icon.clients) == {
        "web/src/lib/brandArt.ts",
        "web/src/components/BrandMark.tsx",
    }
    assert [u.request for u in report.unmatched] == ["/api/v1/nowhere"]
    web = _group(report, "web_api")
    assert set(web.entries) == {
        "src/server/app/icons.py",
        "src/server/app/providers.py",
    }
    assert T + "utils/roles.py" in web.reached


def test_tui_group_and_direct_importer_counts(tmp_path: Path):
    report = audit(_build(tmp_path))
    tui = _group(report, "tui")
    assert "libs/ptc-cli/ptc_cli/slash.py" in tui.entries
    assert not any("tests" in path for path in tui.entries)
    assert [rel for rel, _ in tui.direct] == [T + "far/end.py"]
    far = _row(report, "far")
    # importers outside the package only: slash.py (end); todo.py is inside.
    assert far.direct_importers == ("libs/ptc-cli/ptc_cli/slash.py",)
    assert "tui" in far.direct_groups
    outside = dict(report.outside_importers)
    assert outside[T + "utils/roles.py"] == ("src/server/app/providers.py",)


def test_every_reached_module_is_listed_in_both_formats(tmp_path: Path):
    report = audit(_build(tmp_path))
    summary = render_summary(report)
    markdown = render_markdown(report)
    for group in report.groups:
        for rel in group.reached:
            short = rel[len(T) :]
            assert short in summary, (group.group, rel)
            assert short in markdown, (group.group, rel)
    assert str(tmp_path) not in markdown  # reproducible: no absolute paths


def test_row_split_by_size(tmp_path: Path):
    big = "\n".join(f"V{i} = {i}" for i in range(30)) + "\n"
    root = _build(
        tmp_path, {T + "agent/huge/a.py": big, T + "agent/huge/__init__.py": ""}
    )
    report = audit(root, split_loc=20, max_depth=2)
    names = {row.name for row in report.rows}
    assert {"agent/*.py", "agent/huge", "agent/tools", "far"} <= names
    assert _row(report, "agent/huge").loc == 30
    whole = audit(root, split_loc=10_000)
    assert "agent" in {row.name for row in whole.rows}
    assert sum(row.loc for row in whole.rows) == whole.total_loc


def test_write_keeps_hand_written_text_and_check_detects_stale(tmp_path: Path, capsys):
    root = _build(tmp_path)
    doc = tmp_path / "audit.md"
    doc.write_text(
        f"# Report\n\nintro\n\n{GEN_START}\nold\n{GEN_END}\n\nanalysis\n",
        encoding="utf-8",
    )
    assert main(["--root", str(root), "--check", str(doc)]) == 1
    assert main(["--root", str(root), "--write", str(doc)]) == 0
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("# Report\n\nintro\n\n" + GEN_START)
    assert text.rstrip().endswith("analysis")
    assert "old" not in text
    assert main(["--root", str(root), "--check", str(doc)]) == 0
    main(["--root", str(root), "--write", str(doc)])
    assert doc.read_text(encoding="utf-8") == text  # idempotent
    out = capsys.readouterr().out
    assert "PTC usage audit" in out


def test_missing_root_or_target_exits(tmp_path: Path, capsys):
    assert main(["--root", str(tmp_path / "nope")]) == 2
    root = _build(tmp_path)
    assert main(["--root", str(root), "--target", "src/missing"]) == 2
    assert "not found" in capsys.readouterr().err


def test_dynamic_imports_and_shipped_files_are_reached_not_loaded(tmp_path: Path):
    report = audit(_build(tmp_path))
    cli = _group(report, "cli")
    assert T + "plug/impl.py" in cli.reached
    assert T + "plug/impl.py" not in cli.loaded
    assert T + "plug/runtime/main.py" in cli.reached
    assert dict(report.dynamic) == {T + "plug/__init__.py": (T + "plug/impl.py",)}
    assert dict(report.shipped) == {T + "plug/ship.py": (T + "plug/runtime/main.py",)}
    # dynamic edges are not imports by name: own need stays static
    assert T + "plug/impl.py" not in cli.named
    assert T + "plug/impl.py" not in report.unreached_modules
