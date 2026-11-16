#!/usr/bin/env python3
"""Prove raw IPv4 packets cannot use the real backend as a cross-network router.

First prove the packet fixture against a disposable forwarding router on two
disposable networks. Then target a disposable data-network listener through
the real backend MAC. The real backend's settings are read only. No database
traffic or model calls occur. Run only against the dedicated rootless daemon.
"""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import time
import uuid


def docker(*args):
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          check=True, timeout=30).stdout.strip()


def endpoint(container, network):
    info = json.loads(docker("inspect", container))[0]
    item = info["NetworkSettings"]["Networks"][network]
    ipaddress.IPv4Address(item["IPAddress"])
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", item["MacAddress"]):
        raise ValueError("No usable IPv4/MAC endpoint")
    return item


def emit(source, source_endpoint, router_endpoint, destination, nonce):
    program = f"""
import socket, struct
from pathlib import Path
src_mac = {source_endpoint['MacAddress']!r}
dst_mac = {router_endpoint['MacAddress']!r}
interface = next(p.name for p in Path('/sys/class/net').iterdir() if (p/'address').read_text().strip() == src_mac)
payload = {nonce.encode()!r}
udp = struct.pack('!HHHH', 42319, 42320, len(payload)+8, 0) + payload
ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, len(udp)+20, 19001, 0, 64, 17, 0, socket.inet_aton({source_endpoint['IPAddress']!r}), socket.inet_aton({destination!r}))
total = sum(struct.unpack('!10H', ip))
while total >> 16:
    total = (total & 65535) + (total >> 16)
ip = ip[:10] + struct.pack('!H', (~total) & 65535) + ip[12:]
ethernet = bytes.fromhex(dst_mac.replace(':','') + src_mac.replace(':','')) + b'\\x08\\x00'
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as sock:
    sock.bind((interface, 0))
    for _ in range(3):
        sock.send(ethernet + ip + udp)
"""
    docker("exec", "--user", "0", source, "python3", "-I", "-c", program)


def run(args):
    if not re.fullmatch(r"unix:///run/user/[0-9]+/docker.sock", os.getenv("DOCKER_HOST", "")):
        raise ValueError("Set DOCKER_HOST to the dedicated rootless Unix socket")
    if "name=rootless" not in json.loads(docker("info", "--format", "{{json .SecurityOptions}}")):
        raise ValueError("Refusing a rootful daemon")
    image_id = docker("image", "inspect", args.image, "--format", "{{.Id}}")
    flags = docker("exec", args.backend, "cat", "/proc/sys/net/ipv4/ip_forward",
                   "/proc/sys/net/ipv6/conf/all/forwarding", "/proc/sys/net/ipv6/conf/default/forwarding").split()
    if flags != ["0", "0", "0"]:
        raise ValueError("Real backend forwarding must be disabled for IPv4/IPv6")
    real_router = endpoint(args.backend, args.runtime_network)
    endpoint(args.backend, args.data_network)
    prefix = "conductor-forward-probe-" + uuid.uuid4().hex[:10]
    containers = []
    networks = []
    result = {"status": "failed", "image": image_id, "backend_forwarding": flags,
              "ipv6_packet_test": "not_performed; sysctl state only"}

    def container(suffix, network, program=None, forward=False, raw=False):
        name = prefix + "-" + suffix
        containers.append(name)
        command = ["run", "-d", "--pull=never", "--name", name, "--network", network,
                   "--cpus=0.5", "--memory=128m", "--pids-limit=32", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges:true"]
        if raw:
            command += ["--cap-add=NET_RAW"]
        if forward:
            command += ["--sysctl", "net.ipv4.ip_forward=1"]
        command += [args.image]
        command += ["python3", "-u", "-c", program] if program else ["sleep", "infinity"]
        docker(*command)
        return name

    def listener(suffix, network):
        program = """
import socket, time
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(('0.0.0.0',42320));s.settimeout(.25)
print('READY',flush=True)
end=time.monotonic()+45
while time.monotonic()<end:
    try: data,_=s.recvfrom(2048);print(data.decode(),flush=True)
    except socket.timeout: pass
"""
        name = container(suffix, network, program)
        for _ in range(40):
            if "READY" in docker("logs", name):
                return name
            time.sleep(.1)
        raise RuntimeError("UDP fixture did not start")

    try:
        for suffix in ("a", "b"):
            network = prefix + "-" + suffix
            networks.append(network)
            docker("network", "create", "--internal", network)
        router = container("router", networks[0], forward=True)
        docker("network", "connect", networks[1], router)
        receiver = listener("positive-listener", networks[1])
        sender = container("positive-sender", networks[0], raw=True)
        nonce = prefix + "-positive"
        emit(sender, endpoint(sender, networks[0]), endpoint(router, networks[0]),
             endpoint(receiver, networks[1])["IPAddress"], nonce)
        for _ in range(30):
            if nonce in docker("logs", receiver):
                result["positive_raw_route_control"] = True
                break
            time.sleep(.1)
        else:
            raise RuntimeError("Raw-packet positive control failed; cannot interpret a negative")

        receiver = listener("actual-data-listener", args.data_network)
        sender = container("actual-runtime-sender", args.runtime_network, raw=True)
        nonce = prefix + "-negative"
        emit(sender, endpoint(sender, args.runtime_network), real_router,
             endpoint(receiver, args.data_network)["IPAddress"], nonce)
        time.sleep(2)
        if nonce in docker("logs", receiver):
            raise RuntimeError("The real backend forwarded raw traffic into its data network")
        result.update(status="passed", real_backend_raw_forwarding_blocked=True)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        cleaned = True
        for name in reversed(containers):
            try:
                docker("rm", "-f", name)
            except (OSError, subprocess.SubprocessError):
                cleaned = False
        for name in reversed(networks):
            try:
                docker("network", "rm", name)
            except (OSError, subprocess.SubprocessError):
                cleaned = False
        result["temporary_resources_removed"] = cleaned
        if not cleaned:
            result["status"] = "failed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", required=True)
    parser.add_argument("--runtime-network", default="conductor-runtime")
    parser.add_argument("--data-network", default="conductor-data")
    parser.add_argument("--image", default="conductor-rootless-probe:local")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run(args)
    document = json.dumps(result, indent=2) + "\n"
    if args.output:
        with open(args.output, "w") as output:
            output.write(document)
    print(document, end="")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
