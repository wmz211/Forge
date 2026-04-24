from __future__ import annotations
"""
Token counting utilities.
Mirrors src/utils/tokens.ts.
"""

# Rough estimation: ~4 chars per token (standard approximation)
CHARS_PER_TOKEN = 4


def rough_token_count(text: str) -> int:
    """Rough token estimation: ~4 chars per token."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_tokens_for_messages(messages: list[dict]) -> int:
    """
    Rough token count for a message list.
    Mirrors roughTokenCountEstimationForMessages() in tokenEstimation.ts.
    """
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += rough_token_count(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += rough_token_count(str(block.get("content", "")))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += rough_token_count(fn.get("name", "") + fn.get("arguments", ""))
    return total


def token_count_with_estimation(messages: list[dict], last_usage: dict | None) -> int:
    """
    Mirrors tokenCountWithEstimation() in tokens.ts:
    - Use prompt_tokens from last API response when available (most accurate)
    - Otherwise estimate from message content
    """
    if last_usage:
        return last_usage.get("prompt_tokens", 0)
    return estimate_tokens_for_messages(messages)
