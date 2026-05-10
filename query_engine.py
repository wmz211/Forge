from __future__ import annotations
"""
QueryEngine — faithful port of Claude Code's QueryEngine.ts + sessionStorage.ts.

Session storage mirrors sessionStorage.ts:
  - Format  : JSONL (one JSON object per line, append-only)
  - Path    : ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl
  - Sanitize: replace non-alphanumerics with '-' + long-path hash suffix
  - Each line is a serialized message entry written via appendFile

The JSONL approach means sessions survive crashes and are human-readable with
`cat session.jsonl | jq .`  — identical to what Claude Code stores locally.
"""
import asyncio
import json
import os
import time
import uuid
import re
from pathlib import Path
from typing import AsyncGenerator

from tool import Tool, ToolContext
from services.api import QwenClient
from permissions import make_confirm_fn
from permission_rules import (
    load_rules_by_source,
    flatten_rules_by_source,
    add_rule_to_source,
    remove_rule_from_source,
    RULE_SOURCE_ORDER,
    PERSISTABLE_SOURCES,
    RUNTIME_MUTABLE_SOURCES,
    source_is_readonly,
    get_enabled_setting_sources,
    get_policy_origin,
    load_additional_directories,
)
from query import query_loop, DEFAULT_SYSTEM_PROMPT
from utils.file_state_cache import FileStateCache, create_empty_cache
from utils.memory import inject_memory_into_system_prompt
from utils.hooks import execute_session_start_hooks, execute_user_prompt_submit_hooks


# ── Path helpers (mirrors sanitizePath() + getProjectDir()) ─

def _sanitize_path(cwd: str) -> str:
    """
    Mirrors sanitizePath() from sessionStoragePortable.ts.
    Replaces all non-alphanumeric characters with '-', and for very long
    paths truncates with a deterministic hash suffix.
    """
    max_sanitized_length = 200
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    if len(sanitized) <= max_sanitized_length:
        return sanitized

    # djb2 fallback hash (mirrors simpleHash(djb2Hash()) in TS source)
    h = 0
    for ch in cwd:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    if h & 0x80000000:
        h = -((~h + 1) & 0xFFFFFFFF)
    h = abs(h)

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if h == 0:
        hash36 = "0"
    else:
        out = []
        while h:
            h, rem = divmod(h, 36)
            out.append(digits[rem])
        hash36 = "".join(reversed(out))

    return f"{sanitized[:max_sanitized_length]}-{hash36}"


def _get_projects_dir() -> Path:
    """
    Mirrors getProjectsDir() from sessionStorage.ts:
    ~/.claude/projects/
    """
    home = Path.home()
    return home / ".claude" / "projects"


def _get_project_dir(cwd: str) -> Path:
    """
    Mirrors getProjectDir() from sessionStorage.ts:
    ~/.claude/projects/<sanitized-cwd>/
    """
    return _get_projects_dir() / _sanitize_path(cwd)


def _get_transcript_path(cwd: str, session_id: str) -> Path:
    """
    Mirrors getTranscriptPath() from sessionStorage.ts:
    ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl
    """
    return _get_project_dir(cwd) / f"{session_id}.jsonl"


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def _append_entry(path: Path, entry: dict) -> None:
    """
    Append one JSON entry as a single line to the JSONL transcript.
    Mirrors appendEntryToFile() / appendFileSync() in sessionStorage.ts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _load_jsonl(path: Path) -> list[dict]:
    """
    Read all entries from a JSONL transcript.
    Mirrors parseJSONL() / loadTranscript() in sessionStorage.ts.
    """
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # skip malformed lines, same as Claude Code
    except FileNotFoundError:
        pass
    return entries


def _write_session_header(path: Path, session_id: str, cwd: str, model: str) -> None:
    """
    Write a `summary` header entry at the start of a new session.
    Mirrors the initial header write in sessionStorage.ts.
    Recorded fields are used by get_sessions() for filtering without full parse.
    """
    entry = {
        "type": "summary",
        "sessionId": session_id,
        "cwd": cwd,
        "model": model,
        "createdAt": time.time(),
    }
    _append_entry(path, entry)


def _write_heartbeat(path: Path, session_id: str) -> None:
    """
    Append a heartbeat entry to signal the session is still active.
    Mirrors writeHeartbeat() in sessionStorage.ts — used by session GC / listing
    to distinguish live sessions from stale ones.
    """
    entry = {
        "type": "heartbeat",
        "sessionId": session_id,
        "timestamp": time.time(),
    }
    _append_entry(path, entry)


def get_sessions(
    cwd: str | None = None,
    model: str | None = None,
    since: float | None = None,
    limit: int = 20,
) -> list[dict]:
    """
    Return session metadata for the sessions directory, optionally filtered.
    Mirrors getSessions(filter?) in sessionStorage.ts.

    Reads the first `summary` entry from each JSONL to get metadata without
    loading the full transcript.  Falls back to mtime when no header exists
    (for sessions created before this improvement was added).

    Filter parameters:
      cwd   — exact cwd match
      model — model name match
      since — only sessions with createdAt >= since (epoch seconds)
      limit — max results (newest first)
    """
    projects_dir = _get_projects_dir()
    sessions: list[dict] = []

    if not projects_dir.exists():
        return sessions

    for jsonl in sorted(
        projects_dir.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        # Skip archived sessions
        if "archived" in jsonl.parts:
            continue

        meta: dict = {
            "sessionId": jsonl.stem,
            "path": str(jsonl),
            "cwd": None,
            "model": None,
            "createdAt": jsonl.stat().st_mtime,
            "lastActivity": jsonl.stat().st_mtime,
            "messageCount": 0,
        }

        # Read header entry for structured metadata
        try:
            with open(jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") == "summary":
                        meta["cwd"] = entry.get("cwd")
                        meta["model"] = entry.get("model")
                        meta["createdAt"] = entry.get("createdAt", meta["createdAt"])
                    elif entry.get("type") == "message":
                        meta["messageCount"] += 1
                    elif entry.get("type") == "heartbeat":
                        meta["lastActivity"] = max(
                            meta["lastActivity"],
                            entry.get("timestamp", 0),
                        )
        except OSError:
            pass

        # Apply filters
        if cwd is not None and meta["cwd"] != cwd:
            continue
        if model is not None and meta["model"] != model:
            continue
        if since is not None and meta["createdAt"] < since:
            continue

        sessions.append(meta)
        if len(sessions) >= limit:
            break

    return sessions


def archive_session(session_path: Path) -> bool:
    """
    Move a session JSONL to an `archived/` subdirectory.
    Mirrors archiveSession() in sessionStorage.ts.
    Returns True on success.
    """
    if not session_path.exists():
        return False
    archive_dir = session_path.parent / "archived"
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        session_path.rename(archive_dir / session_path.name)
        return True
    except OSError:
        return False


# ── QueryEngine ───────────────────────────────────────────────────────────────

class QueryEngine:
    """
    Manages a single conversation session.
    Mirrors Claude Code's QueryEngine class in QueryEngine.ts.

    Responsibilities:
    - Persist message history across turns (JSONL format)
    - Manage permission mode and tool context
    - Expose submit_message() as an async generator for streaming events
    - Auto-create the session transcript at ~/.claude/projects/<cwd>/<id>.jsonl
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        cwd: str,
        tools: list[Tool],
        permission_mode: str = "default",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        session_id: str | None = None,
        always_allow: list[str] | None = None,
        always_deny: list[str] | None = None,
        always_ask: list[str] | None = None,
        max_turns: int = 50,
    ):
        self.cwd = cwd
        self.tools = tools
        self.permission_mode = permission_mode
        self.max_turns = max_turns
        self._always_allow_rules: dict[str, list[str]] = load_rules_by_source(
            cwd, "allow"
        )
        self._always_deny_rules: dict[str, list[str]] = load_rules_by_source(
            cwd, "deny"
        )
        self._always_ask_rules: dict[str, list[str]] = load_rules_by_source(
            cwd, "ask"
        )
        if always_allow:
            self._always_allow_rules["session"] = list(always_allow)
        if always_deny:
            self._always_deny_rules["session"] = list(always_deny)
        if always_ask:
            self._always_ask_rules["session"] = list(always_ask)
        self._additional_working_directories = load_additional_directories(cwd)
        self.extra_dirs = self._additional_working_directories

        # Session ID — stable across resume, new UUID for fresh sessions
        self.session_id = session_id or str(uuid.uuid4())

        # JSONL transcript path (mirrors getTranscriptPath())
        self._transcript_path = _get_transcript_path(cwd, self.session_id)

        self._api = QwenClient(api_key=api_key, model=model)
        self._model = model
        self._messages: list[dict] = []
        self._last_usage: dict | None = None
        self._todos: dict[str, list[dict]] = {}
        # Inject CLAUDE.md memory files into the system prompt.
        # Mirrors the memory-attachment step in QueryEngine.ts / attachments.ts.
        self.system_prompt = inject_memory_into_system_prompt(system_prompt, cwd)
        # Mirrors readFileState: FileStateCache in QueryEngine.ts.
        # Shared across all turns in this session; sub-agents get an empty clone.
        self._file_state_cache: FileStateCache = create_empty_cache()

        # AbortController equivalent — shared with all AgentTool instances in
        # this session so Ctrl+C propagates through the entire agent tree.
        # Mirrors toolUseContext.abortController in QueryEngine.ts.
        self._abort_event: asyncio.Event = asyncio.Event()

        is_new = not self._transcript_path.exists()
        self._load_session()
        if is_new:
            # Write session header so get_sessions() can read metadata without
            # loading the full transcript.  Mirrors the summary header write in
            # sessionStorage.ts on first session creation.
            _write_session_header(self._transcript_path, self.session_id, cwd, model)

        # Wire abort_event into every AgentTool in the pool
        self._wire_abort_event()

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def transcript_path(self) -> Path:
        """Public alias for the session transcript path. Mirrors sessionStorage.transcriptPath."""
        return self._transcript_path

    # ── Abort chain ───────────────────────────────────────────────────────────

    def _wire_abort_event(self) -> None:
        """
        Inject the shared abort_event into every AgentTool in the tool pool.
        Mirrors how toolUseContext.abortController is shared across all
        agent instances in the same session in QueryEngine.ts.
        """
        from tools.agent_tool import AgentTool
        for tool in self.tools:
            if isinstance(tool, AgentTool):
                tool._abort_event = self._abort_event

    def abort(self) -> None:
        """
        Signal all in-flight agents in this session to stop.
        Mirrors AbortController.abort() called from the Ctrl+C handler in
        the CLI entrypoint (cli.tsx → createAbortController).
        The next event loop iteration will raise AgentAbortError.
        """
        self._abort_event.set()

    def reset_abort(self) -> None:
        """Clear the abort signal so new submissions can run."""
        self._abort_event = asyncio.Event()
        self._wire_abort_event()

    # ── Session persistence (JSONL) ───────────────────────────────────────────

    def get_rules_by_source(self, behavior: str) -> dict[str, list[str]]:
        if behavior == "allow":
            return {k: list(v) for k, v in self._always_allow_rules.items()}
        if behavior == "deny":
            return {k: list(v) for k, v in self._always_deny_rules.items()}
        if behavior == "ask":
            return {k: list(v) for k, v in self._always_ask_rules.items()}
        raise ValueError(f"Unknown behavior: {behavior}")

    def get_enabled_sources(self) -> tuple[str, ...]:
        return get_enabled_setting_sources()

    def get_policy_origin(self) -> str:
        # Updated when policy rules are loaded via load_rules_from_source.
        return get_policy_origin()

    def _source_is_enabled_for_eval(self, source: str) -> bool:
        if source in (
            "userSettings",
            "projectSettings",
            "localSettings",
            "flagSettings",
            "policySettings",
        ):
            return source in self.get_enabled_sources()
        return True

    def get_effective_rules(self, behavior: str) -> list[str]:
        return flatten_rules_by_source(self.get_rules_by_source(behavior))

    def get_effective_rules_with_source(self, behavior: str) -> list[dict]:
        out: list[dict] = []
        by_source = self.get_rules_by_source(behavior)
        for source in RULE_SOURCE_ORDER:
            if not self._source_is_enabled_for_eval(source):
                continue
            for rule in by_source.get(source, []):
                out.append({"source": source, "rule": rule})
        return out

    def add_permission_rule(
        self,
        behavior: str,
        rule: str,
        source: str = "session",
    ) -> bool:
        if source not in RULE_SOURCE_ORDER:
            return False
        if source_is_readonly(source):
            return False
        if behavior == "allow":
            bucket = self._always_allow_rules
        elif behavior == "deny":
            bucket = self._always_deny_rules
        elif behavior == "ask":
            bucket = self._always_ask_rules
        else:
            return False

        rules = bucket.get(source, [])
        if rule not in rules:
            rules.append(rule)
        bucket[source] = rules

        if source in PERSISTABLE_SOURCES:
            return add_rule_to_source(self.cwd, source, behavior, rule)
        if source in RUNTIME_MUTABLE_SOURCES:
            return True
        return True

    def clear_permission_rule(self, rule: str, source: str | None = None) -> dict:
        sources = [source] if source else list(RULE_SOURCE_ORDER)
        cleared = 0
        blocked: list[str] = []
        for src in sources:
            if source_is_readonly(src):
                blocked.append(src)
                continue
            for behavior, bucket in (
                ("allow", self._always_allow_rules),
                ("deny", self._always_deny_rules),
                ("ask", self._always_ask_rules),
            ):
                if src in bucket:
                    before = len(bucket[src])
                    bucket[src] = [r for r in bucket[src] if r != rule]
                    if len(bucket[src]) != before:
                        cleared += before - len(bucket[src])
                if src in PERSISTABLE_SOURCES:
                    remove_rule_from_source(self.cwd, src, behavior, rule)
        return {"cleared": cleared, "blocked": blocked}

    def _load_session(self) -> None:
        """
        Load message history from the JSONL transcript.
        Mirrors loadTranscript() in sessionStorage.ts.
        Each entry with type 'message' contributes to _messages.
        """
        entries = _load_jsonl(self._transcript_path)
        if not entries:
            return

        # Reconstruct messages from stored entries
        for entry in entries:
            if entry.get("type") == "message":
                msg = entry.get("message")
                if msg:
                    self._messages.append(msg)

        if self._messages:
            print(f"[Session] Resumed {self.session_id[:8]}… "
                  f"({len(self._messages)} messages from "
                  f"{self._transcript_path})")

    def _save_message(self, message: dict) -> None:
        """
        Append a single message to the JSONL transcript.
        Mirrors the appendEntryToFile calls in sessionStorage.ts after each turn.
        """
        entry = {
            "type": "message",
            "sessionId": self.session_id,
            "message": message,
        }
        _append_entry(self._transcript_path, entry)

    # ── Public API ────────────────────────────────────────────────────────────

    async def submit_message(self, prompt: str) -> AsyncGenerator[dict, None]:
        """
        Submit a user message and stream back events.
        State (message history) persists across calls via JSONL.
        """
        # Execute UserPromptSubmit hooks before processing the prompt.
        # Mirrors executeUserPromptSubmitHooks() called in processUserInput.ts.
        # Exit code 2 blocks the prompt; successful stdout is additional context.
        _prompt_hook = await execute_user_prompt_submit_hooks(
            prompt=prompt,
            cwd=self.cwd,
            session_id=self.session_id,
            transcript_path=str(self._transcript_path),
            permission_mode=self.permission_mode,
        )
        if _prompt_hook.get("block"):
            reason = _prompt_hook.get("block_reason", "Blocked by UserPromptSubmit hook")
            yield {"type": "text", "content": f"[Hook blocked prompt]: {reason}"}
            yield {"type": "done", "reason": "hook_blocked", "usage": self._last_usage}
            return

        user_msg = {"role": "user", "content": prompt}
        self._messages.append(user_msg)
        self._save_message(user_msg)

        # Inject hook additional_context as a user-scoped note and persist it so
        # resumed sessions reconstruct the same context the live turn saw.
        # Mirrors the hook_additional_context attachment in processUserInput.ts.
        if _prompt_hook.get("additional_context"):
            _ctx_msg = {
                "role": "user",
                "content": f"[Hook additional context]: {_prompt_hook['additional_context']}",
            }
            self._messages.append(_ctx_msg)
            self._save_message(_ctx_msg)

        # Heartbeat: signal this session is still active before each turn.
        # Mirrors writeHeartbeat() called at the start of each query in
        # sessionStorage.ts — used by session listing to distinguish live vs stale.
        _write_heartbeat(self._transcript_path, self.session_id)

        # Reset abort state from any previous Ctrl+C before each new submission
        if self._abort_event.is_set():
            self.reset_abort()

        ctx = ToolContext(
            cwd=self.cwd,
            permission_mode=self.permission_mode,
            confirm_fn=make_confirm_fn(
                self.permission_mode,
                self.get_effective_rules_with_source("allow"),
                self.get_effective_rules_with_source("deny"),
                self.get_effective_rules_with_source("ask"),
            ),
            always_allow=self.get_effective_rules_with_source("allow"),
            always_deny=self.get_effective_rules_with_source("deny"),
            always_ask=self.get_effective_rules_with_source("ask"),
            additional_working_directories=list(getattr(self, "extra_dirs", self._additional_working_directories)),
            todos=self._todos,
            # Pass session context so AgentTool can write sidechain transcripts
            # and metadata alongside the main session's JSONL file.
            # Mirrors toolUseContext.agentId / sessionStorage context in QueryEngine.ts.
            session_id=self.session_id,
            session_transcript_path=self._transcript_path,
            # Share the session-level file state cache so FileEditTool can enforce
            # must-read-before-edit across all turns in this session.
            file_state_cache=self._file_state_cache,
        )

        last_usage: dict | None = self._last_usage

        async for event in query_loop(
            messages=self._messages,
            tools=self.tools,
            api_client=self._api,
            ctx=ctx,
            system_prompt=self.system_prompt,
            max_turns=self.max_turns,
            last_usage=last_usage,
        ):
            if event["type"] == "done":
                # Persist usage for next turn's token estimation
                if "usage" in event:
                    self._last_usage = event.get("usage")
                yield event
                return

            # Persist assistant/tool-result messages; these are internal events
            # not forwarded to the UI caller.
            if event["type"] == "assistant_message":
                msg = event["message"]
                self._messages.append(msg)
                self._save_message(msg)
                continue
            if event["type"] == "tool_result_message":
                msg = event["message"]
                self._messages.append(msg)
                self._save_message(msg)
                continue

            yield event

    async def process_session_start_hooks(
        self,
        source: str = "startup",
        model: str | None = None,
    ) -> list[dict]:
        """
        Execute SessionStart hooks and return any hook result messages.
        Mirrors processSessionStartHooks() in sessionStart.ts.

        source is one of: "startup", "resume", "clear", "compact"
        Hook messages are returned as dicts — callers can inject them into the
        conversation or display them in the UI.
        """
        return await execute_session_start_hooks(
            source=source,  # type: ignore[arg-type]
            cwd=self.cwd,
            session_id=self.session_id,
            transcript_path=str(self._transcript_path),
            model=model,
        )

    def clear(self) -> None:
        """
        Reset conversation history (in-memory and on-disk).
        Mirrors the /clear command behavior in Claude Code.
        The JSONL is truncated rather than deleted so the session ID is reused.
        """
        self._messages = []
        self._last_usage = None
        self._file_state_cache.clear()
        # Truncate the transcript file
        if self._transcript_path.exists():
            self._transcript_path.write_text("", encoding="utf-8")
        _write_session_header(
            self._transcript_path,
            self.session_id,
            self.cwd,
            self._model,
        )
        print(f"[Session] Cleared {self.session_id[:8]}…")
