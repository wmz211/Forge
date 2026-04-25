"""
/memory [show|edit] — Manage CLAUDE.md memory files.
Mirrors Claude Code's /memory command in commands/memory/.

Claude Code reads CLAUDE.md files from (in load order):
  ~/.claude/CLAUDE.md         (global user memory)
  <ancestors>/CLAUDE.md       (parent directories up to home)
  <cwd>/CLAUDE.md             (project root)
  <cwd>/.claude/CLAUDE.md     (local project memory)
  <cwd>/**/.claude/CLAUDE.md  (subdirectory memory files, depth ≤ 3)

/memory        — show contents of all discovered memory files
/memory edit   — open the project CLAUDE.md in $EDITOR (or notepad on Windows)
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

import commands as _reg

_MEMORY_FILE = "CLAUDE.md"


async def call(args: str, engine) -> str:
    sub = args.strip().lower()

    if sub == "edit":
        # Open the project-level CLAUDE.md (most specific non-global location)
        target = Path(engine.cwd) / _MEMORY_FILE
        target.touch(exist_ok=True)
        editor = os.environ.get("EDITOR", "notepad" if sys.platform == "win32" else "nano")
        try:
            subprocess.run([editor, str(target)])
        except Exception as e:
            return f"  \033[31mCould not open editor:\033[0m {e}\n  File: {target}"
        return f"  \033[32mEditor closed.\033[0m Memory file: {target}"

    # Default: show all discovered memory files using full discovery logic.
    # Uses get_memory_file_entries() which mirrors getMemoryFileContents() in
    # Claude Code — same candidate path list, same deduplication.
    from utils.memory import get_memory_file_entries
    entries = get_memory_file_entries(engine.cwd)

    lines = ["\033[1mMemory files:\033[0m\n"]
    found_any = False

    for entry in entries:
        path = entry["path"]
        label = entry["label"]
        exists = entry["exists"]
        content = entry["content"]

        if exists and content is not None:
            found_any = True
            lines.append(f"  \033[36m{path}\033[0m  ({label})")
            if content:
                for line in content.splitlines()[:20]:
                    lines.append(f"    {line}")
                if len(content.splitlines()) > 20:
                    lines.append(
                        f"    \033[90m... ({len(content.splitlines())} lines total)\033[0m"
                    )
            else:
                lines.append("    \033[90m(empty)\033[0m")
            lines.append("")
        else:
            lines.append(f"  \033[90m{path}  ({label})  — not found\033[0m")

    if not found_any:
        lines.append("  No CLAUDE.md files found.")
        lines.append("  Create one with: /memory edit")

    return "\n".join(lines)


_reg.register({
    "name": "memory",
    "description": "View or edit CLAUDE.md memory files",
    "argument_hint": "[show|edit]",
    "call": call,
})
