# This file is superseded by the services/compact/ package.
# Python 3 will import services/compact/__init__.py instead.
# Do not edit — kept only for git history.
raise ImportError("Import services.compact directly (this file is dead code).")

# ── Constants from autoCompact.ts ────────────────────────────────────────────
# Reserve this many tokens for output during compaction
# Based on p99.99 of compact summary output being 17,387 tokens.
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

# Buffer constants (exact values from autoCompact.ts)
AUTOCOMPACT_BUFFER_TOKENS      = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS   = 20_000
MANUAL_COMPACT_BUFFER_TOKENS    = 3_000

# Qwen3-coder-plus context window (128K tokens)
CONTEXT_WINDOW_TOKENS = 128_000
# Max output tokens we configure in the API call
MAX_OUTPUT_TOKENS = 8_192

# Rough estimation: ~4 chars per token (standard approximation)
CHARS_PER_TOKEN = 4


# ── Token counting (mirrors tokens.ts) ───────────────────────────────────────

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
        # Tool calls
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            total += rough_token_count(fn.get("name", "") + fn.get("arguments", ""))
    return total


def token_count_with_estimation(messages: list[dict], last_usage: dict | None) -> int:
    """
    Mirrors tokenCountWithEstimation() in tokens.ts:
    - Use actual token count from last API response when available
    - Estimate tokens for messages after the last response
    """
    if last_usage:
        # Actual count from API response (most accurate)
        return (
            last_usage.get("prompt_tokens", 0)
            + last_usage.get("completion_tokens", 0)
        )
    # Fallback: rough estimation
    return estimate_tokens_for_messages(messages)


# ── Threshold calculations (mirrors autoCompact.ts) ──────────────────────────

def get_effective_context_window() -> int:
    """
    getEffectiveContextWindowSize() from autoCompact.ts:
    context_window - min(max_output_tokens, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    """
    reserved = min(MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS_FOR_SUMMARY)
    return CONTEXT_WINDOW_TOKENS - reserved


def get_autocompact_threshold() -> int:
    """getAutoCompactThreshold() from autoCompact.ts."""
    return get_effective_context_window() - AUTOCOMPACT_BUFFER_TOKENS


def calculate_token_warning_state(token_usage: int) -> dict:
    """
    Mirrors calculateTokenWarningState() from autoCompact.ts exactly.
    Returns the same fields used in the query loop to gate compaction.
    """
    effective_window  = get_effective_context_window()
    autocompact_threshold = get_autocompact_threshold()

    threshold    = autocompact_threshold
    percent_left = max(0, round(((threshold - token_usage) / threshold) * 100))

    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold   = threshold - ERROR_THRESHOLD_BUFFER_TOKENS
    blocking_limit    = effective_window - MANUAL_COMPACT_BUFFER_TOKENS

    return {
        "percentLeft":                percent_left,
        "isAboveWarningThreshold":    token_usage >= warning_threshold,
        "isAboveErrorThreshold":      token_usage >= error_threshold,
        "isAboveAutoCompactThreshold": token_usage >= autocompact_threshold,
        "isAtBlockingLimit":          token_usage >= blocking_limit,
    }


def needs_compaction(messages: list[dict], last_usage: dict | None = None) -> bool:
    """Returns True when autocompact should fire."""
    token_usage = token_count_with_estimation(messages, last_usage)
    state = calculate_token_warning_state(token_usage)
    return state["isAboveAutoCompactThreshold"]


# ── Compact prompt (exact text from compact/prompt.ts) ───────────────────────

_NO_TOOLS_PREAMBLE = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n"
    "- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.\n"
    "- You already have all the context you need in the conversation above.\n"
    "- Tool calls will be REJECTED and will waste your only turn — you will fail the task.\n"
    "- Your entire response must be plain text: an <analysis> block followed by a <summary> block.\n\n"
)

_NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected and you will fail the task."
)

_DETAILED_ANALYSIS_INSTRUCTION = (
    "Before providing your final summary, wrap your analysis in <analysis> tags to organize "
    "your thoughts and ensure you've covered all necessary points. In your analysis process:\n\n"
    "1. Chronologically analyze each message and section of the conversation. For each section "
    "thoroughly identify:\n"
    "   - The user's explicit requests and intents\n"
    "   - Your approach to addressing the user's requests\n"
    "   - Key decisions, technical concepts and code patterns\n"
    "   - Specific details like:\n"
    "     - file names\n"
    "     - full code snippets\n"
    "     - function signatures\n"
    "     - file edits\n"
    "   - Errors that you ran into and how you fixed them\n"
    "   - Pay special attention to specific user feedback that you received, especially if the "
    "user told you to do something differently.\n"
    "2. Double-check for technical accuracy and completeness, addressing each required element "
    "thoroughly."
)

# BASE_COMPACT_PROMPT from compact/prompt.ts
COMPACT_SYSTEM_PROMPT = (
    _NO_TOOLS_PREAMBLE
    + "Your task is to create a detailed summary of the conversation so far, paying close "
    "attention to the user's explicit requests and your previous actions.\n"
    "This summary should be thorough in capturing technical details, code patterns, and "
    "architectural decisions that would be essential for continuing development work without "
    "losing context.\n\n"
    + _DETAILED_ANALYSIS_INSTRUCTION
    + "\n\nYour summary should include the following sections:\n\n"
    "1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail\n"
    "2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.\n"
    "3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. "
    "Pay special attention to the most recent messages and include full code snippets where applicable.\n"
    "4. Errors and fixes: List all errors that you ran into, and how you fixed them.\n"
    "5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.\n"
    "6. All user messages: List ALL user messages that are not tool results.\n"
    "7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.\n"
    "8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request.\n"
    "9. Optional Next Step: List the next step that you will take that is related to the most recent work.\n\n"
    "Format:\n<analysis>\n[thought process]\n</analysis>\n\n<summary>\n[sections 1-9]\n</summary>"
    + _NO_TOOLS_TRAILER
)


def format_compact_summary(raw: str) -> str:
    """
    Mirrors formatCompactSummary() from compact/prompt.ts:
    - Strip <analysis> block (drafting scratchpad)
    - Unwrap <summary> tags, add 'Summary:' header
    - Collapse extra blank lines
    """
    # Strip <analysis>...</analysis>
    result = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw)

    # Extract and reformat <summary>
    m = re.search(r"<summary>([\s\S]*?)</summary>", result)
    if m:
        content = m.group(1) or ""
        result = re.sub(
            r"<summary>[\s\S]*?</summary>",
            f"Summary:\n{content.strip()}",
            result,
        )

    # Collapse multiple blank lines
    result = re.sub(r"\n\n+", "\n\n", result)
    return result.strip()


def get_compact_user_summary_message(summary: str) -> str:
    """
    Mirrors getCompactUserSummaryMessage() from compact/prompt.ts.
    Wraps the formatted summary in the continuation message.
    """
    formatted = format_compact_summary(summary)
    return (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        f"{formatted}\n\n"
        "Continue the conversation from where it left off without asking the user any further "
        "questions. Resume directly — do not acknowledge the summary, do not recap what was "
        "happening, do not preface with \"I'll continue\" or similar. Pick up the last task "
        "as if the break never happened."
    )


# ── Compaction execution ──────────────────────────────────────────────────────

async def compact(messages: list[dict], api_client, system_override: str | None = None) -> list[dict]:
    """
    Compact the conversation by summarizing it with the LLM.
    Mirrors compactConversation() from compact.ts:
    - Run a forked query (no tools) to generate a structured summary
    - Replace old messages with a single summary user message
    - Keep the compact boundary visible via a system note
    """
    print("\n  \033[33m[Auto-compact triggered — summarizing conversation...]\033[0m")

    # Build conversation text for the summarization call
    history_parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " | ".join(
                str(b.get("content", b)) if isinstance(b, dict) else str(b)
                for b in content
            )
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            names = ", ".join(
                tc.get("function", {}).get("name", "?") for tc in tool_calls
            )
            content = f"[Tool calls: {names}] {content}"
        history_parts.append(f"[{role.upper()}]: {str(content)[:3000]}")

    compact_messages = [{"role": "user", "content": "\n\n".join(history_parts)}]

    # Run a forked no-tools query (mirrors runForkedAgent in compact.ts)
    raw_summary = ""
    async for event in api_client.stream(
        messages=compact_messages,
        tools=None,                     # NO tools — critical (matches NO_TOOLS_PREAMBLE)
        system_prompt=system_override or COMPACT_SYSTEM_PROMPT,
    ):
        if event["type"] == "text":
            raw_summary += event["content"]

    summary_content = get_compact_user_summary_message(raw_summary)

    # Rebuild: single summary message replaces the full history
    # (mirrors buildPostCompactMessages() in compact.ts)
    compacted = [{"role": "user", "content": summary_content}]
    print("  \033[33m[Compact complete]\033[0m\n")
    return compacted
