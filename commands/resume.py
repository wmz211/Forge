"""
/resume [session-id | search] — List or resume a previous conversation.
Mirrors Claude Code's /resume command in commands/resume/.

With no argument: lists the 10 most recent sessions under
  ~/.claude/projects/<sanitized-cwd>/
With an argument: if it matches a session UUID, restores that session;
  otherwise filters the list by the search term.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from datetime import datetime

import commands as _reg
from query_engine import _get_project_dir, _load_jsonl


def _session_preview(path: Path) -> str:
    """Extract first user message from a JSONL transcript for display."""
    entries = _load_jsonl(path)
    for entry in entries:
        if entry.get("type") == "message":
            msg = entry.get("message", {})
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content[:80].replace("\n", " ")
    return "(empty)"


def _session_mtime(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"


async def call(args: str, engine) -> str:
    project_dir = _get_project_dir(engine.cwd)
    if not project_dir.exists():
        return "  No sessions found for this directory."

    sessions = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    query = args.strip()

    # If query looks like a UUID, try to switch directly
    if len(query) == 36 and query.count("-") == 4:
        target = project_dir / f"{query}.jsonl"
        if target.exists():
            _do_resume(engine, query, target)
            return f"  \033[32mResumed session\033[0m {query}"
        return f"  \033[31mSession not found:\033[0m {query}"

    # Filter by search term
    if query:
        sessions = [p for p in sessions
                    if query.lower() in p.stem.lower()
                    or query.lower() in _session_preview(p).lower()]

    if not sessions:
        return "  No matching sessions found."

    lines = ["\033[1mRecent sessions:\033[0m\n"]
    for i, path in enumerate(sessions[:10], 1):
        sid = path.stem
        mtime = _session_mtime(path)
        preview = _session_preview(path)
        active = " \033[32m[current]\033[0m" if sid == engine.session_id else ""
        lines.append(f"  \033[36m{i:2}.\033[0m {sid}{active}")
        lines.append(f"       {mtime}  {preview}")

    lines.append("")
    lines.append("  To resume: /resume <session-id>")
    return "\n".join(lines)


def _do_resume(engine, session_id: str, path: Path) -> None:
    """Load a different session's messages into the engine in-place."""
    from query_engine import _load_jsonl, _get_transcript_path
    engine.session_id = session_id
    engine._transcript_path = path
    engine._messages = []
    engine._last_usage = None
    entries = _load_jsonl(path)
    for entry in entries:
        if entry.get("type") == "message":
            msg = entry.get("message")
            if msg:
                engine._messages.append(msg)


_reg.register({
    "name": "resume",
    "description": "List or resume a previous conversation",
    "aliases": ["continue"],
    "argument_hint": "[session-id | search term]",
    "call": call,
})
