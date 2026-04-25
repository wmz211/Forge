from __future__ import annotations
"""
Microcompact — content-clear old tool results to shrink context without a full
summarisation pass.

Mirrors src/services/compact/microCompact.ts (time-based path only).

The source has two MC paths:
  1. Cached microcompact  — uses cache-editing API (ant-only, requires CACHED_MICROCOMPACT feature).
     Not implemented here: requires internal Anthropic cache-editing endpoints.
  2. Time-based microcompact — fires when the gap since the last assistant message
     exceeds a threshold, meaning the server's prompt cache has almost certainly
     expired.  Clears old compactable tool results before the request.

We implement path 2 (time-based) faithfully.

Constants
─────────
TIME_BASED_MC_CLEARED_MESSAGE  mirrors TIME_BASED_MC_CLEARED_MESSAGE in microCompact.ts
IMAGE_MAX_TOKEN_SIZE            mirrors IMAGE_MAX_TOKEN_SIZE in microCompact.ts
COMPACTABLE_TOOLS               mirrors COMPACTABLE_TOOLS set in microCompact.ts
TIME_BASED_MC_CONFIG            mirrors TIME_BASED_MC_CONFIG_DEFAULTS in timeBasedMCConfig.ts
"""
import time
import json

# ── Constants ────────────────────────────────────────────────────────────────

# Mirrors TIME_BASED_MC_CLEARED_MESSAGE in microCompact.ts
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

# Mirrors IMAGE_MAX_TOKEN_SIZE in microCompact.ts
IMAGE_MAX_TOKEN_SIZE = 2_000

# Mirrors COMPACTABLE_TOOLS set in microCompact.ts.
# Only results from these tools are eligible to be content-cleared.
COMPACTABLE_TOOLS: frozenset[str] = frozenset({
    "Read",
    "Bash",
    "PowerShell",
    "Grep",
    "Glob",
    "WebSearch",
    "WebFetch",
    "Edit",
    "Write",
    # Shell tool names from SHELL_TOOL_NAMES
    "computer",
})

# Mirrors TIME_BASED_MC_CONFIG_DEFAULTS in timeBasedMCConfig.ts.
# Default: disabled; gapThresholdMinutes=60; keepRecent=5.
TIME_BASED_MC_CONFIG = {
    "enabled": False,
    "gap_threshold_minutes": 60.0,
    "keep_recent": 5,
}


# ── Token helpers ─────────────────────────────────────────────────────────────

def _calculate_tool_result_tokens(content) -> int:
    """
    Mirrors calculateToolResultTokens() in microCompact.ts.
    Estimates tokens for a tool result's content (string or list of blocks).
    """
    if not content:
        return 0
    if isinstance(content, str):
        return round(len(content) / 4)
    if isinstance(content, list):
        total = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                total += round(len(item.get("text", "")) / 4)
            elif item.get("type") in ("image", "document"):
                total += IMAGE_MAX_TOKEN_SIZE
        return total
    return 0


# ── Tool ID collection ────────────────────────────────────────────────────────

def collect_compactable_tool_ids(messages: list[dict]) -> list[str]:
    """
    Mirrors collectCompactableToolIds() in microCompact.ts.
    Walk assistant messages, collect tool_use IDs whose name is in COMPACTABLE_TOOLS,
    in encounter order.
    """
    ids: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        # OpenAI format: tool_calls list
        for tc in msg.get("tool_calls") or []:
            fn_name = tc.get("function", {}).get("name", "")
            if fn_name in COMPACTABLE_TOOLS:
                ids.append(tc.get("id", ""))
    return [i for i in ids if i]  # filter empty strings


# ── Time-based trigger evaluation ─────────────────────────────────────────────

def evaluate_time_based_trigger(
    messages: list[dict],
    config: dict | None = None,
) -> dict | None:
    """
    Mirrors evaluateTimeBasedTrigger() in microCompact.ts.

    Returns {"gap_minutes": float, "config": dict} when the trigger fires,
    or None when it doesn't (disabled, gap under threshold, no prior assistant
    message, unparseable timestamp).

    Each message may carry a "_timestamp" field (unix seconds float) set by
    the query loop.  Without it we fall back to None (cannot measure gap).
    """
    cfg = config or TIME_BASED_MC_CONFIG
    if not cfg.get("enabled"):
        return None

    # Find the last assistant message with a timestamp.
    last_assistant = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and "_timestamp" in msg:
            last_assistant = msg
            break

    if last_assistant is None:
        return None

    try:
        gap_minutes = (time.time() - float(last_assistant["_timestamp"])) / 60.0
    except (TypeError, ValueError):
        return None

    if not (gap_minutes >= cfg.get("gap_threshold_minutes", 60.0)):
        return None

    return {"gap_minutes": gap_minutes, "config": cfg}


# ── Time-based microcompact ───────────────────────────────────────────────────

def maybe_time_based_microcompact(
    messages: list[dict],
    config: dict | None = None,
) -> dict | None:
    """
    Mirrors maybeTimeBasedMicrocompact() in microCompact.ts.

    When the time-based trigger fires, content-clear all but the most recent
    keep_recent compactable tool results.

    Returns {"messages": list[dict], "tokens_saved": int} when it fires,
    or None when it doesn't (trigger didn't fire, or nothing to clear).

    Unlike cached MC, this mutates message content directly (appropriate because
    the server cache is cold — there's no cached prefix to preserve).
    """
    trigger = evaluate_time_based_trigger(messages, config)
    if not trigger:
        return None

    gap_minutes = trigger["gap_minutes"]
    cfg = trigger["config"]

    compactable_ids = collect_compactable_tool_ids(messages)
    if not compactable_ids:
        return None

    # Floor at 1: slice(-0) returns the full array; clearing ALL results leaves
    # the model with zero working context.  Always keep at least the last one.
    keep_recent = max(1, cfg.get("keep_recent", 5))
    keep_set   = set(compactable_ids[-keep_recent:])
    clear_set  = set(compactable_ids) - keep_set

    if not clear_set:
        return None

    tokens_saved = 0
    result: list[dict] = []

    for msg in messages:
        if msg.get("role") != "tool":
            result.append(msg)
            continue

        tool_call_id = msg.get("tool_call_id", "")
        if (
            tool_call_id in clear_set
            and msg.get("content") != TIME_BASED_MC_CLEARED_MESSAGE
        ):
            tokens_saved += _calculate_tool_result_tokens(msg.get("content"))
            result.append({**msg, "content": TIME_BASED_MC_CLEARED_MESSAGE})
        else:
            result.append(msg)

    if tokens_saved == 0:
        return None

    print(
        f"  \033[90m[Time-based MC] gap {gap_minutes:.0f}min > "
        f"{cfg.get('gap_threshold_minutes', 60)}min, cleared {len(clear_set)} "
        f"tool results (~{tokens_saved} tokens), kept last {len(keep_set)}\033[0m"
    )

    return {"messages": result, "tokens_saved": tokens_saved}


# ── Public entry point ────────────────────────────────────────────────────────

def microcompact_messages(
    messages: list[dict],
    config: dict | None = None,
) -> dict:
    """
    Mirrors microcompactMessages() in microCompact.ts (time-based path).

    Returns {"messages": list[dict], "tokens_saved": int}.
    When no compaction occurred, messages is the original list and tokens_saved=0.

    The cached-MC path (cache-editing API) is not implemented; only the
    time-based path is reproduced here.
    """
    result = maybe_time_based_microcompact(messages, config)
    if result:
        return result
    return {"messages": messages, "tokens_saved": 0}
