"""Desktop release helper: one version, one installed copy.

These tests use fake .app bundles so they never package Electron or touch the
real ~/Applications.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from scripts import desktop_release


def make_app(root: Path, version: str = "0.1.4", bundle_id: str = desktop_release.BUNDLE_ID) -> Path:
    app = root / "Conductor.app"
    contents = app / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": version,
                "CFBundleExecutable": "Conductor",
            },
            fh,
        )
    return app


def test_bundle_reads_version_and_identity(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(desktop_release, "signing_state", lambda path: "ad-hoc")
    item = desktop_release.bundle(app)
    assert item.version == "0.1.4"
    assert item.bundle_id == desktop_release.BUNDLE_ID
    assert item.signature == "ad-hoc"


def test_status_render_is_not_ready_for_version_drift():
    installed = desktop_release.Bundle(
        path="/tmp/Conductor.app", version="0.1.3", bundle_id=desktop_release.BUNDLE_ID, signature="ad-hoc"
    )
    status = desktop_release.Status(
        repo_version="0.1.4",
        installed=installed,
        problems=["installed 0.1.3 != repo 0.1.4"],
    )
    text = desktop_release.render(status, color=False)
    assert "NOT READY" in text
    assert "0.1.3" in text and "0.1.4" in text


def test_locate_build_prefers_requested_arch(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_release, "DESKTOP", tmp_path)
    app = make_app(tmp_path / "dist" / "oss" / "mac-arm64")
    assert app.is_dir()
    assert desktop_release.locate_build("arm64") == tmp_path / "dist" / "oss" / "mac-arm64" / "Conductor.app"


def test_collect_status_flags_duplicate_build_app(tmp_path, monkeypatch):
    installed = make_app(tmp_path / "Applications")
    duplicate = make_app(tmp_path / "dist" / "oss" / "mac-arm64")
    monkeypatch.setattr(desktop_release, "repo_version", lambda: "0.1.4")
    monkeypatch.setattr(desktop_release, "INSTALL_TARGET", installed)
    monkeypatch.setattr(desktop_release, "installed_app", lambda: installed)
    monkeypatch.setattr(desktop_release, "build_apps", lambda: [duplicate])
    monkeypatch.setattr(desktop_release, "find_apps", lambda roots, max_depth=4: [installed, duplicate])
    monkeypatch.setattr(desktop_release, "signing_state", lambda path: "ad-hoc")

    status = desktop_release.collect_status()
    assert status.ready is True
    assert len(status.duplicates) == 1
    assert any("duplicate" in warning for warning in status.warnings)
