"""Keep app setup's router registrations present without starting the app."""

from __future__ import annotations

import ast
from pathlib import Path


def test_setup_registers_router_metrics_and_dispatch_routers() -> None:
    setup_path = Path(__file__).resolve().parents[4] / "src/server/app/setup.py"
    tree = ast.parse(setup_path.read_text(encoding="utf-8"))

    imported_routers: set[str] = set()
    registered_routers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "src.server.app.router_metrics",
            "src.server.app.dispatch",
            "src.server.app.dispatch_workspaces",
        }:
            imported_routers.update(alias.asname or alias.name for alias in node.names)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "app"
            and node.func.attr == "include_router"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            registered_routers.add(node.args[0].id)

    expected = {"router_metrics_router", "dispatch_router", "dispatch_workspaces_router"}
    assert expected <= imported_routers
    assert expected <= registered_routers
