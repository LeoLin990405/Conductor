"""Start a sandbox interpreter in the workspace the turn belongs to.

Ships into ``_internal/src/`` as ``sitecustomize.py``, which ``site`` imports
at interpreter startup because that directory is on the turn's PYTHONPATH.
The alternative was prepending ``os.chdir(...)`` to the submitted source,
which no provider needs to accept and which breaks the two things a real first
line owns: a ``from __future__`` import becomes a SyntaxError, and every line
number in a traceback is off by one.

Deliberately silent. A missing or unusable directory leaves the process where
the provider started it, which is the computer root, and the paths the agent's
code uses are absolute anyway.
"""

import os

#: The new name first; the legacy PTC_* alias stays readable for one release.
_TURN_CWD_ENVS = ("CONDUCTOR_TURN_CWD", "PTC_TURN_CWD")

_target = next(
    (value for value in (os.environ.get(name) for name in _TURN_CWD_ENVS) if value),
    None,
)
if _target:
    try:
        os.chdir(_target)
    except OSError:
        pass
