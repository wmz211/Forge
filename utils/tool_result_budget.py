from __future__ import annotations
"""
Tool result budget — truncate oversized tool results when context is approaching capacity.
Mirrors applyToolResultBudget() in src/utils/toolResultStorage.ts.

Defense ① in the 5-defense compaction pipeline (query loop):
  ① applyToolResultBudget   ← this file
  ② snipCompactIfNeeded
  ③ microcompactMessages
  ④ applyCollapsesIfNeeded   (not implemented — CONTEXT_COLLAPSE feature)
  ⑤ autocompact

When the total token count exceeds the warning threshold, this function finds
tool-result messages that exceed a per-result size limit and replaces their
content with a truncated version + a note.
"""

from utils.tokens import rough_token_count, token_count_with_estimation
from services.compact.auto_compact import calculate_token_warning_state

# Per-result token cap applied when context is above warning threshold.
# Mirrors MAX_TOOL_RESULT_BYTES / BYTES_PER_TOKEN in toolLimits.ts:
# DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000 chars, which at ~4 chars/token ≈ 12_500 tokens.
# The source uses MAX_TOOL_RESULT_BYTES (200_000 bytes) but we use a char limit.
MAX_TOOL_RESULT_TOKENS = 10_000

_TRUNCATION_NOTE = "\n\n[...output truncated by tool-result budget to fit context window...]"

# Characters per token (fallback estimation)
_CHARS_PER_TOKEN = 4


def _truncate_result_content(content: str, max_tokens: int) -> str:
    """
    Truncate a tool result to max_tokens, preserving the first portion.
    Mirrors the truncation logic in toolResultStorage.ts.
    """
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if len(content) <= max_chars:
        return content
    truncated = content[:max_chars]
    # Don't cut mid-line
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl]
    return truncated + _TRUNCATION_NOTE


def apply_tool_result_budget(
    messages: list[dict],
    last_usage: dict | None = None,
) -> list[dict]:
    """
    Truncate oversized tool results when context exceeds the warning threshold.
    Mirrors applyToolResultBudget() in toolResultStorage.ts.

    Only fires when isAboveWarningThreshold is True (otherwise returns messages
    unchanged — no unnecessary truncation on short conversations).

    Returns a new list; original messages are not mutated.
    """
    token_count = token_count_with_estimation(messages, last_usage)
    state = calculate_token_warning_state(token_count)

    if not state["isAboveWarningThreshold"]:
        return messages

    result: list[dict] = []
    for msg in messages:
        if msg.get("role") != "tool":
            result.append(msg)
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            result.append(msg)
            continue

        if rough_token_count(content) <= MAX_TOOL_RESULT_TOKENS:
            result.append(msg)
            continue

        # Truncate — copy the message dict so we don't mutate caller's data
        new_msg = dict(msg)
        new_msg["content"] = _truncate_result_content(content, MAX_TOOL_RESULT_TOKENS)
        result.append(new_msg)

    return result
