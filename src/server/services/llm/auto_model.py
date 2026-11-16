"""Auto model routing: which concrete model an "auto" selection runs.

An ``auto`` turn picks the first model of a domestic chain whose provider key
is present in this deployment, and hands the rest of the chain to the turn's
fallback list so ``ModelResilienceMiddleware`` degrades across it. The chain is
``LLM_AUTO_CHAIN`` (comma-separated model names) or a built-in domestic default.

The sentinel model name is ``auto`` (the manifest entry ``auto`` with provider
``__auto__``): it exists so the model picker can offer the option and saved
preferences validate, and is always resolved to a real model before clients are
built.
"""

from __future__ import annotations

import logging
import os

from src.llms.llm import LLM as LLMFactory

logger = logging.getLogger(__name__)

AUTO_MODEL = "auto"

#: Domestic chain in preference order: coding-plan flagship, cheap fallbacks,
#: then the newer gateways. Models absent from the manifest or whose provider
#: key is unset are skipped at resolution time.
DEFAULT_AUTO_CHAIN = (
    "kimi-k3-coding",
    "deepseek-flash",
    "glm-5.3-cn-coding",
    "doubao-seed-2-1-turbo",
    "mimo-v2.6-flash",
    "step-router-v1",
)


def auto_chain() -> list[str]:
    """The configured chain; ``LLM_AUTO_CHAIN`` wins over the built-in order."""
    raw = os.getenv("LLM_AUTO_CHAIN", "").strip()
    if raw:
        chain = [name.strip() for name in raw.split(",") if name.strip()]
        if chain:
            return chain
    return list(DEFAULT_AUTO_CHAIN)


def _model_available(model: str) -> bool:
    """Whether this deployment can actually reach ``model`` (manifest + key)."""
    config = LLMFactory.get_model_config()
    info = config.get_model_config(model) or {}
    provider = info.get("provider", "")
    provider_info = config.get_provider_info(provider) or {}
    env_key = provider_info.get("env_key")
    if env_key is None:
        # Local/OAuth-style providers carry no env key; the model is reachable
        # when it is in the manifest at all.
        return bool(info)
    return bool(os.getenv(env_key))


def resolve_auto_model() -> tuple[str, list[str]]:
    """``(model to run, fallback chain after it)`` for an auto turn."""
    chain = auto_chain()
    for model in chain:
        if _model_available(model):
            return model, [name for name in chain if name != model]
    logger.warning("[CHAT] No auto-chain model is both in the manifest and keyed; "
                   "falling back to the first chain entry %r", chain[0] if chain else "")
    return (chain[0], chain[1:]) if chain else ("", [])
