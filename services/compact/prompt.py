from __future__ import annotations
"""
Compaction prompt text and summary formatting.
Mirrors src/services/compact/prompt.ts (getCompactPrompt, formatCompactSummary,
getCompactUserSummaryMessage).
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

_DETAILED_ANALYSIS_INSTRUCTION_BASE = (
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

# Mirrors BASE_COMPACT_PROMPT from prompt.ts (used as the final *user* message
# in the compaction API call, NOT as the system prompt).
_BASE_COMPACT_PROMPT = (
    "Your task is to create a detailed summary of the conversation so far, paying close "
    "attention to the user's explicit requests and your previous actions.\n"
    "This summary should be thorough in capturing technical details, code patterns, and "
    "architectural decisions that would be essential for continuing development work without "
    "losing context.\n\n"
    + _DETAILED_ANALYSIS_INSTRUCTION_BASE
    + "\n\nYour summary should include the following sections:\n\n"
    "1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail\n"
    "2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.\n"
    "3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. "
    "Pay special attention to the most recent messages and include full code snippets where applicable and "
    "include a summary of why this file read or edit is important.\n"
    "4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention "
    "to specific user feedback that you received, especially if the user told you to do something differently.\n"
    "5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.\n"
    "6. All user messages: List ALL user messages that are not tool results. These are critical for "
    "understanding the users' feedback and changing intent.\n"
    "7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.\n"
    "8. Current Work: Describe in detail precisely what was being worked on immediately before this summary "
    "request, paying special attention to the most recent messages from both user and assistant. Include file "
    "names and code snippets where applicable.\n"
    "9. Optional Next Step: List the next step that you will take that is related to the most recent work "
    "you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent "
    "explicit requests, and the task you were working on immediately before this summary request. If your "
    "last task was concluded, then only list next steps if they are explicitly in line with the users "
    "request. Do not start on tangential requests or really old requests that were already completed without "
    "confirming with the user first.\n"
    "                       If there is a next step, include direct quotes from the most recent conversation "
    "showing exactly what task you were working on and where you left off. This should be verbatim to ensure "
    "there's no drift in task interpretation.\n\n"
    "Here's an example of how your output should be structured:\n\n"
    "<example>\n"
    "<analysis>\n"
    "[Your thought process, ensuring all points are covered thoroughly and accurately]\n"
    "</analysis>\n\n"
    "<summary>\n"
    "1. Primary Request and Intent:\n"
    "   [Detailed description]\n\n"
    "2. Key Technical Concepts:\n"
    "   - [Concept 1]\n"
    "   - [Concept 2]\n"
    "   - [...]\n\n"
    "3. Files and Code Sections:\n"
    "   - [File Name 1]\n"
    "      - [Summary of why this file is important]\n"
    "      - [Summary of the changes made to this file, if any]\n"
    "      - [Important Code Snippet]\n"
    "   - [File Name 2]\n"
    "      - [Important Code Snippet]\n"
    "   - [...]\n\n"
    "4. Errors and fixes:\n"
    "    - [Detailed description of error 1]:\n"
    "      - [How you fixed the error]\n"
    "      - [User feedback on the error if any]\n"
    "    - [...]\n\n"
    "5. Problem Solving:\n"
    "   [Description of solved problems and ongoing troubleshooting]\n\n"
    "6. All user messages:\n"
    "    - [Detailed non tool use user message]\n"
    "    - [...]\n\n"
    "7. Pending Tasks:\n"
    "   - [Task 1]\n"
    "   - [Task 2]\n"
    "   - [...]\n\n"
    "8. Current Work:\n"
    "   [Precise description of current work]\n\n"
    "9. Optional Next Step:\n"
    "   [Optional Next step to take]\n\n"
    "</summary>\n"
    "</example>\n\n"
    "Please provide your summary based on the conversation so far, following this structure "
    "and ensuring precision and thoroughness in your response. \n\n"
    "There may be additional summarization instructions provided in the included context. "
    "If so, remember to follow these instructions when creating the above summary. Examples of "
    "instructions include:\n"
    "<example>\n"
    "## Compact Instructions\n"
    "When summarizing the conversation focus on typescript code changes and also remember the "
    "mistakes you made and how you fixed them.\n"
    "</example>\n\n"
    "<example>\n"
    "# Summary instructions\n"
    "When you are using compact - please focus on test output and code changes. Include file reads verbatim.\n"
    "</example>\n"
)

# The system prompt for the compaction API call.
# Mirrors: asSystemPrompt(['You are a helpful AI assistant tasked with summarizing conversations.'])
COMPACT_SYSTEM_PROMPT = "You are a helpful AI assistant tasked with summarizing conversations."


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """
    Mirrors getCompactPrompt() from prompt.ts.
    Returns the user message sent to the summarisation model.
    Prepends the no-tools preamble and appends the no-tools trailer.
    """
    prompt = _NO_TOOLS_PREAMBLE + _BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    prompt += _NO_TOOLS_TRAILER
    return prompt


# ── Summary post-processing ───────────────────────────────────────────────────

def format_compact_summary(raw: str) -> str:
    """
    Mirrors formatCompactSummary() from prompt.ts.
    - Strips the <analysis> drafting scratchpad.
    - Unwraps <summary> tags and prepends 'Summary:'.
    - Collapses extra blank lines.
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


def get_compact_user_summary_message(
    summary: str,
    suppress_follow_up_questions: bool = False,
    transcript_path: str | None = None,
    recent_messages_preserved: bool = False,
) -> str:
    """
    Mirrors getCompactUserSummaryMessage() from prompt.ts.
    Wraps the formatted summary in the continuation message injected as the
    first user message after compaction.

    suppress_follow_up_questions — when True (autocompact), instructs the model
        to resume directly without asking the user anything. Mirrors the
        suppressFollowUpQuestions path in the source.
    transcript_path — when set, appends a hint pointing to the full transcript.
    recent_messages_preserved — appended as a note when True.
    """
    formatted = format_compact_summary(summary)
    base = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        f"{formatted}"
    )

    if transcript_path:
        base += (
            f"\n\nIf you need specific details from before compaction (like exact code snippets, "
            f"error messages, or content you generated), read the full transcript at: {transcript_path}"
        )

    if recent_messages_preserved:
        base += "\n\nRecent messages are preserved verbatim."

    if suppress_follow_up_questions:
        return (
            f"{base}\n\n"
            "Continue the conversation from where it left off without asking the user any further "
            "questions. Resume directly — do not acknowledge the summary, do not recap what was "
            "happening, do not preface with \"I'll continue\" or similar. Pick up the last task "
            "as if the break never happened."
        )

    return base
