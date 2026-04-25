from __future__ import annotations

import re
from typing import Any

from services.mcp import McpServerConfig, McpStdioClient
from tool import Tool, ToolContext


def safe_mcp_tool_name(server_name: str, tool_name: str) -> str:
    server = _safe_part(server_name)
    tool = _safe_part(tool_name)
    return f"mcp__{server}__{tool}"


class McpTool(Tool):
    should_defer = True
    max_result_size_chars = 100_000

    def __init__(self, server: McpServerConfig, tool_def: dict[str, Any]) -> None:
        self.server = server
        self.original_name = str(tool_def["name"])
        self.name = safe_mcp_tool_name(server.name, self.original_name)
        self.description = str(tool_def.get("description") or f"MCP tool {self.original_name}")
        self.search_hint = f"mcp {server.name} {self.original_name}"
        schema = tool_def.get("inputSchema")
        self._schema = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}

    def get_schema(self) -> dict:
        return self._schema

    def render_call_summary(self, args: dict[str, Any]) -> str | None:
        return f"{self.server.name}.{self.original_name}"

    async def call(self, input: dict[str, Any], ctx: ToolContext) -> str:
        config = McpServerConfig(
            name=self.server.name,
            command=self.server.command,
            args=list(self.server.args),
            env=dict(self.server.env),
            cwd=ctx.cwd or self.server.cwd,
        )
        client = McpStdioClient(config)
        try:
            await client.initialize()
            return await client.call_tool(self.original_name, input)
        finally:
            await client.close()


def _safe_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return safe or "unnamed"
