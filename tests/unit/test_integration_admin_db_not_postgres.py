"""CI guard: integration scratch-DB admin connections must not use ``postgres``.

Scratch fixtures connect to a maintenance database to CREATE/DROP the temporary
DB. ``postgres`` is the production database name this suite refuses elsewhere
(``tests/integration/db_guard.py``); admin URIs must use ``template1`` instead.
"""

from __future__ import annotations

from pathlib import Path

INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
FORBIDDEN = '_uri("postgres")'


def test_integration_admin_db_is_not_postgres():
    hits: list[str] = []
    for path in sorted(INTEGRATION.glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN in line:
                hits.append(f"{path.name}:{number}: {line.strip()}")
    assert not hits, (
        "Integration admin DB must use _uri(\"template1\"), not _uri(\"postgres\"):\n"
        + "\n".join(hits)
    )
