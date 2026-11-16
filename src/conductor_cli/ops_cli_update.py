"""Update worker CLIs before their daemons start.

Commands are argv lists taken from the built-in table. A user file may pin,
skip, or point at a binary. It cannot replace the update command, and nothing
here is run through a shell.
"""

from __future__ import annotations

import errno
import json
import os
import re
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ops_support import OpsError, RealSystem, Settings

_LOG_LOCK = threading.Lock()
_TIMEOUT_SECONDS = 300.0
_DEFAULT_INTERVAL_HOURS = 6.0
_SUCCESS = frozenset({"updated"})
_PROVIDER_CLI = {
    "claude": "claude",
    "codex": "codex",
    "kimi": "kimi",
    "opencode": "opencode",
    "grok": "grok",
    "minimax": "mcode",
    "mcode": "mcode",
    "deepseek": "dsh",
    "dsh": "dsh",
    "claude-code": "claude",
    "claudedev": "claude",
    "cursor": "cursor-agent",
    "deepseek-dsh": "dsh",
    "minimax-mmx": "mcode",
    "mmx": "mcode",
    "stepfun": "step",
    "step": "step",
    "zcode": "zcode",
}
_VERSION_TOKEN = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.]+)*")
_ENV_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
    }
)
# The binary could not be started at all: it is not installed (or not at the
# configured path). Reported as its own result, not as a failure.
_NOT_INSTALLED = "not-installed"
_MISSING_MARKS = ("FileNotFoundError", "NotADirectoryError")
_SECRET_MARK = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


@dataclass(frozen=True)
class CliSpec:
    """One worker CLI the fleet knows how to update."""

    name: str
    install: str
    package: str
    binary: str
    update: tuple[str, ...]
    rollback: str
    pin: str = ""
    skip: bool = False
    dist_tag: str = "latest"
    refused: bool = False

    def expand(self, binary: str | None = None) -> CliSpec:
        chosen = binary or self.binary
        return CliSpec(
            name=self.name,
            install=self.install,
            package=self.package,
            binary=chosen,
            update=self._with_binary(chosen),
            rollback=self.rollback,
            pin=self.pin,
            skip=self.skip,
            dist_tag=self.dist_tag,
            refused=self.refused,
        )

    def _with_binary(self, binary: str) -> tuple[str, ...]:
        if not self.update:
            return ()
        if self.install in {"brew-cask", "brew-formula"}:
            return self.update
        if self.install == "npm":
            tag = self.dist_tag or "latest"
            return ("npm", "i", "-g", f"{self.package}@{tag}")
        return (binary, *self.update[1:])


_BUILTIN: tuple[CliSpec, ...] = (
    CliSpec(
        "claude",
        "brew-cask",
        "claude-code",
        "claude",
        ("brew", "upgrade", "--cask", "claude-code"),
        "none",
    ),
    CliSpec(
        "codex",
        "brew-cask",
        "codex",
        "codex",
        ("brew", "upgrade", "--cask", "codex"),
        "none",
    ),
    CliSpec(
        "kimi",
        "brew-formula",
        "kimi-code",
        "kimi",
        ("brew", "upgrade", "kimi-code"),
        "none",
    ),
    CliSpec(
        "opencode",
        "brew-formula",
        "opencode",
        "opencode",
        ("brew", "upgrade", "opencode"),
        "none",
    ),
    CliSpec(
        "grok",
        "npm",
        "@xai-official/grok",
        "grok",
        ("npm", "i", "-g", "@xai-official/grok@latest"),
        "npm",
    ),
    CliSpec(
        "mcode",
        "npm",
        "@minimax-ai/code",
        "mcode",
        ("npm", "i", "-g", "@minimax-ai/code@latest"),
        "npm",
    ),
    CliSpec(
        "dsh",
        "npm",
        "@deepseek-ai/dsh",
        "dsh",
        ("npm", "i", "-g", "@deepseek-ai/dsh@latest"),
        "npm",
    ),
    CliSpec(
        "cursor-agent",
        "cursor-installer",
        "cursor-agent",
        "~/.local/bin/cursor-agent",
        ("~/.local/bin/cursor-agent", "update"),
        "cursor",
    ),
    CliSpec(
        "step",
        "self-update",
        "step",
        # Found on PATH like the other CLIs. A machine that installs step
        # elsewhere sets ``path`` for it in ``~/.conductor/clis.toml``.
        "step",
        ("step", "update"),
        "none",
    ),
    CliSpec("zcode", "app", "zcode", "~/.local/bin/zcode", (), "none"),
)


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _expand(value: str) -> str:
    return str(Path(value).expanduser()) if value.startswith("~") else value


def _argv_ok(argv: tuple[str, ...] | list[str]) -> bool:
    for part in argv:
        if (
            not isinstance(part, str)
            or part == ""
            or part == "sudo"
            or part.endswith("/sudo")
        ):
            return False
        if "://" in part or any(char in part for char in ";|&$`<>"):
            return False
    return True


def _dist_tag_ok(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in "._-" for char in value)


def _npm_version_ok(value: str) -> bool:
    """A previous npm version may carry build metadata. It is still one argv token."""
    return bool(value) and all(char.isalnum() or char in "._-+" for char in value)


def _child_env() -> dict[str, str]:
    """PATH and locale for installers. Secret-shaped names stay in the parent."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(mark in upper for mark in _SECRET_MARK):
            continue
        if key in _ENV_EXACT or key.startswith(
            ("LC_", "HOMEBREW_", "NPM_CONFIG_", "NODE_", "XDG_")
        ):
            env[key] = value
    env.setdefault("PATH", os.defpath or "/usr/bin:/bin")
    return env


def _now(clock: Any) -> datetime:
    if clock is not None:
        stamped = clock()
        if isinstance(stamped, datetime):
            return (
                stamped if stamped.tzinfo is not None else stamped.replace(tzinfo=UTC)
            )
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def log_path(settings: Settings) -> Path:
    return settings.log_dir / "cli-update.jsonl"


def _read_log(path: Path) -> str:
    """Read the jsonl. A symlink is not followed and contributes no history."""
    if path.is_symlink():
        return ""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        chunks: list[bytes] = []
        while True:
            piece = os.read(fd, 65536)
            if not piece:
                break
            chunks.append(piece)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", "replace")


def _append(settings: Settings, row: dict[str, object]) -> None:
    path = log_path(settings)
    payload = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _LOG_LOCK:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            if exc.errno not in {errno.ELOOP, errno.EPERM} or not path.is_symlink():
                raise
            path.unlink()
            fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, payload)
        finally:
            os.close(fd)


def latest_results(settings: Settings) -> list[dict[str, Any]]:
    """Last recorded row for each CLI, in table order."""
    path = log_path(settings)
    found: dict[str, dict[str, Any]] = {}
    for line in _read_log(path).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("name"), str):
            found[row["name"]] = row
    return [found[spec.name] for spec in _BUILTIN if spec.name in found]


def _interval_value(raw: object, fallback: float) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _load_overlay(settings: Settings) -> tuple[dict[str, Any], float]:
    """Pins and skips survive a bad interval. Only a missing file drops them."""
    path = settings.conductor_home / "clis.toml"
    interval = _DEFAULT_INTERVAL_HOURS
    data: dict[str, Any] = {}
    try:
        if path.is_file() and not path.is_symlink():
            loaded = tomllib.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        data = {}
    update = data.get("update")
    if isinstance(update, dict) and update.get("interval_hours") is not None:
        interval = _interval_value(update.get("interval_hours"), interval)
    raw = os.environ.get("CONDUCTOR_CLI_UPDATE_INTERVAL_HOURS")
    if raw:
        interval = _interval_value(raw, interval)
    return data, interval


def load_specs(settings: Settings) -> tuple[list[CliSpec], float]:
    try:
        overlay, interval = _load_overlay(settings)
    except Exception:
        overlay, interval = {}, _DEFAULT_INTERVAL_HOURS
    specs: list[CliSpec] = []
    for base in _BUILTIN:
        row = overlay.get(base.name)
        extra = row if isinstance(row, dict) else {}
        refused = False
        if "update" in extra:
            supplied = extra.get("update")
            supplied_argv = (
                tuple(supplied) if isinstance(supplied, list) else ("invalid",)
            )
            refused = supplied_argv != base.update or not _argv_ok(supplied_argv)
        pin = str(extra.get("pin") or "")
        skip = bool(extra.get("skip"))
        dist_tag = str(extra.get("dist_tag") or base.dist_tag)
        if not _dist_tag_ok(dist_tag):
            refused = True
            dist_tag = base.dist_tag
        binary = str(extra.get("path") or base.binary)
        if not _argv_ok((binary,)):
            refused = True
            binary = base.binary
        spec = CliSpec(
            name=base.name,
            install=base.install,
            package=base.package,
            binary=binary,
            update=base.update,
            rollback=base.rollback,
            pin=pin,
            skip=skip,
            dist_tag=dist_tag,
            refused=refused,
        )
        specs.append(spec.expand(_expand(spec.binary)))
    return specs, interval


def _history(settings: Settings) -> dict[str, datetime]:
    """Last *successful* update per CLI. A later throttle row must not erase it."""
    latest: dict[str, datetime] = {}
    for line in _read_log(log_path(settings)).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("result") not in _SUCCESS:
            continue
        name = row.get("name")
        raw = row.get("at")
        if not isinstance(name, str) or not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        previous = latest.get(name)
        if previous is None or parsed > previous:
            latest[name] = parsed
    return latest


def _row(
    spec: CliSpec,
    previous: str,
    new: str,
    result: str,
    moment: datetime,
    detail: str,
) -> dict[str, object]:
    return {
        "at": _stamp(moment),
        "detail": detail,
        "name": spec.name,
        "new": new,
        "previous": previous,
        "result": result,
    }


def _version_of(stdout: str) -> str:
    lines = stdout.strip().splitlines()
    if not lines:
        return ""
    found = _VERSION_TOKEN.findall(lines[0])
    if found:
        return found[-1]
    parts = lines[0].split()
    return parts[-1] if parts else ""


def _run(system: RealSystem, argv: tuple[str, ...] | list[str], settings: Settings):
    if not _argv_ok(argv):
        raise OpsError("refused update argv")
    return system.run(
        list(argv), cwd=settings.repo, env=_child_env(), timeout=_TIMEOUT_SECONDS
    )


def _probe(system: RealSystem, spec: CliSpec, settings: Settings) -> tuple[bool, str]:
    binary = _expand(spec.binary)
    completed = _run(system, (binary, "--version"), settings)
    if completed.code != 0:
        return False, ""
    return True, _version_of(completed.stdout)


def _help_ok(system: RealSystem, spec: CliSpec, settings: Settings) -> bool:
    completed = _run(system, (_expand(spec.binary), "--help"), settings)
    return completed.code == 0


def _cursor_link() -> Path:
    return _home() / ".local/share/cursor-agent/versions/current"


def _cursor_target() -> Path | None:
    link = _cursor_link()
    if link.is_symlink():
        return Path(os.path.realpath(link))
    return None


def _restore_cursor(previous: Path) -> bool:
    link = _cursor_link()
    if not previous.exists():
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(previous)
    return link.is_symlink() and Path(os.path.realpath(link)) == previous


def _smoke_ok(system: RealSystem, spec: CliSpec, settings: Settings) -> bool:
    """Version and help both have to succeed. A missing or timed-out CLI does not."""
    try:
        version_ok, _new = _probe(system, spec, settings)
        return bool(version_ok and _help_ok(system, spec, settings))
    except (OpsError, OSError):
        return False


def _rollback(
    system: RealSystem,
    settings: Settings,
    spec: CliSpec,
    previous: str,
    cursor_previous: Path | None,
) -> bool:
    """Restore the previous CLI and prove version and help. Exit 0 alone is not enough."""
    try:
        if spec.rollback == "npm":
            if not previous or not _npm_version_ok(previous):
                return False
            argv = ["npm", "i", "-g", f"{spec.package}@{previous}"]
            completed = _run(system, argv, settings)
            if completed.code != 0:
                return False
            return _smoke_ok(system, spec, settings)
        if spec.rollback == "cursor" and cursor_previous is not None:
            if not _restore_cursor(cursor_previous):
                return False
            return _smoke_ok(system, spec, settings)
    except (OpsError, OSError):
        return False
    return False


def _after_installer_started(
    system: RealSystem,
    settings: Settings,
    spec: CliSpec,
    previous: str,
    moment: datetime,
    detail: str,
) -> dict[str, object]:
    """The installer has started, so a failed or impossible smoke check is broken.

    A ``failed`` row still lets the daemon start. Only ``broken`` keeps it down.
    Failures before ``_run`` (skip, pin, throttle, refused argv, dry-run, a
    probe error) stay ``failed`` and do not come through here.
    """
    if _smoke_ok(system, spec, settings):
        return _row(spec, previous, previous, "failed", moment, detail)
    return _row(spec, previous, "", "broken", moment, f"{detail}; smoke failed")


def _is_missing(detail: str) -> bool:
    """True when the probe could not start the binary because it does not exist."""
    return detail.startswith("could not run ") and any(
        mark in detail for mark in _MISSING_MARKS
    )


def _update_one(
    system: RealSystem,
    settings: Settings,
    spec: CliSpec,
    *,
    dry_run: bool,
    force: bool,
    interval: float,
    moment: datetime,
    history: dict[str, datetime],
) -> dict[str, object]:
    if spec.skip or spec.pin:
        reason = "pin" if spec.pin else "skip"
        return _row(spec, "", spec.pin, "skipped", moment, reason)
    if spec.refused:
        return _row(spec, "", "", "failed", moment, "refused custom update command")
    if spec.install == "app" or not spec.update:
        return _row(spec, "", "", "app-managed", moment, "app-managed")
    previous_at = history.get(spec.name)
    if (
        not force
        and previous_at is not None
        and moment - previous_at < timedelta(hours=interval)
    ):
        return _row(spec, "", "", "skipped", moment, "throttle")
    if not _argv_ok(spec.update):
        return _row(spec, "", "", "failed", moment, "refused update argv")
    if dry_run:
        return _row(spec, "", "", "dry-run", moment, " ".join(spec.update))
    cursor_previous = _cursor_target() if spec.rollback == "cursor" else None
    try:
        _ok, previous = _probe(system, spec, settings)
    except OpsError as exc:
        detail = str(exc)
        if _is_missing(detail):
            return _row(spec, "", "", _NOT_INSTALLED, moment, "not installed")
        return _row(spec, "", "", "failed", moment, detail)
    try:
        completed = _run(system, spec.update, settings)
    except OpsError as exc:
        return _after_installer_started(
            system, settings, spec, previous, moment, str(exc)
        )
    if completed.code != 0:
        detail = "network" if "network" in completed.stderr.lower() else "update failed"
        return _after_installer_started(
            system, settings, spec, previous, moment, detail
        )
    try:
        version_ok, new = _probe(system, spec, settings)
        help_ok = _help_ok(system, spec, settings) if version_ok else False
    except OpsError as exc:
        detail = str(exc)
        # The installer already returned 0. A timeout is unknown health, not a
        # pre-install failure, so the daemon stays down. A missing binary can
        # still roll back, but only if version and help then succeed.
        if "timed out" in detail:
            return _row(spec, previous, "", "broken", moment, detail)
        if _rollback(system, settings, spec, previous, cursor_previous):
            return _row(spec, previous, previous, "rolled-back", moment, detail)
        return _row(spec, previous, "", "broken", moment, detail)
    else:
        smoke_detail = "smoke failed"
    if version_ok and help_ok:
        return _row(spec, previous, new, "updated", moment, "")
    if _rollback(system, settings, spec, previous, cursor_previous):
        return _row(spec, previous, previous, "rolled-back", moment, smoke_detail)
    return _row(spec, previous, new, "broken", moment, smoke_detail)


def update_clis(
    system: RealSystem,
    settings: Settings,
    *,
    only: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    clock: Any = None,
    on_finished: Any = None,
) -> list[dict[str, object]]:
    """Update every CLI, or one named CLI. Failures are recorded, not raised.

    Brew installs stay serial with each other. Everything else runs at the
    same time, including while a brew command is still in progress. ``on_finished``
    is called as soon as that CLI's own row is recorded.
    """
    specs, interval = load_specs(settings)
    if only:
        specs = [spec for spec in specs if spec.name == only]
        if not specs:
            raise OpsError(f"unknown cli {only}")
    moment = _now(clock)
    history = _history(settings)
    brew_lock = threading.Lock()

    def run(spec: CliSpec) -> dict[str, object]:
        def execute() -> dict[str, object]:
            try:
                row = _update_one(
                    system,
                    settings,
                    spec,
                    dry_run=dry_run,
                    force=force,
                    interval=interval,
                    moment=moment,
                    history=history,
                )
            except Exception as exc:
                row = _row(spec, "", "", "failed", moment, type(exc).__name__)
            try:
                _append(settings, row)
            except OSError:
                pass
            return row

        if spec.install.startswith("brew"):
            with brew_lock:
                row = execute()
        else:
            row = execute()
        if on_finished is not None:
            on_finished(row)
        return row

    if len(specs) <= 1:
        return [run(spec) for spec in specs]
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        return list(pool.map(run, specs))


def startup_updates(system: RealSystem, settings: Settings) -> list[dict[str, object]]:
    try:
        return update_clis(system, settings)
    except Exception as exc:
        fallback = {
            "at": _stamp(_now(None)),
            "detail": type(exc).__name__,
            "name": "*",
            "new": "",
            "previous": "",
            "result": "failed",
        }
        try:
            _append(settings, fallback)
        except OSError:
            pass
        return [fallback]


def persisted_health(settings: Settings) -> dict[str, str]:
    """Last verified health. skip, dry-run, and a pre-mutation failure do not clear it."""
    health: dict[str, str] = {}
    for line in _read_log(log_path(settings)).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        result = row.get("result")
        name = str(row["name"])
        if result in {"updated", "rolled-back"}:
            health[name] = "ok"
        elif result == "broken":
            health[name] = "broken"
    return health


def daemon_is_blocked(
    provider: str,
    results: list[dict[str, object]],
    settings: Settings | None = None,
) -> bool:
    cli_name = _PROVIDER_CLI.get(provider)
    if cli_name is None:
        return False
    if any(
        row.get("name") == cli_name and row.get("result") == "broken" for row in results
    ):
        return True
    if settings is None:
        return False
    return persisted_health(settings).get(cli_name) == "broken"


def cli_status(system: RealSystem, settings: Settings) -> list[dict[str, object]]:
    """Describe each CLI. Version probes do not update anything."""
    specs, _interval = load_specs(settings)
    known = {row["name"]: row for row in latest_results(settings)}
    described: list[dict[str, object]] = []
    for spec in specs:
        current = ""
        if not spec.refused:
            try:
                ok, current = _probe(system, spec, settings)
                if not ok:
                    current = ""
            except OpsError:
                current = ""
        prior = known.get(spec.name, {})
        described.append(
            {
                "current": current,
                "install": spec.install,
                "latest": prior.get("new") or "unknown",
                "name": spec.name,
                "path": _expand(spec.binary),
                "pin": spec.pin,
                "result": prior.get("result") or "unchecked",
                "skip": spec.skip,
            }
        )
    return described
