from __future__ import annotations
import os
from typing import Any

from tool import Tool, ToolContext


class FileWriteTool(Tool):
    name = "Write"
    description = (
        "Write content to a file (creates or overwrites). "
        "Parent directories are created automatically. "
        "Prefer Edit for modifying existing files."
    )
    is_concurrency_safe = False

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file.",
                },
            },
            "required": ["file_path", "content"],
        }

    def render_call_summary(self, args: dict) -> str:
        lines = args.get("content", "").count("\n") + 1
        return f"{args.get('file_path', '?')}  ({lines} lines)"

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        path = input["file_path"]
        if not os.path.isabs(path):
            path = os.path.join(ctx.cwd, path)

        content = input["content"]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return f"<error>{e}</error>"

        lines = content.count("\n") + 1
        return f"Written {lines} lines to {path}"
