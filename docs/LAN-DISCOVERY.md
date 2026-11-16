# LAN device discovery

The **Computers** dialog in OSS/local mode is a **local sandbox status** panel:
it reports the Docker sandbox that holds this machine's workspaces, its disk,
and the workspaces on it. Creating, resizing, re-keying or removing machines is
a hosted/platform concern, so those controls are hidden locally.

The same panel lists **LAN devices** the host has recently seen. This is a
read-only neighbour view, not a scanner endpoint that takes targets from the
browser.

```
macOS host                                    Docker / Conductor backend
┌──────────────────────────────┐   HTTP      ┌─────────────────────────────┐
│ arp -an / ip neigh           │  /devices   │ GET /api/v1/computers/      │
│ reverse DNS + port probes    │ ──────────▶ │     lan-devices             │
│ scripts/lan_bridge.py :8766  │  sanitized  │ Computers → 本地沙箱状态     │
└──────────────────────────────┘             └─────────────────────────────┘
```

## Start

Host (macOS/Linux):

```bash
python3 scripts/lan_bridge.py --serve --host 0.0.0.0 --port 8766
```

Local `.env`:

```dotenv
LAN_BRIDGE_URL=http://host.docker.internal:8766/devices
# or read a one-shot snapshot:
# LAN_DEVICES_FILE=/path/to/lan-devices.json
```

Restart the backend after editing `.env`:

```bash
docker compose up -d --force-recreate backend
```

Open **工作区 → 计算机**. In OSS mode the dialog is now **本地沙箱状态**; the
**局域网设备** section shows the device count, subnets, reverse-DNS names,
addresses/MACs and whether SSH (22) or Conductor (8000 / 5173) is reachable.

## What is exposed

The host bridge reads the neighbour table (`arp -an`, falling back to
`ip neigh`), probes 22 / 8000 / 5173, and reverse-resolves the hostname. It
forwards:

- address, optional hostname and MAC;
- open-port booleans and the ports among 22 / 8000 / 5173;
- subnets and a timestamp.

No SSH keys, credentials, traffic content or host files are forwarded. The
backend re-validates every address, subnet and MAC and drops unknown fields.
The route accepts no scan target, so an authenticated caller cannot turn it
into a network scanner.

## Limits

- Discovery starts from the host's neighbour cache. A device that has never
  exchanged traffic with the host will not appear until it does; the refresh
  button re-reads the cache.
- Reverse DNS is best-effort; unnamed devices fall back to their address.
- `host.docker.internal` requires the bridge to listen on `0.0.0.0` on Docker
  Desktop. Keep port 8766 off the public internet; the bridge has no auth.
- It lists devices; it does not add them as computers. Adding a LAN machine
  still goes through the existing SSH pairing flow (platform mode / supported
  SSH provider).
