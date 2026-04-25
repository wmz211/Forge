from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


def load_mcp_server_configs(cwd: str) -> list[McpServerConfig]:
    """
    Load Claude-style MCP server configs from user, project, and local settings.

    Supported shape mirrors the common `.mcp.json` / `.claude/settings*.json`
    `mcpServers` map.  This first pass intentionally supports stdio servers,
    which are the default process-backed MCP transport in Claude Code.
    """
    root = Path(cwd)
    sources = [
        Path.home() / ".claude" / "settings.json",
        root / ".mcp.json",
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
    ]
    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        data = _read_json(source)
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            for name, config in servers.items():
                if isinstance(name, str) and isinstance(config, dict):
                    merged[name] = dict(config)

    configs: list[McpServerConfig] = []
    for name, raw in merged.items():
        transport = raw.get("type") or raw.get("transport") or "stdio"
        if transport != "stdio":
            continue
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        args = raw.get("args") if isinstance(raw.get("args"), list) else []
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        configs.append(
            McpServerConfig(
                name=name,
                command=command,
                args=[str(arg) for arg in args],
                env={str(k): str(v) for k, v in env.items()},
                cwd=str(root),
            )
        )
    return configs


async def discover_mcp_tools(cwd: str) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    for config in load_mcp_server_configs(cwd):
        client = McpStdioClient(config)
        try:
            await client.initialize()
            for tool in await client.list_tools():
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    discovered.append({"server": config, "tool": tool})
        except Exception:
            continue
        finally:
            await client.close()
    return discovered


class McpStdioClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "forge", "version": "0.1"},
            },
        )
        await self.notify("notifications/initialized", {})
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self.request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        return format_mcp_tool_result(result)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._ensure_started()
        assert self._proc and self._proc.stdin and self._proc.stdout
        request_id = self._next_id
        self._next_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server {self.config.name} exited before replying to {method}")
            payload = json.loads(line.decode("utf-8"))
            if payload.get("id") != request_id:
                continue
            if "error" in payload:
                error = payload["error"]
                if isinstance(error, dict):
                    raise RuntimeError(str(error.get("message") or error))
                raise RuntimeError(str(error))
            result = payload.get("result")
            return result if isinstance(result, dict) else {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._ensure_started()
        assert self._proc and self._proc.stdin
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin:
            proc.stdin.close()
            try:
                await proc.stdin.wait_closed()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        env = os.environ.copy()
        env.update(self.config.env)
        self._proc = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            cwd=self.config.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )


def format_mcp_tool_result(result: dict[str, Any]) -> str:
    if result.get("isError"):
        prefix = "<error>"
        suffix = "</error>"
    else:
        prefix = ""
        suffix = ""
    content = result.get("content")
    if not isinstance(content, list):
        text = json.dumps(result, ensure_ascii=False)
        return f"{prefix}{text}{suffix}"
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(part for part in parts if part)
    return f"{prefix}{text}{suffix}" if prefix else text


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
