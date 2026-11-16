"""Rich rendering inside the prompt_toolkit Super-agent TUI.

prompt_toolkit owns the screen (layout, scrollback window, composer, keys);
Rich owns the *content* that benefits from rich layout — the welcome panel, the
``/help`` panel, and the ``/model`` table. Rich renderables are captured to
plain text at the conversation width and appended as conversation blocks, so
they scroll with the rest of the transcript and never draw over the composer.

No ANSI escapes cross into the conversation: the capture console is
non-terminal, and the returned text is plain (``text.plain`` is still applied
by the block renderer, so a box-drawing character is the only decoration).
"""

from __future__ import annotations

import io

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DEFAULT_WIDTH = 100
MIN_WIDTH = 20


def render_plain(renderable: object, *, width: int = DEFAULT_WIDTH) -> str:
    """Render one Rich renderable to plain text at the conversation width."""
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=max(MIN_WIDTH, int(width)),
        force_terminal=False,
        color_system=None,
        highlight=False,
        soft_wrap=False,
    )
    console.print(renderable)
    return buffer.getvalue().rstrip("\n")


def welcome_panel(*, width: int = DEFAULT_WIDTH) -> str:
    """The TUI's opening panel."""
    body = Text()
    body.append("signal → triage → dispatch → event record → repair → cross-review\n")
    body.append("Plain text creates a Ticket on Home and follows it live.\n")
    body.append("Type /help for commands and keys; @path attaches a local file.")
    return render_plain(
        Panel(body, title="Conductor Super-agent TUI", border_style="cyan"),
        width=width,
    )


def help_panel(help_text: str, *, width: int = DEFAULT_WIDTH) -> str:
    """The ``/help`` panel, wrapping the shared help text."""
    return render_plain(
        Panel(Text(help_text), title="Super-agent commands & keys", border_style="cyan"),
        width=width,
    )


def model_table(router_model: str, providers: dict[str, list[str]], *, width: int = DEFAULT_WIDTH) -> str:
    """The ``/model`` view: Router model first, then configured providers.

    The router line is printed above the table (not as its title) so a long
    model id is never wrapped by Rich's centered title.
    """
    router_line = render_plain(Text(f"Router model: {router_model}"), width=width)
    table = Table(title="configured models", show_lines=False)
    table.add_column("provider", style="cyan")
    table.add_column("models")
    for provider, names in sorted(providers.items()):
        table.add_row(Text(str(provider)), Text(", ".join(names)))
    return router_line + "\n" + render_plain(table, width=width)


__all__ = [
    "DEFAULT_WIDTH",
    "MIN_WIDTH",
    "help_panel",
    "model_table",
    "render_plain",
    "welcome_panel",
]
