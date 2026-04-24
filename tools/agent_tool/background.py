"""
Background agent task registry with progress tracking and message relay.
Mirrors src/tasks/LocalAgentTask/LocalAgentTask.tsx.

Added over the previous version:
  ProgressTracker  — token_count, tool_use_count, activity_description
                     (mirrors ProgressTracker / AgentProgress types)
  message_queue    — asyncio.Queue for injecting messages into a running agent
                     (mirrors queuePendingMessage / drainPendingMessages)
  abort_event      — asyncio.Event for signalling cancellation to the runner
                     (mirrors AbortController / killAsyncAgent)
  enqueue_message  — public API for SendMessage-like relay
  drain_messages   — called by the running agent between turns
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── ProgressTracker ───────────────────────────────────────────────────────────

@dataclass
class ProgressTracker:
    """
    Tracks live progress of a running agent.
    Mirrors the ProgressTracker type in LocalAgentTask.tsx.
    """
    token_count: int = 0
    tool_use_count: int = 0
    activity_description: str = ""    # last tool used (for UI display)
    summary: str = ""                 # LLM-generated progress summary (if any)

    def update_from_event(self, event: dict[str, Any]) -> None:
        """
        Update tracker fields from a query_loop event.
        Mirrors updateProgressFromMessage() in LocalAgentTask.tsx.
        """
        if event.get("type") == "tool_use":
            self.tool_use_count += 1
            self.activity_description = event.get("name", "")
        elif event.get("type") == "done":
            usage = event.get("usage") or {}
            self.token_count += usage.get("prompt_tokens", 0)
            self.token_count += usage.get("completion_tokens", 0)


# ── BackgroundAgentTask ───────────────────────────────────────────────────────

@dataclass
class BackgroundAgentTask:
    """
    Represents one background-running agent invocation.
    Mirrors LocalAgentTaskState in LocalAgentTask.tsx.
    """
    agent_id: str
    description: str
    prompt: str
    agent_type: str

    # Status mirrors: running | completed | failed
    status: str = "running"
    result: str = ""
    error: str = ""

    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    # asyncio primitives — not serialised
    asyncio_task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)

    # Message relay queue (mirrors queuePendingMessage / drainPendingMessages)
    # The running agent checks this between turns; callers push via enqueue_message()
    _message_queue: asyncio.Queue = field(
        default_factory=asyncio.Queue, repr=False, compare=False
    )

    # Progress tracker (updated live during execution)
    progress: ProgressTracker = field(default_factory=ProgressTracker)

    # Set by the background launcher so complete/fail can write a .done.json
    # alongside the agent's sidechain transcript.
    # Mirrors the session storage path used in runAgent.ts for status persistence.
    session_transcript_path: Path | None = field(default=None, repr=False, compare=False)
    sidechain_agent_id: str = ""  # the agent_id used for the sidechain JSONL

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed")


# ── Module-level registry ─────────────────────────────────────────────────────

_tasks: dict[str, BackgroundAgentTask] = {}


# ── Registration ──────────────────────────────────────────────────────────────

def register_task(
    description: str,
    prompt: str,
    agent_type: str,
) -> BackgroundAgentTask:
    """
    Create and register a new background task.
    Mirrors registerAsyncAgent() in LocalAgentTask.tsx.
    The caller attaches .asyncio_task after asyncio.create_task().
    """
    agent_id = str(uuid.uuid4())
    task = BackgroundAgentTask(
        agent_id=agent_id,
        description=description,
        prompt=prompt,
        agent_type=agent_type,
    )
    _tasks[agent_id] = task
    return task


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _persist_done(task: BackgroundAgentTask) -> None:
    """
    Write a .done.json status file alongside the agent's sidechain JSONL.
    Mirrors the session storage persistence of task status in LocalAgentTask.tsx.
    Best-effort: errors are silently swallowed.
    """
    if not task.session_transcript_path:
        return
    try:
        sid = task.sidechain_agent_id or task.agent_id
        agents_dir = task.session_transcript_path.parent / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        done_path = agents_dir / f"{sid}.done.json"
        done_path.write_text(
            json.dumps({
                "agentId": sid,
                "bgAgentId": task.agent_id,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "elapsed_s": task.elapsed_s,
                "finishedAt": task.finished_at,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def complete_task(agent_id: str, result: str) -> None:
    """Mirrors completeAgentTask() in LocalAgentTask.tsx."""
    if task := _tasks.get(agent_id):
        task.status = "completed"
        task.result = result
        task.finished_at = time.monotonic()
        _persist_done(task)


def fail_task(agent_id: str, error: str) -> None:
    """Mirrors failAgentTask() in LocalAgentTask.tsx."""
    if task := _tasks.get(agent_id):
        task.status = "failed"
        task.error = error
        task.finished_at = time.monotonic()
        _persist_done(task)


def kill_task(agent_id: str) -> bool:
    """
    Signal the abort_event and cancel the asyncio Task.
    Mirrors killAsyncAgent() in LocalAgentTask.tsx.
    Returns True if a running task was signalled.
    """
    task = _tasks.get(agent_id)
    if not task or task.is_done:
        return False
    # Signal abort event first (checked between turns in run_agent.py)
    task.abort_event.set()
    # Then cancel the asyncio task for hard termination
    if task.asyncio_task and not task.asyncio_task.done():
        task.asyncio_task.cancel()
    fail_task(agent_id, "Cancelled by user")
    return True


# ── Message relay (mirrors queuePendingMessage / drainPendingMessages) ─────────

def enqueue_message(agent_id: str, content: str) -> bool:
    """
    Inject a message into a running background agent.
    Mirrors queuePendingMessage() in LocalAgentTask.tsx.
    Returns False if the task is not found or already done.
    """
    task = _tasks.get(agent_id)
    if not task or task.is_done:
        return False
    task._message_queue.put_nowait(content)
    return True


def drain_messages(agent_id: str) -> list[str]:
    """
    Drain all pending messages for the given agent (called between turns).
    Mirrors drainPendingMessages() in LocalAgentTask.tsx.
    Returns empty list if nothing is pending.
    """
    task = _tasks.get(agent_id)
    if not task:
        return []
    messages: list[str] = []
    while not task._message_queue.empty():
        try:
            messages.append(task._message_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return messages


# ── Progress update ───────────────────────────────────────────────────────────

def update_progress(agent_id: str, event: dict[str, Any]) -> None:
    """
    Update the progress tracker from a query_loop event.
    Called by the background runner coroutine on each event.
    Mirrors updateAgentProgress() in LocalAgentTask.tsx.
    """
    if task := _tasks.get(agent_id):
        task.progress.update_from_event(event)


# ── Lookup ────────────────────────────────────────────────────────────────────

def get_task(agent_id: str) -> BackgroundAgentTask | None:
    return _tasks.get(agent_id)


def list_tasks() -> list[BackgroundAgentTask]:
    """Return all registered tasks, newest first."""
    return sorted(_tasks.values(), key=lambda t: t.started_at, reverse=True)
