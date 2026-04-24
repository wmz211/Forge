from __future__ import annotations
"""
Auto-compact thresholds and token warning state.
Mirrors src/services/compact/autoCompact.ts.
"""

from utils.tokens import token_count_with_estimation

# ── Constants (exact values from autoCompact.ts) ─────────────────────────────

# Reserve this many tokens for output during compaction.
# Based on p99.99 of compact summary output being 17,387 tokens.
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# How many tokens to reserve below the autocompact threshold as a safety buffer.
AUTOCOMPACT_BUFFER_TOKENS = 13_000

# How far below the autocompact threshold the warning and error thresholds sit.
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS   = 20_000

# How many tokens to leave free for /compact (manual) — the blocking limit.
MANUAL_COMPACT_BUFFER_TOKENS = 3_000

# Qwen3-coder-plus context window (128K tokens)
CONTEXT_WINDOW_TOKENS = 128_000

# Max output tokens configured in the API call
MAX_OUTPUT_TOKENS = 8_192


# ── Threshold helpers (mirrors getEffectiveContextWindowSize / getAutoCompactThreshold) ─

def get_effective_context_window() -> int:
    """
    Usable context = full window minus space reserved for model output.
    Mirrors getEffectiveContextWindowSize() in autoCompact.ts.
    """
    reserved = min(MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return CONTEXT_WINDOW_TOKENS - reserved


def get_autocompact_threshold() -> int:
    """
    Token count at which autocompact fires.
    Mirrors getAutoCompactThreshold() in autoCompact.ts.
    """
    return get_effective_context_window() - AUTOCOMPACT_BUFFER_TOKENS


# ── Warning state (mirrors calculateTokenWarningState) ───────────────────────

def calculate_token_warning_state(token_usage: int) -> dict:
    """
    Mirrors calculateTokenWarningState() from autoCompact.ts exactly.
    Returns the same fields used in the query loop to gate compaction.
    """
    effective_window       = get_effective_context_window()
    autocompact_threshold  = get_autocompact_threshold()

    threshold    = autocompact_threshold
    percent_left = max(0, round(((threshold - token_usage) / threshold) * 100))

    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold   = threshold - ERROR_THRESHOLD_BUFFER_TOKENS
    blocking_limit    = effective_window - MANUAL_COMPACT_BUFFER_TOKENS

    return {
        "percentLeft":                 percent_left,
        "isAboveWarningThreshold":     token_usage >= warning_threshold,
        "isAboveErrorThreshold":       token_usage >= error_threshold,
        "isAboveAutoCompactThreshold": token_usage >= autocompact_threshold,
        "isAtBlockingLimit":           token_usage >= blocking_limit,
    }


def needs_compaction(messages: list[dict], last_usage: dict | None = None) -> bool:
    """Returns True when autocompact should fire."""
    token_usage = token_count_with_estimation(messages, last_usage)
    state = calculate_token_warning_state(token_usage)
    return state["isAboveAutoCompactThreshold"]
