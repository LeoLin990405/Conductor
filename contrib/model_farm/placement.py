"""Fail-closed placement policy for agent runtimes (not SSH target hosts)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

POLICY_PATH = Path(__file__).resolve().parent / "config" / "executor-placement.json"
RUNTIME_ALIASES = {"claude-cli": "claude", "anthropic": "claude", "codex-cli": "codex",
                   "cursor-cli": "cursor", "qwen-code": "qwen", "gemini-cli": "gemini",
                   "kimi-code": "kimi", "step-code": "step", "minimax-code": "minimax",
                   "codebuddy-code": "codebuddy"}
_SUPPORTED_RUNTIMES = {"claude", "codex", "cursor", "ollama", "qwen", "gemini", "kimi",
                       "step", "minimax", "codebuddy", "ollama-m4-guarded"}


def load_policy(path: Path = POLICY_PATH) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("unsupported executor placement policy")
    return value


def _text(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _canonical_runtime(value: str) -> str:
    return RUNTIME_ALIASES.get(value, value)


def _hosts_for(name: str, policy: dict) -> set[str]:
    hosts = {key for key, config in policy.get("execution_hosts", {}).items()
             if name in config.get("allowed_runtimes", [])}
    candidates = policy.get("runtime_candidates", {}).get(name)
    if candidates is not None:
        hosts.intersection_update(candidates.get("hosts", []))
    return hosts


def classify_assignment(assignment: dict, policy: Optional[dict] = None) -> Tuple[List[Set[str]], List[str]]:
    """Return independent host constraints from runtime, provider, model, and launcher."""
    policy = policy or load_policy()
    runtime = _canonical_runtime(_text(assignment.get("runtime")))
    provider = _text(assignment.get("provider"))
    if provider == "claude":
        provider = "anthropic"
    model = _text(assignment.get("model"))
    launcher = _canonical_runtime(_text(assignment.get("launcher")))
    constraints: List[Set[str]] = []
    reasons: List[str] = []

    def pin(hosts: Set[str], reason: str) -> None:
        constraints.append(hosts)
        reasons.append(reason)

    if runtime in _SUPPORTED_RUNTIMES:
        pin(_hosts_for(runtime, policy), runtime + "_runtime")
    if launcher in _SUPPORTED_RUNTIMES:
        pin(_hosts_for(launcher, policy), launcher + "_launcher")
    # Provider/model pins are independent of the selected launcher. A Cursor
    # launcher may call Claude only where the Anthropic runtime is permitted.
    if provider == "anthropic" or re.search(r"claude|anthropic|sonnet|opus|haiku", model):
        pin(_hosts_for("claude", policy), "anthropic_or_claude_model")
    if provider == "cursor" or model.startswith("cursor"):
        pin(_hosts_for("cursor", policy), "cursor_candidate")
    if provider == "ollama":
        pin(_hosts_for("ollama", policy), "ollama_candidate")
    if provider == "qwen":
        pin(_hosts_for("qwen", policy), "qwen_candidate")
    if provider == "google":
        pin(_hosts_for("gemini", policy), "gemini_candidate")
    if provider == "moonshot":
        pin(_hosts_for("kimi", policy), "kimi_candidate")
    if provider == "stepfun":
        pin(_hosts_for("step", policy), "step_candidate")
    if provider == "minimax":
        pin(_hosts_for("minimax", policy), "minimax_candidate")
    if provider == "tencent_tokenhub":
        pin(_hosts_for("codebuddy", policy), "codebuddy_candidate")
    if runtime == "codex" or launcher == "codex" or re.search(r"(^|[-_/])codex($|[-_/])", model):
        pin(_hosts_for("codex", policy), "codex_placement")
    return constraints, reasons


def resolve_placement(assignment: dict, policy: Optional[dict] = None) -> dict:
    policy = policy or load_policy()
    if not isinstance(assignment, dict):
        return {"status": "blocked", "reason": "assignment_missing"}
    runtime = _canonical_runtime(_text(assignment.get("runtime")))
    provider = _text(assignment.get("provider"))
    if provider == "claude":
        provider = "anthropic"
    model = _text(assignment.get("model"))
    host = _text(assignment.get("executor_host"))
    if not runtime or not provider or not model or not host:
        return {"status": "blocked", "reason": "runtime_provider_model_and_executor_host_required"}
    if runtime not in _SUPPORTED_RUNTIMES:
        return {"status": "blocked", "reason": "unknown_runtime"}
    launcher = _canonical_runtime(_text(assignment.get("launcher")))
    if launcher and launcher not in _SUPPORTED_RUNTIMES:
        return {"status": "blocked", "reason": "unknown_launcher"}
    constraints, reasons = classify_assignment(assignment, policy)
    if not constraints:
        return {"status": "blocked", "reason": "runtime_not_mapped_to_execution_host"}
    allowed_hosts = set.intersection(*constraints)
    if not allowed_hosts:
        return {"status": "blocked", "reason": "runtime_provider_model_placement_conflict",
                "constraints": reasons}
    if host not in allowed_hosts or host not in policy.get("execution_hosts", {}):
        return {"status": "blocked", "reason": "wrong_execution_host",
                "allowed_host_candidates": sorted(allowed_hosts)}
    candidate = policy.get("runtime_candidates", {}).get(runtime, {})
    if candidate.get("enabled") is False:
        return {"status": "blocked", "reason": "runtime_candidate_disabled",
                "verification": candidate.get("verification", "unknown")}
    host_status = candidate.get("host_status", {}).get(host)
    if host_status == "disabled_logged_out":
        return {"status": "blocked", "reason": "runtime_not_authenticated",
                "verification": host_status}
    if host_status and host_status != "verified_ready":
        return {"status": "blocked", "reason": "runtime_candidate_unverified",
                "verification": host_status}
    if runtime == "claude" and provider != "anthropic":
        return {"status": "blocked", "reason": "claude_provider_mismatch"}
    if runtime == "codex" and provider not in {"openai", "codex"}:
        return {"status": "blocked", "reason": "codex_provider_mismatch"}
    if runtime == "ollama" and provider != "ollama":
        return {"status": "blocked", "reason": "ollama_provider_mismatch"}
    if runtime == "qwen" and provider != "qwen":
        return {"status": "blocked", "reason": "qwen_provider_mismatch"}
    if runtime == "gemini" and provider != "google":
        return {"status": "blocked", "reason": "gemini_provider_mismatch"}
    if runtime == "kimi" and provider != "moonshot":
        return {"status": "blocked", "reason": "kimi_provider_mismatch"}
    if runtime == "step" and provider != "stepfun":
        return {"status": "blocked", "reason": "step_provider_mismatch"}
    if runtime == "minimax" and provider != "minimax":
        return {"status": "blocked", "reason": "minimax_provider_mismatch"}
    if runtime == "codebuddy" and provider != "tencent_tokenhub":
        return {"status": "blocked", "reason": "codebuddy_provider_mismatch"}
    host_policy = policy.get("execution_hosts", {}).get(host, {})
    verification = host_policy.get("verification", "unknown")
    if candidate:
        verification = str(candidate.get("binding", "candidate")) + "; " + str(verification)
        if host_status:
            verification += "; " + str(host_status)
    return {"status": "policy_compatible_unverified", "runtime": runtime,
            "host_role": host, "host_label": host_policy.get("hostname"),
            "constraints": reasons, "verification": verification,
            "policy_status": policy.get("policy_status", "unknown")}


def legacy_model_allowed(model: str, policy: Optional[dict] = None) -> Tuple[bool, str]:
    """Legacy box worker placement is unknown; only retain ordinary model compatibility."""
    policy = policy or load_policy()
    value = _text(model)
    blocked = [str(x).casefold() for x in policy.get("legacy_worker_pool", {}).get("blocked_runtime_families", [])]
    if any(re.search(r"(^|[^a-z0-9])" + re.escape(name) + r"([^a-z0-9]|$)", value) for name in blocked):
        return False, "model_requires_pinned_runtime_host"
    return True, "legacy_worker_host_binding_unverified"
