"""
/session command — show session info and list recent sessions.
Mirrors Claude Code's /resume and session-listing functionality.
"""
from __future__ import annotations
import json
from pathlib import Path
import commands as registry


def _list_sessions(engine) -> str:
    """List recent sessions from the project directory."""
    from query_engine import _get_project_dir
    proj_dir = _get_project_dir(engine.cwd)

    if not proj_dir.exists():
        return "No sessions found."

    sessions = sorted(proj_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        return "No sessions found."

    lines = ["Recent sessions (newest first):\n"]
    for i, path in enumerate(sessions[:10]):
        sid = path.stem
        mtime = path.stat().st_mtime
        size = path.stat().st_size
        # Count messages
        count = 0
        try:
            count = sum(1 for line in path.open(encoding="utf-8") if line.strip())
        except OSError:
            pass
        import datetime
        dt = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        marker = " ← current" if sid == engine.session_id else ""
        lines.append(f"  [{i+1}] {sid[:8]}…  {dt}  {count} entries  {size//1024}KB{marker}")

    return "\n".join(lines)


async def _call(args: str, engine) -> str | None:
    usage = engine._last_usage or {}
    lines = [
        f"Session ID : {engine.session_id}",
        f"Model      : {engine._api.model}",
        f"CWD        : {engine.cwd}",
        f"Mode       : {engine.permission_mode}",
        f"Messages   : {len(engine._messages)}",
        f"Log        : {engine.transcript_path}",
        f"Tokens     : {usage.get('prompt_tokens', 0):,} prompt / "
        f"{usage.get('completion_tokens', 0):,} completion",
        "",
    ]
    lines.append(_list_sessions(engine))
    return "\n".join(lines)


registry.register({
    "name": "session",
    "description": "Show current session info and list recent sessions",
    "aliases": ["sessions"],
    "argument_hint": "",
    "call": _call,
})
