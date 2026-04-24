"""
/files command — list files currently in context (read into FileStateCache).
Mirrors Claude Code's /files command in commands/files/files.ts.

Shows all files the agent has read during this session, displayed as relative
paths from cwd. These are the files whose contents the agent "knows about"
and can edit without an explicit re-read.
"""
from __future__ import annotations
import os
import commands as registry


async def _call(args: str, engine) -> str | None:
    cache = getattr(engine, "_file_state_cache", None)
    if cache is None or len(cache) == 0:
        return "No files in context."

    cwd = engine.cwd
    lines = ["Files in context:\n"]
    for path in sorted(cache):
        try:
            rel = os.path.relpath(path, cwd)
        except ValueError:
            rel = path
        state = cache.get(path)
        partial = " (partial)" if (state and state.is_partial_view) else ""
        lines.append(f"  {rel}{partial}")

    return "\n".join(lines)


registry.register({
    "name": "files",
    "description": "List files currently in context (read during this session)",
    "aliases": [],
    "argument_hint": "",
    "call": _call,
})
