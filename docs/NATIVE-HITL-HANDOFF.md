# Native Agent HITL Handoff

Status: **fixed and live at the app-server and tool re-execution level.**
Delegation-level HITL works. Codex per-command HITL now completes a real
approval → resume → turn, including a simulated PTC GraphInterrupt followed by
a second tool invocation that finds the parked runtime. A full browser/PTC run
is still the final acceptance check.

## Working

- `NativeAgent(require_approval=True)` and `NativeAgents(require_approval=True)`
  pause via the PTC graph's existing `interrupt()` and resume from
  `hitl_response`.
- Approval request → decision mapping → run/reject is unit-tested.
- Deck-level native approvals in a native thread are unchanged.
- Codex app-server now accepts `config_overrides` and
  `developer_instructions`; the latter stopped Codex from delegating the test
  command to an internal Luna subagent.

## Fixed: old stream generator closed the resumed client

The real blocker was in `CodexAppServerRuntime`, not the Codex protocol:

- after an approval, the first `stream_turn` generator was finalized while
  `resume_turn` had already set `_parked=False`;
- its `_finish_or_park()` then closed the shared app-server client;
- the resumed turn had no client left, so it hung with no events.

Added a `_resuming` guard: `_finish_or_park()` will not close the client while
a resume is in flight. With the fix, a real app-server approval on an
allowed command resumed and produced `tool_result`, `text_delta`,
`usage` and `turn_completed`.

## Historical evidence

Evidence from Codex CLI `0.162.1` app-server before the guard:

```text
item/commandExecution/requestApproval
  command="/bin/zsh -lc 'touch /tmp/...'"
  availableDecisions=["accept","decline","cancel"]

client response:
  { "decision": "accept" }
  (also tried acceptForSession)

server:
  serverRequest/resolved { requestId: 0 }
  ... then no item/completed and no turn/completed
```

Observed variants:

| Sandbox | Command needs escalation? | Result |
|---|---|---|
| workspace-write | no (touch /tmp) | command completes without approval |
| workspace-write | network (`curl`) | approval requested; after accept: **hang** |
| read-only | write `/tmp` | approval requested; after accept: **hang** |
| read-only | `acceptForSession` | same hang |

The client response was parsed by Codex (its own log showed `decision:
Accept`), and the command was attempted, but the client was closed before the
resumed turn could deliver events. That was the Conductor guard bug above; it is
now fixed. The broker bug (clearing before resume) was also real and fixed;
`acceptSettings` and developer instructions are kept.

The per-command path is enabled by default. The remaining acceptance step is a
full PTC browser run; the runtime, tool re-execution, approval and turn
completion are proven.

## Safety work already in place

- overall command-approval delegation timeout;
- on timeout, cancel the parked app-server runtime and clear the broker;
- subagent handoff avoided by developer instructions;
- no claim of per-command live proof.

## Next options

1. Re-test against a newer Codex CLI; the protocol schema is version-specific.
2. Capture app-server stdout/stderr around `serverRequest/resolved` and file an
   upstream report.
3. For delegated work that needs escalation, keep delegation non-HITL
   (workspace-write) or run it as a real native thread where the HITL path is
   already live-proven.
4. Do not describe `require_command_approval` as production-ready until a real
   approve → completed turn is recorded.
