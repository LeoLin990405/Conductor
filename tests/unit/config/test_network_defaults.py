"""Network defaults: the server binds loopback and CORS stays local.

The bind address and the CORS list decide who can reach an unauthenticated
API, so the safe value is the default and every escape hatch is explicit.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.config.models import InfrastructureConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_PY = REPO_ROOT / "server.py"
LOCAL_CONSOLE_ORIGINS = [
    "http://127.0.0.1:5170",
    "http://localhost:5170",
]


@pytest.fixture(scope="module")
def server():
    """Load the root entrypoint without executing its ``__main__`` block."""
    spec = importlib.util.spec_from_file_location(
        "conductor_server_entrypoint", SERVER_PY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBindHost:
    def test_default_is_loopback(self, server, monkeypatch):
        monkeypatch.delenv("CONDUCTOR_BIND_HOST", raising=False)
        assert server.build_parser().parse_args([]).host == "127.0.0.1"

    def test_blank_environment_value_keeps_the_default(self, server, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_BIND_HOST", "   ")
        assert server.build_parser().parse_args([]).host == "127.0.0.1"

    def test_environment_selects_the_bind_host(self, server, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_BIND_HOST", "192.0.2.10")
        assert server.build_parser().parse_args([]).host == "192.0.2.10"

    def test_explicit_host_wins_over_the_environment(self, server, monkeypatch):
        monkeypatch.setenv("CONDUCTOR_BIND_HOST", "192.0.2.10")
        args = server.build_parser().parse_args(["--host", "127.0.0.1"])
        assert args.host == "127.0.0.1"

    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"]
    )
    def test_loopback_spellings_are_recognized(self, server, host):
        assert server.is_loopback_host(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "::", ""])
    def test_wildcard_and_routable_hosts_are_not_loopback(self, server, host):
        assert server.is_loopback_host(host) is False


class TestBindWarning:
    @staticmethod
    def _start(server, monkeypatch, *argv):
        """Run the startup path with uvicorn captured instead of started."""
        monkeypatch.delenv("CONDUCTOR_WORKERS", raising=False)
        runs: list[dict] = []
        monkeypatch.setattr(
            server.uvicorn, "run", lambda *args, **kwargs: runs.append(kwargs)
        )
        server.main(list(argv))
        assert len(runs) == 1
        return runs[0]

    def test_startup_warns_for_a_non_loopback_host(
        self, server, monkeypatch, caplog
    ):
        with caplog.at_level(logging.WARNING, logger=server.__name__):
            run = self._start(server, monkeypatch, "--host", "0.0.0.0")

        assert run["host"] == "0.0.0.0"
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "non-loopback" in record.getMessage()
        ]
        assert warnings
        assert "0.0.0.0" in warnings[0]

    def test_startup_warns_for_a_non_loopback_environment_value(
        self, server, monkeypatch, caplog
    ):
        monkeypatch.setenv("CONDUCTOR_BIND_HOST", "192.0.2.10")
        with caplog.at_level(logging.WARNING, logger=server.__name__):
            run = self._start(server, monkeypatch)

        assert run["host"] == "192.0.2.10"
        assert any(
            record.levelno == logging.WARNING
            and "non-loopback" in record.getMessage()
            and "192.0.2.10" in record.getMessage()
            for record in caplog.records
        )

    def test_startup_is_quiet_on_loopback(self, server, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger=server.__name__):
            run = self._start(server, monkeypatch)

        assert run["host"] == "127.0.0.1"
        assert not [
            record
            for record in caplog.records
            if "non-loopback" in record.getMessage()
        ]


class TestAllowedOrigins:
    def test_model_default_is_the_local_console(self, monkeypatch):
        monkeypatch.delenv("CONDUCTOR_CORS_ORIGINS", raising=False)
        assert InfrastructureConfig().allowed_origins == LOCAL_CONSOLE_ORIGINS

    def test_shipped_config_yaml_uses_the_same_default(self):
        document = yaml.safe_load(
            (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
        )
        assert document["allowed_origins"] == LOCAL_CONSOLE_ORIGINS

    def test_environment_overrides_the_configured_origins(self, monkeypatch):
        from src.config.settings import get_allowed_origins

        configured = InfrastructureConfig(
            allowed_origins=["http://configured.example"]
        )
        monkeypatch.setenv(
            "CONDUCTOR_CORS_ORIGINS",
            " http://one.example , http://two.example ,",
        )
        with patch(
            "src.config.settings.get_infrastructure_config",
            return_value=configured,
        ):
            assert get_allowed_origins() == [
                "http://one.example",
                "http://two.example",
            ]

    @pytest.mark.parametrize("value", ["*", "http://one.example,*", " * "])
    def test_wildcard_environment_is_refused_with_a_warning(
        self, monkeypatch, caplog, value
    ):
        from src.config.settings import get_allowed_origins

        configured = InfrastructureConfig(
            allowed_origins=["http://configured.example"]
        )
        monkeypatch.setenv("CONDUCTOR_CORS_ORIGINS", value)
        with caplog.at_level(logging.WARNING, logger="src.config.settings"):
            with patch(
                "src.config.settings.get_infrastructure_config",
                return_value=configured,
            ):
                assert get_allowed_origins() == ["http://configured.example"]
        assert any("'*'" in record.getMessage() for record in caplog.records)

    def test_wildcard_in_config_falls_back_to_the_local_console(
        self, monkeypatch, caplog
    ):
        from src.config.settings import get_allowed_origins

        monkeypatch.delenv("CONDUCTOR_CORS_ORIGINS", raising=False)
        configured = InfrastructureConfig(allowed_origins=["*"])
        with caplog.at_level(logging.WARNING, logger="src.config.settings"):
            with patch(
                "src.config.settings.get_infrastructure_config",
                return_value=configured,
            ):
                origins = get_allowed_origins()
        assert origins == LOCAL_CONSOLE_ORIGINS
        assert "*" not in origins
        assert any("'*'" in record.getMessage() for record in caplog.records)

    @pytest.mark.parametrize("blank", ["", "   ", ",", " , "])
    def test_blank_environment_keeps_the_configured_origins(
        self, monkeypatch, blank
    ):
        from src.config.settings import get_allowed_origins

        configured = InfrastructureConfig(
            allowed_origins=["http://configured.example"]
        )
        monkeypatch.setenv("CONDUCTOR_CORS_ORIGINS", blank)
        with patch(
            "src.config.settings.get_infrastructure_config",
            return_value=configured,
        ):
            assert get_allowed_origins() == ["http://configured.example"]
