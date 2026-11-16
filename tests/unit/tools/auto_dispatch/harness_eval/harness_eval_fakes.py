"""Import the harness-eval test helpers as a normal module.

Pytest's default import mode does not treat this directory as a package, so a
relative ``conftest`` import fails at collection.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "conftest.py"
_SPEC = importlib.util.spec_from_file_location("harness_eval_conftest_impl", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load {_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

BLOCKER_TOKEN = _MODULE.BLOCKER_TOKEN
DISPATCH_ID = _MODULE.DISPATCH_ID
EXPERIMENT_ID = _MODULE.EXPERIMENT_ID
LINE_ID = _MODULE.LINE_ID
MODEL = _MODULE.MODEL
REPO_ROOT = _MODULE.REPO_ROOT
REVIEWER = _MODULE.REVIEWER
SHORT_AGENTS = _MODULE.SHORT_AGENTS
VARIANT_ID = _MODULE.VARIANT_ID
VERDICT_TOKEN = _MODULE.VERDICT_TOKEN
WORKER_TOKEN = _MODULE.WORKER_TOKEN
RecordingRunner = _MODULE.RecordingRunner
ScriptedBackend = _MODULE.ScriptedBackend
ScriptedCommands = _MODULE.ScriptedCommands
World = _MODULE.World
block_reply = _MODULE.block_reply
budget_spec = _MODULE.budget_spec
build_world = _MODULE.build_world
commit_file = _MODULE.commit_file
dispatch_document = _MODULE.dispatch_document
dummy_script = _MODULE.dummy_script
experiment_document = _MODULE.experiment_document
git = _MODULE.git
init_repo = _MODULE.init_repo
line_dict = _MODULE.line_dict
make_record = _MODULE.make_record
pass_reply = _MODULE.pass_reply
source_fingerprint = _MODULE.source_fingerprint
write_tree = _MODULE.write_tree
