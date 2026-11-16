"""R9b L03: provider_roles lives in src/utils; provider_usage in src/conductor_cli (wheel)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_provider_modules_live_in_src_utils() -> None:
    from src.utils import provider_roles, provider_usage

    assert Path(provider_roles.__file__).parent == REPO_ROOT / "src" / "utils"
    # conductor_cli needs provider_usage in the installed wheel, so its file lives
    # there; src.utils.provider_usage is an alias of the same module object.
    assert Path(provider_usage.__file__) == REPO_ROOT / "src" / "conductor_cli" / "provider_usage.py"
    assert sys.modules["src.utils.provider_usage"] is sys.modules["src.conductor_cli.provider_usage"]


def test_ptc_agent_shims_reexport_the_same_objects() -> None:
    from ptc_agent.utils import provider_roles as old_roles
    from ptc_agent.utils import provider_usage as old_usage
    from src.utils import provider_roles, provider_usage

    assert old_roles.ProviderRoleError is provider_roles.ProviderRoleError
    assert old_roles.require_provider_role is provider_roles.require_provider_role
    assert old_roles.route_research_provider is provider_roles.route_research_provider
    assert old_usage.balance_state is provider_usage.balance_state


def test_conductor_cli_usage_imports_outside_the_checkout(tmp_path: Path) -> None:
    """The CLI runs from any cwd; it must not need the ``src.`` package prefix."""
    probe = (
        "import conductor_cli.usage, conductor_cli.codexbar_snapshot\n"
        "from datetime import datetime, timezone\n"
        "from conductor_cli.usage import _provider_headroom\n"
        "_provider_headroom({'provider': 'manus'}, datetime.now(timezone.utc))\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")
