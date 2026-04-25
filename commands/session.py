"""
/session command — show session info, list sessions, and archive old ones.
Mirrors Claude Code's /resume and session-listing functionality.

Sub-commands:
  /session          — show current session info + recent sessions
  /session list     — list sessions (optionally filtered: --cwd, --model, --since)
  /session archive  — archive a session by ID prefix
"""
from __future__ import annotations
import datetime
import json
from pathlib import Path
import commands as registry


def _fmt_dt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _list_sessions_display(engine, args: str) -> str:
    """
    List recent sessions using get_sessions() with optional filtering.
    Mirrors getSessions(filter) output in Claude Code's /resume listing.
    """
    from query_engine import get_sessions, _get_project_dir

    # Parse simple --key value flags
    cwd_filter: str | None = None
    model_filter: str | None = None
    since_filter: float | None = None
    limit = 15

    tokens = args.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--cwd" and i + 1 < len(tokens):
            cwd_filter = tokens[i + 1]; i += 2
        elif tok == "--model" and i + 1 < len(tokens):
            model_filter = tokens[i + 1]; i += 2
        elif tok == "--since" and i + 1 < len(tokens):
            # accept "7d", "24h", or epoch float
            raw = tokens[i + 1]; i += 2
            try:
                if raw.endswith("d"):
                    since_filter = datetime.datetime.now().timestamp() - float(raw[:-1]) * 86400
                elif raw.endswith("h"):
                    since_filter = datetime.datetime.now().timestamp() - float(raw[:-1]) * 3600
                else:
                    since_filter = float(raw)
            except ValueError:
                pass
        elif tok == "--limit" and i + 1 < len(tokens):
            try:
                limit = int(tokens[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    sessions = get_sessions(
        cwd=cwd_filter,
        model=model_filter,
        since=since_filter,
        limit=limit,
    )

    if not sessions:
        return "No sessions found."

    lines = [f"Sessions (newest first, showing up to {limit}):\n"]
    for meta in sessions:
        sid = meta["sessionId"]
        marker = " ← current" if sid == engine.session_id else ""
        cwd_label = meta.get("cwd") or "?"
        model_label = meta.get("model") or "?"
        created = _fmt_dt(meta["createdAt"])
        last_act = _fmt_dt(meta["lastActivity"])
        n_msgs = meta["messageCount"]
        lines.append(
            f"  {sid[:8]}…  {created}  (active {last_act})  "
            f"{n_msgs} msgs  [{model_label}]  {cwd_label}{marker}"
        )
    return "\n".join(lines)


def _archive_session(args: str, engine) -> str:
    """
    Archive a session by ID prefix.
    Mirrors archiveSession() in sessionStorage.ts.
    """
    from query_engine import get_sessions, archive_session

    sid_prefix = args.strip()
    if not sid_prefix:
        return "Usage: /session archive <session-id-prefix>"

    sessions = get_sessions(limit=200)
    matched = [s for s in sessions if s["sessionId"].startswith(sid_prefix)]

    if not matched:
        return f"No session found matching prefix: {sid_prefix!r}"
    if len(matched) > 1:
        ids = ", ".join(s["sessionId"][:8] for s in matched)
        return f"Multiple sessions match {sid_prefix!r}: {ids}\nBe more specific."

    meta = matched[0]
    if meta["sessionId"] == engine.session_id:
        return "Cannot archive the current session."

    path = Path(meta["path"])
    if archive_session(path):
        return f"Archived session {meta['sessionId'][:8]}… → {path.parent / 'archived' / path.name}"
    return f"Failed to archive session {meta['sessionId'][:8]}…"


async def _call(args: str, engine) -> str | None:
    sub, _, rest = args.partition(" ")
    rest = rest.strip()

    if sub in ("list", "ls"):
        return _list_sessions_display(engine, rest)

    if sub == "archive":
        return _archive_session(rest, engine)

    # Default: show current session info + recent session list
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
    lines.append(_list_sessions_display(engine, ""))
    return "\n".join(lines)


registry.register({
    "name": "session",
    "description": "Show current session info and list/archive sessions",
    "aliases": ["sessions"],
    "argument_hint": "[list [--cwd CWD] [--model M] [--since 7d] | archive <id-prefix>]",
    "call": _call,
})
