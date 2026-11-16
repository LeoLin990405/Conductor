"""Migration 146 adds one nullable, constrained structured report value."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "migrations" / "versions"


def test_it_follows_145_as_the_single_head():
    spec = importlib.util.spec_from_file_location(
        "migration_146", _VERSIONS / "146_ticket_report_git_check.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("146", "145")
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("149").down_revision == "148"
    assert script.get_revision("146").down_revision == "145"
