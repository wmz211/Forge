from __future__ import annotations
"""
Snip compaction — keep head + tail, delete stale middle messages.

The source has snipCompact.ts gated behind feature('HISTORY_SNIP'), which means
the file does not appear in the open-source distribution.  We reproduce the
documented behaviour from query.ts references:
  - snipCompactIfNeeded(messages) → {messages, tokensFreed, boundaryMessage?}
  - Fires when above the warning threshold AND the list is long enough to snip.
  - Preserves _SNIP_HEAD messages from the start (initial context / compact summary)
    and _SNIP_TAIL messages from the end (recent context).
  - Returns tokensFreed so the caller can subtract it from the token estimate
    before the blocking-limit and autocompact checks.

The constants _SNIP_HEAD=2, _SNIP_TAIL=20 are inferred from the referenced
source behaviour (compact summary occupies the first 1-2 messages).
"""

from utils.tokens import estimate_tokens_for_messages, token_count_with_estimation
from .auto_compact import calculate_token_warning_state

# Messages to preserve from the START of the history.
# The first message(s) after a compact contain the boundary marker and summary,
# which must not be dropped.  Mirrors source constants.
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
    Mirrors snipCompactIfNeeded() from query.ts (via the snipModule import).

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
