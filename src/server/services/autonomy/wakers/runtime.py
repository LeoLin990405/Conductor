"""Wake effect enqueue, handler, and runner registration.

Lives under wakers/ so services/autonomy/effects.py stays the generic runner.
"""

from __future__ import annotations

from src.server.database.autonomy import effects as effects_db
from src.server.services.autonomy.effects import AutonomyEffectRunner

async def enqueue_waker_effect(
    *, user_id: str, idempotency_key: str, payload: dict
) -> bool:
    """Insert one wake effect. The idempotency key is the dedupe identity.

    A second insert of the same key returns False and does not queue another
    row. The payload is redacted again here so a caller that skipped the
    waker boundary still cannot persist a credential. CodexBar exhaust/reset
    inserts take the provider lock so publication cannot land in the middle
    of another quota write's UPDATE snapshot.
    """
    from src.server.services.autonomy.wakers.base import EFFECT_WAKE
    from src.server.services.autonomy.wakers.quota_history import QUOTA_GATE_KINDS
    from src.server.services.autonomy.wakers.quota_store import quota_provider_lock
    from src.server.services.autonomy.wakers.base import (
        canonical_provider_id,
        redact_wake_payload,
    )

    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("waker effect requires an idempotency key")
    redacted = redact_wake_payload(payload)
    body = dict(redacted) if isinstance(redacted, dict) else {}

    async def _insert() -> bool:
        return await effects_db.enqueue_effect(
            user_id=user_id or "",
            kind=EFFECT_WAKE,
            idempotency_key=key,
            payload=body,
        )

    source = str(body.get("source") or "")
    kind = str(body.get("kind") or "")
    provider = canonical_provider_id(body.get("provider"))
    if source == "codexbar" and kind in QUOTA_GATE_KINDS and provider:
        async with quota_provider_lock(provider):
            return await _insert()
    return await _insert()


async def waker_effect(effect) -> None:
    """Handle one wake. Switches and the per-repo fix-CI set are read at run time.

    Repository configuration is loaded only for a RepoBar wake whose switch is
    on. A CodexBar quota recovery must not wait on that lookup.
    """
    from src.server.services.autonomy.wakers.settings import (
        get_autonomy_waker_codexbar_enabled,
        get_autonomy_waker_max_quota_retries,
        get_autonomy_waker_repobar_enabled,
    )
    from src.server.services.autonomy.wakers import apply_waker_effect
    from src.server.services.autonomy.wakers.base import WakeEvent

    event = WakeEvent.from_payload(getattr(effect, "payload", None))
    codexbar_on = get_autonomy_waker_codexbar_enabled()
    repobar_on = get_autonomy_waker_repobar_enabled()
    repos = None
    bindings = None
    if event.source == "repobar" and repobar_on:
        from src.server.services.autonomy.wakers.source_store import fix_ci_repositories
        from src.server.services.autonomy.wakers.repobar import load_repo_bindings

        owner = str(getattr(effect, "user_id", "") or "")
        repos = await fix_ci_repositories(owner)
        bindings = await load_repo_bindings(owner)
    await apply_waker_effect(
        effect,
        codexbar_enabled=codexbar_on,
        repobar_enabled=repobar_on,
        fix_ci_repos=repos,
        bindings=bindings,
        max_retries=get_autonomy_waker_max_quota_retries(),
    )


def register_waker_effect(runner: AutonomyEffectRunner) -> None:
    """Bind the wake handler. A second bind of the same handler is a no-op."""
    from src.server.services.autonomy.wakers.base import EFFECT_WAKE

    if EFFECT_WAKE not in runner.registered:
        runner.register(EFFECT_WAKE, waker_effect)
