"""One durable record per shadow-mode decision (N58 rollout measurement, T4).

A dispatch check that ships in ``shadow`` records on the line or dispatch
snapshot what ``enforce`` would have done, but a snapshot is not a stream: a
rollout review cannot count records across lines, and the store keeps no
judgement that a promoted mode would have acted on. This module writes one
decision per judgement in two places:

* one JSON object per line in ``~/.conductor/logs/shadow-decisions.jsonl``,
  the durable record T4 counts. The file is appended, created ``0600``,
  rotated at UTC midnight and kept for :data:`BACKUP_DAYS` days (45), so a
  judgement sampled today still exists after the backend log has rotated it
  away twice over. ``ts`` (ISO 8601 UTC) is added to every record; the other
  keys are the same ``key=value`` fields the text line carries, values
  tokenised the same way:
  ``{"ts": "...", "mode": "review_repro", "would_change": true,
  "subject": "line-7", "current": "block", "enforced": "merge", ...}``.
* one text ``WARNING`` line in the caller's log (the backend log). Kept on
  purpose: operators already tail ``backend.log`` and grep it for
  ``shadow_decision`` while watching a rollout, and one line per judgement is
  negligible next to the backend's own volume; the JSONL file is what
  survives to be counted. The two carry the same fields, so a sampled text
  line can be matched to its record by ``subject``/``reason``.

**Denominator (口径) table** -- T4 counts its denominator from this table, so
each mode must log exactly what the table says and nothing else:

========================  =====================================================  =========================================
mode                      logs one line per                                   does not log
========================  =====================================================  =========================================
``review_repro``          every BLOCKed verdict whose blockers N51 evaluated    a PASSed verdict (nothing to judge), and
                          in shadow, both when some are downgraded and when all   off/enforce
                          are reproducible. ``would_change`` is true only when
                          no reproducible blocker is left (enforce would MERGE)
``silent_worker``         every classified silent Worker run in shadow           a run that is not silent, an
                                                                                  already-handled run, off/enforce
``dedup``                 every ``decide()`` in shadow that found a ledger entry  a key with no ledger entry (new work,
                          (reusable or not, forced or not); ``would_change`` is    empty key), off
                          true only when enforce would have reused it
``review_strict``         every ``record_attempt`` in shadow (a valid,           off/enforce
                          unparseable or ``would_inconclusive`` reply);
                          ``would_change`` is true only for
                          ``would_inconclusive``
========================  =====================================================  =========================================

``would_change`` says enforce would have produced a different *result* (for a
review: the merge/park outcome; for a dispatch: whether it ran again; for a
worker: whether the line moved). A line with ``would_change=false`` is still a
judgement: it is the denominator -- how many times the check looked -- against
which the changed ones are counted, and ``current``/``enforced`` say why
enforce would have agreed. ``subject`` is the line/ticket id a person samples
to find the judgement again (never a ``file:line``; that lives in ``location``)
and the optional extras are the rest of the context.

Every string value is a single token: whitespace becomes ``_``, ``=`` becomes
``:``, and a value longer than :data:`MAX_VALUE_CHARS` is cut (a log line, not
a record). Boolean and numeric extras keep their JSON type in the record and
are rendered like the text line otherwise. That cap is why only the first 120
characters of one blocker are recorded -- the review's other text is never
written here. A missing value is ``-``.

No caller may fail because of this module: :func:`shadow_decision` catches
every exception, writes one ``debug`` line and lets the decision stand.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

#: The word every shadow decision line starts with, for ``grep``.
PREFIX = "shadow_decision"

#: The most characters one ``key=value`` carries, so one decision is one line.
MAX_VALUE_CHARS = 120

#: The token a missing value gets.
MISSING = "-"

#: The dedicated file logger; it never propagates into the backend log.
LOGGER_NAME = "conductor.shadow"

#: The JSONL file name inside the log directory.
FILE_NAME = "shadow-decisions.jsonl"

#: Full path override, for a caller that does not use the Conductor log dir.
PATH_ENV = "CONDUCTOR_SHADOW_LOG_PATH"

#: How many rotated daily files are kept: 45 days, a 4x margin over the 7 days
#: T4 needs, because rotation only advances on a day a decision is written.
BACKUP_DAYS = 45

_MARKER = "_conductor_shadow_path"

_ATTACH_LOCK = threading.Lock()

_file_logger = logging.getLogger(LOGGER_NAME)
# JSON records must not leak into the backend log through the ``conductor``
# root logger: the backend log is for the text line, this handler for JSONL.
_file_logger.propagate = False
_file_logger.setLevel(logging.INFO)

_debug = logging.getLogger(__name__)


def shadow_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the JSONL record lives: the override, or the CLI's log dir.

    ``CONDUCTOR_LOG_DIR`` and ``CONDUCTOR_HOME`` are the same knobs
    ``cond`` resolves ``backend.log`` under, so the two streams sit together.
    """
    env = os.environ if environ is None else environ
    override = str(env.get(PATH_ENV, "") or "").strip()
    if override:
        return Path(override).expanduser()
    log_dir = str(env.get("CONDUCTOR_LOG_DIR", "") or "").strip()
    if log_dir:
        return Path(log_dir).expanduser() / FILE_NAME
    home = str(env.get("CONDUCTOR_HOME", "") or "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".conductor"
    return root / "logs" / FILE_NAME


class _ShadowFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily-rotated JSONL handler whose file (and every rotation) is ``0600``.

    ``FileHandler`` opens the file again after each rollover, so chmod happens
    in ``_open`` instead of once at startup: the first file, every
    ``shadow-decisions.jsonl.YYYY-MM-DD`` archive and the fresh file after a
    rollover all stay owner-only.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            str(path),
            when="midnight",
            backupCount=BACKUP_DAYS,
            utc=True,
            encoding="utf-8",
            delay=False,
        )

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:  # pragma: no cover - the file already exists via the open
            pass
        return stream


def _attach_file_handler(path: Path) -> logging.Logger:
    """Attach the JSONL handler once per path; a different path replaces it.

    Attaching is idempotent by the marker attribute, not by a module flag: a
    second import of this module (or a reload) sees the live handler on the
    shared ``conductor.shadow`` logger and does not add a second one, so one
    decision can never be written twice.
    """
    target = str(path)
    with _ATTACH_LOCK:
        for handler in list(_file_logger.handlers):
            if getattr(handler, _MARKER, None) == target:
                return _file_logger
        for handler in list(_file_logger.handlers):
            if getattr(handler, _MARKER, None) is not None:
                _file_logger.removeHandler(handler)
                handler.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = _ShadowFileHandler(path)
        setattr(handler, _MARKER, target)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _file_logger.addHandler(handler)
        return _file_logger


def _reset_handlers() -> None:
    """Drop the JSONL handler (tests use it to move the file). Production has no caller."""
    with _ATTACH_LOCK:
        for handler in list(_file_logger.handlers):
            _file_logger.removeHandler(handler)
            handler.close()


def _token(value: object) -> str:
    text = "_".join(str(value if value is not None else "").split())
    text = text.replace("=", ":")[:MAX_VALUE_CHARS]
    return text or MISSING


def _text_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return _token(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def shadow_decision(
    logger: logging.Logger,
    mode: object,
    *,
    would_change: bool,
    subject: object,
    current: object,
    enforced: object,
    reason: object = "",
    **extra: object,
) -> str:
    """Record one shadow judgement and return the text line (tests assert on it).

    ``mode`` names the check (``review_repro``, ``silent_worker``, ``dedup``,
    ``review_strict``); ``current`` is what happens today and ``enforced`` what
    ``enforce`` would do instead. Extra ``key=value`` fields follow in the
    order given, in both the JSON record and the text line. ``subject`` is the
    line/ticket id; a ``file:line`` belongs in an extra named ``location``.
    The mode is passed as a value, not a logger prefix, so every mode lands in
    the same stream.
    """
    fields: dict[str, object] = {
        "mode": _token(mode),
        "would_change": bool(would_change),
        "subject": _token(subject),
        "current": _token(current),
        "enforced": _token(enforced),
    }
    if reason:
        fields["reason"] = _token(reason)
    for name, value in extra.items():
        # Numbers and booleans keep their JSON type; everything else is the
        # same single token the text line shows.
        fields[_token(name)] = value if isinstance(value, (bool, int, float)) else _token(value)
    line = " ".join([PREFIX, *(f"{name}={_text_value(value)}" for name, value in fields.items())])
    try:
        _attach_file_handler(shadow_log_path()).info(
            json.dumps({"ts": _utc_now(), **fields}, ensure_ascii=False)
        )
        logger.warning(line)
    except Exception:  # noqa: BLE001 - a log line must never break the decision
        _debug.debug("[shadow_log] could not record %s", line, exc_info=True)
    return line


__all__ = [
    "BACKUP_DAYS",
    "FILE_NAME",
    "LOGGER_NAME",
    "MAX_VALUE_CHARS",
    "MISSING",
    "PATH_ENV",
    "PREFIX",
    "shadow_decision",
    "shadow_log_path",
]
