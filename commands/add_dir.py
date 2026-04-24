"""
/add-dir <path> — Add a working directory to the session scope.
Mirrors Claude Code's /add-dir command in commands/add-dir/.

Claude Code uses this to expand the set of directories the agent can operate
in. We store additional dirs in engine.extra_dirs and update ToolContext.cwd
awareness. The BashTool and file tools use engine.cwd as base; extra_dirs
extend the allowed scope for permission checks.
"""
from __future__ import annotations
import os
import commands as _reg


async def call(args: str, engine) -> str:
    path = args.strip()
    if not path:
        dirs = getattr(engine, "extra_dirs", [])
        if not dirs:
            return (
                f"  Working directory: \033[36m{engine.cwd}\033[0m\n"
                "  No extra directories added.\n"
                "  Usage: /add-dir <path>"
            )
        lines = [f"  Working directory: \033[36m{engine.cwd}\033[0m", "  Extra directories:"]
        for d in dirs:
            lines.append(f"    \033[36m{d}\033[0m")
        return "\n".join(lines)

    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        return f"  \033[31mNot a directory:\033[0m {resolved}"

    if not hasattr(engine, "extra_dirs"):
        engine.extra_dirs = []

    if resolved == engine.cwd:
        return f"  \033[33mAlready the primary working directory:\033[0m {resolved}"

    if resolved in engine.extra_dirs:
        return f"  \033[33mAlready in scope:\033[0m {resolved}"

    engine.extra_dirs.append(resolved)
    return f"  \033[32mAdded:\033[0m {resolved}\n  Total dirs in scope: {1 + len(engine.extra_dirs)}"


_reg.register({
    "name": "add-dir",
    "description": "Add an extra working directory to the session scope",
    "argument_hint": "<path>",
    "call": call,
})
