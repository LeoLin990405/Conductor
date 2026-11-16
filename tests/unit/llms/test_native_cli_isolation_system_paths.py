"""worker_isolation accepts the macOS /var and /tmp alias spellings (P0 sandbox)."""

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from src.llms.native_cli.base import NativeCliUnavailable
from src.llms.native_cli.isolation import read_workspace_policy, worker_isolation

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases")


def _alias(path: Path) -> Path:
    text = str(path)
    for name in ("var", "tmp"):
        if text.startswith(f"/private/{name}/"):
            return Path(text.replace(f"/private/{name}", f"/{name}", 1))
    pytest.skip("tmp_path is not under a macOS system alias")


def test_state_root_spelled_through_var_alias_is_accepted(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    root = _alias(tmp_path) / "state"
    assert any(part.is_symlink() for part in (root, *root.parents))
    with worker_isolation(
        workspace_id="alias-var", root=root, workdir=work, env_names=set()
    ) as isolation:
        assert isolation.home.is_relative_to(tmp_path / "state")
    assert (tmp_path / "state").is_dir()


def test_state_root_under_slash_tmp_is_accepted(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    holder = Path("/tmp") / f"conductor-p0-{uuid.uuid4().hex}"
    try:
        with worker_isolation(
            workspace_id="alias-tmp", root=holder / "state", workdir=work, env_names=set()
        ) as isolation:
            assert isolation.home.is_relative_to(Path("/private") / holder.relative_to("/"))
    finally:
        shutil.rmtree(Path("/private") / holder.relative_to("/"), ignore_errors=True)


def test_state_root_under_gettempdir_spelling_is_accepted(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    holder = Path(tempfile.mkdtemp(prefix="conductor-p0-", dir=tempfile.gettempdir()))
    spelled = _alias(holder.resolve())
    try:
        with worker_isolation(
            workspace_id="alias-gettempdir", root=spelled / "state", workdir=work, env_names=set()
        ):
            pass
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def test_user_symlink_in_state_root_still_refused(tmp_path):
    work = tmp_path / "workspace"
    work.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    for base in (tmp_path, _alias(tmp_path)):
        with pytest.raises(NativeCliUnavailable, match="cannot follow symlinks"):
            with worker_isolation(
                workspace_id="user-link", root=base / "link" / "state", workdir=work, env_names=set()
            ):
                pass


def test_policy_path_and_root_spelled_through_var_alias_are_accepted(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"a": {"root": str(_alias(root)), "env": []}}))
    os.chmod(policy, 0o600)
    row = read_workspace_policy(_alias(policy), "a", tmp_path / "state")
    assert Path(str(row["root"])) == root
