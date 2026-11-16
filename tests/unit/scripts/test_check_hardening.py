"""The hardening document's references are resolved against the working tree.

Most of these tests build their own little repository under ``tmp_path`` -- a
source tree and a document -- so they check the resolver rather than whatever
the real checkout happens to contain. The last few check the repository's
own ``docs/HARDENING.md``, which is the document the check exists for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_hardening

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_DOCUMENT = REPO_ROOT / "docs" / "HARDENING.md"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A minimal repository with one module and one package module."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    # The package markers, as the real repository has: without them
    # "src.pkg.base" names nothing importable.
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text(
        "class Widget:\n"
        "    size: int = 3\n"
        "\n"
        "def helper() -> None:\n"
        "    return None\n",
        encoding="utf-8",
    )
    (root / "src" / "top.py").write_text(
        "from src.pkg.mod import Widget\n"
        "\n"
        "def build() -> Widget:\n"
        "    return Widget()\n",
        encoding="utf-8",
    )
    # A root-level file, the way a doc names one ("`config.yaml`").
    (root / "config.yaml").write_text("allowed_origins: []\n", encoding="utf-8")
    (root / "docs").mkdir()
    return root


def write_doc(repo: Path, body: str) -> Path:
    document = repo / "docs" / "HARDENING.md"
    document.write_text(body, encoding="utf-8")
    return document


def run(repo: Path, document: Path) -> int:
    return check_hardening.main([str(document), "--repo-root", str(repo)])


def test_missing_file_is_reported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "The daemon policy loader lives in `src/pkg/gone.py`.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "src/pkg/gone.py" in err
    assert "no such file" in err


def test_missing_symbol_is_reported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The named module does not exist at all."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "The daemon policy loader lives in `src/pkg/mod.py` and calls\n"
        "`absent_module.helper`.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "absent_module.helper" in err
    assert "no module or class named 'absent_module'" in err


def test_missing_attribute_on_existing_module_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The module exists; the attribute the doc names does not."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`mod.helper` exists, but `mod.absent_helper` does not.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "mod.absent_helper" in err
    assert "no attribute named 'absent_helper'" in err
    assert "src/pkg/mod.py" in err


def test_fully_resolving_doc_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "The loader is `src/pkg/mod.py`; it defines `mod.helper` and\n"
        "`Widget.size`, and `src/top.py` imports `Widget`. The origins live\n"
        "in `config.yaml`.\n",
    )

    assert run(repo, document) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "resolve" in captured.out


def test_class_attribute_resolves_including_inherited(repo: Path) -> None:
    (repo / "src" / "pkg" / "base.py").write_text(
        "class Base:\n    name: str = 'x'\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "child.py").write_text(
        "from src.pkg.base import Base\n"
        "\n"
        "class Child(Base):\n    size: int = 1\n",
        encoding="utf-8",
    )
    repo_index = check_hardening.Repo(repo)
    checker = check_hardening.Checker(repo_index)

    def reason(name: str) -> str | None:
        return checker.reason_for(
            check_hardening.Reference(raw=name, kind="symbol", chain=tuple(name.split(".")))
        )

    assert reason("Child.size") is None
    assert reason("Child.name") is None
    assert reason("Child.missing") is not None


def test_sibling_file_does_not_satisfy_a_module_attribute(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file beside a module is not an attribute of it.

    "mod.absent" is only true if mod itself binds "absent"; an absent.py
    sitting in the same directory does not make it so.
    """
    (repo / "src" / "pkg" / "absent.py").write_text("", encoding="utf-8")

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`pkg.mod.absent` is claimed, and so is `src/pkg/mod.py:absent`.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "no attribute named 'absent'" in err


def test_package_member_resolves(repo: Path) -> None:
    """A package's own members resolve through the package's __init__."""
    package = repo / "src" / "pkg"
    (package / "__init__.py").write_text(
        "def helper() -> None:\n    return None\n",
        encoding="utf-8",
    )

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`pkg.helper` is defined by the package's __init__.\n",
    )

    assert run(repo, document) == 0


def test_inheritance_resolves_through_the_imported_base(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base class is the one the module imported, not any same-named class.

    Another module's "Base" must not supply an attribute to a child that
    inherits a different "Base".
    """
    (repo / "other").mkdir()
    (repo / "other" / "base.py").write_text(
        "class Base:\n    only_elsewhere: int = 1\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "base.py").write_text(
        "class Base:\n    shared: int = 2\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "child.py").write_text(
        "from src.pkg.base import Base\n"
        "\n"
        "class Child(Base):\n    own: int = 3\n",
        encoding="utf-8",
    )

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`Child.only_elsewhere` is claimed, and so is\n"
        "`src/pkg/child.py:Child.only_elsewhere`.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "no attribute named 'only_elsewhere'" in err


def test_inherited_attribute_from_the_imported_base_resolves(repo: Path) -> None:
    """The base a module actually imported still supplies its attributes."""
    (repo / "src" / "pkg" / "base.py").write_text(
        "class Base:\n    shared: int = 2\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "child.py").write_text(
        "from src.pkg.base import Base\n"
        "\n"
        "class Child(Base):\n    own: int = 3\n",
        encoding="utf-8",
    )

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`Child.shared` is inherited from the base the module imported.\n",
    )

    assert run(repo, document) == 0


def test_aliased_import_resolves_through_the_source_name(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An aliased import names the source's name, not the alias.

    "from src.pkg.base import Base as Alias" binds "Alias" and names "Base"
    over there, so an unrelated class that happens to be called "Alias" in
    the same module must not answer for it.
    """
    (repo / "src" / "pkg" / "base.py").write_text(
        "class Base:\n    attr: int = 1\n"
        "\n"
        "class Alias:\n    bogus: int = 2\n",
        encoding="utf-8",
    )
    (repo / "src" / "consumer.py").write_text(
        "from src.pkg.base import Base as Alias\n",
        encoding="utf-8",
    )

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`consumer.Alias.attr` is claimed.\n",
    )

    assert run(repo, document) == 0

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`consumer.Alias.bogus` is claimed.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "no attribute named 'bogus'" in err


def test_import_of_a_submodule_binds_the_root_package(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """"import app.sub" binds the package, so the submodule is its attribute.

    The package itself does not gain the submodule's members.
    """
    package = repo / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text("member: int = 1\n", encoding="utf-8")
    (repo / "consumer.py").write_text("import app.sub\n", encoding="utf-8")

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`consumer.app.member` is claimed.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "no attribute named 'member'" in err

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`consumer.app.sub.member` is claimed.\n",
    )

    assert run(repo, document) == 0


def test_from_package_import_submodule_resolves(repo: Path) -> None:
    """"from app import sub" binds the submodule, even when __init__ is empty."""
    package = repo / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub.py").write_text("member: int = 1\n", encoding="utf-8")
    (repo / "consumer.py").write_text("from app import sub\n", encoding="utf-8")

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`consumer.sub.member` is claimed.\n",
    )

    assert run(repo, document) == 0


def test_circular_reexport_is_reported_not_fatal(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A circular reexport must not recurse forever.

    The reference is reported as unresolved, and the other unresolved
    references in the document are still listed.
    """
    (repo / "src" / "circ_a.py").write_text(
        "from src.circ_b import X\n",
        encoding="utf-8",
    )
    (repo / "src" / "circ_b.py").write_text(
        "from src.circ_a import X\n",
        encoding="utf-8",
    )
    (repo / "src" / "circ_c.py").write_text(
        "from src.circ_a import X\n",
        encoding="utf-8",
    )

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`circ_c.X.attr` and `circ_c.nothing_here` are claimed.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "circ_c.X.attr" in err
    assert "circ_c.nothing_here" in err
    assert "2 unresolved reference(s)" in err


def test_anchored_symbol_resolves_in_its_own_file(repo: Path) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`src/pkg/mod.py:helper()` is the entry point.\n",
    )

    assert run(repo, document) == 0


def test_anchored_symbol_with_a_single_name_is_checked(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file-anchored symbol may be one name; the file says where it lives."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`src/pkg/mod.py:absent_helper()` is claimed.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "src/pkg/mod.py:absent_helper()" in err
    assert "no attribute named 'absent_helper'" in err


def test_ambiguous_class_names_every_candidate(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two classes can share a name; the report must not hide one of them."""
    (repo / "other").mkdir()
    (repo / "other" / "widget.py").write_text("class Widget:\n    pass\n", encoding="utf-8")

    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`Widget.absent` is referenced.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "no attribute named 'absent'" in err
    assert "src/pkg/mod.py" in err
    assert "other/widget.py" in err


def test_bare_call_resolves_to_a_defined_function(repo: Path) -> None:
    """A single word written as a call names a function the tree defines."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`helper()` and `build()` exist, and so does `Widget()`.\n",
    )

    assert run(repo, document) == 0


def test_bare_call_to_a_missing_function_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Renaming a call in the doc to something undefined fails the check.

    Without the parentheses a single word is prose and is not checked, so
    the call form is the only way a doc names a bare function -- and the
    only way such a claim can rot unnoticed.
    """
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`nonexistent_helper()` is claimed.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "`nonexistent_helper()`" in err
    assert "no function or class named 'nonexistent_helper'" in err


def test_bare_call_does_not_resolve_to_a_nested_local(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A function defined inside another function is not callable by a reader."""
    (repo / "src" / "nested.py").write_text(
        "def outer() -> None:\n"
        "    def inner_only() -> None:\n"
        "        return None\n",
        encoding="utf-8",
    )
    document = write_doc(repo, "# Hardening\n\n`inner_only()` is claimed.\n")

    assert run(repo, document) == 1
    assert "inner_only" in capsys.readouterr().err


def test_missing_class_and_missing_root_file_are_reported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An invented class and an invented repo-root file both fail, together."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "`NoSuchClass.size` and `missing_config.yaml` are claimed, beside\n"
        "the real `Widget.size` and `config.yaml`.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "2 unresolved reference(s)" in err
    assert "no module or class named 'NoSuchClass'" in err
    assert "no such file: missing_config.yaml" in err


def test_non_reference_spans_are_ignored(repo: Path) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "Read `~/.config/conductor/daemon_policy.yaml`, see `-`,\n"
        "and call `http://127.0.0.1:8000/health`.\n",
    )

    assert run(repo, document) == 0


# --- short options and dot names (N69) ------------------------------------


def test_short_options_and_dot_names_are_checked(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "src" / "flags.py").write_text(
        'ARGS = ("-c", "core.fsmonitor=false")\nCACHES = {".venv"}\n',
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("x\n", encoding="utf-8")
    good = write_doc(repo, "# H\n\nPass `-c`; skip `.venv` per `.gitignore`.\n")
    assert run(repo, good) == 0
    capsys.readouterr()

    bad = write_doc(repo, "# H\n\nPass `-x`; skip `.tox` per `.dockerignore`.\n")
    assert run(repo, bad) == 1
    err = capsys.readouterr().err
    assert "3 unresolved reference(s)" in err
    assert "no option '-x'" in err
    assert "named '.tox'" in err
    assert "named '.dockerignore'" in err


def test_a_short_option_only_a_test_spells_does_not_resolve(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text('ARGS = ("-q",)\n', encoding="utf-8")
    document = write_doc(repo, "# H\n\nRun with `-q`.\n")

    assert run(repo, document) == 1


# --- spans with whitespace (should-fix 3a) ---------------------------------


@pytest.mark.parametrize(
    ("body", "culprit"),
    [
        # A command line: the old checker dropped any span with a space.
        ("Run `python src/pkg/gone.py --help` to see it.\n", "src/pkg/gone.py"),
        # A span that wraps onto the next line of its paragraph.
        ("The loader is `src/pkg/mod.py\nsrc/pkg/gone.py` here.\n", "src/pkg/gone.py"),
        # A dotted name the paragraph wrapped after its dot.
        ("It calls `pkg.mod.\nno_such_helper` first.\n", "pkg.mod.no_such_helper"),
        # A span that opens with a newline.
        ("It lives in `\nsrc/pkg/gone.py`.\n", "src/pkg/gone.py"),
        # A call written with its arguments.
        ("It calls `no_such_helper(settings)` first.\n", "no_such_helper()"),
    ],
)
def test_whitespace_inside_a_span_does_not_hide_a_reference(
    repo: Path, capsys: pytest.CaptureFixture[str], body: str, culprit: str
) -> None:
    document = write_doc(repo, "# Hardening\n\n" + body)

    assert run(repo, document) == 1

    assert culprit in capsys.readouterr().err


def test_a_command_span_whose_words_resolve_passes(repo: Path) -> None:
    (repo / "src" / "pkg" / "cli.py").write_text(
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument(\"--host\")\n",
        encoding="utf-8",
    )
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "Run `python src/pkg/cli.py --host 127.0.0.1` or\n"
        "`` src.pkg.mod.helper() `` -- `Widget(size=3)` is a call too.\n",
    )

    assert run(repo, document) == 0


def test_a_span_never_reaches_across_a_blank_line(repo: Path) -> None:
    """An unmatched backtick is literal; it does not pair with the next paragraph."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "A stray ` backtick here.\n\n"
        "And src/pkg/gone.py` in prose.\n",
    )

    assert run(repo, document) == 0


# --- line anchors (should-fix 3b) ------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "reason"),
    [
        ("src/pkg/gone.py:12", "no such file: src/pkg/gone.py"),
        ("src/pkg/gone.py:12-20", "no such file: src/pkg/gone.py"),
        ("src/pkg/gone.py:10:5", "no such file: src/pkg/gone.py"),
        ("src/pkg/gone.py#L10", "no such file: src/pkg/gone.py"),
        ("src/pkg/gone.py:not-a-name", "no such file: src/pkg/gone.py"),
        ("src/pkg/mod.py:2-99", "line 99 is past the end of the file (5 lines)"),
        ("src/pkg/mod.py:99", "line 99 is past the end of the file (5 lines)"),
        ("src/pkg/mod.py#L4-L40", "line 40 is past the end of the file (5 lines)"),
        ("src/pkg/mod.py:4-2", "line range 4-2 is empty"),
    ],
)
def test_a_line_anchor_must_name_lines_the_file_has(
    repo: Path, capsys: pytest.CaptureFixture[str], anchor: str, reason: str
) -> None:
    document = write_doc(repo, f"# Hardening\n\nThe check is at `{anchor}`.\n")

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert anchor in err
    assert reason in err


def test_a_line_anchor_inside_the_file_resolves(repo: Path) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "See `src/pkg/mod.py:1`, `src/pkg/mod.py:1-5`, `src/pkg/mod.py:4:5`\n"
        "and `src/pkg/mod.py#L2-L3`.\n",
    )

    assert run(repo, document) == 0


# --- environment names and options (should-fix 3c) -------------------------


@pytest.fixture()
def literal_repo(repo: Path) -> Path:
    """The repo, with one module that reads a variable and parses an option."""
    (repo / "src" / "pkg" / "settings.py").write_text(
        "import argparse\n"
        "import os\n"
        "\n"
        "POLICY = os.environ.get(\"CONDUCTOR_REAL_POLICY\", \"\")\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument(\"--real-flag\")\n",
        encoding="utf-8",
    )
    # A test that still spells a removed option must not keep it alive.
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text(
        "ARGS = [\"--removed-flag\", \"CONDUCTOR_REMOVED\"]\n", encoding="utf-8"
    )
    # A longer string or a comment that mentions it is not the literal either.
    (repo / "src" / "pkg" / "notes.py").write_text(
        "# --ghost-flag and CONDUCTOR_GHOST\n"
        "HELP = \"pass --ghost-flag to enable CONDUCTOR_GHOST\"\n",
        encoding="utf-8",
    )
    return repo


def test_environment_names_and_options_that_the_code_reads_resolve(
    literal_repo: Path,
) -> None:
    document = write_doc(
        literal_repo,
        "# Hardening\n\n"
        "`CONDUCTOR_REAL_POLICY` moves it, and `--real-flag` or\n"
        "`--real-flag=/tmp/x` sets it.\n",
    )

    assert run(literal_repo, document) == 0


@pytest.mark.parametrize(
    ("span", "reason"),
    [
        ("CONDUCTOR_REAL_POLICYX", "no environment variable 'CONDUCTOR_REAL_POLICYX'"),
        ("CONDUCTOR_REMOVED", "no environment variable 'CONDUCTOR_REMOVED'"),
        ("CONDUCTOR_GHOST", "no environment variable 'CONDUCTOR_GHOST'"),
        ("--no-such-flag", "no option '--no-such-flag'"),
        ("--removed-flag", "no option '--removed-flag'"),
        ("--ghost-flag", "no option '--ghost-flag'"),
        ("cond up --no-such-flag", "no option '--no-such-flag'"),
    ],
)
def test_environment_names_and_options_the_code_never_spells_fail(
    literal_repo: Path, capsys: pytest.CaptureFixture[str], span: str, reason: str
) -> None:
    document = write_doc(literal_repo, f"# Hardening\n\nSet `{span}` to change it.\n")

    assert run(literal_repo, document) == 1

    assert reason in capsys.readouterr().err


# --- fences (should-fix 3d) ------------------------------------------------


def test_an_unclosed_tilde_fence_is_reported(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stray fence would hide every claim after it; that is an error, not a pass."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "~~~\n"
        "## Later section\n\n"
        "The loader lives in `src/pkg/gone.py`.\n",
    )

    assert run(repo, document) == 1

    err = capsys.readouterr().err
    assert "HARDENING.md:3: `~~~`" in err
    assert "unclosed code fence" in err


def test_a_backtick_fence_does_not_close_a_tilde_fence(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a run of the opener's own character closes a fence."""
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "~~~\n"
        "```\n"
        "~~~\n\n"
        "The loader lives in `src/pkg/gone.py`.\n",
    )

    assert run(repo, document) == 1

    assert "no such file: src/pkg/gone.py" in capsys.readouterr().err


def test_a_shorter_run_does_not_close_a_longer_fence(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "````\n"
        "```\n"
        "````\n\n"
        "It calls `no_such_helper()`.\n",
    )

    assert run(repo, document) == 1

    assert "no function or class named 'no_such_helper'" in capsys.readouterr().err


def test_a_closed_tilde_fence_is_skipped(repo: Path) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "~~~~ text\n"
        "`src/pkg/gone.py` is only an example here\n"
        "~~~~~\n\n"
        "The real one is `src/pkg/mod.py`.\n",
    )

    assert run(repo, document) == 0


# --- the repository's own document ----------------------------------------


def test_the_real_document_exists() -> None:
    """The check has a document to check."""
    assert REAL_DOCUMENT.is_file()


def test_the_real_document_claims_references() -> None:
    """The document is not empty of claims, so the check is not vacuous.

    Without this a document with no backticked names would pass every
    resolution test without resolving anything.
    """
    references = check_hardening.extract_references(
        REAL_DOCUMENT.read_text(encoding="utf-8")
    )

    assert len(references) >= 20


def test_the_real_document_resolves_against_this_tree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every reference in the document names something this tree contains.

    This is the promise the document makes to its reader: the files and
    symbols it names are the ones that implement what it describes.
    """
    assert check_hardening.main([str(REAL_DOCUMENT), "--repo-root", str(REPO_ROOT)]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "resolve" in captured.out


def test_the_real_document_fails_when_it_names_a_symbol_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A claim about a symbol the code does not have fails the check.

    The real document is copied and one claim is broken on purpose. A
    resolver that accepted a name it cannot back up would let this pass, so
    this test fails on such a resolver.
    """
    document = tmp_path / "HARDENING.md"
    document.write_text(
        REAL_DOCUMENT.read_text(encoding="utf-8")
        + "\nA helper that does not exist: `ops_runtime.no_such_helper`.\n",
        encoding="utf-8",
    )

    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    err = capsys.readouterr().err
    assert "ops_runtime.no_such_helper" in err
    assert "no attribute named 'no_such_helper'" in err


def test_the_real_document_fails_when_it_names_a_file_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A claim about a file the tree does not have fails the check."""
    document = tmp_path / "HARDENING.md"
    document.write_text(
        REAL_DOCUMENT.read_text(encoding="utf-8")
        + "\nA policy module that was never here: `src/conductor_cli/sandbox_policy.py`.\n",
        encoding="utf-8",
    )

    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    err = capsys.readouterr().err
    assert "src/conductor_cli/sandbox_policy.py" in err
    assert "no such file" in err


def test_the_real_document_fails_when_it_names_a_member_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A claim about a class member the class does not have fails the check.

    The module and the class are both real here; only the member is
    invented, which is the case a path-only check would miss.
    """
    document = tmp_path / "HARDENING.md"
    document.write_text(
        REAL_DOCUMENT.read_text(encoding="utf-8")
        + "\nA setting that was never added: `Settings.no_such_setting`.\n",
        encoding="utf-8",
    )

    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    err = capsys.readouterr().err
    assert "Settings.no_such_setting" in err
    assert "no attribute named 'no_such_setting'" in err


@pytest.mark.parametrize(
    ("original", "mutated", "reason"),
    [
        (
            "`ops_runtime.backend_argv`",
            "`ops_runtime.nonexistent_backend_argv`",
            "no attribute named 'nonexistent_backend_argv'",
        ),
        (
            "`Settings.backend_host`",
            "`NoSuchClass.backend_host`",
            "no module or class named 'NoSuchClass'",
        ),
        (
            "`server.py` itself",
            "`missing_server.py` itself",
            "no such file: missing_server.py",
        ),
    ],
)
def test_the_real_document_fails_when_a_claim_is_mutated(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    original: str,
    mutated: str,
    reason: str,
) -> None:
    """Breaking one real claim in place -- not appending one -- fails the check.

    These are the mutations a reviewer used to show the first checker
    agreeing with a wrong document: a renamed helper, an invented class
    holding a real attribute name, and a missing repository-root file.
    """
    text = REAL_DOCUMENT.read_text(encoding="utf-8")
    assert original in text
    document = tmp_path / "HARDENING.md"
    document.write_text(text.replace(original, mutated), encoding="utf-8")

    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    err = capsys.readouterr().err
    assert mutated.split()[0].strip("`") in err
    assert reason in err


def test_the_real_document_checks_bare_calls_against_this_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`backend_argv()` is real; `nonexistent_backend_argv()` is not."""
    document = tmp_path / "HARDENING.md"
    document.write_text(
        REAL_DOCUMENT.read_text(encoding="utf-8")
        + "\nThe backend starts from `backend_argv()`.\n",
        encoding="utf-8",
    )
    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 0
    capsys.readouterr()

    document.write_text(
        REAL_DOCUMENT.read_text(encoding="utf-8")
        + "\nThe backend starts from `nonexistent_backend_argv()`.\n",
        encoding="utf-8",
    )
    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    err = capsys.readouterr().err
    assert "no function or class named 'nonexistent_backend_argv'" in err


@pytest.mark.parametrize(
    "claim",
    [
        # ops_static.main serves through SimpleHTTPRequestHandler with no
        # containment, so a symlink in the build directory escapes it.
        "no directory escape",
        # PolicyStore resolves the configured path, so symlinks are followed.
        "It may not\nbe a symlink",
        # The daemon loads the device's trusted-keys file, not the example.
        "beside the old one in\n`config/daemon_trusted_keys.example.json`",
        # doctor._file_check never looks at the owner or the contents.
        "rather than only at whether it exists",
    ],
)
def test_the_real_document_drops_claims_the_code_does_not_back(claim: str) -> None:
    """Claims a review showed to be false stay out of the document.

    The reference check proves a name exists, not that the sentence around
    it is true; these were true-sounding sentences about real names.
    """
    assert claim not in REAL_DOCUMENT.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("original", "mutated", "reason"),
    [
        (
            "`git_delivery.is_hardened_git_invocation`",
            "`git_delivery.is_hardened_git_invocationx`",
            "no attribute named 'is_hardened_git_invocationx'",
        ),
        (
            "`docker_worker_runtime._docker_git_runner`",
            "`docker_worker_runtime._docker_git_runnerx`",
            "no attribute named '_docker_git_runnerx'",
        ),
        (
            "`CONDUCTOR_WORKSPACE_POLICY`",
            "`CONDUCTOR_WORKSPACE_POLICYX`",
            "no environment variable 'CONDUCTOR_WORKSPACE_POLICYX'",
        ),
        (
            "`--trusted-keys`",
            "`--trusted-keyz`",
            "no option '--trusted-keyz'",
        ),
        (
            "`src.conductor_cli.ops_cli up --foreground`",
            "`src.conductor_cli.ops_clix up --foreground`",
            "no attribute named 'ops_clix'",
        ),
        (
            "## 5. Where the operator",
            "~~~\n## 5. Where the operator",
            "unclosed code fence",
        ),
    ],
)
def test_the_real_document_catches_the_round_two_mutations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    original: str,
    mutated: str,
    reason: str,
) -> None:
    """The git section, env names, options, command spans and fences are all checked."""
    text = REAL_DOCUMENT.read_text(encoding="utf-8")
    assert original in text
    document = tmp_path / "HARDENING.md"
    document.write_text(text.replace(original, mutated, 1), encoding="utf-8")

    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    assert reason in capsys.readouterr().err


# --- round three: GIT_ names, bare code names, links (should-fix 4) --------


def test_git_environment_names_are_checked_like_conductor_ones(
    literal_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (literal_repo / "src" / "pkg" / "gitenv.py").write_text(
        'ENV = {"GIT_REAL_SETTING": "1"}\n', encoding="utf-8"
    )
    document = write_doc(literal_repo, "# Hardening\n\nIt sets `GIT_REAL_SETTING`.\n")
    assert run(literal_repo, document) == 0
    capsys.readouterr()

    document = write_doc(literal_repo, "# Hardening\n\nIt sets `GIT_REAL_SETTINGZ`.\n")
    assert run(literal_repo, document) == 1
    assert "no environment variable 'GIT_REAL_SETTINGZ'" in capsys.readouterr().err


@pytest.fixture()
def names_repo(repo: Path) -> Path:
    """The repo, with the kinds of name a doc mentions without qualifying."""
    (repo / "src" / "pkg" / "names.py").write_text(
        "from http.server import SimpleHTTPRequestHandler\n"
        "\n"
        "DEFAULT_POLICY_PATH = 'x'\n"
        "\n"
        "def load_settings() -> None:\n"
        "    return None\n"
        "\n"
        "class PolicyStore:\n"
        "    backend_host: str = '127.0.0.1'\n"
        "\n"
        "    def _read_snapshot(self) -> str:\n"
        "        # ghost_helper is only mentioned in this comment\n"
        "        return {'git_check': 'policy_refused'}['git_check']\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "ops_tool.py").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "helpers.py").write_text(
        "def test_only_helper() -> None:\n    return None\n", encoding="utf-8"
    )
    return repo


def test_bare_code_names_that_the_code_has_resolve(names_repo: Path) -> None:
    document = write_doc(
        names_repo,
        "# Hardening\n\n"
        "`load_settings` reads it into `PolicyStore`, whose `backend_host` and\n"
        "`_read_snapshot` matter; `DEFAULT_POLICY_PATH` is the default, `ops_tool`\n"
        "the module, `SimpleHTTPRequestHandler` the server, and a refusal reads\n"
        "`git_check: policy_refused`. Plain words such as `missing`, `ok` and\n"
        "`Settings` are prose.\n",
    )

    assert run(names_repo, document) == 0


@pytest.mark.parametrize(
    "span",
    [
        "load_settingz",
        "PolicyStorez",
        "_read_snapshotz",
        "test_only_helper",
        "ghost_helper",
        "git_check: policy_refusedz",
    ],
)
def test_bare_code_names_the_code_does_not_have_fail(
    names_repo: Path, capsys: pytest.CaptureFixture[str], span: str
) -> None:
    document = write_doc(names_repo, f"# Hardening\n\nSee `{span}`.\n")

    assert run(names_repo, document) == 1

    culprit = span.split()[-1]
    err = capsys.readouterr().err
    assert f"`{culprit}`" in err
    assert f"no definition, module or string constant named '{culprit}'" in err


def test_links_to_files_that_exist_resolve(repo: Path) -> None:
    document = write_doc(
        repo,
        "# Hardening\n\n"
        "See [the module](../src/pkg/mod.py), [its lines](../src/pkg/mod.py#L2-L3),\n"
        "[the site](https://example.com/x.py), [below](#later) and\n"
        "![a picture](../config.yaml \"title\"). A span `[x](nope.py)` is no link.\n\n"
        "[def]: ../src/top.py\n",
    )

    assert run(repo, document) == 0


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        ("[gone](../src/pkg/gone.py)", "no such file: ../src/pkg/gone.py"),
        # Relative to the document (docs/), not to the repository root.
        ("[mod](src/pkg/mod.py)", "no such file: src/pkg/mod.py"),
        ("[`gone.py`](../src/pkg/gone.py)", "no such file: ../src/pkg/gone.py"),
        ("[lines](../src/pkg/mod.py#L99)", "line 99 is past the end of the file (5 lines)"),
        ("[dir](../src/nope/)", "no such directory: ../src/nope/"),
        ("[g]: ../src/pkg/gone.py", "no such file: ../src/pkg/gone.py"),
    ],
)
def test_links_to_files_that_do_not_exist_fail(
    repo: Path, capsys: pytest.CaptureFixture[str], link: str, reason: str
) -> None:
    document = write_doc(repo, f"# Hardening\n\nSee below.\n\n{link}\n")

    assert run(repo, document) == 1

    assert reason in capsys.readouterr().err


def test_the_real_document_names_the_hidden_attribute_refusal() -> None:
    text = REAL_DOCUMENT.read_text(encoding="utf-8")

    assert "`git_delivery._refuse_hidden_attribute_files`" in text
    # The check proves names exist, never that the code does what is said.
    assert "proves only that the" in text
    assert "it does not prove" in text


@pytest.mark.parametrize(
    ("original", "mutated", "reason"),
    [
        (
            "`git_delivery._refuse_hidden_attribute_files`",
            "`git_delivery._refuse_hidden_attribute_filez`",
            "no attribute named '_refuse_hidden_attribute_filez'",
        ),
        (
            "`GIT_NO_REPLACE_OBJECTS`",
            "`GIT_NO_REPLACE_OBJECTZ`",
            "no environment variable 'GIT_NO_REPLACE_OBJECTZ'",
        ),
        (
            "`load_settings` sets",
            "`load_settingz` sets",
            "named 'load_settingz'",
        ),
        (
            "`git_check: unsupported_runtime`",
            "`git_check: unsupported_runtimez`",
            "named 'unsupported_runtimez'",
        ),
        (
            "## 7. Tests",
            "See [the runtime](../src/conductor_cli/nope_runtime.py).\n\n## 7. Tests",
            "no such file: ../src/conductor_cli/nope_runtime.py",
        ),
    ],
)
def test_the_real_document_catches_the_round_three_mutations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    original: str,
    mutated: str,
    reason: str,
) -> None:
    text = REAL_DOCUMENT.read_text(encoding="utf-8")
    assert original in text
    document = tmp_path / "HARDENING.md"
    document.write_text(text.replace(original, mutated, 1), encoding="utf-8")

    assert check_hardening.main([str(document), "--repo-root", str(REPO_ROOT)]) == 1

    assert reason in capsys.readouterr().err
