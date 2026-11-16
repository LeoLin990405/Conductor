"""The Worker coverage table matches the native adapter registry."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.llms.native_cli import registry

ROOT = Path(__file__).resolve().parents[4]
COVERAGE = ROOT / "docs" / "WORKER-COVERAGE.md"
HEADER = "| Model | 写入 | 审查 | 在名册中 | 安装前提 |"
REQUIRED_COLUMNS = ["Model", "写入", "审查", "在名册中", "安装前提"]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _coverage_models() -> list[str]:
    lines = [line.strip() for line in COVERAGE.read_text(encoding="utf-8").splitlines()]
    assert HEADER in lines, f"coverage table header not found in {COVERAGE}"
    header_index = lines.index(HEADER)
    assert _cells(lines[header_index]) == REQUIRED_COLUMNS

    separator = _cells(lines[header_index + 1])
    assert len(separator) == len(REQUIRED_COLUMNS)
    assert all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)

    models = []
    for line in lines[header_index + 2 :]:
        if not line.startswith("|"):
            break
        cells = _cells(line)
        assert len(cells) == len(REQUIRED_COLUMNS), line
        match = re.fullmatch(r"`([^`]+)`", cells[0])
        assert match is not None, f"model name must be backtick-quoted: {line}"
        models.append(match.group(1))
    assert models, "coverage table has no model rows"
    return models


@pytest.mark.skip(reason="docs/WORKER-COVERAGE.md is not in the public snapshot")
def test_coverage_table_matches_the_adapter_registry(tmp_path, monkeypatch) -> None:
    # The host's compat file is not part of the documented example set.
    monkeypatch.setattr("src.config.compat.Path.home", lambda: tmp_path)
    models = _coverage_models()

    assert len(models) == len(set(models)), "duplicate model in coverage table"
    assert set(models) == {row["provider"] for row in registry.list_adapters()} | {
        "qwen-compat"
    }
