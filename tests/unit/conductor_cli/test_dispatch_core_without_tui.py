"""Core CLI must start when the optional TUI package is not installed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VECTORS = ROOT / "tests/fixtures/dispatch_timeline_vectors_v1.json"

_PROBE = r"""
import importlib
import json
import pkgutil
import sys
from pathlib import Path

class _BlockPTC:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ptc_cli" or fullname.startswith("ptc_cli."):
            raise ModuleNotFoundError("simulated absent optional cli extra: " + fullname)

sys.meta_path.insert(0, _BlockPTC())
sys.modules["ptc_cli"] = None

import conductor_cli

imported = []
for module in pkgutil.walk_packages(conductor_cli.__path__, conductor_cli.__name__ + "."):
    if module.name.endswith(".__main__"):
        continue
    importlib.import_module(module.name)
    imported.append(module.name)
print("imported", len(imported))
for name in (
    "conductor_cli.cli",
    "conductor_cli.client",
    "conductor_cli.dispatch",
    "conductor_cli.dispatch_timeline",
    "conductor_cli.output",
):
    if name not in sys.modules:
        raise SystemExit("missing " + name)

from conductor_cli.cli import build_parser, main
from conductor_cli.dispatch import render_status

def run_help(argv):
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    if code not in (0, None):
        raise SystemExit("help failed " + " ".join(argv) + " " + repr(code))
    print("help", " ".join(argv))

run_help(["--help"])
run_help(["status", "--help"])
run_help(["dispatch", "--help"])

parser = build_parser()
args, unknown = parser.parse_known_args(["dispatch", "status", "d3adbeef", "--json"])
if args.command != "dispatch" or list(args.args) != ["status", "d3adbeef"] or unknown != ["--json"]:
    raise SystemExit("json parse failed " + repr((args.command, list(args.args), unknown)))
print("json dispatch status")

status_args, status_unknown = parser.parse_known_args(["status", "--json"])
if status_args.command != "status" or status_unknown != ["--json"]:
    raise SystemExit("status json parse failed")
print("json status")

vectors = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["vectors"]
record = next(item["record"] for item in vectors if item["id"] == "w0-cancel")
summary = {"id": "d3adbeef", "lines": [{"id": "line-0"}], "gates": {"lines": {"line-0": record}}}
text = "\n".join(render_status(summary))
if "G6 人工  已取消" not in text:
    raise SystemExit("render_status missed the cancelled human gate")
primary = next(line for line in text.splitlines() if "主审" in line)
if "当前" in primary:
    raise SystemExit("render_status marked primary current")
print("render_status")
"""


def test_core_cli_runs_without_the_optional_tui() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, str(VECTORS)],
        cwd=ROOT,
        # The probe asserts the zh labels; the locale picks the language.
        env={**os.environ, "LC_ALL": "zh_CN.UTF-8"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "help --help" in completed.stdout
    assert "help status --help" in completed.stdout
    assert "json dispatch status" in completed.stdout
    assert "json status" in completed.stdout
    assert "render_status" in completed.stdout
    assert "ptc_cli" not in completed.stderr
