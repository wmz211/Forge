from __future__ import annotations
"""
Compaction prompt text and summary formatting.
Mirrors src/services/compact/prompt.ts.
"""
import re

# ── Prompt fragments (exact text from prompt.ts) ──────────────────────────────

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


# ── Summary post-processing (mirrors formatCompactSummary / getCompactUserSummaryMessage) ─

def format_compact_summary(raw: str) -> str:
    """
    Mirrors formatCompactSummary() from compact/prompt.ts:
    - Strip <analysis> block (drafting scratchpad)
    - Unwrap <summary> tags, add 'Summary:' header
    - Collapse extra blank lines
    """
    result = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw)

    m = re.search(r"<summary>([\s\S]*?)</summary>", result)
    if m:
        content = m.group(1) or ""
        result = re.sub(
            r"<summary>[\s\S]*?</summary>",
            f"Summary:\n{content.strip()}",
            result,
        )

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
