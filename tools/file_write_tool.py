from __future__ import annotations
import os
from typing import Any

from tool import Tool, ToolContext
from utils.file_state_cache import FileState


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

    async def validate_input(self, input: dict[str, Any], ctx: ToolContext) -> tuple[bool, str | None]:
        path = _resolve_path(str(input.get("file_path", "")), ctx.cwd)
        if os.path.exists(path):
            cached = ctx.file_state_cache.get(path)
            if cached is None or cached.is_partial_view:
                return False, "File has not been read yet. Read it first before writing to it."
        return True, None

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        path = _resolve_path(input["file_path"], ctx.cwd)

        content = input["content"]
        if os.path.exists(path):
            cached = ctx.file_state_cache.get(path)
            if cached is None or cached.is_partial_view:
                return (
                    "<error>File has not been read yet. Read it first before writing to it.</error>"
                )
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    existing_content = f.read()
                current_mtime = os.path.getmtime(path)
            except Exception as e:
                return f"<error>{e}</error>"
            if current_mtime > (cached.mtime_at_read or 0) + 0.001:
                is_full_read = cached.offset is None and cached.limit is None
                if not (is_full_read and existing_content == cached.content):
                    ctx.file_state_cache.delete(path)
                    return (
                        "<error>File has been modified since read, either by the user "
                        "or by a linter. Read it again before attempting to write it.</error>"
                    )
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            mtime = os.path.getmtime(path)
        except Exception as e:
            return f"<error>{e}</error>"

        ctx.file_state_cache.set(
            path,
            FileState(
                content=content,
                offset=None,
                limit=None,
                is_partial_view=False,
                mtime_at_read=mtime,
            ),
        )

        lines = content.count("\n") + 1
        return f"Written {lines} lines to {path}"


def _resolve_path(path: str, cwd: str) -> str:
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(path)
