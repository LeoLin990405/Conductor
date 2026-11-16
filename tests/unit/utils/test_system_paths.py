import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.utils.system_paths import canonical_system_path

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases")


def _fake_lstat(uid: int, *, link: bool = True):
    mode = (stat.S_IFLNK if link else stat.S_IFDIR) | 0o755
    return lambda _path: SimpleNamespace(st_mode=mode, st_uid=uid)


@darwin_only
def test_real_tmp_and_var_aliases_are_rewritten():
    assert canonical_system_path("/tmp/x") == Path("/private/tmp/x")
    assert canonical_system_path("/var/folders/x") == Path("/private/var/folders/x")
    assert canonical_system_path(Path("/etc/hosts")) == Path("/private/etc/hosts")
    assert canonical_system_path("/tmp") == Path("/private/tmp")


@darwin_only
def test_rewritten_tmpdir_spelling_passes_the_symlink_check(tmp_path):
    alias = Path(str(tmp_path).replace("/private/var", "/var", 1))
    assert any(part.is_symlink() for part in (alias, *alias.parents))
    canonical = canonical_system_path(alias)
    assert canonical == tmp_path
    assert not any(part.is_symlink() for part in (canonical, *canonical.parents))


def test_non_alias_paths_are_unchanged():
    for raw in ("/Users/x/tmp", "/private/tmp/x", "relative/tmp", "tmp/x", "/", "/tmpx/y"):
        assert canonical_system_path(raw) == Path(raw)


def test_user_symlink_deeper_in_the_path_is_not_rewritten(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    raw = link / "state"
    # Spell it through the /var alias when tmp_path lives under /private/var.
    spelled = Path(str(raw).replace("/private/var", "/var", 1))
    canonical = canonical_system_path(spelled)
    assert canonical == canonical_system_path(spelled.parent) / "state"
    assert "link" in canonical.parts
    assert any(part.is_symlink() for part in (canonical, *canonical.parents))


def test_alias_not_owned_by_root_is_not_rewritten():
    path = canonical_system_path(
        "/tmp/x", _lstat=_fake_lstat(uid=os.getuid() or 501), _readlink=lambda _p: "private/tmp"
    )
    assert path == Path("/tmp/x")


def test_alias_pointing_elsewhere_is_not_rewritten():
    for target in ("/Users/attacker/tmp", "private/var", "/private/tmp/../var", "../private/tmp"):
        path = canonical_system_path(
            "/tmp/x", _lstat=_fake_lstat(uid=0), _readlink=lambda _p, t=target: t
        )
        assert path == Path("/tmp/x"), target


def test_alias_that_is_not_a_symlink_or_is_missing_is_not_rewritten():
    assert canonical_system_path(
        "/tmp/x", _lstat=_fake_lstat(uid=0, link=False), _readlink=lambda _p: "private/tmp"
    ) == Path("/tmp/x")

    def missing(_path):
        raise FileNotFoundError(_path)

    assert canonical_system_path("/tmp/x", _lstat=missing) == Path("/tmp/x")


def test_injected_alias_table_limits_rewrites():
    fake = {"_lstat": _fake_lstat(uid=0), "_readlink": lambda p: "/private" + p}
    assert canonical_system_path("/tmp/x", **fake) == Path("/private/tmp/x")
    assert canonical_system_path("/opt/x", **fake) == Path("/opt/x")
    assert canonical_system_path("/tmp/x", _aliases={"var"}, **fake) == Path("/tmp/x")
    assert canonical_system_path("/opt/x", _aliases={"opt"}, **fake) == Path("/private/opt/x")
