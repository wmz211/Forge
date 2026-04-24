"""
/init command — create or update the project CLAUDE.md file.
Mirrors the /init command in Claude Code's commands.ts.

Behavior:
  - If no CLAUDE.md exists in cwd, create one by asking the LLM to analyze
    the project and write a useful instructions file.
  - If one already exists, offer to review and refresh it.
  - Writes the result to <cwd>/CLAUDE.md and reloads it into the system prompt.
"""
from __future__ import annotations
import os
from pathlib import Path

import commands as registry

_INIT_PROMPT = """\
Analyze this project directory and create a concise CLAUDE.md file that will \
help an AI coding assistant understand the project quickly. Include:

1. **Project overview** — what this project does in 1-2 sentences
2. **Tech stack** — language, frameworks, key libraries
3. **Directory structure** — brief guide to the main directories
4. **Build & run** — how to install, build, test, and run the project
5. **Code style** — naming conventions, formatting rules, any style guide
6. **Key files** — the most important files a new contributor should read

Keep it concise (under 500 words). Use Markdown formatting.
Start immediately — no preamble or meta-commentary.
"""

_REFRESH_PROMPT = """\
Review the existing CLAUDE.md above and update it to reflect the current state \
of the project. Improve any outdated information, add missing details, and keep \
it under 500 words. Return ONLY the updated CLAUDE.md content — no commentary.
"""


async def _call(args: str, engine) -> str | None:
    cwd = Path(engine.cwd)
    target = cwd / "CLAUDE.md"
    existing = ""

    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    action = args.strip().lower() if args.strip() else "create"
    if action not in ("refresh", "update"):
        action = "refresh" if existing else "create"

    # Build a directory listing for context
    try:
        entries = sorted(
            p.name for p in cwd.iterdir()
            if not p.name.startswith(".")
            and p.name not in {"node_modules", "__pycache__", ".venv", "venv"}
        )
        listing = "\n".join(entries[:60])
    except OSError:
        listing = "(could not list directory)"

    if action == "create":
        prompt_text = (
            f"Project directory: {cwd}\n\n"
            f"Directory contents:\n{listing}\n\n"
            + _INIT_PROMPT
        )
        print("  [/init] Analysing project and generating CLAUDE.md…")
    else:
        prompt_text = (
            f"Project directory: {cwd}\n\n"
            f"Existing CLAUDE.md:\n{existing}\n\n"
            f"Directory contents:\n{listing}\n\n"
            + _REFRESH_PROMPT
        )
        print("  [/init] Refreshing CLAUDE.md…")

    # Use the engine's API client directly (no tool calls needed)
    result_text = ""
    async for event in engine._api.stream(
        messages=[{"role": "user", "content": prompt_text}],
        tools=None,
        system_prompt=(
            "You are a technical writer creating project documentation. "
            "Return ONLY the CLAUDE.md content — no preamble, no commentary."
        ),
    ):
        if event["type"] == "text":
            result_text += event["content"]

    if not result_text.strip():
        return "[/init] Failed to generate CLAUDE.md — the model returned no content."

    # Strip accidental markdown code fences (model sometimes wraps in ```markdown)
    content = result_text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        # Remove first and last fence lines
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    target.write_text(content + "\n", encoding="utf-8")

    # Reload memory into the engine's system prompt so the new CLAUDE.md
    # takes effect in the current session without requiring a restart.
    from utils.memory import inject_memory_into_system_prompt
    from query import DEFAULT_SYSTEM_PROMPT
    # Rebuild from base prompt (strip old memory section if present)
    base = engine.system_prompt
    if "<memory>" in base:
        base = base[:base.index("<memory>")].rstrip()
    engine.system_prompt = inject_memory_into_system_prompt(base, engine.cwd)

    return (
        f"  CLAUDE.md written to {target}\n\n"
        f"Preview:\n{content[:400]}{'…' if len(content) > 400 else ''}"
    )


registry.register({
    "name": "init",
    "description": "Create or refresh the project CLAUDE.md instructions file",
    "aliases": [],
    "argument_hint": "[refresh]",
    "call": _call,
})
