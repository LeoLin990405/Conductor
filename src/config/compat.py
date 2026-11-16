"""Strict loading for externally configured compatibility workers."""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

__all__ = [
    "ADAPTER_CONTROLLED_ENV_NAMES",
    "CompatConfigError",
    "CompatWorkerConfig",
    "load_compat_workers",
    "validate_compat_base_url",
]

ADAPTER_CONTROLLED_ENV_NAMES = frozenset(
    {
        "HOME",
        "DSH_HOME",
        "NATIVE_CLI_DSH_HOME",
        "NATIVE_CLI_DSH_BIN",
        "NATIVE_CLI_DSH_PROFILE",
    }
)

_NAME = re.compile(r"[a-z][a-z0-9-]{1,31}")
_API_KEY_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROTOCOLS = frozenset({"openai-completions", "openai-responses", "anthropic-messages"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class CompatWorkerConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key_env: str
    context_window_tokens: int
    review_capable: bool = False


class CompatConfigError(ValueError):
    """A safe-to-display configuration error with a stable machine reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _invalid_base_url() -> CompatConfigError:
    return CompatConfigError(
        "invalid_base_url",
        "Worker base_url must be a valid HTTPS URL without credentials, query, or fragment",
    )


def validate_compat_base_url(value: str, *, allow_loopback: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid_base_url()
    if any(
        char.isspace() or unicodedata.category(char).startswith("C") for char in value
    ):
        raise _invalid_base_url()
    if "?" in value or "#" in value:
        raise _invalid_base_url()

    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise _invalid_base_url() from None

    if (
        parts.scheme not in {"http", "https"}
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _invalid_base_url()
    if parts.scheme == "http" and (
        not allow_loopback or hostname not in _LOOPBACK_HOSTS
    ):
        raise _invalid_base_url()

    authority = parts.netloc
    ipv6 = ":" in hostname
    if ipv6:
        if not authority.startswith("[") or "]" not in authority:
            raise _invalid_base_url()
        port_suffix = authority.split("]", 1)[1]
        if port_suffix:
            port_text = port_suffix.removeprefix(":")
            if (
                not port_suffix.startswith(":")
                or not port_text.isascii()
                or not port_text.isdigit()
            ):
                raise _invalid_base_url()
    elif ":" in authority:
        port_text = authority.rsplit(":", 1)[1]
        if (
            authority.count(":") != 1
            or not port_text.isascii()
            or not port_text.isdigit()
        ):
            raise _invalid_base_url()

    host = f"[{hostname}]" if ipv6 else hostname
    if port is not None:
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{host}{path}"


def load_compat_workers(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    reserved_names: Collection[str] = (),
    allow_loopback: bool = False,
) -> tuple[CompatWorkerConfig, ...]:
    config_path = (
        Path(path).expanduser()
        if path is not None
        else Path.home() / ".config" / "conductor" / "compat-workers.yaml"
    )
    try:
        metadata = config_path.lstat()
    except FileNotFoundError:
        return ()
    except OSError:
        raise CompatConfigError(
            "invalid_config",
            "Compatibility worker configuration could not be inspected",
        ) from None

    if not stat.S_ISREG(metadata.st_mode):
        raise CompatConfigError(
            "invalid_config",
            "Compatibility worker configuration must be a regular file",
        )
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CompatConfigError(
            "insecure_config_permissions",
            "Compatibility worker configuration must not grant group or other permissions",
        )

    try:
        with config_path.open("r", encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, yaml.YAMLError):
        raise CompatConfigError(
            "invalid_config",
            "Compatibility worker configuration could not be read as YAML",
        ) from None

    if not isinstance(document, Mapping) or not isinstance(
        document.get("workers"), list
    ):
        raise CompatConfigError(
            "invalid_config",
            "Compatibility worker configuration must contain a workers list",
        )

    env = os.environ if environ is None else environ
    reserved = set(reserved_names)
    seen: set[str] = set()
    workers: list[CompatWorkerConfig] = []

    for index, entry in enumerate(document["workers"]):
        if not isinstance(entry, Mapping):
            raise CompatConfigError(
                "invalid_config", f"Worker entry {index} must be a mapping"
            )

        name = entry.get("name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise CompatConfigError(
                "invalid_name", f"Worker entry {index} has an invalid name"
            )
        if name in seen:
            raise CompatConfigError(
                "duplicate_name", f"Worker entry {index} duplicates an earlier name"
            )
        if name in reserved:
            raise CompatConfigError(
                "adapter_name_conflict",
                f"Worker entry {index} conflicts with a reserved adapter name",
            )

        protocol = entry.get("protocol")
        if not isinstance(protocol, str) or protocol not in _PROTOCOLS:
            raise CompatConfigError(
                "invalid_protocol", f"Worker entry {index} has an invalid protocol"
            )

        base_url = validate_compat_base_url(
            entry.get("base_url"), allow_loopback=allow_loopback
        )

        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            raise CompatConfigError(
                "invalid_model", f"Worker entry {index} must have a nonempty model"
            )

        context_window = entry.get("context_window_tokens")
        if (
            not isinstance(context_window, int)
            or isinstance(context_window, bool)
            or context_window <= 0
        ):
            raise CompatConfigError(
                "invalid_context_window",
                f"Worker entry {index} must have a positive integer context window",
            )

        review_capable = entry.get("review_capable", False)
        if review_capable is not False:
            raise CompatConfigError(
                "invalid_review_capable",
                f"Worker entry {index} must set review_capable to literal false",
            )

        api_key_env = entry.get("api_key_env")
        if (
            not isinstance(api_key_env, str)
            or _API_KEY_ENV.fullmatch(api_key_env) is None
            or api_key_env in ADAPTER_CONTROLLED_ENV_NAMES
        ):
            raise CompatConfigError(
                "invalid_api_key_env",
                f"Worker entry {index} has an invalid or adapter-controlled "
                "API key environment identifier",
            )
        if not env.get(api_key_env):
            raise CompatConfigError(
                "missing_api_key_env",
                f"Worker entry {index} API key environment variable is missing or empty",
            )

        seen.add(name)
        workers.append(
            CompatWorkerConfig(
                name=name,
                protocol=protocol,
                base_url=base_url,
                model=model,
                api_key_env=api_key_env,
                context_window_tokens=context_window,
                review_capable=review_capable,
            )
        )

    return tuple(workers)
