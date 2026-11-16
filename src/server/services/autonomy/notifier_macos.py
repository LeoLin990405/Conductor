"""macOS notification channel for operator alerts.

``osascript`` is the banner. It runs only on darwin, the script is one
argument (no shell), and the title and body are AppleScript string literals.
A caller injects ``runner`` so a test never starts ``osascript``.

``CONDUCTOR_NOTIFY_MACOS`` set to ``0``, ``off``, ``false``, ``no`` or
``disabled`` turns the banner off (the inbox row and badge stay). The
default runner also refuses while pytest is running a test, so a test that
forgets to inject a runner on a Mac cannot post a real banner.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

Runner = Callable[[Sequence[str]], int]

_TITLE_CHARS = 120
_BODY_CHARS = 240
_OSASCRIPT_TIMEOUT_SECONDS = 5
SWITCH_ENV = "CONDUCTOR_NOTIFY_MACOS"
_OFF = frozenset({"0", "off", "false", "no", "disabled"})


def banners_enabled(environ: object | None = None) -> bool:
    """Whether the default osascript runner may post a banner."""
    source = os.environ if environ is None else environ
    if source.get("PYTEST_CURRENT_TEST"):  # type: ignore[union-attr]
        return False
    value = str(source.get(SWITCH_ENV) or "").strip().lower()  # type: ignore[union-attr]
    return value not in _OFF


def escape_applescript(text: str) -> str:
    """Make ``text`` safe inside an AppleScript double-quoted string.

    Backslash first, then quotes, so a quote cannot close the literal and a
    backslash cannot escape the quote we add. Newlines would split the
    ``-e`` script; they become spaces. Other control characters are dropped.
    """
    cleaned: list[str] = []
    for char in str(text):
        if char in "\n\r\t":
            cleaned.append(" ")
        elif char.isprintable():
            cleaned.append(char)
    flattened = "".join(cleaned)
    return flattened.replace("\\", "\\\\").replace('"', '\\"')


def _clip(text: str, limit: int) -> str:
    flattened = " ".join(str(text).split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1].rstrip() + "…"


def _default_runner(argv: Sequence[str]) -> int:
    completed = subprocess.run(  # noqa: S603 - argv is a fixed osascript list
        list(argv),
        check=False,
        capture_output=True,
        timeout=_OSASCRIPT_TIMEOUT_SECONDS,
    )
    return int(completed.returncode)


def deliver(
    title: str,
    body: str,
    *,
    runner: Runner | None = None,
    platform: str | None = None,
) -> bool | None:
    """Show one banner.

    ``True`` when osascript exits 0, ``False`` when it fails, ``None`` when
    this is not darwin or the default runner is switched off (see
    :func:`banners_enabled`); the runner is not called then.
    """
    system = sys.platform if platform is None else platform
    if system != "darwin":
        return None
    safe_title = escape_applescript(_clip(title, _TITLE_CHARS))
    safe_body = escape_applescript(_clip(body, _BODY_CHARS))
    script = f'display notification "{safe_body}" with title "{safe_title}"'
    if runner is None and not banners_enabled():
        return None
    run = runner or _default_runner
    try:
        code = run(["osascript", "-e", script])
    except Exception:  # noqa: BLE001 - a banner must not fail the inbox write
        logger.exception("[autonomy] osascript notification failed")
        return False
    return code == 0
