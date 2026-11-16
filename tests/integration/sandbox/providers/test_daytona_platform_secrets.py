"""Live Daytona Secret tests: placeholder mount, egress substitution, host restriction, rotation.

The substitution oracle is an HTTPS API the operator names with
``PLATFORM_SECRET_ORACLE_URL`` (key in ``PLATFORM_SECRET_ORACLE_API_KEY``): a
request carrying the key in the ``apikey`` header must return 200 only when
the key is valid and 401 otherwise, so a 200 proves Daytona substituted the
placeholder at egress; an unsubstituted placeholder always yields 401. Without
both variables every test here skips. Substitution happens ONLY in HTTPS request
headers (never query strings), and echo services such as httpbin.org are
substitution dead zones — never use them to canary substitution.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import urllib.parse
import uuid

import pytest

from src.config.runtime.core import DaytonaConfig
from ptc_agent.core.sandbox.platform_secrets import (
    PlatformSecretDefinition,
    ResolvedPlatformSecret,
)
from ptc_agent.core.sandbox.providers.daytona import DaytonaProvider


pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider_daytona,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        "daytona"
        not in os.getenv(
            "SANDBOX_TEST_PROVIDERS",
            os.getenv("SANDBOX_TEST_PROVIDER", "memory"),
        )
        .lower()
        .split(","),
        reason="requires Daytona provider",
    ),
]

ORACLE_ENV_VAR = "ORACLE_API_KEY"


def _oracle_url() -> str:
    url = os.getenv("PLATFORM_SECRET_ORACLE_URL", "")
    if not url.startswith("https://"):
        pytest.skip(
            "PLATFORM_SECRET_ORACLE_URL (an HTTPS API that answers 200 only for a "
            "valid key in the apikey header) is required for the live Daytona "
            "Secret test"
        )
    return url


def _oracle_host() -> str:
    return urllib.parse.urlsplit(_oracle_url()).hostname or ""


def _required_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        pytest.skip(f"{name} is required for the live Daytona Secret test")
    return value


def _resolved(
    *,
    env_var: str,
    name: str,
    value: str,
    hosts: tuple[str, ...],
) -> tuple[ResolvedPlatformSecret, ...]:
    definition = PlatformSecretDefinition(
        source_env_var=env_var,
        sandbox_env_var=env_var,
        name_suffix=name,
        description="Conductor Daytona Secret integration test",
        hosts=hosts,
    )
    return (ResolvedPlatformSecret(definition=definition, name=name, value=value),)


def _oracle_probe_command(env_var: str) -> str:
    """In-sandbox oracle request with the key in the `apikey` header.

    Emits only booleans and the HTTP status — never the key or placeholder.
    """
    code = f"""
import json, os, urllib.error, urllib.request
key = os.environ[{env_var!r}]
request = urllib.request.Request(
    {_oracle_url()!r},
    headers={{'apikey': key}},
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        status = response.status
        ok = True
except urllib.error.HTTPError as e:
    status = e.code
    ok = False
print(json.dumps({{'placeholder': key.startswith('dtn_secret_'), 'status': status, 'ok': ok}}))
"""
    return f"python3 -c {shlex.quote(code)}"


def _provider() -> DaytonaProvider:
    api_key = _required_env("DAYTONA_API_KEY")
    base_url = os.getenv("DAYTONA_BASE_URL", "https://app.daytona.io/api")
    return DaytonaProvider(
        DaytonaConfig(api_key=api_key, base_url=base_url, snapshot_enabled=False)
    )


async def _probe(runtime, env_var: str) -> dict:
    result = await runtime.exec(_oracle_probe_command(env_var))
    return json.loads(result.stdout)


async def test_substitution_and_rotation_through_the_oracle():
    """Junk value -> 401; rotate to the real key -> 200 without sandbox recreation.

    The 401 -> 200 transition proves substitution AND rotation in one signal:
    an unsubstituted placeholder can never produce 200.
    """
    oracle_key = _required_env("PLATFORM_SECRET_ORACLE_API_KEY")
    oracle_host = _oracle_host()
    provider = _provider()
    runtime = None
    identity = None
    name = f"conductor_test_{uuid.uuid4().hex}"
    junk = uuid.uuid4().hex
    try:
        identity = (
            await provider.reconcile_platform_secrets(
                _resolved(
                    env_var=ORACLE_ENV_VAR, name=name, value=junk, hosts=(oracle_host,)
                )
            )
        )[0]
        runtime = await provider.create(platform_secret_bindings={ORACLE_ENV_VAR: name})

        env_result = await runtime.exec(
            f"python3 -c 'import os; print(os.environ[\"{ORACLE_ENV_VAR}\"])'"
        )
        placeholder = env_result.stdout.strip()
        assert placeholder == identity.placeholder
        assert placeholder.startswith("dtn_secret_")
        assert placeholder != junk
        assert placeholder != oracle_key

        before = await _probe(runtime, ORACLE_ENV_VAR)
        assert before["placeholder"] is True
        assert before["status"] == 401

        rotated_identity = (
            await provider.reconcile_platform_secrets(
                _resolved(
                    env_var=ORACLE_ENV_VAR, name=name, value=oracle_key, hosts=(oracle_host,)
                )
            )
        )[0]
        assert rotated_identity.placeholder == placeholder

        # Rotation propagates through the egress layer asynchronously
        # (~30s observed live), so allow a 90s window.
        after = None
        for _ in range(30):
            after = await _probe(runtime, ORACLE_ENV_VAR)
            if after["status"] == 200:
                break
            await asyncio.sleep(3)
        assert after == {"placeholder": True, "status": 200, "ok": True}
    finally:
        try:
            if runtime is not None:
                await runtime.delete()
        finally:
            try:
                if identity is not None:
                    await provider._client.secret.delete(identity.provider_secret_id)
            finally:
                await provider.close()


async def test_hot_identity_swap_reaches_new_processes_without_restart():
    """Identity swap via remount on a RUNNING sandbox — no restart needed.

    The restart-free half of convergence: after ``update_secrets`` a fresh
    exec process sees the NEW placeholder immediately, and egress substitutes
    it (junk-bound baseline 401 -> real-key secret 200). This is the
    assumption the request-path hot resync and the sweep's certified-behind
    branch stand on (probe run 2026-07-23 measured 0s to first 200).
    """
    import hashlib

    from ptc_agent.core.sandbox.platform_secrets import (
        remount_platform_secret_bindings,
    )

    oracle_key = _required_env("PLATFORM_SECRET_ORACLE_API_KEY")
    oracle_host = _oracle_host()
    provider = _provider()
    runtime = None
    identity_a = identity_b = None
    name_a = f"conductor_test_a_{uuid.uuid4().hex}"
    name_b = f"conductor_test_b_{uuid.uuid4().hex}"
    junk = uuid.uuid4().hex
    try:
        # Secret A: junk value (401 oracle). Secret B: real key (200 oracle).
        identity_a = (
            await provider.reconcile_platform_secrets(
                _resolved(
                    env_var=ORACLE_ENV_VAR, name=name_a, value=junk, hosts=(oracle_host,)
                )
            )
        )[0]
        identity_b = (
            await provider.reconcile_platform_secrets(
                _resolved(
                    env_var=ORACLE_ENV_VAR,
                    name=name_b,
                    value=oracle_key,
                    hosts=(oracle_host,),
                )
            )
        )[0]
        assert identity_a.placeholder != identity_b.placeholder
        runtime = await provider.create(
            platform_secret_bindings={ORACLE_ENV_VAR: name_a}
        )

        before = await _probe(runtime, ORACLE_ENV_VAR)
        assert before["placeholder"] is True
        assert before["status"] == 401

        # Hot swap A -> B on the running sandbox; no stop/start anywhere.
        await remount_platform_secret_bindings(
            runtime,
            expected={
                ORACLE_ENV_VAR: hashlib.sha256(
                    identity_b.placeholder.encode()
                ).hexdigest()
            },
            bindings={ORACLE_ENV_VAR: name_b},
        )

        env_result = await runtime.exec(
            f"python3 -c 'import os; print(os.environ[\"{ORACLE_ENV_VAR}\"])'"
        )
        assert env_result.stdout.strip() == identity_b.placeholder

        after = None
        for _ in range(30):
            after = await _probe(runtime, ORACLE_ENV_VAR)
            if after["status"] == 200:
                break
            await asyncio.sleep(3)
        assert after == {"placeholder": True, "status": 200, "ok": True}
    finally:
        try:
            if runtime is not None:
                await runtime.delete()
        finally:
            try:
                for identity in (identity_a, identity_b):
                    if identity is not None:
                        await provider._client.secret.delete(
                            identity.provider_secret_id
                        )
            finally:
                await provider.close()


async def test_host_restriction_blocks_substitution():
    """A Secret allowed only for another host must NOT be substituted toward the oracle.

    Uses the real key as the value so a restriction failure is observable as
    an improper 200; the value can only ever reach the oracle's own servers.
    """
    oracle_key = _required_env("PLATFORM_SECRET_ORACLE_API_KEY")
    _oracle_url()  # skips without an oracle
    provider = _provider()
    runtime = None
    identity = None
    name = f"conductor_test_{uuid.uuid4().hex}"
    try:
        identity = (
            await provider.reconcile_platform_secrets(
                _resolved(
                    env_var=ORACLE_ENV_VAR,
                    name=name,
                    value=oracle_key,
                    hosts=("example.com",),
                )
            )
        )[0]
        runtime = await provider.create(platform_secret_bindings={ORACLE_ENV_VAR: name})

        result = await _probe(runtime, ORACLE_ENV_VAR)
        assert result["placeholder"] is True
        assert result["status"] == 401
        assert result["ok"] is False
    finally:
        try:
            if runtime is not None:
                await runtime.delete()
        finally:
            try:
                if identity is not None:
                    await provider._client.secret.delete(identity.provider_secret_id)
            finally:
                await provider.close()
