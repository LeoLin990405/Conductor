# Runbook — dev dispatch rehearsal

Offline evidence for handbook section 2. This does not start the backend,
does not open a socket, and does not push. Sections 3 and 4 are not run here.

## 0. Preconditions

- A checkout of `feat/dispatch-e2e-rehearsal` with its own `.venv`
  (`uv sync --frozen --group dev --extra test --extra cli`).
- `--root` is an empty directory, or a path that does not exist yet.
  The script creates `runs/`, `proposed/`, `home/`, and `repo/` under it.
- Do not point `--root` at a home directory or at `~/.conductor`.

## 1. Command

From the repository root:

```bash
.venv/bin/python scripts/dispatch/e2e_rehearsal.py --root /tmp/conductor-e2e --json
```

`--json` prints the evidence. The same document is written to
`/tmp/conductor-e2e/evidence.json`. Without `--json`, the evidence file is
still written and the process stays quiet on stdout.

The child environment, not the caller, receives:

- `CONDUCTOR_DISPATCH_ADAPTERS=dev`
- `CONDUCTOR_DISPATCH_GATES=on`
- `CONDUCTOR_DISPATCH_POLL_SECONDS=2`
- `CONDUCTOR_DISPATCH_RUNS_DIR`, `CONDUCTOR_DISPATCH_PROPOSED_DIR`,
  `CONDUCTOR_DISPATCH_HOME`, and `HOME` under `--root`
- `CONDUCTOR_DISPATCH_REPO` as a one-commit git repository created under
  `--root/repo`
- `CONDUCTOR_DISPATCH_G2_COMMAND` and `CONDUCTOR_DISPATCH_G5_COMMAND` set to
  the current interpreter running `import sys; sys.exit(0)`

Both gate commands are that one-line process. Evidence records the public
form `python -c "import sys; sys.exit(0)"` and the exit code copied from the
gate record. The interpreter path itself is not written into the evidence.
The disposable repository contains one Alembic revision so the single-head
check can pass, plus the integrate branch and the three dev line branches
the gate runner checks out.

`DispatchService` attaches `GateRunner` only when the adapter is `live`.
The script builds the dev collaborators itself and attaches `GateRunner`
because `CONDUCTOR_DISPATCH_GATES=on`. It does not change `src/`.

Status is the CLI handler in a child process. That child is given an API
client that raises HTTP 404, so the handler reads the local record and adds
`rendered`. Approve and reject resume the same in-process dev machine.
The CLI `approve` and `reject` handlers only POST to the API and do not
fall back when the server is absent, so they are not shelled out.

## 2. What the evidence contains

Each step has `invocation`, `command`, `exit_code`, `duration_ms`, `run_id`,
and `assertion`. `invocation` is `cli` or `in_process:<function>`. Start,
approve, and reject call `drive_dev` or `decide` in process. Their `command`
field names that function. Only `status` is `cli`, because that step calls
the CLI status handler. `proof` records `adapter=dev`, `gates=on`,
`api=not-contacted`, and `launcher=recording` (the dev launcher, not the
native launch script).

The approve handler itself only returns the API response. This rehearsal
does not shell out to it. Passing approve means the following status view
keeps every record field and adds `rendered`.

The file must not contain an email address, an approval token, or the
operator's home directory. Local records under `home/` and `proposed/` can
contain the approval token. Do not publish those directories. Publish
`evidence.json` only.

Expected steps:

1. `start` reaches `APPROVAL`.
2. `status` shows the eight positions in order: G0 SPEC, D1 DAG, 实现中,
   G2 聚焦测试, 主审, 副审, G5 集成, G6 人工. The brief's names
   `implementing` and `次审` are the product labels `实现中` and `副审`.
3. `approve` keeps every record field and adds `rendered`.
4. `start-reject` then `reject`. The rendered G6 line shows the stop
   (`阻塞`) and is not `未到达`. A successful run exits 0.

## 3. Recovery and interrupt

Not run. This waits on W3b and W4. The handbook is more specific: killing
the dispatcher during G2 and restarting it waits on W3b; interrupting
`dispatch_launch.sh` with Ctrl-C waits on W7 B5/B6. Reassign and the
write-scope checks in handbook section 2 step 5 wait on W4 and are not in
this script.

## 4. Native adapter

Not run. Native is manual: the user turns on a worker CLI. This script never
sets `CONDUCTOR_DISPATCH_ADAPTERS=native` and never starts a developer agent.
