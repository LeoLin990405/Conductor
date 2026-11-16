# mmteam-cc A2A / MCP bridge (absorbed reference)

Source: `LeoLin990405/mmteam-cc` @ `a42a5db` (2026-11-04), Apache-2.0.
See `LICENSE` and `NOTICE` copied from the source repository.

Absorbed:

- `bin/mmteam-a2a-server.py` — minimal A2A v0.3 HTTP server
- `bin/mmteam-mcp.py` — stdio JSON-RPC MCP bridge for A2A
  spawn/send/fanout/pipeline
- `bin/mmteam-a2a-monitor.py` — local A2A monitor
- `commands/{fanout,pipeline,create,spawn,supervise}.md` — orchestration
  commands
- `docs/mmteam-orchestrator.md` — team orchestration skill

This is a **reference absorb**; the scripts are standalone and are not wired into
Conductor's `src/server/services/autonomy` runtime. Conductor already has A2A;
the MCP bridge is the unique piece retained for future integration.

No tokens, bearer registries, `.env`, or installation scripts were copied.
