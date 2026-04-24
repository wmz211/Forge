from __future__ import annotations

from typing import Any

from tool import Tool, ToolContext


_VALID_STATUSES = {"pending", "in_progress", "completed"}

_DESCRIPTION = (
    "Update the todo list for the current session. Use proactively to track "
    "progress and pending tasks. Keep at most one task in_progress at a time, "
    "and always provide both content and activeForm for each task."
)

_RESULT_MESSAGE = (
    "Todos have been modified successfully. Ensure that you continue to use "
    "the todo list to track your progress. Please proceed with the current "
    "tasks if applicable"
)


class TodoWriteTool(Tool):
    name = "TodoWrite"
    description = _DESCRIPTION
    search_hint = "manage the session task checklist"
    is_concurrency_safe = False
    max_result_size_chars = 100_000
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The updated todo list",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Imperative task description, e.g. Run tests",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                            "activeForm": {
                                "type": "string",
                                "description": "Present-continuous form, e.g. Running tests",
                            },
                        },
                        "required": ["content", "status", "activeForm"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["todos"],
            "additionalProperties": False,
        }

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        todos = input.get("todos")
        if not isinstance(todos, list):
            return False, "TodoWrite requires a todos array."
        in_progress = 0
        for index, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return False, f"Todo at index {index} must be an object."
            content = todo.get("content")
            active = todo.get("activeForm")
            status = todo.get("status")
            if not isinstance(content, str) or not content.strip():
                return False, f"Todo at index {index} requires non-empty content."
            if not isinstance(active, str) or not active.strip():
                return False, f"Todo at index {index} requires non-empty activeForm."
            if status not in _VALID_STATUSES:
                return False, f"Todo at index {index} has invalid status: {status!r}."
            if status == "in_progress":
                in_progress += 1
        if in_progress > 1:
            return False, "TodoWrite expects at most one todo item with status in_progress."
        return True, None

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        todos = args.get("todos") if isinstance(args, dict) else None
        if not isinstance(todos, list):
            return "update todo list"
        counts = {"pending": 0, "in_progress": 0, "completed": 0}
        for todo in todos:
            if isinstance(todo, dict) and todo.get("status") in counts:
                counts[todo["status"]] += 1
        return (
            f"{len(todos)} items: {counts['in_progress']} in progress, "
            f"{counts['pending']} pending, {counts['completed']} completed"
        )

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        todos = [dict(todo) for todo in input.get("todos", [])]
        key = ctx.session_id or "default"
        old_todos = ctx.todos.get(key, [])
        all_done = bool(todos) and all(todo.get("status") == "completed" for todo in todos)
        ctx.todos[key] = [] if all_done else todos

        verification_nudge = ""
        if all_done and len(todos) >= 3 and not any("verif" in str(t.get("content", "")).lower() for t in todos):
            verification_nudge = (
                "\n\nNOTE: You just closed out 3+ tasks and none of them was a "
                "verification step. Before writing your final summary, run an "
                "appropriate verification step."
            )

        return (
            f"{_RESULT_MESSAGE}{verification_nudge}\n"
            f"<oldTodos>{len(old_todos)}</oldTodos>\n"
            f"<newTodos>{len(todos)}</newTodos>"
        )
