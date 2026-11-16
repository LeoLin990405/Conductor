"""Run the official ``lark-cli`` for Feishu/Lark: typed argv and a bounded call.

The CLI owns the Feishu app binding and user authorization, so this module
keeps no credentials: ``_build_args`` builds a typed, read-first command and
``_run_lark`` runs it in its own process group and returns what it printed.
Used by the Router's Feishu Source (``services/autonomy/feishu.py``) and, until
it is retired, the in-server ``FeishuCLI`` agent tool.
"""

from __future__ import annotations

import asyncio
import os
import shutil

from src.llms.native_cli.process import kill_child_group, spawn_group

_ALLOWED_ACTIONS = {
    "status",
    "list_chats",
    "search_chats",
    "read_messages",
    "search_messages",
    "send_message",
}


def _binary() -> str | None:
    configured = os.getenv("FEISHU_CLI_BIN", "").strip()
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    return shutil.which("lark-cli")


def _identity_args(identity: str, *, default: str = "") -> list[str]:
    identity = (identity or default or "").strip().lower()
    if not identity:
        return []
    if identity not in {"bot", "user"}:
        raise ValueError("identity 只能是 bot、user 或空")
    return ["--as", identity]


def _build_args(
    action: str,
    *,
    chat_id: str,
    query: str,
    start: str,
    end: str,
    limit: int,
    text: str,
    identity: str,
) -> list[str]:
    """The CLI argv for one typed action. Never carries credentials."""
    action = action.strip().lower()
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"action 必须是 {', '.join(sorted(_ALLOWED_ACTIONS))}")
    limit = max(1, min(int(limit), 50))
    page = ["--format", "json", "--page-size", str(limit)]
    if action == "status":
        return ["whoami"]
    if action == "list_chats":
        return ["im", "+chat-list", *page, "--types", "group", *_identity_args(identity, default="user")]
    if action == "search_chats":
        if not query.strip():
            raise ValueError("search_chats 需要 query（群名关键字）")
        return ["im", "+chat-search", "--query", query.strip(), *page, *_identity_args(identity, default="user")]
    if action == "read_messages":
        if not chat_id.strip():
            raise ValueError("read_messages 需要 chat_id（先用 search_chats 查 oc_...）")
        args = [
            "im", "+chat-messages-list",
            "--chat-id", chat_id.strip(),
            "--order", "desc",
            *page,
        ]
        if start.strip():
            args += ["--start", start.strip()]
        if end.strip():
            args += ["--end", end.strip()]
        args += _identity_args(identity, default="user")
        return args
    if action == "search_messages":
        if not query.strip() and not chat_id.strip():
            raise ValueError("search_messages 至少需要 query 或 chat_id")
        args = ["im", "+messages-search", *page]
        if query.strip():
            args += ["--query", query.strip()]
        if chat_id.strip():
            args += ["--chat-id", chat_id.strip()]
        if start.strip():
            args += ["--start", start.strip()]
        if end.strip():
            args += ["--end", end.strip()]
        args += _identity_args(identity, default="user")
        return args
    # send_message
    if not chat_id.strip() or not text.strip():
        raise ValueError("send_message 需要 chat_id 和 text")
    return [
        "im", "+messages-send",
        "--chat-id", chat_id.strip(),
        "--text", text,
        "--format", "json",
        *_identity_args(identity),
    ]


async def _run_lark(args: list[str], *, timeout: float) -> tuple[int, str, str]:
    binary = _binary()
    if binary is None:
        return (
            127,
            "",
            "找不到 lark-cli。安装：npm install -g @larksuite/cli；"
            "或在 FEISHU_CLI_BIN 指向可执行文件。然后运行 lark-cli config init --new。",
        )
    # ``lark-cli`` is a node shim and takes the model's arguments, so it can
    # block for as long as a login or a network wait lasts: it is owned like an
    # agent's child, killed by group on a timeout and on a cancellation.
    try:
        process = await spawn_group(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except OSError as exc:
        return 127, "", f"无法启动 lark-cli：{exc}"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        kill_child_group(process)
        await process.communicate()
        return 124, "", f"lark-cli 超时（{timeout:g}s）"
    except BaseException:
        kill_child_group(process)
        raise
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


__all__ = ["_ALLOWED_ACTIONS", "_binary", "_build_args", "_identity_args", "_run_lark"]
