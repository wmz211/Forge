"""
Agent resumption from sidechain transcript.
Mirrors src/tools/AgentTool/resumeAgent.ts.

resumeAgentBackground() in the original:
  1. Reads the agent's JSONL sidechain transcript via getAgentTranscript()
  2. Filters out incomplete tool calls (filterUnresolvedToolUses)
  3. Reconstructs messages + appends the new continuation prompt
  4. Re-registers a new async agent with the same agentId
  5. Runs it in the background

Our port:
  • resume_agent() — sync call, returns result text (foreground)
  • resume_agent_background() — fire-and-forget, registers in background.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tool import Tool, ToolContext
from .run_agent import load_sidechain_messages, _agents_dir, AgentAbortError
from . import background as _bg
from .color_manager import color_label


# ── Message filtering (mirrors filterUnresolvedToolUses in messages.ts) ────────

def _filter_orphaned_tool_calls(messages: list[dict]) -> list[dict]:
    """
    Remove tool_use blocks that have no matching tool_result, and tool_result
    blocks that have no matching tool_use.  Avoids API errors on resume.

    Mirrors filterUnresolvedToolUses() + filterIncompleteToolCalls() in
    src/utils/messages.ts.
    """
    # Collect all tool_use ids in assistant messages
    used_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            tc_list = msg.get("tool_calls") or []
            for tc in tc_list:
                if tc_id := tc.get("id"):
                    used_ids.add(tc_id)

    # Collect all tool_result ids in user messages
    result_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            if tc_id := msg.get("tool_call_id"):
                result_ids.add(tc_id)

    # Drop assistant messages whose tool_calls have no result
    # Drop tool messages whose tool_call_id has no corresponding use
    filtered: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            tc_list = msg.get("tool_calls") or []
            if tc_list:
                # Keep only if ALL tool calls have results
                if all(tc.get("id") in result_ids for tc in tc_list):
                    filtered.append(msg)
                # else drop the incomplete assistant turn entirely
            else:
                filtered.append(msg)
        elif role == "tool":
            if msg.get("tool_call_id") in used_ids:
                filtered.append(msg)
            # else drop orphaned tool_result
        else:
            filtered.append(msg)

    return filtered


# ── Core resume logic ─────────────────────────────────────────────────────────

async def _run_resume(
    agent_id: str,
    continuation_prompt: str,
    tools: list[Tool],
    api_client: Any,
    cwd: str,
    system_prompt: str,
    permission_mode: str,
    max_turns: int,
    session_transcript_path: Path,
    abort_event: asyncio.Event | None = None,
) -> str:
    """
    Internal coroutine: load sidechain, append prompt, run agent.
    """
    from .run_agent import run_agent

    agents_dir = _agents_dir(session_transcript_path)
    past_messages = load_sidechain_messages(agents_dir, agent_id)

    # Filter orphaned tool calls before reconstructing
    past_messages = _filter_orphaned_tool_calls(past_messages)

    # Build the resumed message list: history + new continuation
    # Mirrors resumeAgent: [...transcript.messages, createUserMessage(prompt)]
    if continuation_prompt.strip():
        past_messages.append({"role": "user", "content": continuation_prompt})

    # We re-use run_agent but pass the history as fork_messages
    # (fork_messages are prepended as context, then the agent gets a user msg)
    # Since we already appended the continuation, pass fork_messages without a
    # separate prompt — use a sentinel to signal "no additional user prompt"
    result = await run_agent(
        prompt=continuation_prompt,
        tools=tools,
        api_client=api_client,
        cwd=cwd,
        base_system_prompt=system_prompt,
        permission_mode=permission_mode,
        max_turns=max_turns,
        agent_id=agent_id,
        agent_type="resumed",
        parent_session_id="",
        description=f"Resume of {agent_id[:8]}",
        session_transcript_path=session_transcript_path,
        abort_event=abort_event,
        fork_messages=past_messages[:-1],  # history without the continuation
    )
    return result


# ── Public API ────────────────────────────────────────────────────────────────

async def resume_agent(
    agent_id: str,
    continuation_prompt: str,
    tools: list[Tool],
    api_client: Any,
    cwd: str,
    system_prompt: str,
    permission_mode: str,
    max_turns: int,
    session_transcript_path: Path,
) -> str:
    """
    Resume a previous agent.

    If the agent is still running as a background task, inject the continuation
    prompt via enqueue_message() — the running loop picks it up between turns.
    Mirrors appendMessageToLocalAgent() called from REPL.tsx in Claude Code.

    If the agent has already completed/failed, fall back to rebuild-and-rerun:
    load the sidechain transcript and re-execute from history.
    """
    task = _bg.get_task(agent_id)
    if task and not task.is_done:
        success = _bg.enqueue_message(agent_id, continuation_prompt)
        if success:
            return (
                f"Message injected into running agent {agent_id[:8]}.\n"
                f"The agent will process it between turns."
            )

    return await _run_resume(
        agent_id=agent_id,
        continuation_prompt=continuation_prompt,
        tools=tools,
        api_client=api_client,
        cwd=cwd,
        system_prompt=system_prompt,
        permission_mode=permission_mode,
        max_turns=max_turns,
        session_transcript_path=session_transcript_path,
    )


async def resume_agent_background(
    agent_id: str,
    continuation_prompt: str,
    tools: list[Tool],
    api_client: Any,
    cwd: str,
    system_prompt: str,
    permission_mode: str,
    max_turns: int,
    session_transcript_path: Path,
) -> str:
    """
    Re-launch or continue a previous agent in the background.
    Mirrors resumeAgentBackground() in resumeAgent.ts.

    If still running: inject into the live agent (same task, no new registration).
    If finished: register a new background task and rebuild from sidechain.
    """
    task = _bg.get_task(agent_id)
    if task and not task.is_done:
        # Agent still running — inject and return immediately.
        # Mirrors the appendMessageToLocalAgent path in resumeAgent.ts.
        success = _bg.enqueue_message(agent_id, continuation_prompt)
        if success:
            return (
                f"Message injected into running agent {agent_id[:8]}.\n"
                f"  status: running (continued in-place)"
            )

    # Agent finished — register a new background task and rebuild from sidechain.
    task_record = _bg.register_task(
        description=f"Resume of {agent_id[:8]}",
        prompt=continuation_prompt,
        agent_type="resumed",
    )
    new_agent_id = task_record.agent_id
    badge = color_label("resumed")

    async def _runner() -> None:
        try:
            result = await _run_resume(
                agent_id=agent_id,
                continuation_prompt=continuation_prompt,
                tools=tools,
                api_client=api_client,
                cwd=cwd,
                system_prompt=system_prompt,
                permission_mode=permission_mode,
                max_turns=max_turns,
                session_transcript_path=session_transcript_path,
                abort_event=task_record.abort_event,
            )
            _bg.complete_task(new_agent_id, result)
            print(f"\n  {badge} Resumed agent completed ({agent_id[:8]}…)")
        except (asyncio.CancelledError, AgentAbortError):
            _bg.fail_task(new_agent_id, "Cancelled")
        except Exception as exc:
            _bg.fail_task(new_agent_id, str(exc))
            print(f"\n  {badge} Resumed agent failed: {exc}")

    asyncio_task = asyncio.create_task(_runner())
    task_record.asyncio_task = asyncio_task

    return (
        f"Agent resume launched in background.\n"
        f"  originalAgentId: {agent_id}\n"
        f"  newAgentId: {new_agent_id}\n"
        f"  status: running"
    )
