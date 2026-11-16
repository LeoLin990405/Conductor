#!/usr/bin/env python3
"""Validate the deployment's configuration layer before startup.

The backend fails late on a malformed config.yaml or a model that names a
provider nobody declared. This checks the files a deployment actually edits
(.env, config.yaml, agent_config.yaml, the model manifests, the local override)
in one pass, and says which file and key is wrong.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent

errors: list[str] = []
notes: list[str] = []


def load_yaml(path: Path):
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML ships with the backend env
        notes.append(f"{path.name}: skipped (PyYAML not installed; run with .venv/bin/python)")
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report, don't traceback
        errors.append(f"{path.name}: YAML parse failed: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{path.name}: expected a mapping at the top level")
        return None
    return data


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{path.name}: JSON parse failed: {exc}")
        return None


def check_models() -> None:
    manifest = ROOT / "src/llms/manifest"
    providers = load_json(manifest / "providers.json")
    models = load_json(manifest / "models.json")
    if providers is None or models is None:
        return
    # providers.json groups its records under provider_config; accept a flat
    # mapping too, so a hand-written local override validates the same way.
    if isinstance(providers, dict) and isinstance(providers.get("provider_config"), dict):
        providers = providers["provider_config"]
    if not isinstance(providers, dict) or not providers:
        errors.append("providers.json: expected a non-empty mapping")
        return
    # A model names either a provider or one of its variants (codex-oauth,
    # z-ai-cn-coding, …), so both sets are valid addresses.
    known = set(providers)
    for entry in providers.values():
        variants = entry.get("variants") if isinstance(entry, dict) else None
        if isinstance(variants, dict):
            known.update(variants)
    if not isinstance(models, dict) or not models:
        errors.append("models.json: expected a non-empty mapping")
        return
    for name, model in models.items():
        if not isinstance(model, dict):
            errors.append(f"models.json[{name}]: expected a mapping")
            continue
        provider = model.get("provider")
        if not provider:
            errors.append(f"models.json[{name}]: missing provider")
        elif str(provider).startswith("__"):
            continue  # __auto__: the router is a model entry, not a provider
        elif provider not in known:
            errors.append(f"models.json[{name}]: provider {provider!r} not in providers.json")
    auto = models.get("auto")
    if auto is None:
        errors.append("models.json: the deployment default model 'auto' is missing")
    elif not auto.get("visible", True):
        errors.append("models.json[auto]: the deployment default model must be visible")
    notes.append(f"models: {len(models)} model(s) across {len(providers)} provider(s)")


def check_local_override() -> None:
    local = ROOT / "config/llm.local.json"
    example = ROOT / "config/llm.local.example.json"
    path = local if local.exists() else example
    if not path.exists():
        return
    data = load_json(path)
    if data is None:
        return
    if not isinstance(data, dict):
        errors.append(f"{path.name}: expected a mapping")
        return
    providers = data.get("providers") or {}
    models = data.get("models") or {}
    for name, model in models.items():
        if not isinstance(model, dict):
            continue
        provider = model.get("provider")
        if provider and provider not in providers:
            errors.append(
                f"{path.name}[models.{name}]: provider {provider!r} not declared in "
                f"the override's providers block"
            )
    if local.exists():
        notes.append(f"local override: {path.name} present")


def check_env() -> None:
    example = ROOT / ".env.example"
    if not example.exists():
        errors.append(".env.example: missing")
        return
    keys: set[str] = set()
    for lineno, line in enumerate(example.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in keys:
            errors.append(f".env.example:{lineno}: duplicate key {key}")
        keys.add(key)
    notes.append(f".env.example: {len(keys)} key(s)")


def check_runtime_config() -> None:
    config = load_yaml(ROOT / "config.yaml")
    agent = load_yaml(ROOT / "agent_config.yaml")
    if config is None or agent is None:
        return
    llm = config.get("llm")
    if llm is not None and not isinstance(llm, dict):
        errors.append("config.yaml: llm must be a mapping or null")
    insight = config.get("market_insight") or {}
    tz = insight.get("timezone")
    if not tz:
        errors.append("config.yaml: market_insight.timezone is missing")
    else:
        try:
            ZoneInfo(str(tz))
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"config.yaml: market_insight.timezone {tz!r} is not a known IANA zone")
        else:
            notes.append(f"market_insight timezone: {tz}")
    skills = agent.get("skills")
    if skills is not None and not isinstance(skills, dict):
        errors.append("agent_config.yaml: skills must be a mapping")
    sandbox = agent.get("sandbox")
    if sandbox is not None and not isinstance(sandbox, dict):
        errors.append("agent_config.yaml: sandbox must be a mapping")


def main() -> int:
    for name in ("config.yaml", "agent_config.yaml"):
        if not (ROOT / name).exists():
            errors.append(f"{name}: missing")
    check_env()
    check_runtime_config()
    check_models()
    check_local_override()

    if notes:
        print("Configuration layer:")
        for note in notes:
            print(f"  ok  {note}")
    if errors:
        print("\nConfiguration errors:", file=sys.stderr)
        for err in errors:
            print(f"  !!  {err}", file=sys.stderr)
        return 1
    print("\nConfiguration layer is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
