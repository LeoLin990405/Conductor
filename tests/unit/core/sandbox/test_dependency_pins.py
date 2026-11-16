"""Guards the hand-mirrored sandbox dependency list.

``DEFAULT_DEPENDENCIES`` drives the Daytona snapshot, the E2B template and the no-snapshot
install; ``Dockerfile.sandbox`` re-spells the same list because a Docker build
cannot import Python. Nothing enforced the mirror, so the two images could
drift into different toolchains for the same workspace — the failure surfaces
as a mysterious ``ModuleNotFoundError`` in one provider only.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

from ptc_agent.core.sandbox._defaults import (
    DEFAULT_DEPENDENCIES,
    SANDBOX_PLAYWRIGHT_VERSION,
)

_ROOT = Path(__file__).resolve().parents[4]
_DOCKERFILE = _ROOT / "Dockerfile.sandbox"
_PYPROJECT = _ROOT / "pyproject.toml"

_ARG_RE = re.compile(r"^ARG\s+(\w+)=(\S+)", re.MULTILINE)
_SUBST_RE = re.compile(r"\$\{(\w+)\}")


def _dockerfile_args() -> dict[str, str]:
    """``ARG NAME=default`` declarations, which the install lines interpolate."""
    return dict(_ARG_RE.findall(_DOCKERFILE.read_text()))


def _expand(text: str, args: dict[str, str]) -> str:
    return _SUBST_RE.sub(lambda m: args.get(m.group(1), m.group(0)), text)


def _logical_lines(text: str) -> list[str]:
    """Shell lines with backslash continuations folded into one string."""
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        lines.append(buffer + line)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def _dockerfile_install_packages() -> list[str]:
    """Package arguments of ``Dockerfile.sandbox``'s ``uv pip install``."""
    command = next(
        line for line in _logical_lines(_DOCKERFILE.read_text()) if "uv pip install" in line
    )
    tokens = shlex.split(_expand(command, _dockerfile_args()))
    start = tokens.index("install") + 1
    end = tokens.index("&&", start)
    packages: list[str] = []
    for token in tokens[start:end]:
        if not token.startswith("-"):
            packages.append(token)
    return packages


def test_dockerfile_mirrors_default_dependencies():
    packages = _dockerfile_install_packages()
    assert set(packages) == set(DEFAULT_DEPENDENCIES)
    assert len(packages) == len(set(packages)), "duplicate package in Dockerfile.sandbox"


def test_both_playwright_ports_ride_one_version():
    """Each language port resolves its browser revision from its own version
    number, so a drift between the npm and Python pins bakes a second Chromium
    (plus a second headless shell) into the image instead of sharing one."""
    args = _dockerfile_args()
    assert args["PLAYWRIGHT_VERSION"] == SANDBOX_PLAYWRIGHT_VERSION
    assert f"playwright=={SANDBOX_PLAYWRIGHT_VERSION}" in DEFAULT_DEPENDENCIES

    npm_line = next(
        line
        for line in _logical_lines(_DOCKERFILE.read_text())
        if "npm install -g" in line
    )
    assert f"playwright@{SANDBOX_PLAYWRIGHT_VERSION}" in _expand(npm_line, args)


def test_mcp_sdk_pin_is_exact_and_mirrored():
    """Host and sandbox MCP clients must use the patched SDK release."""
    root_dependencies = tomllib.loads(_PYPROJECT.read_text())["project"]["dependencies"]
    root_mcp = next(dep for dep in root_dependencies if dep.startswith("mcp"))
    sandbox_mcp = next(dep for dep in DEFAULT_DEPENDENCIES if dep.startswith("mcp"))
    assert root_mcp == sandbox_mcp == "mcp==2.3.0"


def test_no_range_specifiers_in_default_dependencies():
    """The list is joined into a shell command, where ``<`` becomes a redirect."""
    offenders = [d for d in DEFAULT_DEPENDENCIES if "<" in d or ">" in d]
    assert offenders == []
