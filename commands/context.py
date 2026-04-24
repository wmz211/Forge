"""
/context — Visualize current context window usage.
Mirrors Claude Code's /context command in commands/context/.

Shows a colored progress bar of how much context has been consumed,
matching the warning/error thresholds from autoCompact.ts.
"""
from __future__ import annotations
import commands as _reg
from services.compact import (
    calculate_token_warning_state,
    CONTEXT_WINDOW_TOKENS,
    get_autocompact_threshold,
)
from utils.tokens import token_count_with_estimation

_BAR_WIDTH = 40


def _make_bar(used: int, total: int, width: int = _BAR_WIDTH) -> str:
    frac = min(used / total, 1.0)
    filled = round(frac * width)
    empty  = width - filled
    pct = frac * 100

    if pct >= 90:
        color = "\033[31m"   # red
    elif pct >= 70:
        color = "\033[33m"   # yellow
    else:
        color = "\033[32m"   # green

    bar = color + "█" * filled + "\033[90m" + "░" * empty + "\033[0m"
    return f"[{bar}] {pct:5.1f}%"


async def call(args: str, engine) -> str:
    token_usage = token_count_with_estimation(engine._messages, engine._last_usage)
    state = calculate_token_warning_state(token_usage)
    threshold = get_autocompact_threshold()

    bar = _make_bar(token_usage, CONTEXT_WINDOW_TOKENS)
    pct_left = state["percentLeft"]

    status = ""
    if state["isAtBlockingLimit"]:
        status = " \033[31m[BLOCKING — must compact now]\033[0m"
    elif state["isAboveAutoCompactThreshold"]:
        status = " \033[33m[auto-compact threshold reached]\033[0m"
    elif state["isAboveErrorThreshold"]:
        status = " \033[33m[above error threshold]\033[0m"
    elif state["isAboveWarningThreshold"]:
        status = " \033[33m[above warning threshold]\033[0m"

    lines = [
        "\033[1mContext window usage:\033[0m",
        f"  {bar}{status}",
        "",
        f"  Used         : \033[36m{token_usage:,}\033[0m tokens",
        f"  Context limit: {CONTEXT_WINDOW_TOKENS:,} tokens",
        f"  Auto-compact @: {threshold:,} tokens",
        f"  Remaining    : \033[1m{pct_left}%\033[0m before auto-compact",
    ]
    return "\n".join(lines)


_reg.register({
    "name": "context",
    "description": "Visualize current context window usage",
    "aliases": ["ctx"],
    "call": call,
})
