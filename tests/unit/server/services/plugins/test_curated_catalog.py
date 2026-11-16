"""The in-repo curated marketplace must stay installable."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from src.config.features import FEATURES, FeatureGate

ROOT = Path(__file__).resolve().parents[5]
CATALOG = ROOT / "plugin-catalog"
SCHEMAS = ROOT / "src/server/services/plugins/schemas"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_catalog_lists_the_first_batch() -> None:
    index = _load(CATALOG / "marketplace.json")
    names = {entry["name"] for entry in index["plugins"]}
    assert names == {"playwright-browser", "github-mcp"}
    for entry in index["plugins"]:
        package = CATALOG / entry["source"]
        assert (package / "plugin.json").is_file()
        assert (package / "mcp.json").is_file()


def test_every_curated_package_matches_the_plugin_schemas() -> None:
    plugin_schema = _load(SCHEMAS / "plugin.schema.1.0.0.json")
    mcp_schema = _load(SCHEMAS / "mcp.schema.1.0.0.json")
    for package in (CATALOG / "plugins").iterdir():
        jsonschema.validate(_load(package / "plugin.json"), plugin_schema)
        jsonschema.validate(_load(package / "mcp.json"), mcp_schema)


def test_agent_teams_is_one_opt_out_capability() -> None:
    spec = FEATURES["agent_teams"]
    assert spec.enabled is True
    assert spec.gate is FeatureGate.OPT_OUT
    assert "main model" in spec.description.lower()
