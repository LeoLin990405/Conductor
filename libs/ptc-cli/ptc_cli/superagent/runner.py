"""Entry point for the interactive Super-agent experience.

``cond`` (and ``ptc-agent --super-agent``) land here. The mode is chosen from
the terminal and the flags:

* ``-p/--print PROMPT`` — non-interactive one-shot: create one Ticket, follow
  it, print the result, exit with the Ticket's outcome.
* full-screen TUI — an interactive terminal of at least 80 columns;
* Rich line shell — a pipe, a narrow terminal, ``--no-tui`` or
  ``CONDUCTOR_NO_TUI``;
* ``--exec COMMAND`` — run one Super-agent command through the line shell and
  exit (unchanged behaviour).

``--continue`` reopens the most recent TUI session in the TUI; the line
fallback resumes its daemon and last Ticket notice only, because the line
shell has no transcript.
"""

from __future__ import annotations

from .shell import SuperAgentShell
from .text import plain
from .tui_permissions import PermissionManager
from .tui import detect_mode, run_print_mode, run_tui
from .tui_sessions import SessionStore


async def run_super_agent(
    *,
    server_url: str,
    user_id: str,
    command: str | None = None,
    no_splash: bool = False,
    print_prompt: str | None = None,
    continue_session: bool = False,
    no_tui: bool = False,
) -> int:
    """Run the Super-agent surface in the right mode for this terminal."""
    if command:
        shell = SuperAgentShell(
            server_url=server_url,
            user_id=user_id,
            command=command,
            no_splash=no_splash,
        )
        return await shell.run()
    if print_prompt is not None:
        return await run_print_mode(
            server_url=server_url,
            user_id=user_id,
            prompt=print_prompt,
        )
    mode = detect_mode(no_tui=no_tui, print_prompt=print_prompt)
    if mode == "tui":
        return await run_tui(
            server_url=server_url,
            user_id=user_id,
            no_splash=no_splash,
            continue_session=continue_session,
        )
    shell = SuperAgentShell(
        server_url=server_url,
        user_id=user_id,
        command=None,
        no_splash=no_splash,
        permissions=PermissionManager(),
    )
    if continue_session:
        record = SessionStore().latest()
        if record is not None:
            shell._daemon_id = record.daemon_id or shell._daemon_id
            shell._daemon_label = record.daemon_label or shell._daemon_label
            target = plain(record.last_ticket_id)[:8] if record.last_ticket_id else "none"
            # Session files are local but still untrusted input: a daemon
            # label recorded from a server must not emit terminal controls.
            print(
                f"resumed session {plain(record.session_id)} "
                f"(line mode; last Ticket {target}; daemon "
                f"{plain(shell._daemon_label) or 'auto'})"
            )
    return await shell.run()


__all__ = ["run_super_agent"]
