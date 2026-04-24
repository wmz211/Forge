"""
/clear command — reset conversation history.
Mirrors Claude Code's /clear command in commands.ts.

Clears in-memory message history AND truncates the JSONL transcript on disk,
then reloads CLAUDE.md memory so the fresh context still has project instructions.
"""
from __future__ import annotations
import commands as registry


async def _call(args: str, engine) -> str | None:
    engine.clear()

    # After clearing, re-inject CLAUDE.md into the now-reset system prompt
    # so memory survives the /clear (mirrors Claude Code behavior where the
    # system prompt is rebuilt after /clear from static + attachment sources).
    from utils.memory import inject_memory_into_system_prompt
    base = engine.system_prompt
    if "<memory>" in base:
        base = base[:base.index("<memory>")].rstrip()
    engine.system_prompt = inject_memory_into_system_prompt(base, engine.cwd)

    return "Conversation history cleared. Memory files reloaded."


registry.register({
    "name": "clear",
    "description": "Clear conversation history and start fresh",
    "aliases": ["reset"],
    "argument_hint": "",
    "call": _call,
})
