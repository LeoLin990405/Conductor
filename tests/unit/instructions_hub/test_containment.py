"""Instructions hub: a planned file must stay inside the isolated HOME.

A path under the HOME is only inside it while its ancestors stay there.
``<home>/.agents`` can be a symlink that points elsewhere, and a literal
prefix under the HOME would not notice that. Reading the file the CLI
would really read would hand out content the hub was never pointed at, so
the hub refuses the path instead.

The same rule covers the library: ``<home>/.conductor`` and the fragment
and profile folders under it can be symlinks too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.instructions_hub.core import HubError
from src.instructions_hub.fragments import load_fragments
from src.instructions_hub.profiles import load_profiles
from src.instructions_hub.render import render
from src.instructions_hub.store import diff_one, library_root, status
from src.instructions_hub.targets import TARGETS, target_path

SECRET = "SECRET EXTERNAL CONTENT\n"


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory outside the isolated HOME, with a file in it."""
    target = tmp_path / "outside"
    target.mkdir()
    (target / "AGENTS.md").write_text(SECRET, encoding="utf-8")
    return target


def test_a_symlinked_agents_directory_is_refused(
    home: Path, outside: Path
) -> None:
    # <home>/.agents points outside, so the file the CLI would read is not
    # under the HOME at all.
    (home / ".agents").symlink_to(outside)
    with pytest.raises(ValueError, match="inside the isolated HOME"):
        target_path(next(t for t in TARGETS if t.name == "kimi"), home)


def test_status_does_not_report_a_file_outside_the_home(
    home: Path, outside: Path
) -> None:
    # The row must not claim that the outside file exists, and the other
    # targets must still report.
    (home / ".agents").symlink_to(outside)
    rows = {row["target"]: row for row in status(home)["targets"]}
    kimi = rows["kimi"]
    assert "inside the isolated HOME" in kimi["error"]
    assert kimi["matches"] is False
    # A target that does not load the shared file still reports.
    assert rows["zcode"]["files"]
    assert rows["zcode"]["matches"] is False


def test_diff_does_not_expose_a_file_outside_the_home(
    home: Path, outside: Path
) -> None:
    # The outside content must not appear in the diff.
    (home / ".agents").symlink_to(outside)
    with pytest.raises(ValueError, match="inside the isolated HOME"):
        diff_one(home, "operator", "kimi")


def test_the_outside_content_never_reaches_the_output(
    home: Path, outside: Path
) -> None:
    # Whatever the command reports, the external file's content is not in
    # it. This is the property the leak broke.
    (home / ".agents").symlink_to(outside)
    rows = {row["target"]: row for row in status(home)["targets"]}
    assert SECRET not in repr(rows)
    with pytest.raises(ValueError):
        diff_one(home, "operator", "kimi")


def test_a_symlinked_agents_directory_is_refused_for_every_shared_target(
    home: Path, outside: Path
) -> None:
    # Kimi and DSH share .agents/AGENTS.md, and both must refuse it.
    (home / ".agents").symlink_to(outside)
    for target in ("kimi", "dsh"):
        with pytest.raises(ValueError, match="inside the isolated HOME"):
            render("operator", target, home=home)


def test_a_symlinked_default_library_is_refused(home: Path, outside: Path) -> None:
    # <home>/.conductor points outside, so the default library is not under
    # the HOME.
    (home / ".conductor").symlink_to(outside)
    with pytest.raises(HubError, match="inside the isolated HOME"):
        library_root(home)


def test_a_symlinked_rendered_body_is_refused(home: Path, outside: Path) -> None:
    # The body the Claude stub names lives under <home>/.conductor, so a
    # symlinked .conductor would send it outside the HOME.
    (home / ".conductor").symlink_to(outside)
    with pytest.raises(HubError, match="inside the isolated HOME"):
        render("operator", "claude", home=home)


def test_a_symlinked_fragments_folder_is_refused(home: Path, outside: Path) -> None:
    # The fragments folder is a symlink, so each file in it is a real file
    # somewhere else.
    library = home / ".conductor" / "instructions"
    (library / "profiles").mkdir(parents=True)
    (library / "fragments").symlink_to(outside)
    (outside / "leak.md").write_text(SECRET, encoding="utf-8")
    with pytest.raises(HubError, match="must stay inside the library"):
        load_fragments(library)


def test_a_symlinked_profiles_folder_is_refused(home: Path, outside: Path) -> None:
    library = home / ".conductor" / "instructions"
    (library / "fragments").mkdir(parents=True)
    (library / "profiles").symlink_to(outside)
    (outside / "leak.toml").write_text('fragments = []\n', encoding="utf-8")
    with pytest.raises(HubError, match="must stay inside the library"):
        load_profiles(library)


def test_a_library_outside_the_home_can_still_be_named(
    tmp_path: Path, home: Path
) -> None:
    # An explicit --library is the operator's own choice and is not under
    # the HOME, so it stays allowed. Only the default library is pinned.
    library = tmp_path / "elsewhere"
    (library / "fragments").mkdir(parents=True)
    (library / "fragments" / "ok.md").write_text("# Ok\n", encoding="utf-8")
    loaded = load_fragments(library)
    assert loaded["ok"] == "# Ok\n"
