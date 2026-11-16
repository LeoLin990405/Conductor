"""The small command-line entry point the launch script calls.

It is a module of its own on purpose. ``python -m src.llms.native_cli.failure``
would execute ``failure`` twice: importing the package already loads it (the
adapters import it to register their rules), and running it as ``__main__``
then runs the module a second time under a second name -- which Python warns
about on stderr, on every single Worker failure. A module that is *not*
imported by the package has no such problem.

The classifier itself, its vocabulary and its rules stay in ``failure.py``;
this file only turns them into a command.
"""

from __future__ import annotations

from src.llms.native_cli.failure import main


if __name__ == "__main__":
    raise SystemExit(main())
