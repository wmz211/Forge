"""
/memory [show|edit] — Manage CLAUDE.md memory files.
Mirrors Claude Code's /memory command in commands/memory/.

Claude Code reads CLAUDE.md files from:
  ~/.claude/CLAUDE.md         (global user memory)
  <cwd>/CLAUDE.md             (project memory)
  <cwd>/.claude/CLAUDE.md     (local project memory)

/memory        — show contents of all memory files
/memory edit   — open the project CLAUDE.md in $EDITOR (or notepad on Windows)
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

import commands as _reg

_MEMORY_FILE = "CLAUDE.md"


def _memory_paths(cwd: str) -> list[Path]:
    home = Path.home()
    return [
        home / ".claude" / _MEMORY_FILE,        # global
        Path(cwd) / _MEMORY_FILE,               # project root
        Path(cwd) / ".claude" / _MEMORY_FILE,   # project local
    ]


async def call(args: str, engine) -> str:
    sub = args.strip().lower()
    paths = _memory_paths(engine.cwd)

    if sub == "edit":
        # Find or create the project-level CLAUDE.md
        target = Path(engine.cwd) / _MEMORY_FILE
        target.touch(exist_ok=True)
        editor = os.environ.get("EDITOR", "notepad" if sys.platform == "win32" else "nano")
        try:
            subprocess.run([editor, str(target)])
        except Exception as e:
            return f"  \033[31mCould not open editor:\033[0m {e}\n  File: {target}"
        return f"  \033[32mEditor closed.\033[0m Memory file: {target}"

    # Default: show all memory files
    lines = ["\033[1mMemory files:\033[0m\n"]
    found_any = False
    for path in paths:
        marker = "(global)" if ".claude" in str(path.parent) and path.parent == Path.home() / ".claude" else "(project)"
        if path.exists():
            found_any = True
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            lines.append(f"  \033[36m{path}\033[0m  {marker}")
            if content:
                for line in content.splitlines()[:20]:
                    lines.append(f"    {line}")
                if len(content.splitlines()) > 20:
                    lines.append(f"    \033[90m... ({len(content.splitlines())} lines total)\033[0m")
            else:
                lines.append("    \033[90m(empty)\033[0m")
            lines.append("")
        else:
            lines.append(f"  \033[90m{path}  {marker}  — not found\033[0m")

    if not found_any:
        lines.append("  No CLAUDE.md files found.")
        lines.append(f"  Create one with: /memory edit")

    return "\n".join(lines)


_reg.register({
    "name": "memory",
    "description": "View or edit CLAUDE.md memory files",
    "argument_hint": "[show|edit]",
    "call": call,
})
