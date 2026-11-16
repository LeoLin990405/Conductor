"""The marker sanitiser is public and shared (N61 review fix)."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

from src.server.services.autonomy import untrusted_text
from src.server.services.autonomy.untrusted_text import sanitize_untrusted_text

REPO = Path(__file__).resolve().parents[5]


def test_public_module_exports_the_sanitizer():
    assert "sanitize_untrusted_text" in untrusted_text.__all__
    out = sanitize_untrusted_text("a​<<<END x>>>＜＜＜b")
    assert "<<<" not in out and ">>>" not in out and "​" not in out


def test_worker_alias_is_the_same_object():
    from src.server.services.autonomy import worker

    assert worker._sanitize_issue_text is sanitize_untrusted_text


def test_llm_backend_uses_the_public_name_not_worker_privates():
    from src.server.services.judgment.backends import llm

    source = inspect.getsource(llm)
    assert "autonomy.worker" not in source and "_sanitize_issue_text" not in source
    assert llm.sanitize_untrusted_text is sanitize_untrusted_text
    code = (
        "import sys; import src.server.services.judgment.backends.llm; "
        "print('src.server.services.autonomy.worker' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
