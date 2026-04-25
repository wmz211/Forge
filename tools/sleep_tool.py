from __future__ import annotations
"""
Sleep tool.
Mirrors src/tools/SleepTool/ in Claude Code source.

Lets the model wait for a specified number of seconds without holding a shell
process. The user can interrupt the sleep by sending Ctrl-C (KeyboardInterrupt).

Schema mirrors the TypeScript inputSchema:
  duration_seconds: int — how long to sleep (positive integer)
"""
import asyncio
from typing import Any

from tool import Tool, ToolContext

SLEEP_TOOL_NAME = "Sleep"

_DESCRIPTION = "Wait for a specified duration"

_PROMPT = """\
Wait for a specified duration. The user can interrupt the sleep at any time.

Use this when the user tells you to sleep or rest, when you have nothing to do, or when you're waiting for something.

You can call this concurrently with other tools — it won't interfere with them.

Prefer this over `Bash(sleep ...)` — it doesn't hold a shell process.
"""


class SleepTool(Tool):
    """
    Mirrors SleepTool from the source.
    Uses asyncio.sleep so concurrent tools are not blocked.
    """
    name = SLEEP_TOOL_NAME
    description = _DESCRIPTION
    # Sleep is concurrency-safe (it just waits, doesn't touch shared state).
    is_concurrency_safe = True
    should_defer = True

    def get_schema(self) -> dict:
        return {
            "name": self.name,
            "description": _PROMPT,
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Number of seconds to sleep (positive integer)",
                        "minimum": 1,
                    },
                },
                "required": ["duration_seconds"],
            },
        }

    def to_openai_tool(self) -> dict:
        schema = self.get_schema()
        return {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["parameters"],
            },
        }

    async def validate_input(
        self, input: dict[str, Any], ctx: ToolContext
    ) -> tuple[bool, str | None]:
        duration = input.get("duration_seconds")
        if not isinstance(duration, int) or duration < 1:
            return False, "duration_seconds must be a positive integer"
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        duration = int(input.get("duration_seconds", 1))
        try:
            await asyncio.sleep(duration)
            return f"Slept for {duration} second{'s' if duration != 1 else ''}."
        except asyncio.CancelledError:
            return "Sleep was cancelled."
