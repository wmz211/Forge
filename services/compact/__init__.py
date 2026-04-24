"""
services.compact — context compaction package.

Mirrors the src/services/compact/ directory in Claude Code source:
  auto_compact.py  ← autoCompact.ts   (constants, thresholds, needs_compaction)
  prompt.py        ← prompt.ts        (COMPACT_SYSTEM_PROMPT, formatting helpers)
  compact.py       ← compact.ts       (compact() execution)

Token utilities live in utils/tokens.py (mirrors src/utils/tokens.ts).
"""

from .auto_compact import (
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    AUTOCOMPACT_BUFFER_TOKENS,
    WARNING_THRESHOLD_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    CONTEXT_WINDOW_TOKENS,
    MAX_OUTPUT_TOKENS,
    get_effective_context_window,
    get_autocompact_threshold,
    calculate_token_warning_state,
    needs_compaction,
)
from .prompt import (
    COMPACT_SYSTEM_PROMPT,
    format_compact_summary,
    get_compact_user_summary_message,
)
from .compact import compact, COMPACT_BOUNDARY_KEY
from .snip_compact import snip_compact_if_needed

__all__ = [
    # auto_compact
    "MAX_OUTPUT_TOKENS_FOR_SUMMARY",
    "AUTOCOMPACT_BUFFER_TOKENS",
    "WARNING_THRESHOLD_BUFFER_TOKENS",
    "ERROR_THRESHOLD_BUFFER_TOKENS",
    "MANUAL_COMPACT_BUFFER_TOKENS",
    "CONTEXT_WINDOW_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "get_effective_context_window",
    "get_autocompact_threshold",
    "calculate_token_warning_state",
    "needs_compaction",
    # prompt
    "COMPACT_SYSTEM_PROMPT",
    "format_compact_summary",
    "get_compact_user_summary_message",
    # compact
    "compact",
    "COMPACT_BOUNDARY_KEY",
    # snip_compact
    "snip_compact_if_needed",
]
