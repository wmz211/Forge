"""
MonitorTool — stream events from a long-running background process.

Mirrors the MonitorTool referenced in Claude Code source.
Feature-gated: enabled only when FORGE_MONITOR_TOOL=1.

Each stdout line from the monitored process becomes a notification event.
The model can use this to poll an async background task started with BashTool.
"""
from __future__ import annotations
import asyncio
import os
from tool import Tool, ToolContext

MONITOR_TOOL_NAME = "Monitor"

_DESCRIPTION = (
    "Stream events (stdout lines) from a long-running background process. "
    "Use this to watch an async task started with Bash."
)

_PROMPT = """\
Monitor a background process by its task ID and receive its stdout lines as
notification events. Useful for watching long-running commands (builds, tests,
deploys) without blocking the main agent loop.

Parameters:
  task_id: The task ID returned by a previous Bash --background call.
  timeout_ms (optional): Maximum milliseconds to wait for output. Defaults to
    30000 (30 s). The tool returns when the process exits or the timeout fires.

Returns all stdout lines collected during the monitoring window, one per line.
"""


class MonitorTool(Tool):
    """
    Mirrors MonitorTool (feature-gated: FORGE_MONITOR_TOOL=1).

    Input schema:
      task_id: str — ID of the background task to monitor.
      timeout_ms: int (optional) — max wait in ms (default 30 000).
    """

    name = MONITOR_TOOL_NAME
    description = _DESCRIPTION
    should_defer = True

    def is_enabled(self) -> bool:
        return os.environ.get("FORGE_MONITOR_TOOL", "0") == "1"

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID of the background process to monitor.",
                    },
                    "timeout_ms": {
                        "type": "number",
                        "description": "Max milliseconds to wait for output (default 30 000).",
                    },
                },
                "required": ["task_id"],
            },
        }

    async def call(self, tool_input: dict, ctx: ToolContext) -> str:
        task_id = tool_input.get("task_id", "")
        timeout_ms = tool_input.get("timeout_ms", 30_000)
        timeout_s = timeout_ms / 1000.0

        # Look up the background task in the same registry used by Bash,
        # PowerShell, TaskStop, and TaskOutput.
        task_registry = ctx.todos.setdefault("_bg_tasks", {})
        task = task_registry.get(task_id)
        if task is None:
            return f"<error>No background task with id '{task_id}'.</error>"

        proc = task.get("proc")
        output_buf: list[str] = task.get("output", [])
        capture_task = task.get("capture_task")
        before = len("".join(output_buf))
        deadline = asyncio.get_event_loop().time() + timeout_s
        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                if capture_task is not None and capture_task.done():
                    await capture_task
                    break
                if proc is not None and proc.returncode is not None:
                    break
                await asyncio.sleep(min(remaining, 0.25))
                current = "".join(output_buf)
                if len(current) > before:
                    break
        except asyncio.TimeoutError:
            pass

        text = "".join(output_buf)[before:]
        if not text:
            return f"No output from task '{task_id}' within {timeout_ms}ms."
        return text.rstrip()
