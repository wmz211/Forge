"""
/status — Show session status: model, cwd, session ID, token usage, permissions.
Mirrors Claude Code's /status command in commands/status/.
"""
from __future__ import annotations
import os
import commands as _reg
from services.compact import CONTEXT_WINDOW_TOKENS
from utils.tokens import token_count_with_estimation


async def call(args: str, engine) -> str:
    token_usage = token_count_with_estimation(engine._messages, engine._last_usage)
    pct = token_usage / CONTEXT_WINDOW_TOKENS * 100

    total_in  = getattr(engine, "_total_input_tokens",  0)
    total_out = getattr(engine, "_total_output_tokens", 0)

    lines = [
        "\033[1mForge Status\033[0m",
        "",
        f"  Model        : \033[36m{engine._api.model}\033[0m",
        f"  CWD          : {engine.cwd}",
        f"  Permission   : \033[33m{engine.permission_mode}\033[0m",
        f"  Session ID   : {engine.session_id}",
        f"  Transcript   : {engine.transcript_path}",
        "",
        f"  Messages     : {len(engine._messages)}",
        f"  Context used : \033[{'31' if pct > 80 else '33' if pct > 60 else '32'}m{token_usage:,}\033[0m / {CONTEXT_WINDOW_TOKENS:,} tokens ({pct:.1f}%)",
        f"  Session total: input={total_in:,}  output={total_out:,}",
    ]
    return "\n".join(lines)


_reg.register({
    "name": "status",
    "description": "Show session status: model, cwd, token usage, permissions",
    "call": call,
})
