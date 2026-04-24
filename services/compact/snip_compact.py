from __future__ import annotations
"""
Snip compaction — keep head + tail, delete stale middle messages.
Mirrors src/services/compact/snipCompact.ts.

Only fires when above the warning threshold AND the message list is long
enough to have a meaningful "middle" to remove.  The number of tokens freed
is returned so the caller can adjust its threshold estimate without a full
re-count (mirrors the snipTokensFreed subtraction in queryLoop).
"""

from utils.tokens import token_count_with_estimation, estimate_tokens_for_messages
from .auto_compact import calculate_token_warning_state

# ── Constants (mirrors snipCompact.ts) ───────────────────────────────────────
# Messages to preserve from the START of the history (initial context /
# the compact-summary message from a previous autocompact run).
_SNIP_HEAD = 2

# Messages to preserve from the END of the history (recent context).
_SNIP_TAIL = 20

# Minimum list length before we attempt a snip at all.
_SNIP_MIN_MESSAGES = _SNIP_HEAD + _SNIP_TAIL + 1


def snip_compact_if_needed(
    messages: list[dict],
    last_usage: dict | None = None,
) -> tuple[list[dict], int]:
    """
    Remove messages from the middle of the history when context is getting full.
    Mirrors snipCompactIfNeeded() in snipCompact.ts.

    Returns
    -------
    (new_messages, tokens_freed)
        new_messages  — the (possibly shorter) message list
        tokens_freed  — rough estimate of tokens removed; 0 if nothing was snipped.
                        Subtract from the running token estimate to avoid triggering
                        a heavier compaction step unnecessarily.
    """
    if len(messages) <= _SNIP_MIN_MESSAGES:
        return messages, 0

    # Only snip when we are approaching the warning threshold.
    token_count = token_count_with_estimation(messages, last_usage)
    state = calculate_token_warning_state(token_count)
    if not state["isAboveWarningThreshold"]:
        return messages, 0

    head    = messages[:_SNIP_HEAD]
    tail    = messages[-_SNIP_TAIL:]
    snipped = messages[_SNIP_HEAD : len(messages) - _SNIP_TAIL]

    tokens_freed = estimate_tokens_for_messages(snipped)
    return head + tail, tokens_freed
