from __future__ import annotations
"""
Token counting utilities.
Mirrors src/utils/tokens.ts + src/services/tokenEstimation.ts.
"""
import json

# ── roughTokenCountEstimation ─────────────────────────────────────────────────
# Mirrors roughTokenCountEstimation(content, bytesPerToken=4) in tokenEstimation.ts.
BYTES_PER_TOKEN = 4

_IMAGE_MAX_TOKEN_SIZE = 2000  # mirrors IMAGE_MAX_TOKEN_SIZE in microCompact.ts


def rough_token_count(text: str, bytes_per_token: int = BYTES_PER_TOKEN) -> int:
    """
    Mirrors roughTokenCountEstimation() from tokenEstimation.ts.
    bytes_per_token=4 is the default; dense JSON files use 2.
    """
    return round(len(text) / bytes_per_token)


def bytes_per_token_for_file_type(file_extension: str) -> int:
    """
    Mirrors bytesPerTokenForFileType() from tokenEstimation.ts.
    Dense JSON has many single-character tokens, so ratio is ~2.
    """
    if file_extension in ("json", "jsonl", "jsonc"):
        return 2
    return 4


def rough_token_count_for_file_type(content: str, file_extension: str) -> int:
    """Mirrors roughTokenCountEstimationForFileType() from tokenEstimation.ts."""
    return rough_token_count(content, bytes_per_token_for_file_type(file_extension))


# ── Per-block token estimation ────────────────────────────────────────────────

def _rough_token_count_for_block(block) -> int:
    """
    Mirrors roughTokenCountEstimationForBlock() from tokenEstimation.ts.

    Handles each content block type:
      text            → count the text field
      image/document  → flat 2000 tokens (avoids base64 blowup)
      tool_result     → recurse into content (string or list)
      tool_use        → count name + JSON-serialized input
      thinking        → count the thinking field
      redacted_thinking → count the data field
      anything else   → count JSON serialization
    """
    if isinstance(block, str):
        return rough_token_count(block)
    if not isinstance(block, dict):
        return rough_token_count(str(block))

    btype = block.get("type", "")

    if btype == "text":
        return rough_token_count(block.get("text", ""))

    if btype in ("image", "document"):
        # https://platform.claude.com/docs/en/build-with-claude/vision
        # Conservative estimate matching microCompact's IMAGE_MAX_TOKEN_SIZE.
        return _IMAGE_MAX_TOKEN_SIZE

    if btype == "tool_result":
        return _rough_token_count_for_content(block.get("content"))

    if btype == "tool_use":
        # input is arbitrary JSON the model generated — stringify once for the char count.
        # Mirrors: roughTokenCountEstimation(block.name + jsonStringify(block.input ?? {}))
        return rough_token_count(
            block.get("name", "") + json.dumps(block.get("input") or {})
        )

    if btype == "thinking":
        return rough_token_count(block.get("thinking", ""))

    if btype == "redacted_thinking":
        return rough_token_count(block.get("data", ""))

    # server_tool_use, web_search_tool_result, mcp_tool_use, etc.
    # Stringify-length tracks the serialized form; key/bracket overhead is
    # single-digit percent on real blocks.
    return rough_token_count(json.dumps(block))


def _rough_token_count_for_content(content) -> int:
    """
    Mirrors roughTokenCountEstimationForContent() from tokenEstimation.ts.
    Handles string | list[block] | None.
    """
    if not content:
        return 0
    if isinstance(content, str):
        return rough_token_count(content)
    if isinstance(content, list):
        return sum(_rough_token_count_for_block(b) for b in content)
    return rough_token_count(str(content))


# ── Message-level estimation ───────────────────────────────────────────────────

def rough_token_count_for_message(message: dict) -> int:
    """
    Mirrors roughTokenCountEstimationForMessage() from tokenEstimation.ts.
    Handles user/assistant message dicts in OpenAI format.
    """
    role = message.get("role", "")
    if role in ("assistant", "user"):
        content = message.get("content")
        total = _rough_token_count_for_content(content)
        # OpenAI-format tool_calls on assistant messages
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += rough_token_count(
                fn.get("name", "") + fn.get("arguments", "")
            )
        return total
    # tool / system messages
    content = message.get("content")
    if isinstance(content, str):
        return rough_token_count(content)
    return 0


def estimate_tokens_for_messages(messages: list[dict]) -> int:
    """
    Mirrors roughTokenCountEstimationForMessages() from tokenEstimation.ts.
    Sums per-message estimates.
    """
    return sum(rough_token_count_for_message(m) for m in messages)


# ── Token count with API-usage anchor ────────────────────────────────────────

def get_token_count_from_usage(usage: dict) -> int:
    """
    Mirrors getTokenCountFromUsage() from tokens.ts.
    Full context window = input + cache_creation + cache_read + output.
    """
    return (
        usage.get("prompt_tokens", 0)
        + usage.get("cache_creation_tokens", 0)
        + usage.get("cache_read_tokens", 0)
        + usage.get("completion_tokens", 0)
    )


def token_count_from_last_api_response(messages: list[dict]) -> int:
    """
    Mirrors tokenCountFromLastAPIResponse() from tokens.ts.
    Walks backwards to find the last message with usage data.
    """
    for msg in reversed(messages):
        usage = msg.get("_usage")
        if usage:
            return get_token_count_from_usage(usage)
    return 0


def token_count_with_estimation(
    messages: list[dict],
    last_usage: dict | None = None,
) -> int:
    """
    Mirrors tokenCountWithEstimation() from tokens.ts.

    Priority:
    1. last_usage dict (from the most recent API response) — most accurate.
    2. Rough estimation from message content.

    The `last_usage` dict may use either OpenAI-compat keys
    (prompt_tokens / completion_tokens) or Anthropic-compat keys
    (input_tokens / output_tokens / cache_creation_input_tokens /
    cache_read_input_tokens).  We normalise both.
    """
    if last_usage:
        # OpenAI compat (dashscope)
        pt = last_usage.get("prompt_tokens", 0)
        ct = last_usage.get("completion_tokens", 0)
        # Anthropic compat
        it  = last_usage.get("input_tokens", 0)
        ot  = last_usage.get("output_tokens", 0)
        cc  = last_usage.get("cache_creation_input_tokens", 0)
        cr  = last_usage.get("cache_read_input_tokens", 0)
        total = (pt + ct) or (it + ot + cc + cr)
        if total > 0:
            return total
    return estimate_tokens_for_messages(messages)
