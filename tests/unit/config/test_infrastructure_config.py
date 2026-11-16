"""
Tests for InfrastructureConfig (src/config/models.py) — typed config.yaml model.

Covers:
- InfrastructureConfig loads all sections from config.yaml
- BackgroundExecutionConfig subagent timeouts
- settings.py accessors delegate to InfrastructureConfig (backward compat)
"""

import math
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config.models import (
    BackgroundExecutionConfig,
    InfrastructureConfig,
)


# ---------------------------------------------------------------------------
# New model fields
# ---------------------------------------------------------------------------


class TestBackgroundExecutionSubagentTimeouts:
    def test_subagent_collector_timeout(self):
        cfg = BackgroundExecutionConfig(subagent_collector_timeout=60)
        assert cfg.subagent_collector_timeout == 60

    def test_subagent_orphan_collector_timeout(self):
        cfg = BackgroundExecutionConfig(subagent_orphan_collector_timeout=300)
        assert cfg.subagent_orphan_collector_timeout == 300

    def test_defaults(self):
        cfg = BackgroundExecutionConfig()
        assert cfg.subagent_collector_timeout == 120
        assert cfg.subagent_orphan_collector_timeout == 600


class TestCompactionTimeoutValidation:
    """The compaction/admission timeouts gate ``asyncio.wait_for``; a 0 or
    negative value would fire the timeout instantly (a hung-summarize false
    positive on every turn), so they must be strictly positive at config load."""

    @pytest.mark.parametrize("value", [0, -1, -180.0])
    def test_rejects_non_positive_compaction_timeout(self, value):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BackgroundExecutionConfig(compaction_timeout=value)

    @pytest.mark.parametrize("value", [0, -1, -180.0])
    def test_rejects_non_positive_admission_compaction_wait_timeout(self, value):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BackgroundExecutionConfig(admission_compaction_wait_timeout=value)

    def test_accepts_positive_values(self):
        cfg = BackgroundExecutionConfig(
            compaction_timeout=120.0, admission_compaction_wait_timeout=200.0
        )
        assert cfg.compaction_timeout == 120.0
        assert cfg.admission_compaction_wait_timeout == 200.0


class TestInfrastructureConfigComplete:
    def test_extra_fields_allowed(self):
        """InfrastructureConfig allows extra fields (forward compat)."""
        cfg = InfrastructureConfig(unknown_future_field="hello")
        assert cfg.unknown_future_field == "hello"

    def test_full_config_yaml_shape(self):
        """Test loading a dict that mirrors real config.yaml structure."""
        data = {
            "debug": True,
            "workflow_timeout": 1600,
            "sse_keepalive_interval": 30,
            "allowed_origins": ["http://localhost:3000"],
            "log_level": "DEBUG",
            "background_execution": {
                "max_concurrent_workflows": 50,
                "subagent_collector_timeout": 60,
                "subagent_orphan_collector_timeout": 300,
            },
            "redis": {
                "cache_enabled": True,
                "max_connections": 20,
            },
        }
        cfg = InfrastructureConfig(**data)
        assert cfg.debug is True
        assert cfg.workflow_timeout == 1600
        assert cfg.background_execution.subagent_collector_timeout == 60

    def test_memory_federation_requires_explicit_user_owned_absolute_paths(self):
        cfg = InfrastructureConfig()
        assert cfg.memory_federation.user_bindings == {}
        configured = InfrastructureConfig(
            memory_federation={
                "user_bindings": {
                    "user-a": {"memfw_path": "/srv/memory/user-a"},
                    "user-b": {"memfw_path": "/srv/memory/user-b"},
                }
            }
        )
        assert configured.memory_federation.user_bindings["user-a"].memfw_path == "/srv/memory/user-a"

    def test_memory_federation_rejects_shared_or_relative_repository_paths(self):
        with pytest.raises(ValidationError, match="one user only"):
            InfrastructureConfig(
                memory_federation={
                    "user_bindings": {
                        "user-a": {"memfw_path": "/srv/memory/shared"},
                        "user-b": {"memfw_path": "/srv/memory/./shared"},
                    }
                }
            )
        with pytest.raises(ValidationError, match="absolute"):
            InfrastructureConfig(
                memory_federation={
                    "user_bindings": {"user-a": {"memfw_path": "memory/user-a"}}
                }
            )


# ---------------------------------------------------------------------------
# settings.py backward compatibility (accessors delegate to typed config)
# ---------------------------------------------------------------------------


class TestSettingsBackwardCompat:
    """Verify settings.py accessor functions still return correct values."""

    def _patch_infra_config(self, **overrides):
        """Return a patch context that makes get_infrastructure_config return a custom config."""
        cfg = InfrastructureConfig(**overrides)
        return patch("src.config.settings.get_infrastructure_config", return_value=cfg)

    def test_get_workflow_timeout(self):
        from src.config.settings import get_workflow_timeout
        with self._patch_infra_config(workflow_timeout=999):
            assert get_workflow_timeout() == 999

    def test_memory_binding_lookup_is_exactly_per_user(self):
        from src.config.settings import get_memory_federation_binding

        with self._patch_infra_config(
            memory_federation={
                "user_bindings": {
                    "user-a": {"memfw_path": "/srv/memory/a"},
                    "user-b": {"memfw_path": "/srv/memory/b"},
                }
            }
        ):
            assert get_memory_federation_binding("user-a").memfw_path == "/srv/memory/a"
            assert get_memory_federation_binding("user-b").memfw_path == "/srv/memory/b"
            assert get_memory_federation_binding("user-c") is None

    def test_memory_binding_config_is_reloaded_per_config_path(self, tmp_path):
        from src.config.core import load_infrastructure_config

        config_a = tmp_path / "a.yaml"
        config_b = tmp_path / "b.yaml"
        config_a.write_text(
            "memory_federation:\n  user_bindings:\n    user-a:\n      memfw_path: /srv/a\n",
            encoding="utf-8",
        )
        config_b.write_text(
            "memory_federation:\n  user_bindings:\n    user-b:\n      memfw_path: /srv/b\n",
            encoding="utf-8",
        )
        load_infrastructure_config.cache_clear()
        try:
            a = load_infrastructure_config(str(config_a))
            b = load_infrastructure_config(str(config_b))
            assert set(a.memory_federation.user_bindings) == {"user-a"}
            assert set(b.memory_federation.user_bindings) == {"user-b"}
        finally:
            load_infrastructure_config.cache_clear()

    def test_get_debug_mode(self):
        from src.config.settings import get_debug_mode
        with self._patch_infra_config(debug=True):
            assert get_debug_mode() is True

    def test_autonomy_router_model_defaults_to_normal_user_model(self):
        from src.config.settings import get_autonomy_router_model

        with self._patch_infra_config():
            assert get_autonomy_router_model() is None

    def test_autonomy_router_model_returns_explicit_override(self):
        from src.config.settings import get_autonomy_router_model

        with self._patch_infra_config(autonomy_router_model="  router-model  "):
            assert get_autonomy_router_model() == "router-model"

    def test_autonomy_stop_ack_timeout_defaults_and_overrides(self):
        from src.config.settings import get_autonomy_stop_ack_timeout

        with self._patch_infra_config():
            assert get_autonomy_stop_ack_timeout() == 30.0
        with self._patch_infra_config(autonomy_stop_ack_timeout_seconds=45.0):
            assert get_autonomy_stop_ack_timeout() == 45.0

    def test_autonomy_stop_ack_timeout_rejects_non_positive(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            InfrastructureConfig(autonomy_stop_ack_timeout_seconds=0)

    def test_native_runtime_routing_config(self):
        from src.config.settings import get_native_runtime_routing_config

        configured = {
            "preferred_order": ["minimax", "codex"],
            "fallback_only": ["claude-code"],
            "disabled_providers": ["grok"],
        }
        with self._patch_infra_config(native_runtime_routing=configured):
            result = get_native_runtime_routing_config()
        assert result.preferred_order == ["minimax", "codex"]
        assert result.fallback_only == ["claude-code"]
        assert result.disabled_providers == ["grok"]

    def test_get_allowed_origins(self, monkeypatch):
        from src.config.settings import get_allowed_origins

        # The environment override is covered in test_network_defaults.py; this
        # test is about the config.yaml accessor, so keep the env out of it.
        monkeypatch.delenv("CONDUCTOR_CORS_ORIGINS", raising=False)
        with self._patch_infra_config(allowed_origins=["http://example.com"]):
            assert get_allowed_origins() == ["http://example.com"]

    def test_get_redis_max_connections(self):
        from src.config.settings import get_redis_max_connections
        with self._patch_infra_config(redis={"cache_enabled": True, "max_connections": 42}):
            assert get_redis_max_connections() == 42

    def test_is_redis_cache_enabled(self):
        from src.config.settings import is_redis_cache_enabled
        with self._patch_infra_config(redis={"cache_enabled": False}):
            assert is_redis_cache_enabled() is False

    def test_get_sse_keepalive_interval(self):
        from src.config.settings import get_sse_keepalive_interval
        with self._patch_infra_config(sse_keepalive_interval=30):
            assert get_sse_keepalive_interval() == 30

    def test_background_execution_accessors(self):
        from src.config.settings import (
            get_max_concurrent_workflows,
            get_subagent_collector_timeout,
        )
        bg = {"max_concurrent_workflows": 25, "subagent_collector_timeout": 60}
        with self._patch_infra_config(background_execution=bg):
            assert get_max_concurrent_workflows() == 25
            assert get_subagent_collector_timeout() == 60


def test_workflow_run_timeout_must_outlast_its_own_children() -> None:
    """At or below one child_timeout the run-level kill always beats the
    per-child one, so every timeout surfaces as the blunt whole-run failure and
    no child is ever named. The shipped default clears it with margin.
    """
    from src.config.models import WorkflowOrchestrationConfig

    default = WorkflowOrchestrationConfig()
    waves = math.ceil(default.max_dispatches_per_run / default.max_concurrent_children)
    # The default still covers a fully serialized worst case; the validator
    # below no longer demands it.
    assert default.run_timeout > waves * default.child_timeout

    with pytest.raises(ValidationError, match="must exceed child_timeout"):
        WorkflowOrchestrationConfig(child_timeout=1800, run_timeout=1800)


def test_a_narrow_fan_out_is_a_configuration_not_a_contradiction() -> None:
    """The floor must not encode how wide a script chooses to fan out.

    Requiring the run to cover every dispatch serialized made settings each
    field accepts on its own unsatisfiable together: one child at a time needs
    64 waves of child_timeout, past the maximum run_timeout the field allows,
    so no value of run_timeout could load the config at all.
    """
    from src.config.models import WorkflowOrchestrationConfig

    serial = WorkflowOrchestrationConfig(max_concurrent_children=1)
    assert serial.max_concurrent_children == 1

    patient = WorkflowOrchestrationConfig(max_concurrent_children=4, child_timeout=7200)
    assert patient.child_timeout == 7200


class TestAutonomyCloudWorkerRuntimeConfig:
    """The cloud Worker OS-isolation switch is env/config and defaults to off."""

    @staticmethod
    def _patch_infra(**overrides):
        cfg = InfrastructureConfig(**overrides)
        return patch("src.config.settings.get_infrastructure_config", return_value=cfg)

    def test_defaults_to_off(self):
        from src.config.settings import get_autonomy_cloud_worker_runtime

        with self._patch_infra():
            assert get_autonomy_cloud_worker_runtime() is None

    def test_reads_a_configured_runtime(self):
        from src.config.settings import get_autonomy_cloud_worker_runtime

        with self._patch_infra(autonomy_cloud_worker_runtime=" docker "):
            assert get_autonomy_cloud_worker_runtime() == "docker"

    def test_environment_overrides_config(self, monkeypatch):
        from src.config.settings import get_autonomy_cloud_worker_runtime

        monkeypatch.setenv("AUTONOMY_CLOUD_WORKER_RUNTIME", " gvisor ")
        with self._patch_infra(autonomy_cloud_worker_runtime="docker"):
            assert get_autonomy_cloud_worker_runtime() == "gvisor"

    def test_empty_environment_value_explicitly_means_off(self, monkeypatch):
        from src.config.settings import get_autonomy_cloud_worker_runtime

        monkeypatch.setenv("AUTONOMY_CLOUD_WORKER_RUNTIME", "   ")
        with self._patch_infra(autonomy_cloud_worker_runtime="docker"):
            assert get_autonomy_cloud_worker_runtime() is None

    def test_off_word_reaches_the_gate_which_owns_its_meaning(self, monkeypatch):
        from src.config.settings import get_autonomy_cloud_worker_runtime

        monkeypatch.setenv("AUTONOMY_CLOUD_WORKER_RUNTIME", "none")
        with self._patch_infra():
            assert get_autonomy_cloud_worker_runtime() == "none"

    def test_rejects_an_over_long_runtime_id(self):
        with pytest.raises(ValidationError):
            InfrastructureConfig(autonomy_cloud_worker_runtime="x" * 65)
