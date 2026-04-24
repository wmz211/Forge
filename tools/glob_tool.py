from __future__ import annotations
import glob
import os
from typing import Any

from tool import Tool, ToolContext

MAX_RESULTS = 500


class GlobTool(Tool):
    name = "Glob"
    description = (
        "Find files matching a glob pattern. "
        "Returns matching file paths sorted by modification time. "
        "Example patterns: '**/*.py', 'src/**/*.ts', '*.md'"
    )
    is_concurrency_safe = True

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files against.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: cwd).",
                },
            },
            "required": ["pattern"],
        }

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        pattern = input["pattern"]
        base = input.get("path", ctx.cwd)
        if not os.path.isabs(base):
            base = os.path.join(ctx.cwd, base)

        full_pattern = os.path.join(base, pattern)
        matches = glob.glob(full_pattern, recursive=True)

        # Sort by modification time (newest first)
        matches.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)

        if not matches:
            return "No files found matching the pattern."

        if len(matches) > MAX_RESULTS:
            matches = matches[:MAX_RESULTS]
            truncated = True
        else:
            truncated = False

        result = "\n".join(matches)
        if truncated:
            result += f"\n... (showing first {MAX_RESULTS} results)"
        return result
