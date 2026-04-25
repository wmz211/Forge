from __future__ import annotations
"""
Task management tools — TaskCreate, TaskUpdate, TaskGet, TaskList, TaskStop, TaskOutput.
Mirrors src/tools/TaskCreateTool, TaskUpdateTool, TaskGetTool, TaskListTool,
TaskStopTool, TaskOutputTool.

Tasks are stored in ctx.todos["_tasks"] (keyed by task ID).
Each task is a dict with: id, subject, description, activeForm, status,
  owner, blocks, blockedBy, metadata.

Statuses: pending | in_progress | completed | failed | cancelled | deleted

TaskStop / TaskOutput mirror the background-bash integration:
  background tasks are stored in ctx.todos["_bg_tasks"] by task_id.
"""

import asyncio
import json
import uuid
from typing import Any

from tool import Tool, ToolContext

# ── Task status constants ──────────────────────────────────────────────────────
VALID_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "failed", "cancelled"}
)

# ── Shared storage helpers ─────────────────────────────────────────────────────

def _task_store(ctx: ToolContext) -> dict[str, dict]:
    """Get or create the session-level task store from ctx.todos."""
    return ctx.todos.setdefault("_tasks", {})


def _bg_store(ctx: ToolContext) -> dict[str, dict]:
    """Get or create the background-process store from ctx.todos."""
    return ctx.todos.setdefault("_bg_tasks", {})


# ──────────────────────────────────────────────────────────────────────────────
# TaskCreate
# ──────────────────────────────────────────────────────────────────────────────

class TaskCreateTool(Tool):
    """
    Create a new task.
    Mirrors TaskCreateTool in src/tools/TaskCreateTool/TaskCreateTool.ts.
    """
    name = "TaskCreate"
    description = "Create a new task in the task list"
    is_concurrency_safe = True
    should_defer = True

    _PROMPT = """\
Create a new task with a subject (title) and description.

Tasks have statuses: pending, in_progress, completed, failed, cancelled.
Use TaskUpdate to change status. Use TaskList to see all tasks.
"""

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self._PROMPT,
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "A brief title for the task",
                    },
                    "description": {
                        "type": "string",
                        "description": "What needs to be done",
                    },
                    "activeForm": {
                        "type": "string",
                        "description": "Present continuous form shown while in_progress (e.g. 'Running tests')",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Arbitrary metadata to attach to the task",
                    },
                },
                "required": ["subject", "description"],
            },
        }

    def to_openai_tool(self) -> dict:
        s = self.get_schema()
        return {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        if not input.get("subject"):
            return False, "subject is required"
        if not input.get("description"):
            return False, "description is required"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        task_id = str(uuid.uuid4())[:8]
        task = {
            "id": task_id,
            "subject": input["subject"],
            "description": input["description"],
            "activeForm": input.get("activeForm", ""),
            "status": "pending",
            "owner": None,
            "blocks": [],
            "blockedBy": [],
            "metadata": input.get("metadata") or {},
        }
        _task_store(ctx)[task_id] = task
        return json.dumps({"task": {"id": task_id, "subject": task["subject"]}})


# ──────────────────────────────────────────────────────────────────────────────
# TaskUpdate
# ──────────────────────────────────────────────────────────────────────────────

class TaskUpdateTool(Tool):
    """
    Update a task's status or fields.
    Mirrors TaskUpdateTool in src/tools/TaskUpdateTool/TaskUpdateTool.ts.
    """
    name = "TaskUpdate"
    description = "Update a task's status, subject, description, or other fields"
    is_concurrency_safe = False
    should_defer = True

    _PROMPT = """\
Update a task's fields. Use status='deleted' to delete a task.

Valid statuses: pending, in_progress, completed, failed, cancelled, deleted
"""

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self._PROMPT,
            "parameters": {
                "type": "object",
                "properties": {
                    "taskId": {
                        "type": "string",
                        "description": "The ID of the task to update",
                    },
                    "subject": {"type": "string", "description": "New subject for the task"},
                    "description": {"type": "string", "description": "New description for the task"},
                    "activeForm": {"type": "string", "description": "Present continuous form shown while in_progress"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "failed", "cancelled", "deleted"],
                        "description": "New status. Use 'deleted' to remove the task.",
                    },
                    "owner": {"type": "string", "description": "New owner for the task"},
                    "addBlocks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task IDs that this task blocks",
                    },
                    "addBlockedBy": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task IDs that block this task",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Metadata keys to merge. Set a key to null to delete it.",
                    },
                },
                "required": ["taskId"],
            },
        }

    def to_openai_tool(self) -> dict:
        s = self.get_schema()
        return {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        task_id = input.get("taskId")
        if not task_id:
            return False, "taskId is required"
        if task_id not in _task_store(ctx):
            return False, f"Task not found: {task_id}"
        status = input.get("status")
        if status and status not in VALID_STATUSES and status != "deleted":
            return False, f"Invalid status: {status}. Must be one of: {', '.join(sorted(VALID_STATUSES | {'deleted'}))}"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        task_id = input["taskId"]
        store = _task_store(ctx)
        task = store[task_id]
        old_status = task["status"]

        updated_fields: list[str] = []

        if input.get("status") == "deleted":
            del store[task_id]
            return json.dumps({"success": True, "taskId": task_id, "updatedFields": ["deleted"]})

        for field in ("subject", "description", "activeForm", "owner"):
            if field in input and input[field] is not None:
                task[field] = input[field]
                updated_fields.append(field)

        if "status" in input and input["status"]:
            task["status"] = input["status"]
            updated_fields.append("status")

        for block_id in input.get("addBlocks", []):
            if block_id not in task["blocks"]:
                task["blocks"].append(block_id)
                updated_fields.append(f"blocks+{block_id}")

        for blocked_by_id in input.get("addBlockedBy", []):
            if blocked_by_id not in task["blockedBy"]:
                task["blockedBy"].append(blocked_by_id)
                updated_fields.append(f"blockedBy+{blocked_by_id}")

        if "metadata" in input and isinstance(input["metadata"], dict):
            for k, v in input["metadata"].items():
                if v is None:
                    task["metadata"].pop(k, None)
                else:
                    task["metadata"][k] = v
            updated_fields.append("metadata")

        result: dict[str, Any] = {"success": True, "taskId": task_id, "updatedFields": updated_fields}
        if old_status != task["status"]:
            result["statusChange"] = {"from": old_status, "to": task["status"]}
        return json.dumps(result)


# ──────────────────────────────────────────────────────────────────────────────
# TaskGet
# ──────────────────────────────────────────────────────────────────────────────

class TaskGetTool(Tool):
    """
    Retrieve a task by ID.
    Mirrors TaskGetTool in src/tools/TaskGetTool/TaskGetTool.ts.
    """
    name = "TaskGet"
    description = "Retrieve a task by its ID"
    is_concurrency_safe = True
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": "Get the details of a task by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "taskId": {"type": "string", "description": "The ID of the task to retrieve"},
                },
                "required": ["taskId"],
            },
        }

    def to_openai_tool(self) -> dict:
        s = self.get_schema()
        return {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        if not input.get("taskId"):
            return False, "taskId is required"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        task_id = input["taskId"]
        task = _task_store(ctx).get(task_id)
        if not task:
            return json.dumps({"task": None})
        return json.dumps({
            "task": {
                "id": task["id"],
                "subject": task["subject"],
                "description": task["description"],
                "status": task["status"],
                "blocks": task["blocks"],
                "blockedBy": task["blockedBy"],
                "owner": task.get("owner"),
                "activeForm": task.get("activeForm", ""),
                "metadata": task.get("metadata", {}),
            }
        })


# ──────────────────────────────────────────────────────────────────────────────
# TaskList
# ──────────────────────────────────────────────────────────────────────────────

class TaskListTool(Tool):
    """
    List all tasks in the current session.
    Mirrors TaskListTool in src/tools/TaskListTool/TaskListTool.ts.
    """
    name = "TaskList"
    description = "List all tasks in the current session"
    is_concurrency_safe = True
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": "List all tasks. Returns id, subject, status, owner, blockedBy for each task.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }

    def to_openai_tool(self) -> dict:
        s = self.get_schema()
        return {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        tasks = list(_task_store(ctx).values())
        return json.dumps({
            "tasks": [
                {
                    "id": t["id"],
                    "subject": t["subject"],
                    "status": t["status"],
                    "owner": t.get("owner"),
                    "blockedBy": t["blockedBy"],
                }
                for t in tasks
            ]
        })


# ──────────────────────────────────────────────────────────────────────────────
# TaskStop
# ──────────────────────────────────────────────────────────────────────────────

class TaskStopTool(Tool):
    """
    Stop a background task (background bash process).
    Mirrors TaskStopTool in src/tools/TaskStopTool/TaskStopTool.ts.
    Aliases: KillShell (deprecated backward-compat alias from source).
    """
    name = "TaskStop"
    description = "Stop a running background task"
    is_concurrency_safe = True
    should_defer = True
    aliases = ("KillShell",)

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": "Stop a running background task by its task_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The ID of the background task to stop",
                    },
                    "shell_id": {
                        "type": "string",
                        "description": "Deprecated: use task_id instead",
                    },
                },
            },
        }

    def to_openai_tool(self) -> dict:
        s = self.get_schema()
        return {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        task_id = input.get("task_id") or input.get("shell_id")
        if not task_id:
            return False, "Missing required parameter: task_id"
        bg = _bg_store(ctx)
        if task_id not in bg:
            return False, f"No background task found with ID: {task_id}"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        task_id = input.get("task_id") or input.get("shell_id", "")
        bg = _bg_store(ctx)
        task_info = bg.get(task_id, {})

        proc: asyncio.subprocess.Process | None = task_info.get("proc")
        command = task_info.get("command", task_id)
        task_type = task_info.get("type", "local_bash")

        if proc is not None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
            except ProcessLookupError:
                pass

        del bg[task_id]
        return json.dumps({
            "message": f"Stopped task {task_id}",
            "task_id": task_id,
            "task_type": task_type,
            "command": command,
        })


# ──────────────────────────────────────────────────────────────────────────────
# TaskOutput
# ──────────────────────────────────────────────────────────────────────────────

class TaskOutputTool(Tool):
    """
    Get output from a background task.
    Mirrors TaskOutputTool in src/tools/TaskOutputTool/TaskOutputTool.tsx.
    """
    name = "TaskOutput"
    description = "Get the output of a background task"
    is_concurrency_safe = True
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": (
                "Get the current output of a background task. "
                "Pass block=true (default) to wait for completion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to get output from",
                    },
                    "block": {
                        "type": "boolean",
                        "description": "Whether to wait for task completion (default: true)",
                        "default": True,
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max wait time in ms when block=true (default: 30000)",
                        "default": 30000,
                    },
                },
                "required": ["task_id"],
            },
        }

    def to_openai_tool(self) -> dict:
        s = self.get_schema()
        return {"type": "function", "function": {"name": s["name"], "description": s["description"], "parameters": s["parameters"]}}

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        if not input.get("task_id"):
            return False, "task_id is required"
        if input["task_id"] not in _bg_store(ctx):
            return False, f"No background task found with ID: {input['task_id']}"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        task_id: str = input["task_id"]
        block: bool = bool(input.get("block", True))
        timeout_ms: float = float(input.get("timeout", 30000))
        timeout_s = timeout_ms / 1000.0

        bg = _bg_store(ctx)
        task_info = bg.get(task_id, {})
        proc: asyncio.subprocess.Process | None = task_info.get("proc")
        command = task_info.get("command", task_id)
        output_buf: list[str] = task_info.get("output", [])
        capture_task: asyncio.Task | None = task_info.get("capture_task")

        status = "running"
        exit_code = None

        if proc is not None and block:
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_s)
                if capture_task is not None:
                    await asyncio.wait_for(capture_task, timeout=1.0)
                exit_code = proc.returncode
                status = "completed" if exit_code == 0 else "failed"
                # Clean up from bg store on natural completion
                del bg[task_id]
            except asyncio.TimeoutError:
                status = "timeout"
        elif proc is not None:
            exit_code = proc.returncode  # None if still running
            if exit_code is not None:
                if capture_task is not None and capture_task.done():
                    await capture_task
                status = "completed" if exit_code == 0 else "failed"
                del bg[task_id]

        output_text = "".join(output_buf)
        retrieval_status = "success" if status in ("completed", "failed", "running") else "timeout"

        return json.dumps({
            "retrieval_status": retrieval_status,
            "task": {
                "task_id": task_id,
                "task_type": task_info.get("type", "local_bash"),
                "status": status,
                "description": command,
                "output": output_text,
                "exitCode": exit_code,
            },
        })
