"""
Core agent execution engine.
Mirrors src/tools/AgentTool/runAgent.ts.

Responsibilities extracted from __init__.py:
  • System prompt enhancement (env details injection)
  • AbortController equivalent (asyncio.Event)
  • FileStateCache cloning per sub-agent
  • Agent metadata writing (agentId, type, parent, worktree, started_at)
  • Sidechain transcript recording (separate JSONL per agent run)
  • Running the query loop and accumulating output

This file does NOT contain the AgentTool schema or call() dispatcher
(those stay in __init__.py, mirroring AgentTool.tsx vs runAgent.ts split).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from utils.env_info import enhance_system_prompt
from utils.file_state_cache import FileStateCache, create_empty_cache
from utils.memory import inject_memory_into_system_prompt
from tool import Tool, ToolContext


# ── Agent metadata + sidechain transcript paths ───────────────────────────────
# Mirrors getAgentTranscriptPath() + writeAgentMetadata() in sessionStorage.ts.
#
# Layout:
#   ~/.claude/projects/<sanitized-cwd>/<session-id>/agents/<agent-id>.jsonl
#   ~/.claude/projects/<sanitized-cwd>/<session-id>/agents/<agent-id>.meta.json

def _agents_dir(session_transcript_path: Path) -> Path:
    """
    Returns the per-session agents/ directory, creating it if needed.
    Mirrors the subagents/ subdirectory in sessionStorage.ts.
    """
    d = session_transcript_path.parent / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sidechain_path(agents_dir: Path, agent_id: str) -> Path:
    return agents_dir / f"{agent_id}.jsonl"


def _meta_path(agents_dir: Path, agent_id: str) -> Path:
    return agents_dir / f"{agent_id}.meta.json"


def write_agent_metadata(
    agents_dir: Path,
    agent_id: str,
    agent_type: str,
    parent_session_id: str,
    cwd: str,
    description: str,
    worktree_path: str | None = None,
) -> None:
    """
    Write agent metadata to a .meta.json file alongside the sidechain JSONL.
    Mirrors writeAgentMetadata() in sessionStorage.ts.

    Stored fields match the TypeScript interface:
      agentType, parentSessionId, worktreePath, startedAt, cwd, description
    """
    meta = {
        "agentId": agent_id,
        "agentType": agent_type,
        "parentSessionId": parent_session_id,
        "cwd": cwd,
        "description": description,
        "worktreePath": worktree_path,
        "startedAt": time.time(),
    }
    path = _meta_path(agents_dir, agent_id)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def append_sidechain_entry(agents_dir: Path, agent_id: str, message: dict) -> None:
    """
    Append one message to the agent's sidechain JSONL transcript.
    Mirrors recordSidechainTranscript() append logic in sessionStorage.ts.
    """
    path = _sidechain_path(agents_dir, agent_id)
    line = json.dumps({"agentId": agent_id, "message": message}, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_sidechain_messages(agents_dir: Path, agent_id: str) -> list[dict]:
    """
    Read all messages from a sidechain JSONL.
    Mirrors getAgentTranscript() in sessionStorage.ts (used by resumeAgent).
    """
    path = _sidechain_path(agents_dir, agent_id)
    messages: list[dict] = []
    if not path.exists():
        return messages
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                    if msg := entry.get("message"):
                        messages.append(msg)
                except json.JSONDecodeError:
                    pass
    return messages


# ── AbortController equivalent ────────────────────────────────────────────────
# Mirrors AbortController / AbortSignal in the TypeScript runtime.
# asyncio.Event: set() = abort, is_set() = signal.aborted

class AgentAbortError(Exception):
    """Raised when an agent is cancelled via its abort_event."""


# ── Core runner ───────────────────────────────────────────────────────────────

async def run_agent(
    *,
    prompt: str,
    tools: list[Tool],
    api_client: Any,
    cwd: str,
    base_system_prompt: str,
    permission_mode: str,
    always_allow: list[str] | None = None,
    always_deny: list[str] | None = None,
    always_ask: list[str] | None = None,
    max_turns: int,
    agent_id: str,
    agent_type: str,
    parent_session_id: str,
    description: str,
    session_transcript_path: Path,
    abort_event: asyncio.Event | None = None,
    file_state_cache: FileStateCache | None = None,
    worktree_path: str | None = None,
    fork_messages: list[dict] | None = None,
    model_override: str | None = None,
) -> str:
    """
    Run a complete agent loop and return the accumulated text output.
    Mirrors the core body of runAgent() in runAgent.ts.

    Parameters
    ----------
    prompt                  — user message to the agent
    tools                   — resolved tool pool (already filtered by AgentTool)
    api_client              — Qwen/API client
    cwd                     — working directory for this agent (may differ from parent if worktree)
    base_system_prompt      — agent definition's raw system prompt
    permission_mode         — inherited from parent agent
    max_turns               — hard turn limit
    agent_id                — pre-assigned UUID (mirrors createAgentId() caller)
    agent_type              — e.g. 'Explore', 'general-purpose'
    parent_session_id       — session ID of the spawning agent/main loop
    description             — short description from AgentTool input
    session_transcript_path — main session's JSONL path (used to locate agents/ dir)
    abort_event             — asyncio.Event; when set, agent stops after current turn
    file_state_cache        — cloned or empty FileStateCache for this agent
    worktree_path           — set if isolation='worktree'
    fork_messages           — if set, prepend as context (fork subagent pattern)
    """
    # Lazy imports to avoid circular dependency
    from query import query_loop
    from permissions import make_confirm_fn
    from . import background as _bg

    # ── 0. Model override (mirrors model parameter in AgentTool schema) ───────
    # When the caller specifies a different model, spin up a fresh client for
    # this agent run only — the parent client is unaffected.
    # Mirrors the per-call model selection path in AgentTool.tsx.
    effective_client = api_client
    if model_override:
        try:
            from services.api import QwenClient
            effective_client = QwenClient(
                api_key=api_client._client.api_key,
                model=model_override,
                enable_thinking=getattr(api_client, "_thinking", None),
            )
        except Exception:
            pass  # fall back to parent client if override fails

    # ── 1. System prompt enhancement (mirrors getAgentSystemPrompt()) ─────────
    # enhanceSystemPromptWithEnvDetails appends Notes + <env> block.
    # Sub-agents ALWAYS get this; main loop uses a richer prompt from prompts.ts.
    enhanced_prompt = await enhance_system_prompt(base_system_prompt, cwd)
    # Memory snapshot: inject CLAUDE.md files discovered from the agent's cwd.
    # Mirrors the getAttachments() / memory-file injection path in attachments.ts
    # that runs for every agent query, including sub-agents.
    enhanced_prompt = inject_memory_into_system_prompt(enhanced_prompt, cwd)

    # ── 2. File state cache ────────────────────────────────────────────────────
    # Fresh sub-agents get an empty cache; fork sub-agents inherit a clone.
    # Mirrors: forkContextMessages → cloneFileStateCache else createNew
    cache = file_state_cache if file_state_cache is not None else create_empty_cache()

    # ── 3. Write agent metadata (async, best-effort) ──────────────────────────
    # Mirrors: void writeAgentMetadata(agentId, {...}) in runAgent.ts
    agents_dir = _agents_dir(session_transcript_path)
    try:
        write_agent_metadata(
            agents_dir=agents_dir,
            agent_id=agent_id,
            agent_type=agent_type,
            parent_session_id=parent_session_id,
            cwd=cwd,
            description=description,
            worktree_path=worktree_path,
        )
    except Exception:
        pass  # metadata is non-critical; don't abort the agent run

    # ── 4. Build initial messages ──────────────────────────────────────────────
    # fork_messages prepended = fork sub-agent inherits parent context
    # Mirrors: contextMessages = forkContextMessages ? filterIncompleteToolCalls(…) : []
    initial_messages: list[dict] = []
    if fork_messages:
        initial_messages.extend(fork_messages)
    initial_messages.append({"role": "user", "content": prompt})

    # Record initial messages to sidechain transcript
    for msg in initial_messages:
        try:
            append_sidechain_entry(agents_dir, agent_id, msg)
        except Exception:
            pass

    # ── 5. Tool context with abort awareness ──────────────────────────────────
    # Mirrors: agentAbortController used inside the query loop
    _abort = abort_event or asyncio.Event()

    def _confirm(tool_name: str, description: str, tool_input: dict | None = None) -> bool:
        # Check abort before any tool execution
        if _abort.is_set():
            raise AgentAbortError(f"Agent {agent_id} aborted before tool: {tool_name}")
        from permissions import make_confirm_fn
        return make_confirm_fn(
            permission_mode,
            always_allow,
            always_deny,
            always_ask,
        )(tool_name, description, tool_input)

    ctx = ToolContext(
        cwd=cwd,
        permission_mode=permission_mode,
        confirm_fn=_confirm,
        always_allow=list(always_allow or []),
        always_deny=list(always_deny or []),
        always_ask=list(always_ask or []),
        todos={},
        # Sub-agents get a fresh (or cloned) FileStateCache so their reads and
        # edits are tracked independently from the parent session.
        # Mirrors: fork sub-agents inherit cloneFileStateCache(), others get new.
        file_state_cache=cache,
        session_id=agent_id,
        session_transcript_path=session_transcript_path,
    )

    # ── 6. Run query loop ──────────────────────────────────────────────────────
    text_parts: list[str] = []
    current_messages = list(initial_messages)

    # Build message_source drain callback.
    # Mirrors drainPendingMessages() wired into attachments.ts between turns.
    # _bg.drain_messages() returns [] for agents not in the background registry,
    # so this is safe to wire unconditionally for all agent runs.
    def _drain() -> list[str]:
        return _bg.drain_messages(agent_id)

    async for event in query_loop(
        messages=current_messages,
        tools=tools,
        api_client=effective_client,
        ctx=ctx,
        system_prompt=enhanced_prompt,
        max_turns=max_turns,
        message_source=_drain,
    ):
        # Check abort between events
        if _abort.is_set():
            raise AgentAbortError(f"Agent {agent_id} aborted mid-stream")

        # Update background progress tracker (no-op if not a background agent)
        _bg.update_progress(agent_id, event)

        if event["type"] == "text":
            text_parts.append(event["content"])

        # Record assistant messages to sidechain transcript (mirrors recordSidechainTranscript)
        elif event["type"] == "assistant_message":
            try:
                append_sidechain_entry(agents_dir, agent_id, event["message"])
            except Exception:
                pass

        elif event["type"] == "tool_result_message":
            try:
                append_sidechain_entry(agents_dir, agent_id, event["message"])
            except Exception:
                pass

        # Persist injected messages (from enqueue_message / SendMessage relay)
        # to the sidechain so resume can reconstruct the full conversation.
        elif event["type"] == "injected_message":
            try:
                append_sidechain_entry(agents_dir, agent_id, event["message"])
            except Exception:
                pass

    return "".join(text_parts).strip()
