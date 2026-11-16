# Live E2E

The default Playwright suite mocks the backend. `make e2e-live` adds the real
stack: the running frontend (`:5173`), running backend (`:8000`), real backend
APIs, and real vendor native CLIs.

## Run

Prerequisites:

```bash
make doctor
make dev        # backend
make dev-web    # frontend
```

Then:

```bash
make e2e-live
make e2e-live ARGS="--quick"                 # API only
make e2e-live ARGS="--providers grok,kimi"   # subset of native turns
make e2e-live ARGS="--json"
```

`make e2e-live` discovers the Home workspace through
`GET /api/v1/workspaces?include_home=true` (or uses `CONDUCTOR_WORKSPACE_ID`).

## UI task walkthrough

```bash
make e2e-ui
```

This is the real-task check for the new shell:

1. Research: submit a real question from `/research`, wait for the real thread,
   and require the exact assistant reply (`UI-WALKTHROUGH-OK` by default);
2. Command Palette: search "settings", land on `/settings` and its heading;
3. Command Palette action: run "Toggle theme", verify the document theme
   changes, then toggle it back.

It fails on page errors. Override `UI_WALKTHROUGH_PROMPT` /
`UI_WALKTHROUGH_REPLY` if the configured model should answer differently.

## What is real

| Check | Real path |
|---|---|
| backend health | `GET /health` + checkpointer |
| native inventory | `GET /api/v1/native-agents`, all 8 expected providers live |
| native turns | real browser composer + real React thread + real vendor CLI, one exact reply per provider |
| native HITL | real Codex command approval: card -> Approve -> file created -> resumed turn (default provider `codex`) |
| UI walkthrough | real Research question/thread/reply + Command Palette search/navigation + theme action (`make e2e-ui`) |
| key pages | `/research`, `/dashboard`, `/chat`, `/chat/<ws>`, `/memory`, `/automations`, `/plugins`, `/settings`, `/settings?tab=experiments` at desktop + mobile |

Last recorded run (2026-10-30):

```text
native inventory: 8 live
browser native turns: 8/8 passed (codex, deepseek-dsh, grok, kimi,
  mimo, minimax, stepfun, zcode)
native HITL: codex approval -> approve -> file -> resumed turn, page errors 0
UI walkthrough: Research reply, 4 live data cards, Settings, theme toggle
page smoke: 20/20 desktop+mobile checks, 0 page errors
```

## HITL coverage

`web/scripts/live-native-hitl-smoke.mjs` exercises the real native approval
chain for Codex: prompt -> command approval request -> browser Approve -> file
created -> exact resumed reply. The mocked Playwright suite still covers the
card states and the HITL resume error paths.

Run it alone:

```bash
cd web
CONDUCTOR_WORKSPACE_ID=<uuid> node scripts/live-native-hitl-smoke.mjs
```
