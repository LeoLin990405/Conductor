"""R9b L07: paths, project_context and credit_gate moved out of src/ptc_agent."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_modules_live_at_their_new_paths() -> None:
    from src.server.services import credit_gate, project_context
    from src.utils import paths

    assert Path(paths.__file__) == REPO_ROOT / "src" / "utils" / "paths.py"
    services = REPO_ROOT / "src" / "server" / "services"
    assert Path(project_context.__file__) == services / "project_context.py"
    assert Path(credit_gate.__file__) == services / "credit_gate.py"


def test_paths_constants_do_not_depend_on_the_file_location() -> None:
    """The move is one directory shallower; nothing in paths.py may derive from it."""
    from src.utils import paths

    source = Path(paths.__file__).read_text(encoding="utf-8")
    assert "__file__" not in source
    assert paths.DEFAULT_SANDBOX_ROOT == "/home/workspace"
    assert paths.SANDBOX_ROOTS == ("/home/workspace", "/home/daytona")
    assert paths.LEGACY_ROOT_DIRS == ("code", "tools")
    assert paths.MEMORY_INDEX_FILENAME == "memory.md"
    assert paths.MEMO_INDEX_FILENAME == "memo.md"
    assert paths.DEFAULT_LAYOUT == paths.SandboxLayout.default()


def test_ptc_agent_shims_reexport_the_same_objects() -> None:
    from ptc_agent.agent import middleware as old_middleware
    from ptc_agent.agent.middleware import credit_gate as old_gate
    from ptc_agent.core import paths as old_paths
    from ptc_agent.core import project_context as old_context
    from src.server.services import credit_gate, project_context
    from src.utils import paths

    assert old_paths.SandboxLayout is paths.SandboxLayout
    assert old_paths.WorkspaceLayout is paths.WorkspaceLayout
    assert old_paths.logged_path is paths.logged_path
    assert old_paths.DEFAULT_LAYOUT is paths.DEFAULT_LAYOUT

    assert old_context.ProjectContext is project_context.ProjectContext
    assert old_context.run_with_project is project_context.run_with_project
    # One ContextVar, or a project set through one path is invisible to the other.
    assert old_context._current is project_context._current
    assert old_context.__all__ == project_context.__all__

    assert old_gate.CreditLease is credit_gate.CreditLease
    assert old_gate.CreditStopError is credit_gate.CreditStopError
    assert old_gate.current_credit_gate is credit_gate.current_credit_gate
    assert old_gate.run_with_credit_gate is credit_gate.run_with_credit_gate
    assert old_middleware.CreditGateMiddleware is credit_gate.CreditGateMiddleware


def test_credit_gate_port_uses_the_moved_module() -> None:
    from src.server.services import credit_gate, credit_gate_port

    assert credit_gate_port.CreditLease is credit_gate.CreditLease
    assert credit_gate_port.CreditGateState is credit_gate.CreditGateState


def test_any_cwd_entry_points_import_outside_the_checkout(tmp_path: Path) -> None:
    """The TUI and conductor_cli run from any cwd, where ``src`` is not importable."""
    probe = (
        "import conductor_cli.cli\n"
        "import ptc_cli.commands.slash, ptc_cli.input.completers\n"
        "import sys\n"
        "assert 'src.utils.paths' not in sys.modules, 'src prefix leaked'\n"
        "assert ptc_cli.commands.slash.DEFAULT_SANDBOX_ROOT == '/home/workspace'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")
