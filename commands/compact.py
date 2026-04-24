"""
/compact [instructions] — Manually trigger context compaction.
Mirrors Claude Code's /compact command in commands/compact/compact.ts.

Runs a forked no-tools LLM call to summarize the conversation, then replaces
the message history with the summary (same as auto-compact but user-triggered,
with optional custom instructions appended to the prompt).
"""
from __future__ import annotations
import commands as _reg
from services.compact import compact, COMPACT_SYSTEM_PROMPT, get_compact_user_summary_message


async def call(args: str, engine) -> str:
    messages = engine._messages
    if not messages:
        return "\033[31mNo messages to compact.\033[0m"

    custom = args.strip()
    system = COMPACT_SYSTEM_PROMPT
    if custom:
        system = system + f"\n\nAdditional instructions: {custom}"

    print("\033[33m[Compacting conversation...]\033[0m")
    compacted = await compact(messages, engine._api, system_override=system)

    engine._messages[:] = compacted
    # Persist the new (single) summary message
    if engine._transcript_path.exists():
        engine._transcript_path.write_text("", encoding="utf-8")
    for msg in compacted:
        engine._save_message(msg)

    return "\033[32mCompaction complete.\033[0m Context has been summarized."


_reg.register({
    "name": "compact",
    "description": "Summarize and compress the conversation history",
    "argument_hint": "[custom instructions]",
    "call": call,
})
