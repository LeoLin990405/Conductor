"""Render Conductor launchd and systemd units from templates.

``install`` only writes files. It does not call launchctl. ``enable`` calls
``launchctl`` from PATH so tests can substitute a fake.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .ops_fleet import FleetFile, load_fleet
from .ops_support import OpsError, RealSystem, Settings, ensure_private_dir

_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "deploy"


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _label(prefix: str, provider: str = "", ident: str = "") -> str:
    if not provider:
        label = f"com.conductor.{prefix}"
    else:
        raw = f"com.conductor.daemon.{provider}.{ident}"
        label = "".join(ch if ch.isalnum() or ch in ".-" else "-" for ch in raw)
    if not label.startswith("com.conductor."):
        raise OpsError("refusing a launchd label outside com.conductor.*")
    return label


def _render(template: str, values: dict[str, str]) -> str:
    text = template
    for key, value in values.items():
        text = text.replace(f"__{key}__", value)
    if "__" in text:
        raise OpsError("launchd template has an unreplaced placeholder")
    return text


def _plist_template() -> str:
    path = _TEMPLATE_ROOT / "launchd" / "conductor.plist.in"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OpsError(f"missing launchd template: {type(exc).__name__}") from exc


def _systemd_template() -> str:
    path = _TEMPLATE_ROOT / "systemd" / "conductor.service.in"
    return path.read_text(encoding="utf-8")


def _newsyslog_template() -> str:
    path = _TEMPLATE_ROOT / "newsyslog" / "conductor.conf.in"
    return path.read_text(encoding="utf-8")


def _units(settings: Settings, fleet: FleetFile) -> list[dict[str, str]]:
    common = {
        "PYTHON": xml_escape(str(settings.python)),
        "REPO": xml_escape(str(settings.repo)),
        "HOME": xml_escape(str(settings.conductor_home)),
        "BACKEND_PORT": str(settings.backend_port),
        "WEB_PORT": str(settings.web_port),
        "SECRETS": xml_escape(str(settings.secrets_file)),
        "WORKTREE_ROOT": xml_escape(str(settings.worktree_root)),
    }
    # One supervisor. It starts the backend, web, and daemons. Per-role
    # plists would each run `up --foreground` and start three copies.
    units = [
        {
            "role": "supervisor",
            "label": _label("supervisor"),
            "extra": "",
            "log": "supervisor.log",
        }
    ]
    for unit in units:
        unit.update(common)
    return units


def render_plist(settings: Settings, unit: dict[str, str], *, enabled: bool) -> str:
    log_path = settings.log_dir / unit["log"]
    values = {
        "LABEL": xml_escape(unit["label"]),
        "PYTHON": unit["PYTHON"],
        "REPO": unit["REPO"],
        "HOME": unit["HOME"],
        "BACKEND_PORT": unit["BACKEND_PORT"],
        "WEB_PORT": unit["WEB_PORT"],
        "SECRETS": unit["SECRETS"],
        "WORKTREE_ROOT": xml_escape(str(settings.worktree_root)),
        "ROLE": xml_escape(unit["role"]),
        "EXTRA_ARGS": unit.get("extra", ""),
        "LOG": xml_escape(str(log_path)),
        "RUN_AT_LOAD": "<true/>" if enabled else "<false/>",
        "KEEP_ALIVE": "<true/>" if enabled else "<false/>",
    }
    return _render(_plist_template(), values)


def render_systemd(settings: Settings, unit: dict[str, str]) -> str:
    extra = ""
    if unit["role"] == "daemon":
        extra = f"--provider {unit.get('provider', '')} --id {unit.get('ident', '')}"
    values = {
        "ROLE": unit["role"],
        "PYTHON": str(settings.python),
        "REPO": str(settings.repo),
        "HOME": str(settings.conductor_home),
        "SECRETS": str(settings.secrets_file),
        "WORKTREE_ROOT": str(settings.worktree_root),
        "EXTRA_PLAIN": extra,
        "LABEL": unit["label"],
    }
    return _render(_systemd_template(), values)


def _migrate_old_labels(directory: Path) -> list[str]:
    moved = []
    for name in ("com.leo.conductor.server.plist", "com.leo.conductor.daemon.plist"):
        path = directory / name
        if path.exists() and not path.is_symlink():
            destination = path.with_suffix(".plist.migrated")
            path.replace(destination)
            moved.append(name)
    return moved


_CODEXBAR_LABEL = "com.conductor.codexbar-snapshot"
_MAIN_CLONE = "default-workspace/AlphaCN"


def _is_main_clone(path: str) -> bool:
    return _MAIN_CLONE in path.replace("\\", "/")


def render_codexbar_plist(settings: Settings) -> str:
    """Render the snapshot job so it runs from this checkout, not the main clone."""
    python = str(settings.python)
    repo = str(settings.repo)
    if _is_main_clone(python) or _is_main_clone(repo):
        raise OpsError("codexbar snapshot refuses the main clone")
    template = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "launchd"
        / "com.conductor.codexbar-snapshot.plist"
    )
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise OpsError(f"missing codexbar launchd template: {type(exc).__name__}") from exc
    home = str(Path(os.environ.get("HOME", str(Path.home()))).expanduser())
    codexbar = os.environ.get("CONDUCTOR_CODEXBAR", "/opt/homebrew/bin/codexbar")
    return _render(
        text,
        {
            "PYTHON": xml_escape(python),
            "REPO": xml_escape(repo),
            "CODEXBAR": xml_escape(codexbar),
            "HOME": xml_escape(home),
        },
    )


def _write_codexbar_plist(settings: Settings) -> Path:
    path = settings.launchd_dir / f"{_CODEXBAR_LABEL}.plist"
    path.write_text(render_codexbar_plist(settings), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def install_units(
    system: RealSystem,
    settings: Settings,
    fleet: FleetFile | None = None,
    *,
    enabled: bool = False,
) -> list[Path]:
    """Write plists into the chosen directory. Does not bootstrap launchctl."""
    fleet = fleet or load_fleet(settings)
    ensure_private_dir(settings.launchd_dir)
    ensure_private_dir(settings.log_dir)
    _migrate_old_labels(settings.launchd_dir)
    written: list[Path] = []
    labels: list[str] = []
    for unit in _units(settings, fleet):
        path = settings.launchd_dir / f"{unit['label']}.plist"
        path.write_text(render_plist(settings, unit, enabled=enabled), encoding="utf-8")
        os.chmod(path, 0o600)
        written.append(path)
        labels.append(unit["label"])
        if settings.systemd_dir is not None:
            ensure_private_dir(settings.systemd_dir)
            service = settings.systemd_dir / f"{unit['label']}.service"
            service.write_text(render_systemd(settings, unit), encoding="utf-8")
            os.chmod(service, 0o600)
    written.append(_write_codexbar_plist(settings))
    labels.append(_CODEXBAR_LABEL)
    snippet = _render(
        _newsyslog_template(),
        {"LOGDIR": str(settings.log_dir)},
    )
    (settings.log_dir / "newsyslog-conductor.conf").write_text(
        snippet, encoding="utf-8"
    )
    if settings.newsyslog_dir is not None:
        ensure_private_dir(settings.newsyslog_dir)
        (settings.newsyslog_dir / "conductor.conf").write_text(
            snippet, encoding="utf-8"
        )
    marker = settings.conductor_home / "run" / "launchd-labels.json"
    ensure_private_dir(marker.parent)
    marker.write_text(json.dumps({"labels": labels}, indent=2), encoding="utf-8")
    os.chmod(marker, 0o600)
    return written


def enable_units(
    system: RealSystem, settings: Settings, fleet: FleetFile | None = None
) -> list[Path]:
    written = install_units(system, settings, fleet, enabled=True)
    for path in written:
        label = path.name[: -len(".plist")]
        system.run(
            ["launchctl", "bootout", f"{settings.launchd_domain}/{label}"],
            cwd=settings.repo,
        )
        result = system.run(
            ["launchctl", "bootstrap", settings.launchd_domain, str(path)],
            cwd=settings.repo,
        )
        # A fake or a previous bootout may make bootout itself fail; bootstrap must succeed.
        if result.code != 0:
            raise OpsError(f"launchctl bootstrap failed for {label}")
    return written


def disable_units(
    system: RealSystem, settings: Settings, fleet: FleetFile | None = None
) -> None:
    fleet = fleet or load_fleet(settings)
    labels = [unit["label"] for unit in _units(settings, fleet)]
    labels.append(_CODEXBAR_LABEL)
    for label in labels:
        system.run(
            ["launchctl", "bootout", f"{settings.launchd_domain}/{label}"],
            cwd=settings.repo,
        )
    install_units(system, settings, fleet, enabled=False)


def uninstall_units(
    system: RealSystem, settings: Settings, fleet: FleetFile | None = None
) -> None:
    fleet = fleet or load_fleet(settings)
    labels = [unit["label"] for unit in _units(settings, fleet)]
    labels.append(_CODEXBAR_LABEL)
    for label in labels:
        system.run(
            ["launchctl", "bootout", f"{settings.launchd_domain}/{label}"],
            cwd=settings.repo,
        )
        path = settings.launchd_dir / f"{label}.plist"
        path.unlink(missing_ok=True)
    marker = settings.conductor_home / "run" / "launchd-labels.json"
    marker.unlink(missing_ok=True)
