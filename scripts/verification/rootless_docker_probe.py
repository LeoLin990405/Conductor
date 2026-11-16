#!/usr/bin/env python3
"""Exercise real rootless Docker, FUSE and private preview routing, without LLMs.

Run as the dedicated rootless daemon user, with DOCKER_HOST naming its socket:
  uv run python scripts/verification/rootless_docker_probe.py \
      --image conductor-sandbox:latest --output probe.json

Uses two temporary containers and a dedicated temporary network, removing all.
The image must already exist. This is provider validation, not HTTP/LLM E2E.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import shlex
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.config.runtime.core import DockerConfig  # noqa: E402
from ptc_agent.core.sandbox.providers.docker import DockerProvider, _CGROUP_LIMITS_SCRIPT  # noqa: E402

_FUSE_SERVER = b'''
import errno, stat
from mfusepy import FUSE, FuseOSError, Operations

class Probe(Operations):
    content = bytearray(b"rootless-fuse-ok\\n")
    def getattr(self, path, fh=None):
        if path == "/":
            return {"st_mode": stat.S_IFDIR | 0o777, "st_nlink": 2, "st_uid": 0, "st_gid": 0}
        if path == "/probe.txt":
            return {"st_mode": stat.S_IFREG | 0o666, "st_nlink": 1, "st_size": len(self.content), "st_uid": 0, "st_gid": 0}
        raise FuseOSError(errno.ENOENT)
    def readdir(self, path, fh):
        return [".", "..", "probe.txt"]
    def open(self, path, flags):
        return 0
    def read(self, path, size, offset, fh):
        return bytes(self.content[offset:offset + size])
    def truncate(self, path, length, fh=None):
        del self.content[length:]
        self.content.extend(b"\\0" * max(0, length - len(self.content)))
    def write(self, path, data, offset, fh):
        self.content.extend(b"\\0" * max(0, offset + len(data) - len(self.content)))
        self.content[offset:offset + len(data)] = data
        return len(data)
    def flush(self, path, fh):
        return 0
    def fsync(self, path, datasync, fh):
        return 0

FUSE(Probe(), "/tmp/conductor-fuse-probe", foreground=True, allow_other=True, default_permissions=True)
'''

_MOUNT_PRESENT = "from pathlib import Path; print(any(line.split()[4] == '/tmp/conductor-fuse-probe' for line in Path('/proc/self/mountinfo').read_text().splitlines()))"


async def unmount_fuse(runtime) -> bool:
    present = await runtime.exec_as_root(f"python3 -I -c {shlex.quote(_MOUNT_PRESENT)}", timeout=5)
    if present.exit_code or present.stdout.strip() not in {"True", "False"}:
        return False
    if present.stdout.strip() == "True":
        await runtime.exec_as_root("umount /tmp/conductor-fuse-probe", timeout=5)
    absent = await runtime.exec_as_root(f"python3 -I -c {shlex.quote(_MOUNT_PRESENT)}", timeout=5)
    return absent.exit_code == 0 and absent.stdout.strip() == "False"


def validate_egress_policy(args: argparse.Namespace) -> None:
    if not args.blocked_url:
        raise ValueError("--public-egress requires at least one known host/private --blocked-url")
    if len(args.blocked_url) > 8:
        raise ValueError("Use at most eight explicitly owned/private probe targets")
    for url in args.blocked_url:
        target = urlsplit(url)
        if target.scheme not in ("http", "https") or target.username or target.password or target.query or target.fragment or target.path not in ("", "/", "/healthz"):
            raise ValueError("Blocked probes must be non-sensitive HTTP IP-literal roots or /healthz")
        ipaddress.ip_address(target.hostname or "")
    for path in (args.policy_config, args.policy_proof):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Public egress requires root-owned policy config and proof")
    config = json.loads(args.policy_config.read_text())
    proof = json.loads(args.policy_proof.read_text())
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if config.get("uid") != os.getuid() or proof.get("config") != digest:
        raise ValueError("Public egress probe must run as the policy's dedicated UID with matching proof")


async def probe_egress(runtime, blocked_urls: list[str]) -> dict:
    script = f"""
import json, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
request = urllib.request.Request('https://qt.gtimg.cn/q=sh000001', headers={{'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'}})
with opener.open(request, timeout=8) as response:
    body = response.read(4096)
    assert response.status == 200 and b'v_sh000001=' in body, 'CN quote response was not usable'
blocked = []
for url in {blocked_urls!r}:
    try:
        with opener.open(url, timeout=3) as response:
            response.read(1)
    except urllib.error.HTTPError:
        blocked.append(False)
    except (urllib.error.URLError, OSError, TimeoutError):
        blocked.append(True)
    else:
        blocked.append(False)
print(json.dumps({{'cn_quote_received': True, 'blocked_targets': blocked}}))
"""
    result = await runtime.exec(f"python3 -I -c {shlex.quote(script)}", timeout=45)
    if result.exit_code:
        raise RuntimeError("Public CN quote probe failed (check private runtime logs for transport details)")
    findings = json.loads(result.stdout)
    if not all(findings["blocked_targets"]):
        raise RuntimeError("A host/private HTTP target was reachable from the sandbox")
    return findings


async def run(args: argparse.Namespace) -> dict:
    if args.public_egress:
        validate_egress_policy(args)
    network_name = args.network or f"conductor-probe-{uuid.uuid4().hex[:12]}"
    provider = DockerProvider(DockerConfig(
        image=args.image, network_mode=network_name, preview_transport="container",
        require_rootless=True, cpu_count=1, memory_limit="1g", pids_limit=128,
    ))
    runtimes = []
    network = None
    fuse_runtime = None
    result = {"utc": datetime.now(timezone.utc).isoformat(), "status": "failed", "scope": "rootless provider/FUSE/private preview, no HTTP app or LLM calls"}
    try:
        client = await provider._get_client()
        # Do not let a missing image trigger the provider's auto-build.
        await client.images.inspect(args.image)
        # Never attach probes to an application's existing network or delete it.
        if await client.networks.list(filters={"name": network_name}):
            raise RuntimeError("Probe network already exists; choose a new name")
        network = await client.networks.create({
            "Name": network_name, "Driver": "bridge", "Internal": not args.public_egress,
            "Labels": {"io.conductor.verification": "rootless-probe"},
        })
        server = await provider.create()
        runtimes.append(server)
        peer = await provider.create()
        runtimes.append(peer)
        for runtime in runtimes:
            info = await runtime._container.show()
            if (info.get("HostConfig") or {}).get("PortBindings"):
                raise RuntimeError("Sandbox published a host port")
        kernel = await server.exec_as_root(f"python3 -I -c {shlex.quote(_CGROUP_LIMITS_SCRIPT)}")
        result["kernel_limits"] = json.loads(kernel.stdout)
        if args.public_egress:
            result["egress"] = await probe_egress(server, args.blocked_url)
            result["egress"]["counter_correlation"] = "requires root nft counter snapshot before/after this run"

        await server.upload_file((ROOT / "src/ptc_agent/core/sandbox/livefs_runtime/mfusepy.py").read_bytes(), "/tmp/mfusepy.py")
        await server.upload_file(_FUSE_SERVER, "/tmp/conductor-fuse-probe.py")
        fuse_runtime = server
        started = await server.exec_as_root(
            "mkdir -p /tmp/conductor-fuse-probe; FUSE_LIBRARY_NAME=fuse3 nohup python3 /tmp/conductor-fuse-probe.py >/tmp/conductor-fuse-probe.log 2>&1 </dev/null &"
        )
        if started.exit_code:
            raise RuntimeError("FUSE probe did not launch")
        for _ in range(40):
            mounted = await server.exec("cat /tmp/conductor-fuse-probe/probe.txt", timeout=2)
            if mounted.exit_code == 0 and mounted.stdout.strip() == "rootless-fuse-ok":
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Rootless FUSE mount/read failed")
        result["fuse_read"] = True
        write_script = "from pathlib import Path; p=Path('/tmp/conductor-fuse-probe/probe.txt'); p.write_text('rootless-fuse-write-ok\\n'); assert p.read_text() == 'rootless-fuse-write-ok\\n'"
        written = await server.exec(f"python3 -I -c {shlex.quote(write_script)}", timeout=5)
        if written.exit_code:
            raise RuntimeError("Rootless FUSE write/readback failed")
        result["fuse_write"] = True
        result["fuse_unmounted"] = await unmount_fuse(server)
        if not result["fuse_unmounted"]:
            raise RuntimeError("Rootless FUSE explicit unmount failed")
        fuse_runtime = None

        await server.upload_file(b"private-preview-ok", f"{server.working_dir}/probe.txt")
        await server.stop(force=True)
        server = await provider.get(server.id)
        await provider.prepare_reconnect(server)
        await server.start()
        retained = await server.exec("cat probe.txt", timeout=5)
        if retained.exit_code or retained.stdout.strip() != "private-preview-ok":
            raise RuntimeError("Sandbox restart did not preserve its files")
        result["restart_preserves_files"] = True
        launched = await server.exec(
            "nohup python3 -m http.server 8080 --bind 127.0.0.1 >/tmp/conductor-http-probe.log 2>&1 </dev/null &"
        )
        if launched.exit_code:
            raise RuntimeError("Preview probe server did not launch")
        preview = await server.get_preview_url(8080)
        for _ in range(40):
            script = "import urllib.request; print(urllib.request.urlopen(" + repr(preview.url + "/probe.txt") + ", timeout=2).read().decode())"
            fetched = await peer.exec(f"python3 -I -c {shlex.quote(script)}", timeout=4)
            if fetched.exit_code == 0 and fetched.stdout.strip() == "private-preview-ok":
                break
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError("Private preview was not reachable from a peer container")
        result.update(status="passed", private_preview=True, host_ports_published=False)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        cleaned = True
        if fuse_runtime is not None:
            try:
                result["fuse_unmounted"] = await unmount_fuse(fuse_runtime)
            except Exception:
                result["fuse_unmounted"] = False
        for runtime in reversed(runtimes):
            try:
                await runtime.delete()
            except Exception:
                cleaned = False
        result["containers_removed"] = cleaned
        if network is not None:
            try:
                removed = await network.delete()
                if await client.networks.list(filters={"name": network_name}):
                    removed = False
                result["network_removed"] = removed
            except Exception:
                result["network_removed"] = False
            cleaned = cleaned and result["network_removed"]
        try:
            await provider.close()
        except Exception:
            cleaned = False
        if not cleaned:
            result["status"] = "failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", help="New temporary network name (default: unique conductor-probe-…)")
    parser.add_argument("--image", default="conductor-sandbox:latest")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--public-egress", action="store_true", help="Requires installed root-owned UID policy; also test public CN data and blocked IPs")
    parser.add_argument("--blocked-url", action="append", default=[], help="Known host/private IP HTTP root that must be unreachable; at most eight")
    parser.add_argument("--policy-config", type=Path, default=Path("/etc/conductor/egress-policy.json"))
    parser.add_argument("--policy-proof", type=Path, default=Path("/run/conductor-egress/proof.json"))
    args = parser.parse_args()
    result = asyncio.run(run(args))
    document = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(document)
    print(document, end="")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
