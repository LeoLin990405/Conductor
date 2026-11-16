# Optional launchd jobs

`cond launchd install` writes LaunchAgents **disabled** (`RunAtLoad` and
`KeepAlive` false, not loaded) into `~/Library/LaunchAgents`
(`CONDUCTOR_LAUNCHD_DIR`). The checkout is this repository.
`CONDUCTOR_REPO_ROOT` in the plist is that path; there is no
`~/conductor-wt/int-r2` default.

```bash
cond launchd install
```

What it writes:

- `com.conductor.supervisor.plist` — `cond up --foreground` with
  `WorkingDirectory` and `CONDUCTOR_REPO_ROOT` set to this checkout. That
  one process starts the backend, the web console, and every daemon in
  `~/.conductor/daemons.toml`.
- `com.conductor.codexbar-snapshot.plist` — the usage snapshot job. The
  template next to this file is filled by `cond launchd render-codexbar`
  and by `install`. It does not start a worker daemon.

A daemon other than Codex is another `[[daemon]]` block in
`~/.conductor/daemons.toml` (`provider` and `id`). The supervisor starts it
with the rest of the fleet. With the backend already running, one provider
in the foreground (this is not a separate LaunchAgent):

```bash
cond foreground daemon --provider cursor --id cursor-id
```

`provider` is any name in `ops_fleet.PROVIDER_ENV` (`cursor`,
`deepseek-dsh`, `kimi`, `grok`, `zcode`, `stepfun`, `minimax`, `claude`,
`mimo`, `doubao`, `deepseek`, or `codex`). See `docs/COND.md`.

An old `com.leo.conductor.server.plist` or `com.leo.conductor.daemon.plist`
in the same directory is renamed to `*.plist.migrated` and is not loaded.
Those jobs pointed at the retired int-r2 checkout.

Enable, disable, uninstall:

```bash
cond launchd enable     # rewrite enabled, bootout, then bootstrap
cond launchd disable    # bootout, rewrite RunAtLoad/KeepAlive=false
cond launchd uninstall  # bootout and delete the plists
```

`install` never calls `launchctl`. Leave the jobs unloaded until each
daemon has a token in `~/.config/conductor/daemons/<id>.token` and
`~/.conductor-daemon/workspaces-cli.json` is in place, or a KeepAlive job
will restart-loop. `cond` can also start the same fleet on demand
(`cond up`) without launchd.
