"""Isolation settings fail closed without relying on Docker's echoed limits."""

import json
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config.runtime.core import DockerConfig
from ptc_agent.core.sandbox.providers.docker import DockerProvider, DockerRuntime, _CGROUP_LIMITS_SCRIPT
from ptc_agent.core.sandbox.runtime import ExecResult
from tests.unit.core.sandbox.test_docker_provider import _make_exec_mock, _make_mock_container


def provider(**settings):
    instance = DockerProvider(DockerConfig(cpu_count=1, memory_limit="1g", **settings))
    container = _make_mock_container()
    client = MagicMock()
    client.system.info = AsyncMock(return_value={
        "SecurityOptions": ["name=rootless"], "CgroupDriver": "systemd", "CgroupVersion": "2",
    })
    client.images.inspect = AsyncMock()
    client.containers.create = AsyncMock(return_value=container)
    client.containers.get = AsyncMock(return_value=container)
    instance._client = client
    return instance, client, container


@pytest.mark.parametrize("network", ["", "bridge", "default", "host", "none", "container:other"])
def test_private_previews_require_a_named_shared_network(network):
    with pytest.raises(ValueError, match="shared, named"):
        DockerConfig(preview_transport="container", network_mode=network)


def test_private_previews_reject_the_host_override():
    with pytest.raises(ValueError, match="PREVIEW_PROXY_BASE_URL"):
        DockerConfig(preview_transport="container", network_mode="app-runtime", preview_base_url="http://localhost")


def test_publish_address_must_be_literal_and_public_requires_explicit_configuration():
    assert DockerConfig().published_host_ip == "127.0.0.1"
    assert DockerConfig(published_host_ip="0.0.0.0").published_host_ip == "0.0.0.0"
    with pytest.raises(ValueError):
        DockerConfig(published_host_ip="public.example")


@pytest.mark.asyncio
async def test_container_transport_keeps_fuse_and_resource_bounds_without_publishing():
    instance, client, container = provider(preview_transport="container", network_mode="app-runtime")
    runtime = await instance.create()
    sent = client.containers.create.call_args.kwargs["config"]
    host = sent["HostConfig"]
    assert "PortBindings" not in host
    assert "ExtraHosts" not in host
    assert host["NetworkMode"] == "app-runtime"
    assert host["CapAdd"] == ["SYS_ADMIN", "SYS_PTRACE"]
    assert host["Devices"][0]["PathOnHost"] == "/dev/fuse"
    assert host["PidsLimit"] == 512
    assert host["Memory"] == 1024**3
    assert "MemorySwap" not in host
    container.exec = AsyncMock(side_effect=lambda **_: _make_exec_mock(output="123\n"))
    first = await runtime.get_preview_url(8080)
    health = await runtime.get_preview_link(8080)
    assert first.url == health.url == f"http://conductor-sandbox-{runtime.id}:13000"
    assert first.browser_ready is health.browser_ready is False
    assert first.process_local is health.process_local is False
    # Reacquisition derives the same private route, without a process-local cache.
    recovered = await instance.get(runtime.id)
    recovered_preview = await recovered.get_preview_url(8080)
    assert recovered_preview.url == first.url
    assert recovered_preview.browser_ready is False


@pytest.mark.asyncio
async def test_legacy_public_bind_is_an_explicit_opt_in():
    instance, client, _ = provider(published_host_ip="0.0.0.0")
    await instance.create()
    host = client.containers.create.call_args.kwargs["config"]["HostConfig"]
    assert host["PortBindings"]["13000/tcp"] == [{"HostIp": "0.0.0.0", "HostPort": ""}]


@pytest.mark.asyncio
async def test_private_mode_stops_a_legacy_published_container_without_deleting_files():
    instance, _, container = provider(preview_transport="container", network_mode="app-runtime")
    container._container["HostConfig"] = {"PortBindings": {"13000/tcp": [{"HostPort": "30001"}]}}
    with pytest.raises(RuntimeError, match="migrate or recreate"):
        await instance.get("docker-existing")
    container.stop.assert_awaited_once_with(t=0)
    container.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("info", [
    {"SecurityOptions": [], "CgroupDriver": "systemd", "CgroupVersion": "2"},
    {"SecurityOptions": ["name=rootless"], "CgroupDriver": "none", "CgroupVersion": "2"},
    {"SecurityOptions": ["name=rootless"], "CgroupDriver": "systemd", "CgroupVersion": "1"},
])
async def test_strict_deployment_refuses_an_unsafe_daemon_before_image_or_container_work(info):
    instance, client, _ = provider(require_rootless=True)
    client.system.info.return_value = info
    with pytest.raises(RuntimeError, match="requires"):
        await instance.create()
    client.images.inspect.assert_not_awaited()
    client.containers.create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [
    {"cpus": None, "memory": 1024**3, "pids": 512},
    {"cpus": 1, "memory": None, "pids": 512},
    {"cpus": 1, "memory": 1024**3, "pids": None},
    {"cpus": 2, "memory": 1024**3, "pids": 512},
    {"cpus": 1, "memory": 2 * 1024**3, "pids": 512},
    {"cpus": 1, "memory": 1024**3, "pids": 4096},
])
async def test_kernel_unbounded_or_oversized_limits_remove_a_new_container(limits):
    instance, _, container = provider(require_rootless=True)
    result = ExecResult(stdout=json.dumps(limits), stderr="", exit_code=0)
    with patch.object(DockerRuntime, "exec_as_root", AsyncMock(return_value=result)):
        with pytest.raises(RuntimeError, match="kernel CPU/memory/PID"):
            await instance.create()
    container.delete.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_kernel_probe_accepts_limits_and_executes_before_returning_runtime():
    instance, client, container = provider(require_rootless=True)
    result = ExecResult(stdout=json.dumps({"cpus": 1, "memory": 1024**3, "pids": 512}), stderr="", exit_code=0)
    with patch.object(DockerRuntime, "exec_as_root", AsyncMock(return_value=result)) as probe:
        await instance.create()
    assert "cgroup.procs" in probe.call_args.args[0]
    assert "cpu.max" in probe.call_args.args[0]
    assert client.containers.create.call_args.kwargs["config"]["HostConfig"]["MemorySwap"] == 1024**3
    container.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_limits", [True, False])
async def test_stopped_reconnect_defers_kernel_exec_until_started_and_retains_files(valid_limits):
    instance, _, container = provider(require_rootless=True)
    container._container["State"]["Status"] = "exited"
    container._container["HostConfig"] = {
        "NanoCpus": 10**9, "Memory": 1024**3, "MemorySwap": 1024**3, "PidsLimit": 512,
    }
    runtime = await instance.get("docker-existing")

    async def start():
        container._container["State"]["Status"] = "running"

    container.start.side_effect = start

    async def sample(*_args, **_kwargs):
        assert container._container["State"]["Status"] == "running"
        limits = {"cpus": 1, "memory": 1024**3, "pids": 512} if valid_limits else {}
        return ExecResult(stdout=json.dumps(limits), stderr="", exit_code=0)

    with patch.object(DockerRuntime, "exec_as_root", AsyncMock(side_effect=sample)) as probe:
        # The generic reconnect flow calls prepare before start.
        await instance.prepare_reconnect(runtime)
        probe.assert_not_awaited()
        if valid_limits:
            await runtime.start()
            container.stop.assert_not_awaited()
        else:
            with pytest.raises(RuntimeError, match="kernel CPU/memory/PID"):
                await runtime.start()
            container.stop.assert_awaited_once_with(t=0)
        probe.assert_awaited_once()
    container.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_stops_but_retains_files_when_kernel_verification_fails():
    instance, _, container = provider(require_rootless=True)
    container._container["HostConfig"] = {
        "NanoCpus": 10**9, "Memory": 1024**3, "MemorySwap": 1024**3, "PidsLimit": 512,
    }
    runtime = DockerRuntime(container, runtime_id="docker-existing")
    with patch.object(DockerRuntime, "exec_as_root", AsyncMock(return_value=ExecResult(stdout="{}", stderr="", exit_code=0))):
        with pytest.raises(RuntimeError, match="kernel CPU/memory/PID"):
            await instance.prepare_reconnect(runtime)
    container.stop.assert_awaited_once_with(t=0)
    container.delete.assert_not_awaited()


@pytest.mark.skipif(sys.platform != "linux", reason="the kernel cgroup probe runs only on Linux")
def test_cgroup_sampler_reads_its_actual_linux_group():
    result = subprocess.run([sys.executable, "-I", "-c", _CGROUP_LIMITS_SCRIPT], capture_output=True, text=True, check=True)
    assert set(json.loads(result.stdout)) == {"cpus", "memory", "pids"}
