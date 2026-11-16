#!/usr/bin/env python3
"""Write a sanitized local CodexBar quota snapshot."""

from __future__ import annotations

import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root))
sys.path.insert(0, str(_repo_root / "src"))

from src.conductor_cli.codexbar_snapshot import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
