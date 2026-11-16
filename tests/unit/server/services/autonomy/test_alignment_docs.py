from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]


def test_alignment_document_counts_and_key_code_references_are_current():
    document = (ROOT / "docs/SUPER-AGENT-ALIGNMENT.md").read_text()
    concept_rows = [line for line in document.splitlines() if line.startswith("| §")]
    counts = Counter(
        status
        for row in concept_rows
        for status in ("Aligned", "Partial", "Missing")
        if f"**{status}.**" in row
    )

    summary = {
        status: int(next(
            line.split("|")[2].strip()
            for line in document.splitlines()
            if line.startswith(f"| {status} |")
        ))
        for status in ("Aligned", "Partial", "Missing")
    }
    total = int(next(
        line.split("|")[2].strip().replace("**", "")
        for line in document.splitlines()
        if line.startswith("| **Total** |")
    ))
    assert {status: counts[status] for status in summary} == summary
    assert sum(counts.values()) == total == len(concept_rows)
    for reference in (
        "src/server/services/autonomy/github.py",
        "src/server/services/autonomy/feishu.py",
        "src/server/services/autonomy/security.py",
        "src/server/services/autonomy/routing.py",
        "src/server/services/autonomy/router.py",
        "src/server/services/autonomy/worker.py",
        "src/server/services/autonomy/inputs.py",
        "src/config/settings.py",
    ):
        assert (ROOT / reference).is_file()
        assert reference in document

    beyond = (ROOT / "docs/SUPER-AGENT-BEYOND.md").read_text()
    for section in ("§8.1", "§8.2", "§8.3", "§8.4"):
        assert section in beyond
